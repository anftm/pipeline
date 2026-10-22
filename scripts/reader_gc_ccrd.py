#!/usr/bin/env python3
"""Retire old CCRD OCR generations after an exact live-consumer check.

Publication is additive: `publish_ccrd_ocr.py` keeps the current release and
writes one rollback root per parent. This tier is the only writer allowed to
delete that history. It keeps the current release plus a configurable number
of ancestors and only removes complete, committed generations older than that
frontier.

Deletion order is control roots first, then data. If the data phase is
interrupted, the next plan still sees the expired generation as unreachable
and lists its members as orphans; it cannot make a kept release incomplete.
Any intent that is still `uploading`, or any generation that cannot be tied
into the current parent chain, blocks the whole plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    from . import ccrd_lifecycle as lifecycle
    from .reader_bucket_store import HubBucketStore
    from .reader_gc_fence import assert_workflow_context
except ImportError:
    import ccrd_lifecycle as lifecycle
    from reader_bucket_store import HubBucketStore
    from reader_gc_fence import assert_workflow_context


BUCKET = lifecycle.BUCKET
CURRENT = "control/ocr-current.json"
IDS = "control/ocr-ids.json"
AUTOMATION = "control/ocr-automation.json"
CONSUMER = "control/ocr-consumer.json"
JOURNAL_PREFIX = "reports/ccrd-ocr-gc/"
LIVE_STATUS = "https://vomebook-search.hf.space/api/index-status"


class Blocker(Exception):
    """A condition that must stop planning before any write happens."""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(store, path, *, required=True):
    try:
        raw = store.read_bytes(BUCKET, path)
    except FileNotFoundError:
        if required:
            raise Blocker(f"missing required control document: {path}")
        return None, None
    try:
        payload = json.loads(raw)
    except ValueError:
        raise Blocker(f"invalid JSON document: {path}")
    return payload, raw


def generation_of(path, prefix):
    name = path[len(prefix):]
    if "/" in name or not name.endswith(".json"):
        raise Blocker(f"unexpected control filename: {path}")
    generation = name[:-5]
    if not re.fullmatch(r"[0-9a-f]{64}", generation):
        raise Blocker(f"invalid generation filename: {path}")
    return generation


def schema(payload, path):
    try:
        return lifecycle.references(payload, path)
    except ValueError as error:
        raise Blocker(f"{path}: {error}")


def inventory(store, include_staging):
    prefixes = lifecycle.MANAGED_PREFIXES if include_staging else tuple(
        prefix for prefix in lifecycle.MANAGED_PREFIXES if not prefix.startswith("staging/ocr/"))
    sizes = {}
    for prefix in prefixes:
        try:
            files = store.list_files(BUCKET, (prefix,))
        except Exception as error:
            raise Blocker(f"cannot inventory {prefix}: {error}")
        for path in files:
            if path in sizes:
                continue
            known = getattr(store, "object_sizes", {}).get((BUCKET, path))
            if type(known) is int and known >= 0:
                sizes[path] = known
                continue
            try:
                sizes[path] = store.stat(BUCKET, path)
            except FileNotFoundError:
                continue
    return sizes


def legacy_inventory(store):
    """Report legacy CCRD/CW generations without making them candidates.

    `generations/` predates the OCR library and is outside every managed OCR
    prefix. It has its own pointer (`current.json`) and is never touched by
    this tier; the report exists so its occupancy is not mistaken for OCR.
    """
    try:
        files = store.list_files(BUCKET, ("generations/",))
    except Exception as error:
        raise Blocker(f"cannot inventory generations/: {error}")
    sizes = {}
    for path in files:
        try:
            sizes[path] = store.stat(BUCKET, path)
        except FileNotFoundError:
            continue
    groups = {}
    for path, size in sizes.items():
        parts = path.split("/")
        generation = parts[1] if len(parts) > 2 else ""
        groups.setdefault(generation, 0)
        groups[generation] += size
    try:
        current, _ = read_json(store, "current.json", required=False)
    except Blocker:
        current = None
    active = set()
    if current and current.get("format") == 1:
        for item in current.get("databases", {}).values():
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                parts = item["path"].split("/")
                if len(parts) > 2 and parts[0] == "generations":
                    active.add(parts[1])
    return {"files": len(sizes), "bytes": sum(sizes.values()),
            "generations": len(groups),
            "active_generations": sorted(active),
            "active_bytes": sum(groups.get(gen, 0) for gen in active),
            "retired_generations": sorted(set(groups) - active),
            "retired_bytes": sum(size for gen, size in groups.items() if gen not in active),
            "note": "legacy CCRD/CW generations are outside this tier; no deletion is planned"}


def require_member(sizes, ref, label):
    path = ref["path"]
    if path not in sizes:
        raise Blocker(f"{label}: referenced object is missing: {path}")
    if sizes[path] != ref["bytes"]:
        raise Blocker(f"{label}: object size differs from its reference: {path}")


def intent_members(intent, path, sizes, *, strict):
    """Managed CCRD members named by an intent.

    Intents also retain Reader provenance and a few legacy non-managed CCRD
    documents. Those are outside this tier's deletion namespace. A strict
    caller (a kept or deferred generation) must have every managed member; an
    expiring generation may already be partially deleted by an interrupted
    run, so its missing members are reported instead of blocking recovery.
    """
    members, missing = set(), set()
    for ref in intent["resources"]:
        if ref["bucket"] != BUCKET:
            continue
        if ref["path"] not in sizes:
            if strict:
                raise Blocker(f"{path}: referenced object is missing: {ref['path']}")
            missing.add(ref["path"])
            continue
        require_member(sizes, ref, path)
        members.add(ref["path"])
    return members, missing


def release_identity(store, generation, sizes):
    """Validate one release without walking its thousands of export pages.

    The publication intent is the authoritative, upload-complete member list:
    `publish_ccrd_ocr.py` writes every release/export/TXT/database reference
    there before publishing the pointer, and `merge_unpromoted_intent` carries
    old members across a resumed upload. Re-reading all export manifests on
    every plan multiplied the request count by the whole library.
    """
    path = f"releases/ocr-{generation}/manifest.json"
    release, raw = read_json(store, path)
    if release.get("generation") != generation:
        raise Blocker(f"{path}: release generation differs from its path")
    refs = schema(release, path)
    if sizes.get(path) != len(raw):
        raise Blocker(f"{path}: release manifest size differs from the inventory")
    database = release.get("database", {})
    if not database.get("path", "").startswith("indexes/ocr/"):
        raise Blocker(f"{path}: database is outside indexes/ocr/")
    for ref in release.get("exports", []):
        if ref.get("bucket") == BUCKET and not ref.get("path", "").startswith("text/exports/"):
            raise Blocker(f"{path}: export is outside text/exports/")
    return path, release, raw, refs


def kept_closure(store, generation, sizes, *, verify_contents=False):
    """Exact protected set for one kept generation.

    The intent already names every member; the release manifest is checked for
    identity and is itself the fourth-domain root. A missing member blocks the
    plan. `verify_contents` additionally re-reads TXT bodies, which is not the
    default because publication already verified them.
    """
    path, release, raw, refs = release_identity(store, generation, sizes)
    intent_path = f"control/intents/{generation}.json"
    intent, _ = read_json(store, intent_path)
    found, missing = intent_members(intent, intent_path, sizes, strict=False)
    if missing:
        raise Blocker(f"{intent_path}: referenced object is missing: {sorted(missing)[0]}")
    members = {member: sizes[member] for member in found}
    members[path] = len(raw)
    for ref in refs:
        if ref.get("bucket") != BUCKET:
            continue
        member = ref.get("path", "")
        if member.startswith(("text/exports/", "text/objects/", "indexes/ocr/", "releases/ocr-")) \
                and member not in members:
            raise Blocker(f"{path}: release member is absent from its intent: {member}")
    if verify_contents:
        for ref in intent["resources"]:
            if ref.get("bucket") != BUCKET or not ref["path"].startswith("text/objects/"):
                continue
            body = store.read_bytes(BUCKET, ref["path"])
            if len(body) != ref["bytes"] or digest(body) != ref["sha256"]:
                raise Blocker(f"{ref['path']}: TXT checksum differs from its reference")
    return members


def live_projection(client):
    response = client.get(LIVE_STATUS, headers={"Cache-Control": "no-cache"}, timeout=60)
    response.raise_for_status()
    return response.json()


def accept_live(client, generation):
    if client is None:
        raise Blocker("live CCRD status was not checked")
    status = live_projection(client)
    # `ocr_release` is populated only after the Space pinned the exact OCR
    # activation. The deployed source list names the logical OCR repositories,
    # not the physical "OCR" database, so requiring a literal "OCR" source
    # would reject a healthy deployment.
    if (status.get("ready") is not True or status.get("ocr_release") != generation
            or not {"CCRD", "CW"} <= set(status.get("loaded_sources", []))):
        raise Blocker("live CCRD has not loaded the exact current OCR generation")
    return status


def plan(store, *, keep_generations=2, limit=0, include_staging=False,
         client=None, verify_live=True, verify_contents=False):
    """Build a plan without writing or deleting anything."""
    if keep_generations < 2:
        raise ValueError("keep-generations must retain the current release and its rollback parent")
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must not be negative")
    pointer, pointer_raw = read_json(store, CURRENT)
    if pointer.get("kind") != "ccrd-ocr-current" or pointer.get("version") != 1:
        raise Blocker("current OCR pointer has an unknown schema")
    generation = pointer.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation):
        raise Blocker("current OCR pointer has no exact generation")
    if pointer.get("release", {}).get("path") != f"releases/ocr-{generation}/manifest.json":
        raise Blocker("current OCR pointer does not name its release manifest")
    if pointer.get("release") not in pointer.get("resources", []):
        raise Blocker("current OCR pointer does not retain its release")
    schema(pointer, CURRENT)
    if store.read_bytes(BUCKET, CURRENT) != pointer_raw:
        raise Blocker("OCR pointer changed while it was being read")
    sizes = inventory(store, include_staging)

    intents, retained, control_snapshot = {}, {}, set()
    for path in sorted(store.list_files(BUCKET, ("control/intents/",))):
        intent, raw = read_json(store, path)
        gen = generation_of(path, "control/intents/")
        if intent.get("kind") != "ccrd-ocr-intent" or intent.get("version") != 1:
            raise Blocker(f"{path}: unknown intent schema")
        if intent.get("generation") != gen:
            raise Blocker(f"{path}: intent generation differs from its path")
        status = intent.get("status")
        if status not in {"uploading", "committed", "retired"}:
            raise Blocker(f"{path}: unknown intent status")
        schema(intent, path)
        intents[gen] = {"path": path, "status": status, "parent": intent.get("expected_parent"),
                        "payload": intent, "raw": raw}
        control_snapshot.add(path)
    for path in sorted(store.list_files(BUCKET, ("control/retained/",))):
        history, raw = read_json(store, path)
        gen = generation_of(path, "control/retained/")
        if history.get("kind") != "ccrd-ocr-retention" or history.get("version") != 1:
            raise Blocker(f"{path}: unknown retention schema")
        status = history.get("status", "committed")
        if status == "retired":
            if history.get("resources") != []:
                raise Blocker(f"{path}: retired retention still names resources")
            retained[gen] = {"path": path, "status": "retired", "ref": None, "raw": raw}
        elif status == "committed":
            refs = schema(history, path)
            if len(refs) != 1 or refs[0]["path"] != f"releases/ocr-{gen}/manifest.json":
                raise Blocker(f"{path}: retention does not name its own release")
            retained[gen] = {"path": path, "status": "committed", "ref": refs[0], "raw": raw}
        else:
            raise Blocker(f"{path}: unknown retention status")
        control_snapshot.add(path)

    chain, cursor = [], generation
    while cursor:
        if cursor in chain:
            raise Blocker("OCR parent chain contains a cycle")
        if cursor not in intents:
            raise Blocker(f"generation {cursor} has no intent")
        chain.append(cursor)
        cursor = intents[cursor]["parent"]
    if pointer.get("parent") is None:
        if len(chain) != 1:
            raise Blocker("current pointer has no parent but the chain is longer")
    elif chain[1:2] != [pointer["parent"]]:
        raise Blocker("current pointer parent differs from the intent chain")

    # A committed generation outside the live chain is a publication race, not
    # history: its pointer may be about to move or the Space may not have
    # caught up. Never delete around that state.
    for gen, item in sorted(intents.items()):
        if gen in chain:
            continue
        if item["status"] == "uploading":
            # An interrupted upload is not a deletion grant, but it must not
            # block reclamation of older committed history either. Its members
            # stay protected until an operator resolves the intent.
            continue
        raise Blocker(f"committed generation is outside the current parent chain: {gen}")
    for gen in sorted(retained):
        if gen not in chain:
            raise Blocker(f"retained generation is outside the current parent chain: {gen}")
    if intents[generation]["status"] != "committed":
        raise Blocker("the current generation is not committed")

    open_intents = sorted(gen for gen, item in intents.items()
                          if gen not in chain and item["status"] == "uploading")

    keep = chain[:keep_generations]
    frontier = chain[keep_generations:]
    already_retired = [gen for gen in frontier if intents[gen]["status"] == "retired"]
    # A partially applied run leaves a retired intent and possibly some
    # objects behind. Finish that cleanup before retiring anything newer.
    cleanup = [gen for gen in already_retired
               if (gen in retained and retained[gen]["status"] == "committed")
               or any(path.startswith((f"releases/ocr-{gen}/", f"indexes/ocr/{gen}/"))
                      for path in sizes)]
    fresh = [gen for gen in frontier if intents[gen]["status"] == "committed"]
    if cleanup:
        expiring = cleanup[:limit] if limit else cleanup
        deferred = [gen for gen in fresh if gen not in expiring]
    else:
        # Expire from the oldest end so repeated limited runs make progress and
        # the newest rollback ancestors survive longest.
        expiring = fresh[-limit:] if limit else fresh
        deferred = fresh[:len(fresh) - len(expiring)]
    # A retired generation with no leftover objects is already complete and
    # must not be treated as a deferred closure root.
    completed = [gen for gen in already_retired if gen not in cleanup]
    if generation not in keep:
        raise Blocker("retention frontier dropped the current generation")
    for gen in keep:
        if gen != generation and (gen not in retained or retained[gen]["status"] != "committed"):
            raise Blocker(f"kept generation has no live rollback retention root: {gen}")
    for gen in expiring:
        if intents[gen]["status"] == "committed" and (
                gen not in retained or retained[gen]["status"] != "committed"):
            raise Blocker(f"cannot expire a generation without live rollback history: {gen}")

    protected = set()
    protected.update(ref["path"] for ref in pointer.get("resources", []))
    ids_payload = None
    for name in (IDS, AUTOMATION, CONSUMER):
        payload, _ = read_json(store, name, required=(name == IDS))
        if payload is None:
            continue
        if name == IDS:
            ids_payload = payload
        for ref in schema(payload, name):
            if ref["bucket"] == BUCKET:
                protected.add(ref["path"])
    if pointer["release"] not in ids_payload.get("resources", []):
        raise Blocker("stable OCR ID registry does not retain the current release")
    for gen in keep + deferred:
        protected.update(kept_closure(store, gen, sizes, verify_contents=verify_contents))
    for gen in open_intents:
        members, _ = intent_members(intents[gen]["payload"], intents[gen]["path"], sizes, strict=False)
        protected.update(members)
        protected.add(intents[gen]["path"])
        # The intent (including `merge_unpromoted_intent` carry-over) is the
        # authoritative member list for an interrupted upload. Its release may
        # be incomplete, so it is deliberately not treated as a closure root.
    # Deferred generations keep their control roots too; only the explicit
    # expiration set may remove history in this run.
    for gen in keep + deferred:
        protected.add(intents[gen]["path"])
        if gen in retained:
            protected.add(retained[gen]["path"])

    expired = {}
    for gen in expiring:
        members, missing = intent_members(intents[gen]["payload"], intents[gen]["path"], sizes, strict=False)
        for path in (f"releases/ocr-{gen}/manifest.json", f"indexes/ocr/{gen}/OCR.sqlite3"):
            if path in sizes:
                members.add(path)
            else:
                missing.add(path)
        expired[gen] = {"members": members, "missing": missing, "bytes": sum(sizes[path] for path in members),
                        "intent": intents[gen]["path"],
                        "retained": retained[gen]["path"] if gen in retained else None}

    candidates = sorted(path for path in sizes if path not in protected)
    release_generations = {
        path[len("releases/ocr-"):].split("/", 1)[0]
        for path in sizes
        if path.startswith("releases/ocr-") and path.endswith("/manifest.json")
    }
    unknown = sorted(release_generations - set(chain) - set(intents) - set(retained))
    if unknown:
        raise Blocker(f"OCR releases outside the current history: {', '.join(unknown[:3])}")
    if any(path.startswith("control/") for path in candidates):
        raise Blocker("managed candidate list unexpectedly contains control documents")
    # Both control roots stay as small retired audit records. Stripping their
    # resources before data deletion keeps the four-bucket graph complete
    # during an interrupted run and never leaves a dangling reference.
    retire_intents = sorted({intents[gen]["path"] for gen in expiring
                             if intents[gen]["status"] == "committed"})
    retire_retention = sorted({retained[gen]["path"] for gen in expiring
                               if gen in retained and retained[gen]["status"] == "committed"})
    allowed_control = sorted(set(retire_intents) | set(retire_retention))
    control_bytes = 0
    for gen in expiring:
        if intents[gen]["status"] == "committed":
            retired = retired_intent(intents[gen]["payload"], now())
            control_bytes += max(0, len(intents[gen]["raw"]) - len(
                json.dumps(retired, ensure_ascii=False, sort_keys=True).encode() + b"\n"))
        if gen in retained and retained[gen]["status"] == "committed":
            retired = retired_retention(retained[gen]["raw"], now())
            control_bytes += max(0, len(retained[gen]["raw"]) - len(
                json.dumps(retired, ensure_ascii=False, sort_keys=True).encode() + b"\n"))
    candidate_bytes = sum(sizes[path] for path in candidates)
    live = accept_live(client, generation) if verify_live else None
    if store.read_bytes(BUCKET, CURRENT) != pointer_raw:
        raise Blocker("OCR pointer changed during live-consumer acceptance")

    return {
        "version": 1, "kind": "ccrd-ocr-gc-plan", "observed_at": now(),
        "bucket": BUCKET, "current": generation, "parent": pointer.get("parent"),
        "keep_generations": keep_generations,
        "kept": keep, "expiring": expiring, "deferred": deferred,
        "live": {"checked": bool(verify_live), "generation": generation,
                 "ready": live.get("ready") if live else None,
                 "loaded_sources": live.get("loaded_sources") if live else None},
        "objects": {"managed": len(sizes), "managed_bytes": sum(sizes.values()),
                    "protected": len(protected), "candidates": len(candidates),
                    "candidate_bytes": candidate_bytes,
                    "control_candidates": len(allowed_control), "control_bytes": control_bytes,
                    "total_reclaimable_bytes": candidate_bytes + control_bytes},
        "candidates": candidates,
        "candidate_sizes": {path: sizes[path] for path in candidates},
        "control_candidates": allowed_control,
        "retire_intents": retire_intents,
        "retire_retention": retire_retention,
        "expired": {gen: {"members": len(item["members"]), "missing": len(item["missing"]),
                          "bytes": item["bytes"], "intent": item["intent"],
                          "retained": item["retained"]}
                    for gen, item in expired.items()},
        "generations": {"total": len(chain), "kept": len(keep),
                        "expiring": len(expiring), "deferred": len(deferred),
                        "already_retired": already_retired,
                        "completed": completed,
                        "open_intents": open_intents},
        "limit": limit,
        "control_snapshot": sorted(control_snapshot),
        "legacy_generations": legacy_inventory(store),
        "deletion_enabled": False,
    }


def retired_retention(raw, stamp):
    history = json.loads(raw)
    retired = {key: value for key, value in history.items() if key not in {"resources"}}
    retired["status"] = "retired"
    retired["resources"] = []
    retired["retired_at"] = stamp
    retired["release"] = history.get("resources", [None])[0]
    return retired


def retired_intent(intent, stamp):
    retired = {key: value for key, value in intent.items() if key not in {"resources"}}
    retired["status"] = "retired"
    retired["resources"] = []
    retired["retired_at"] = stamp
    return retired


def apply(store, report, *, client=None):
    """Retire expired control roots, then delete the planned data objects.

    The caller holds the publish-ccrd-index lock. The pointer, the control
    document set and the live generation are rechecked here as the last write
    barrier. Control roots become small retired audit records; data deletion
    is the only destructive step.
    """
    if report.get("kind") != "ccrd-ocr-gc-plan" or report.get("bucket") != BUCKET:
        raise ValueError("not a CCRD OCR GC plan")
    if client is not None:
        accept_live(client, report["current"])
    pointer, _ = read_json(store, CURRENT)
    if pointer.get("generation") != report["current"]:
        raise Blocker("OCR pointer changed before deletion")
    current_control = set(store.list_files(BUCKET, ("control/intents/",))) \
        | set(store.list_files(BUCKET, ("control/retained/",)))
    if current_control != set(report["control_snapshot"]):
        raise Blocker("OCR control history changed before deletion")

    retire = sorted(set(report.get("retire_intents", [])))
    retire_retention = sorted(set(report.get("retire_retention", [])))
    allowed_retire = {f"control/intents/{gen}.json" for gen in report["expiring"]}
    allowed_retention = {f"control/retained/{gen}.json" for gen in report["expiring"]}
    if not set(retire) <= allowed_retire or not set(retire_retention) <= allowed_retention:
        raise Blocker("refusing to rewrite an unexpected control document")
    retire_set = set(retire)
    retention_set = set(retire_retention)
    for gen in report["expiring"]:
        intent_path = f"control/intents/{gen}.json"
        intent, _ = read_json(store, intent_path)
        if intent.get("generation") != gen:
            raise Blocker(f"intent generation changed before retirement: {intent_path}")
        if intent_path in retire_set:
            if intent.get("status") != "committed":
                raise Blocker(f"intent changed before retirement: {intent_path}")
            store.put_json(BUCKET, intent_path, retired_intent(intent, now()))
        elif intent.get("status") != "retired":
            raise Blocker(f"unexpected intent status before cleanup: {intent_path}")

        retention_path = f"control/retained/{gen}.json"
        try:
            history, history_raw = read_json(store, retention_path)
        except Blocker:
            history, history_raw = None, None
        if retention_path in retention_set:
            if history is None or history.get("status", "committed") != "committed":
                raise Blocker(f"retention changed before retirement: {retention_path}")
            store.put_json(BUCKET, retention_path, retired_retention(history_raw, now()))
        elif history is not None and history.get("status") != "retired":
            raise Blocker(f"unexpected retention status before cleanup: {retention_path}")
    for gen in report["expiring"]:
        intent_path = f"control/intents/{gen}.json"
        intent, _ = read_json(store, intent_path)
        if intent.get("status") != "retired" or intent.get("resources") != []:
            raise RuntimeError(f"intent retirement readback failed: {intent_path}")
        retention_path = f"control/retained/{gen}.json"
        if retention_path in retention_set:
            history, _ = read_json(store, retention_path)
            if history.get("status") != "retired" or history.get("resources") != []:
                raise RuntimeError(f"retention retirement readback failed: {retention_path}")

    candidates = list(report.get("candidates", []))
    if any(path.startswith("control/") for path in candidates):
        raise Blocker("refusing to delete a control document as data")
    deleted_objects = 0
    deleted_bytes = 0
    try:
        for path in candidates:
            deleted_bytes += report.get("candidate_sizes", {}).get(path, 0)
            store.delete(BUCKET, [path])
            deleted_objects += 1
        if candidates:
            remaining = set(store.list_files(BUCKET, lifecycle.MANAGED_PREFIXES))
            if any(path in remaining for path in candidates):
                raise RuntimeError("data deletion readback failed")
    finally:
        report["deleted"] = {"objects": deleted_objects, "bytes": deleted_bytes,
                             "control": 0, "intents_retired": len(retire),
                             "retention_retired": len(retire_retention)}
        report["finished_at"] = now()
    report["deletion_enabled"] = True
    report["applied"] = True
    pointer_after, _ = read_json(store, CURRENT)
    if pointer_after.get("generation") != report["current"]:
        raise Blocker("OCR pointer changed during deletion")
    if client is not None:
        report["live_after"] = {"checked": True, "generation": report["current"],
                                "ready": True}
        accept_live(client, report["current"])
    return report


def write_journal(store, report):
    stamp = report["observed_at"].replace(":", "").replace("-", "")
    run = os.environ.get("GITHUB_RUN_ID", "local")
    path = f"{JOURNAL_PREFIX}{stamp}-{run}-{report['current'][:12]}.json"
    store.put_json(BUCKET, path, report)
    report["journal"] = path
    return path


def write_output(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="delete after planning; default is plan only")
    parser.add_argument("--keep-generations", type=int, default=2,
                        help="current plus this many ancestors; minimum 2 (default: 2)")
    parser.add_argument("--limit", type=int, default=0,
                        help="maximum generations to expire in this run; zero means all")
    parser.add_argument("--include-staging", action="store_true",
                        help="also reclaim abandoned staging/ocr objects")
    parser.add_argument("--verify-contents", action="store_true",
                        help="also re-read every kept TXT body (slow; publication already checked it)")
    parser.add_argument("--skip-live", action="store_true",
                        help="skip live CCRD acceptance; only valid outside Actions")
    parser.add_argument("--output", type=Path, default=Path("output/gc/ccrd-ocr-gc.json"))
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must not be negative")
    if args.skip_live and os.environ.get("GITHUB_ACTIONS") == "true":
        parser.error("--skip-live is not allowed in Actions")
    if args.apply:
        assert_workflow_context(".github/workflows/reader-gc-ccrd.yml")
    store = HubBucketStore()
    with httpx.Client() as client:
        try:
            report = plan(store, keep_generations=args.keep_generations, limit=args.limit,
                          include_staging=args.include_staging, client=client,
                          verify_live=not args.skip_live, verify_contents=args.verify_contents)
        except Blocker as error:
            # A plan must still produce evidence when a safety gate trips.
            report = {"version": 1, "kind": "ccrd-ocr-gc-plan", "observed_at": now(),
                      "bucket": BUCKET, "current": None, "blockers": [str(error)],
                      "deletion_enabled": False, "applied": False}
            write_output(args.output, report)
            print(json.dumps({"blockers": report["blockers"]}, ensure_ascii=False, indent=2))
            return 2 if args.apply else 0
        # Preserve the plan artifact even if the destructive phase fails.
        write_output(args.output, report)
        if args.apply:
            try:
                report["phase"] = "planned"
                write_journal(store, report)
                apply(store, report, client=client)
                report["phase"] = "applied"
                write_journal(store, report)
            except Exception as error:
                report["phase"] = "failed"
                report["error"] = f"{type(error).__name__}: {error}"
                try:
                    write_journal(store, report)
                except Exception:
                    pass
                raise
            finally:
                write_output(args.output, report)
    write_output(args.output, report)
    print(json.dumps({key: report[key] for key in
                      ("current", "kept", "expiring", "deferred", "objects", "deleted",
                       "blockers", "journal", "phase", "error")
                      if key in report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
