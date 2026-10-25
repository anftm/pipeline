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
import time
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


def read_json(store, path, *, required=True, cached=False):
    try:
        reader = getattr(store, 'read_cached_bytes', store.read_bytes) if cached else store.read_bytes
        raw = reader(BUCKET, path)
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
    release, raw = read_json(store, path, cached=True)
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
    rollout, rollout_raw = read_json(store, 'control/newspaper-rollout.json', required=False)
    if rollout and rollout.get('status') != 'committed':
        raise Blocker('pending newspaper rollout retains the previous OCR generation; collection is blocked')
    if keep_generations < 2:
        raise ValueError("keep-generations must retain the current release and its rollback parent")
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must not be negative")
    pointer, pointer_raw = read_json(store, CURRENT, required=False)
    if pointer is None:
        # A deliberately withdrawn library is a normal scheduled no-op. Any
        # leftover content requires its saved reset plan, never guessed deletion.
        remaining = store.list_files(BUCKET, lifecycle.MANAGED_PREFIXES)
        if remaining:
            raise Blocker('OCR pointer absent with remaining data; resume the explicit withdrawal plan')
        if verify_live:
            accept_live(client, None)
        return {'version': 1, 'kind': 'ccrd-ocr-gc-plan', 'observed_at': now(),
                'bucket': BUCKET, 'current': None, 'reason': 'no-active-ocr-library',
                'kept': [], 'expiring': [], 'deferred': [], 'candidates': [],
                'objects': {'candidates': 0, 'candidate_bytes': 0}, 'deletion_enabled': False}
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
    control_hashes = {CURRENT: digest(pointer_raw),
                      'control/newspaper-rollout.json': digest(rollout_raw) if rollout_raw is not None else None}
    for path in sorted(store.list_files(BUCKET, ("control/intents/",))):
        intent, raw = read_json(store, path, cached=True)
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
        control_hashes[path] = digest(raw)
    for path in sorted(store.list_files(BUCKET, ("control/retained/",))):
        history, raw = read_json(store, path, cached=True)
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
        control_hashes[path] = digest(raw)

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
        payload, control_raw = read_json(store, name, required=(name == IDS))
        control_hashes[name] = digest(control_raw) if control_raw is not None else None
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
        'control_hashes': control_hashes,
        'protected_sizes': {path: sizes[path] for path in protected if path in sizes},
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


def apply(store, report, *, client=None, checkpoint=None, batch_size=500):
    """Retire expired control roots, then delete the planned data objects.

    The caller holds the publish-ccrd-index lock. The pointer, the control
    document set and the live generation are rechecked here as the last write
    barrier. Control roots become small retired audit records; data deletion
    is the only destructive step.
    """
    if report.get("kind") != "ccrd-ocr-gc-plan" or report.get("bucket") != BUCKET:
        raise ValueError("not a CCRD OCR GC plan")
    if not 1 <= batch_size <= 1000:
        raise ValueError('invalid CCRD deletion batch size')
    if report.get('reason') == 'no-active-ocr-library':
        if read_json(store, CURRENT, required=False)[0] is not None or store.list_files(BUCKET, lifecycle.MANAGED_PREFIXES):
            raise Blocker('OCR library changed after empty plan')
        if client is not None:
            accept_live(client, None)
        report.update(applied=True, complete=True, deleted={'objects': 0, 'bytes': 0, 'control': 0})
        return report
    if client is not None:
        accept_live(client, report["current"])
    pointer, _ = read_json(store, CURRENT)
    if pointer.get("generation") != report["current"]:
        raise Blocker("OCR pointer changed before deletion")
    current_control = set(store.list_files(BUCKET, ("control/intents/",))) \
        | set(store.list_files(BUCKET, ("control/retained/",)))
    if current_control != set(report["control_snapshot"]):
        raise Blocker("OCR control history changed before deletion")
    expected_controls = dict(report['control_hashes'])
    def barrier():
        controls = set(store.list_files(BUCKET, ('control/intents/', 'control/retained/')))
        if controls != set(report['control_snapshot']):
            raise Blocker('OCR control history changed during deletion')
        for path, expected in expected_controls.items():
            _, raw = read_json(store, path, required=expected is not None,
                               cached=path.startswith(('control/intents/', 'control/retained/')))
            if (digest(raw) if raw is not None else None) != expected:
                raise Blocker('OCR control content changed before deletion: ' + path)
    barrier()

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
            expected_controls[intent_path] = digest(store.read_bytes(BUCKET, intent_path))
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
            expected_controls[retention_path] = digest(store.read_bytes(BUCKET, retention_path))
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
    report['batches'] = []
    report['complete'] = False
    try:
        for start in range(0, len(candidates), batch_size):
            barrier()
            paths = candidates[start:start + batch_size]
            batch = {'paths': paths, 'status': 'submitted'}
            report['batches'].append(batch)
            if checkpoint:
                checkpoint(report)
            store.delete(BUCKET, paths)
            batch['status'] = 'accepted'
            deleted_bytes += sum(report.get('candidate_sizes', {}).get(path, 0) for path in paths)
            deleted_objects += len(paths)
            report['accepted'] = {'objects': deleted_objects, 'bytes': deleted_bytes}
            if checkpoint:
                checkpoint(report)
        if candidates:
            remaining = inventory(store, True)
            if any(path in remaining for path in candidates):
                raise RuntimeError("data deletion readback failed")
            if any(remaining.get(path) != size for path, size in report['protected_sizes'].items()):
                raise RuntimeError('retained CCRD object disappeared or changed during collection')
        barrier()
        if client is not None:
            accept_live(client, report['current'])
            report['live_after'] = {'checked': True, 'generation': report['current'], 'ready': True}
        for batch in report['batches']:
            batch['status'] = 'verified-deleted'
        report['complete'] = True
    finally:
        report["deleted"] = {"objects": deleted_objects if report['complete'] else 0,
                             "bytes": deleted_bytes if report['complete'] else 0,
                             "control": 0, "intents_retired": len(retire),
                             "retention_retired": len(retire_retention)}
        report["finished_at"] = now()
    report["deletion_enabled"] = True
    report["applied"] = True
    return report


def write_journal(store, report):
    stamp = report["observed_at"].replace(":", "").replace("-", "")
    run = os.environ.get("GITHUB_RUN_ID", "local")
    path = f"{JOURNAL_PREFIX}{stamp}-{run}-{str(report['current'])[:12]}.json"
    store.put_json(BUCKET, path, report)
    report["journal"] = path
    return path



LEGACY_CURRENT = "current.json"
LEGACY_PREFIX = "generations/"
INDEX_PREFIXES = (LEGACY_PREFIX, 'indexes/newspapers/')
LEGACY_JOURNAL_PREFIX = "reports/ccrd-legacy-gc/"
SOURCES_URL = "https://vomebook-search.hf.space/api/sources"


def legacy_pointer(store):
    payload, raw = read_json(store, LEGACY_CURRENT)
    if payload.get('format') == 2:
        refs = schema(payload, LEGACY_CURRENT)
        generation = payload['generation']
        if not re.fullmatch(r'ccrd-four-[0-9a-f]{64}', generation):
            raise Blocker('four-source pointer has an invalid generation')
        return payload, raw, {ref['path']: ref for ref in refs}, {generation}
    if payload.get("format") != 1 or payload.get("tokenizer_version") != lifecycle.TOKENIZER:
        raise Blocker("legacy CCRD pointer has an unknown format or tokenizer")
    databases = payload.get("databases")
    if not isinstance(databases, dict) or set(databases) != {"CCRD", "CW"}:
        raise Blocker("legacy CCRD pointer must name CCRD and CW")
    refs, active_dirs = {}, set()
    for name, item in databases.items():
        if not isinstance(item, dict):
            raise Blocker(f"legacy CCRD pointer has no {name} identity")
        path = item.get("path", "")
        match = re.fullmatch(r"generations/([^/]+)/(CCRD|CW)\.sqlite3", path)
        if not match or match.group(2) != name:
            raise Blocker(f"legacy CCRD pointer has an invalid {name} path")
        if (type(item.get("documents")) is not int or item["documents"] < 1
                or item.get("fts_rows") != item["documents"]):
            raise Blocker(f"legacy CCRD pointer has invalid {name} counts")
        ref = {"bucket": BUCKET, "path": path, "sha256": item.get("sha256"),
               "bytes": item.get("bytes"), "role": "runtime"}
        lifecycle.resource(ref)
        refs[path] = ref
        active_dirs.add(match.group(1))
    return payload, raw, refs, active_dirs


def legacy_manifest(store, directory, sizes):
    path = f"generations/{directory}/manifest.json"
    if path not in sizes:
        raise Blocker(f"missing legacy generation manifest: {path}")
    payload, raw = read_json(store, path, cached=True)
    if payload.get('format') == 2:
        refs = schema(payload, path)
        members = {path, *(ref['path'] for ref in refs)}
        missing = members - sizes.keys()
        for ref in refs:
            if ref['path'] in sizes:
                require_member(sizes, ref, path)
        previous = payload.get('previous_index_manifest_sha256')
        if not re.fullmatch(r'[0-9a-f]{64}', str(previous)):
            raise Blocker(path + ': missing exact previous index manifest')
        # Four-source manifests use content identity and parent hashes. S3's
        # immutable object timestamp orders branches without changing the schema.
        created = getattr(store, 'object_modified', {}).get((BUCKET, path))
        if created is None:
            created = datetime.min.replace(tzinfo=timezone.utc)
        return {'dir': directory, 'path': path, 'created': created, 'refs': members,
                'missing': missing, 'databases': payload['databases'], 'raw_bytes': len(raw),
                'sha256': digest(raw), 'previous_sha256': previous}
    if payload.get("format") != 1 or payload.get("tokenizer_version") != lifecycle.TOKENIZER:
        raise Blocker(f"{path}: unknown legacy generation schema")
    if payload.get("created_at") is None:
        raise Blocker(f"{path}: generation has no created_at")
    try:
        created = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
    except ValueError:
        raise Blocker(f"{path}: invalid created_at")
    if created.tzinfo is None:
        raise Blocker(f'{path}: created_at must include a timezone')
    databases = payload.get("databases")
    if not isinstance(databases, dict) or set(databases) != {"CCRD", "CW"}:
        raise Blocker(f"{path}: generation must name CCRD and CW")
    refs, missing = {path}, set()
    for name, item in databases.items():
        if not isinstance(item, dict):
            raise Blocker(f"{path}: invalid {name} identity")
        db_path = item.get("path", "")
        if (not db_path.startswith(LEGACY_PREFIX) or not db_path.endswith(f"/{name}.sqlite3")
                or type(item.get("documents")) is not int or item["documents"] < 1
                or item.get("fts_rows") != item["documents"]):
            raise Blocker(f"{path}: invalid {name} identity")
        ref = {"bucket": BUCKET, "path": db_path, "sha256": item.get("sha256"),
               "bytes": item.get("bytes"), "role": "runtime"}
        lifecycle.resource(ref)
        refs.add(db_path)
        if db_path not in sizes:
            missing.add(db_path)
        elif sizes[db_path] != ref["bytes"]:
            raise Blocker(f"{path}: {name} size differs from its manifest")
    return {"dir": directory, "path": path, "created": created, "refs": refs,
            "missing": missing, "databases": databases, "raw_bytes": len(raw), 'sha256': digest(raw)}


def legacy_tree(store):
    sizes = {}
    try:
        files = store.list_files(BUCKET, INDEX_PREFIXES)
    except Exception as error:
        raise Blocker(f"cannot inventory {LEGACY_PREFIX}: {error}")
    for path in files:
        known = getattr(store, "object_sizes", {}).get((BUCKET, path))
        if type(known) is int and known >= 0:
            sizes[path] = known
            continue
        try:
            sizes[path] = store.stat(BUCKET, path)
        except FileNotFoundError:
            continue
    dirs = {}
    for path in sizes:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "generations":
            dirs.setdefault(parts[1], set()).add(path)
    return sizes, dirs


def legacy_live(client, pointer):
    if client is None:
        raise Blocker("live CCRD status was not checked")
    response = client.get(LIVE_STATUS, headers={"Cache-Control": "no-cache"}, timeout=60)
    response.raise_for_status()
    status = response.json()
    bucket = status.get("bucket") or {}
    expected_sources = set(pointer['databases'])
    four_source = pointer.get('format') == 2
    if (status.get("ready") is not True or bucket.get("state") != "ready"
            or bucket.get("source") not in ({'space-activation', 'bucket-pointer'} if four_source else {'bucket-pointer'})
            or set(bucket.get("sources") or []) != expected_sources
            or not expected_sources <= set(status.get("loaded_sources") or [])):
        raise Blocker("live CCRD is not serving the bucket legacy pointer")
    if four_source and (bucket.get('generation') != pointer['generation']
                        or bucket.get('base_metadata_sha256') != pointer['base_metadata_sha256']):
        raise Blocker('live CCRD has not loaded the exact four-source index generation')
    sources = client.get(SOURCES_URL, headers={"Cache-Control": "no-cache"}, timeout=60)
    sources.raise_for_status()
    rows = sources.json()
    if isinstance(rows, dict):
        rows = rows.get("sources", [])
    counts = {row.get("slug"): row.get("count") for row in rows if isinstance(row, dict)}
    expected = {name: item['documents'] for name, item in pointer['databases'].items()}
    if any(counts.get(name) != expected[name] for name in expected):
        raise Blocker("live CCRD source counts differ from the bucket legacy pointer")
    return {"bucket": bucket, "counts": counts, "checked_at": now()}


def index_control_pins(store):
    """Rollouts and OCR control roots may pin an older base index or archive."""
    paths = store.list_files(BUCKET, ('control/',))
    hashes, refs = {}, {}
    for path in sorted(paths):
        payload, raw = read_json(store, path, cached=True)
        hashes[path] = digest(raw)
        for ref in schema(payload, path):
            if ref['bucket'] == BUCKET and ref['path'].startswith(INDEX_PREFIXES):
                refs[ref['path']] = ref
    return hashes, refs


def legacy_plan(store, *, keep_generations=2, limit=0, client=None, verify_live=True):
    if type(keep_generations) is not int or keep_generations < 1:
        raise ValueError("keep-generations must be a positive integer for legacy generations")
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must not be negative")
    pointer, pointer_raw, active_refs, active_dirs = legacy_pointer(store)
    sizes, dirs = legacy_tree(store)
    for path, ref in active_refs.items():
        require_member(sizes, ref, "legacy pointer")
    if not active_dirs or any(d not in dirs for d in active_dirs):
        raise Blocker("legacy pointer names a missing generation directory")

    manifests = {}
    for directory, files in dirs.items():
        if f"generations/{directory}/manifest.json" in files:
            manifests[directory] = legacy_manifest(store, directory, sizes)
    control_hashes, control_refs = index_control_pins(store)
    for ref in control_refs.values():
        require_member(sizes, ref, 'index control pin')
    protected = set(active_refs)
    protected.update(control_refs)
    for directory in active_dirs:
        protected.update(dirs[directory])
        manifest = manifests.get(directory)
        if manifest is None:
            raise Blocker(f"active legacy generation lacks a manifest: {directory}")
        if manifest["missing"]:
            raise Blocker(f"active legacy generation is incomplete: {directory}")
        if pointer.get('format') == 2 and manifest['sha256'] != digest(pointer_raw):
            raise Blocker('current four-source manifest differs from its pointer')
        protected.update(manifest["refs"])

    candidates = [d for d in manifests if d not in active_dirs]
    ordered = sorted(candidates, key=lambda d: (manifests[d]["created"], d), reverse=True)
    keep_old = max(0, keep_generations - len(active_dirs))
    kept_old = []
    by_hash = {item['sha256']: directory for directory, item in manifests.items()}
    parent_hash = pointer.get('previous_index_manifest_sha256')
    while parent_hash and len(kept_old) < keep_old:
        parent = by_hash.get(parent_hash)
        if parent is None or parent in active_dirs or parent in kept_old:
            raise Blocker('exact index rollback ancestor is missing or cyclic')
        kept_old.append(parent)
        parent_hash = manifests[parent].get('previous_sha256')
    for directory in ordered:
        if len(kept_old) >= keep_old:
            break
        if directory not in kept_old:
            kept_old.append(directory)
    remaining = [d for d in ordered if d not in kept_old]
    oldest = sorted(remaining, key=lambda d: (manifests[d]["created"], d))
    expiring = oldest[:limit] if limit else oldest
    deferred = oldest[len(expiring):]

    for directory in [*kept_old, *deferred]:
        if manifests[directory]["missing"]:
            raise Blocker(f"kept legacy generation is incomplete: {directory}")
        protected.update(dirs[directory])
        protected.update(manifests[directory]["refs"])
    for directory in [*kept_old, *deferred]:
        protected.add(manifests[directory]["path"])

    # A retained manifest can reference databases outside its own generation.
    # Expand any manifest pinned by a rollout before choosing deletion targets.
    pending = [item for item in manifests.values() if item['path'] in protected]
    visited = set()
    while pending:
        item = pending.pop()
        if item['path'] in visited:
            continue
        visited.add(item['path'])
        if item['missing']:
            raise Blocker('retained index manifest is incomplete: ' + item['path'])
        protected.update(item['refs'])
        protected.update(dirs[item['dir']])
        pending.extend(other for other in manifests.values()
                       if other['path'] in protected and other['path'] not in visited)

    generation_candidates = sorted(path for path in sizes
                                   if path.startswith(INDEX_PREFIXES) and path not in protected)
    if any(path.startswith("control/") for path in generation_candidates):
        raise Blocker("legacy candidate list unexpectedly contains control documents")
    live = legacy_live(client, pointer) if verify_live else None
    if store.read_bytes(BUCKET, LEGACY_CURRENT) != pointer_raw:
        raise Blocker("legacy CCRD pointer changed during planning")

    rows = []
    for directory in sorted(dirs):
        manifest = manifests.get(directory)
        rows.append({"directory": directory,
                     "created_at": manifest["created"].isoformat() if manifest else None,
                     "bytes": sum(sizes[path] for path in dirs[directory]),
                     "documents": manifest["databases"] if manifest else None,
                     "state": ("current" if directory in active_dirs else
                               "kept" if directory in kept_old else
                               "expiring" if directory in expiring else
                               "deferred" if directory in deferred else
                               "orphan")})
    return {
        "version": 1, "kind": "ccrd-legacy-gc-plan", "scope": "legacy-generations",
        "observed_at": now(), "bucket": BUCKET, "current_dirs": sorted(active_dirs),
        "pointer_sha256": digest(pointer_raw),
        "documents": {name: item['documents'] for name, item in pointer['databases'].items()},
        "live": live, "keep_generations": keep_generations,
        "kept": kept_old, "expiring": expiring, "deferred": deferred,
        "generations": rows,
        "objects": {"managed": len(sizes), "managed_bytes": sum(sizes.values()),
                    "protected": len(protected),
                    "candidates": len(generation_candidates),
                    "candidate_bytes": sum(sizes[path] for path in generation_candidates),
                    "total_reclaimable_bytes": sum(sizes[path] for path in generation_candidates)},
        "candidates": generation_candidates,
        "candidate_sizes": {path: sizes[path] for path in generation_candidates},
        "inventory_snapshot": sorted(sizes),
        "protected_sizes": {path: sizes[path] for path in protected if path in sizes},
        'manifest_hashes': {item['path']: item['sha256'] for item in manifests.values()},
        'control_hashes': control_hashes,
        "deletion_enabled": False,
    }


def legacy_apply(store, report, *, client=None, checkpoint=None, batch_size=500):
    if report.get("kind") != "ccrd-legacy-gc-plan" or report.get("bucket") != BUCKET:
        raise ValueError("not a legacy CCRD GC plan")
    if not 1 <= batch_size <= 1000:
        raise ValueError('invalid legacy deletion batch size')
    pointer, pointer_raw, _, _ = legacy_pointer(store)
    if digest(pointer_raw) != report["pointer_sha256"]:
        raise Blocker("legacy CCRD pointer changed before deletion")
    if client is not None:
        legacy_live(client, pointer)
    sizes, _ = legacy_tree(store)
    if sorted(sizes) != report["inventory_snapshot"]:
        raise Blocker("legacy generations inventory changed before deletion")
    for path, size in report["protected_sizes"].items():
        if sizes.get(path) != size:
            raise Blocker(f"kept legacy object changed before deletion: {path}")

    candidates = list(report.get("candidates", []))
    if any(not path.startswith(INDEX_PREFIXES) for path in candidates):
        raise Blocker("refusing to delete outside managed index prefixes")
    deleted_paths = set()
    deleted_bytes = 0
    report.update(complete=False, batches=[])
    def barrier():
        if digest(store.read_bytes(BUCKET, LEGACY_CURRENT)) != report['pointer_sha256']:
            raise Blocker('legacy CCRD pointer changed during deletion')
        listed = store.list_files(BUCKET, INDEX_PREFIXES)
        known_manifests = set(report['manifest_hashes']) - deleted_paths
        actual_manifests = {path for path in listed if re.fullmatch(r'generations/[^/]+/manifest\.json', path)}
        if actual_manifests != known_manifests:
            raise Blocker('index manifest inventory changed during deletion')
        for path, expected in report['manifest_hashes'].items():
            if path not in deleted_paths and digest(read_json(store, path, cached=True)[1]) != expected:
                raise Blocker('legacy generation manifest changed during deletion: ' + path)
        if index_control_pins(store)[0] != report['control_hashes']:
            raise Blocker('index control references changed during deletion')
    def remove(paths):
        nonlocal deleted_bytes
        for start in range(0, len(paths), batch_size):
            barrier()
            batch = paths[start:start + batch_size]
            receipt = {'paths': batch, 'status': 'submitted'}
            report['batches'].append(receipt)
            if checkpoint:
                checkpoint(report)
            store.delete(BUCKET, batch)
            deleted_paths.update(batch)
            deleted_bytes += sum(report['candidate_sizes'].get(path, 0) for path in batch)
            receipt['status'] = 'accepted'
            report['accepted'] = {'objects': len(deleted_paths), 'bytes': deleted_bytes}
            if checkpoint:
                checkpoint(report)
    try:
        # Remove the manifest first: a retry then treats the directory as an
        # orphan and can finish deleting its remaining files without guessing
        # a retention timestamp.
        manifests = [f'generations/{directory}/manifest.json' for directory in report['expiring']]
        remove([path for path in manifests if path in candidates])
        remove([path for path in candidates if path not in deleted_paths])
        remaining, _ = legacy_tree(store)
        if set(candidates) & remaining.keys():
            raise RuntimeError("legacy deletion readback failed")
        if any(remaining.get(path) != size for path, size in report['protected_sizes'].items()):
            raise RuntimeError('retained legacy object disappeared or changed during collection')
        barrier()
        if client is not None:
            report['live_after'] = legacy_live(client, pointer)
        for batch in report['batches']:
            batch['status'] = 'verified-deleted'
        report['complete'] = True
    finally:
        report["deleted"] = {"objects": len(deleted_paths) if report['complete'] else 0,
                             "bytes": deleted_bytes if report['complete'] else 0}
        report["finished_at"] = now()
    report["deletion_enabled"] = True
    report["applied"] = True
    return report


def write_legacy_journal(store, report):
    stamp = report["observed_at"].replace(":", "").replace("-", "")
    run = os.environ.get("GITHUB_RUN_ID", "local")
    current = "-".join(report["current_dirs"])[:12]
    path = f"{LEGACY_JOURNAL_PREFIX}{stamp}-{run}-{current}.json"
    store.put_json(BUCKET, path, report)
    report["journal"] = path
    return path


def write_output(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("ocr", "legacy-generations"), default="ocr",
                        help="ocr: retired OCR releases; legacy-generations: old CCRD/CW indexes")
    parser.add_argument("--apply", action="store_true",
                        help="delete after planning; default is plan only")
    parser.add_argument("--keep-generations", type=int, default=2,
                        help="total retained generations including current; OCR minimum 2, legacy minimum 1")
    parser.add_argument("--limit", type=int, default=0,
                        help="maximum generations to expire in this run; zero means all")
    parser.add_argument("--include-staging", action="store_true",
                        help="also reclaim abandoned staging/ocr objects (OCR scope)")
    parser.add_argument("--verify-contents", action="store_true",
                        help="also re-read kept TXT bodies (OCR scope; slow)")
    parser.add_argument("--skip-live", action="store_true",
                        help="skip live CCRD acceptance; only valid outside Actions")
    parser.add_argument('--storage', choices=('hub', 's3'), default='hub')
    parser.add_argument("--output", type=Path, default=Path("output/gc/ccrd-ocr-gc.json"))
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must not be negative")
    if args.scope == "ocr" and args.keep_generations < 2:
        parser.error("OCR scope must retain the current release and its rollback parent")
    if args.scope == "legacy-generations" and args.keep_generations < 1:
        parser.error("legacy scope must retain at least the current generation")
    if args.scope != "ocr" and (args.include_staging or args.verify_contents):
        parser.error("--include-staging/--verify-contents only apply to OCR scope")
    if args.skip_live and os.environ.get("GITHUB_ACTIONS") == "true":
        parser.error("--skip-live is not allowed in Actions")
    if args.apply:
        assert_workflow_context(".github/workflows/reader-gc-ccrd.yml")

    plan_kind = "ccrd-legacy-gc-plan" if args.scope == "legacy-generations" else "ccrd-ocr-gc-plan"
    if args.storage == 's3':
        try:
            from .reader_gc_store import CcrdCollectionStore
        except ImportError:
            from reader_gc_store import CcrdCollectionStore
        store = CcrdCollectionStore()
    else:
        store = HubBucketStore()
    with httpx.Client() as client:
        try:
            if args.scope == "legacy-generations":
                report = legacy_plan(store, keep_generations=args.keep_generations,
                                     limit=args.limit, client=client,
                                     verify_live=not args.skip_live)
            else:
                report = plan(store, keep_generations=args.keep_generations, limit=args.limit,
                              include_staging=args.include_staging, client=client,
                              verify_live=not args.skip_live, verify_contents=args.verify_contents)
        except Blocker as error:
            report = {"version": 1, "kind": plan_kind, "scope": args.scope,
                      "observed_at": now(), "bucket": BUCKET, "blockers": [str(error)],
                      "deletion_enabled": False, "applied": False}
            write_output(args.output, report)
            print(json.dumps({"scope": args.scope, "blockers": report["blockers"]},
                             ensure_ascii=False, indent=2))
            return 2 if args.apply else 0
        write_output(args.output, report)
        if args.apply:
            journal = write_legacy_journal if args.scope == "legacy-generations" else write_journal
            try:
                report["phase"] = "planned"
                journal(store, report)
                last_save = [0.0]
                def checkpoint(value):
                    if value.get('complete') or time.monotonic() - last_save[0] > 30:
                        write_output(args.output, value)
                        journal(store, value)
                        last_save[0] = time.monotonic()
                if args.scope == "legacy-generations":
                    legacy_apply(store, report, client=client, checkpoint=checkpoint)
                else:
                    apply(store, report, client=client, checkpoint=checkpoint)
                report["phase"] = "applied"
                journal(store, report)
            except Exception as error:
                report["phase"] = "failed"
                report["error"] = f"{type(error).__name__}: {error}"
                try:
                    journal(store, report)
                except Exception:
                    pass
                raise
            finally:
                write_output(args.output, report)
    write_output(args.output, report)
    keys = ("scope", "current", "current_dirs", "kept", "expiring", "deferred", "objects",
            "deleted", "blockers", "journal", "phase", "error")
    print(json.dumps({key: report[key] for key in keys if key in report},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
