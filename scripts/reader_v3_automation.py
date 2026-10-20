#!/usr/bin/env python3
"""Durable, budgeted OCR admission under the existing central v3 writer lock."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlsplit

try:
    from . import publish_reader_v3 as publication, pdf_text_layer as text, shared
except ImportError:
    import publish_reader_v3 as publication
    import pdf_text_layer as text
    import shared

STATE = "reader-index/v3/automation.json"
REGISTRY = "reader-index/pdf_ocr_manifest.json"
RENDER_REGISTRY = "reader-index/pdf_render_manifest.json"
BUILDS = "reader-index/v3/builds"
# Builds run in parallel outside reader-sidecar; only promotion is serialized.
DAILY_ATTEMPTS = 384
DAILY_PAGES = 200000
MAX_ATTEMPTS = 3
WAVE_SIZE = 8
BUILD_TIMEOUT = timedelta(hours=6)
# Recipe revision whose unusable-text handling falls back to a native text layer.
REVIEW_RECIPE = "native-text-fallback-v1"


def clock():
    return datetime.now(timezone.utc)


def load_state(store):
    try:
        state = json.loads(store.read_bytes(publication.ASSETS, STATE))
    except FileNotFoundError:
        return {"version": 1, "kind": "reader-v3-automation", "tasks": {}, "days": {}}
    if (state.get("version") != 1 or state.get("kind") != "reader-v3-automation"
            or not isinstance(state.get("tasks"), dict) or not isinstance(state.get("days"), dict)):
        raise ValueError("invalid v3 automation state")
    return state


def save_state(store, state):
    publication.assert_writer(store)
    raw = publication.encode(state)
    store.put_bytes(publication.ASSETS, STATE, raw)
    if store.read_bytes(publication.ASSETS, STATE) != raw:
        raise ValueError("automation checkpoint readback mismatch")


def retry(store, options, *, apply=False):
    """Explicit operator retry preserves history and the already charged day budget."""
    queue = options.get("queue", "build")
    if queue == "build-dispatch":
        return recover_dispatches(store, options, apply=apply)
    if queue == "build-timeout":
        return recover_execution_timeouts(store, options, apply=apply)
    if queue == "correction":
        try:
            from . import reader_v3_correction as correction
        except ImportError:
            import reader_v3_correction as correction
        state, persist = correction.load_state(store), correction.save
    elif queue == "build":
        state, persist = load_state(store), save_state
    else:
        raise ValueError("unknown retry queue")
    ids = options.get("task_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("explicit retry task IDs required")
    import os
    actor = os.environ.get("GITHUB_ACTOR", "").strip()
    if not actor:
        raise ValueError("authenticated retry actor required")
    tasks = [state["tasks"][text.sha(identity)] for identity in ids]
    if any(t["status"] not in {"failed", "retry"} for t in tasks):
        raise ValueError("only failed/retry tasks may be retried")
    if apply:
        publication.assert_writer(store)
        for task in tasks:
            task.setdefault("retries", []).append({"actor": actor, "at": clock().isoformat(),
                "attempts": task["attempts"], "failures": task.get("failures", 0), "error_type": task.get("error_type")})
            task.update(status="pending", attempts=0, failures=0)
            for name in ("retry_at", "retry_day", "error_type", "review_status", "review_error", "merge_error"):
                task.pop(name, None)
        persist(store, state)
    return {"applied": apply, "queue": queue, "retried": ids}


def primary_spec(key, entry):
    """Pin a PDF independently of whether its OCR is complete."""
    source = text.sha(entry.get("source_sha256"))
    size, count = entry.get("source_bytes"), entry.get("page_count")
    if (entry.get("status") != "ready" or type(count) is not int or count < 1
            or type(size) is not int or not 1 <= size <= 512 * 1024 * 1024):
        raise ValueError("PDF entry lacks bounded complete source metadata")
    spec = {"source_key": key, "source_sha256": source, "vector_previews": True}
    djvu = entry.get("source_format") == "djvu"
    if djvu and entry.get("source_kind", "upstream") != "upstream":
        raise ValueError("native DjVu primary must be the pinned original")
    suffix = ".djvu" if djvu else ".pdf"
    if entry.get("source_kind", "upstream") == "upstream":
        origin = {"repo": entry.get("repo"), "path": entry.get("path"), "revision": entry.get("source_revision")}
        if (not isinstance(origin["repo"], str) or not re.fullmatch(r"VoiceOfML/[A-Za-z0-9._-]+", origin["repo"])
                or not isinstance(origin["path"], str) or not origin["path"].lower().endswith(suffix)
                or any(p in {"", ".", ".."} for p in origin["path"].split("/"))
                or "\\" in origin["path"] or any(ord(c) < 32 for c in origin["path"])
                or not isinstance(origin["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", origin["revision"])
                or key != origin["repo"] + "\0" + origin["path"]):
            raise ValueError("original source is not pinned")
        root = f"objects/{source[:2]}/{source}/{text.digest({'primary': 1})[:16]}"
        spec.update(primary_source=origin, primary={"bucket": shared.PDF_PAGES_BUCKET,
                    "path": root + "/document" + suffix, "sha256": source, "bytes": size, "role": "runtime"})
        if djvu:
            # Separate key only for DjVu, so existing PDF task identities are unchanged.
            spec["primary_format"] = "djvu"
    else:
        url = urlsplit(entry.get("source_url", ""))
        match = re.fullmatch(r"/buckets/(vomebook/(?:pdf-pages-v2|reader-assets-v2))/resolve/(.+)", url.path)
        if not entry.get("source_url"):
            bucket, path = entry.get("reader_assets_bucket"), entry.get("reader_assets_path")
        elif url.scheme == "https" and url.netloc == "huggingface.co" and not url.query and not url.fragment and match:
            bucket, path = match[1], unquote(match[2])
        else:
            raise ValueError("derived primary is not a current-bucket resource")
        primary = {"bucket": bucket, "path": path, "sha256": source, "bytes": size, "role": "runtime"}
        publication.v3.resource(primary)
        if bucket not in {shared.PDF_PAGES_BUCKET, shared.READER_ASSETS_BUCKET} or not (publication.PUBLIC_OBJECT.fullmatch(primary["path"]) or re.fullmatch(
                r"derived/[A-Za-z0-9._-]+/[a-z0-9]{32}/document\.pdf", primary["path"]) or re.fullmatch(
                r"documents/pdf/[a-z0-9_-]+/[0-9a-f]{64}/document\.pdf", primary["path"])):
            raise ValueError("derived primary path is not published")
        spec["primary"] = primary
    return spec, count


def spec_for(key, entry):
    """Admit pinned complete OCR without changing existing task identities."""
    spec, count = primary_spec(key, entry)
    source = spec["source_sha256"]
    ocr = {"bucket": shared.PDF_PAGES_BUCKET, "path": entry["ocr_manifest"],
           "sha256": entry["ocr_manifest_sha256"], "bytes": entry["ocr_manifest_bytes"], "role": "provenance"}
    publication.v3.resource(ocr)
    if not re.fullmatch(r"objects/" + source[:2] + "/" + source + r"/[0-9a-f]{16}/ocr-manifest\.json", ocr["path"]):
        raise ValueError("OCR resource/source mismatch")
    spec["ocr_manifest"] = ocr
    if entry.get("source_kind", "upstream") != "upstream":
        spec["require_usable_text"] = True
    return spec, count


def discover(state, registry, rendered=None):
    if registry.get("version") != 1 or not isinstance(registry.get("files"), dict):
        raise ValueError("invalid OCR registry")
    ignored = {}
    active_identities = set()
    for key, entry in sorted(registry["files"].items()):
        if entry.get("status") != "ready":
            continue
        try:
            spec, count = spec_for(key, entry)
        except (ValueError, KeyError, TypeError) as error:
            ignored[key] = type(error).__name__
            continue
        identity = text.digest(spec)
        active_identities.add(identity)
        state["tasks"].setdefault(identity, {"spec": spec, "page_count": count, "status": "pending", "attempts": 0})
    if rendered is not None:
        if rendered.get("version") != 1 or not isinstance(rendered.get("files"), dict):
            raise ValueError("invalid PDF render registry")
        for key, entry in sorted(rendered["files"].items()):
            # A ready text package goes directly through the full builder. Never
            # replace it with an image-only candidate just to show a fast count.
            existing = registry["files"].get(key, {})
            if (existing.get("status") in {"ready", "skipped", "failed"} or existing.get("ocr_manifest")
                    or existing.get("text_layer") or entry.get("status") != "ready"):
                continue
            try:
                spec, count = primary_spec(key, entry)
                spec["preview_only"] = True
            except (ValueError, KeyError, TypeError) as error:
                ignored[key] = type(error).__name__
                continue
            identity = text.digest(spec)
            active_identities.add(identity)
            state["tasks"].setdefault(identity, {"spec": spec, "page_count": count, "status": "pending", "attempts": 0})
    for identity, task in state["tasks"].items():
        existing_ocr = registry["files"].get(task["spec"].get("source_key"), {})
        if (task["spec"].get("preview_only") and task["status"] not in {"published", "protected"}
                and (existing_ocr.get("status") in {"ready", "skipped"}
                     or existing_ocr.get("ocr_manifest") or existing_ocr.get("text_layer"))):
            # OCR may finish while previews are being built. Keep that complete
            # v2 text route live until the full v3 candidate can preserve it.
            task["status"] = "superseded"
            continue
        if identity not in active_identities and task["status"] in {
                "pending", "retry", "dispatch-pending", "needs-review"} and not task.get("candidate"):
            task["status"] = "superseded"
    return ignored


def _text_summary(store, manifest):
    """Usability summary without reading full text unless the layer is raw."""
    layer = manifest.get("text_layer")
    if not layer:
        return {"usable": False, "revision": None, "accepted": False, "quality": None}
    index = publication.v3.decode(publication.v3.verified_read(
        layer, lambda item: store.read_bytes(item["bucket"], item["path"])))
    quality, revision = index.get("quality"), index.get("revision")
    accepted = quality == "partially-reviewed"
    usable = False
    if not accepted and revision != "effective":
        book = publication.v3.decode(publication.v3.verified_read(
            index["book_text"], lambda item: store.read_bytes(item["bucket"], item["path"])))
        text = "".join(page.get("text") or "" for page in book["pages"])
        usable = any(char.isalnum() for char in text)
    return {"usable": usable, "revision": revision, "accepted": accepted, "quality": quality}


def _probe_text(store, task, catalog):
    """Cache whether a staged candidate may repair an unusable live text layer.

    A converted source can keep an OCR layer produced before the render
    environment had CJK fonts/CMaps; such a layer may be one symbol (``●``)
    although the same primary PDF has a complete text layer. The probe is only
    read when the candidate differs from the live generation, so ordinary
    books keep their current cheap path.
    """
    active = catalog["files"].get(task["spec"]["source_key"])
    candidate = task.get("candidate")
    if not active or not candidate or task["spec"].get("preview_only"):
        task.pop("text_probe", None)
        return
    identity = [candidate["path"], candidate["sha256"], active["reading_generation"]]
    probe = task.get("text_probe")
    if probe and probe.get("identity") == identity:
        return
    if task.get("catalog_generation") == active["reading_generation"]:
        # This task's own promotion produced the live entry; no repair applies.
        task["text_probe"] = {"identity": identity, "matches_active": True}
        return
    try:
        candidate_body = publication.read_index(store, candidate)
        generation = candidate_body.get("reading_generation")
        task["candidate_generation"] = generation
        if generation == active["reading_generation"]:
            task["text_probe"] = {"identity": identity, "matches_active": True}
            return
        candidate_manifest = publication.v3.decode(publication.v3.verified_read(
            candidate_body["reading"], lambda item: store.read_bytes(item["bucket"], item["path"])))
    except (FileNotFoundError, ValueError, KeyError):
        task.pop("text_probe", None)
        return
    if (active.get("source_sha256") != task["spec"]["source_sha256"]
            or active.get("components", {}).get("text") != "ready"
            or candidate_body.get("source_sha256") != active["source_sha256"]):
        task.pop("text_probe", None)
        return
    active_manifest = publication.v3.decode(publication.v3.verified_read(
        active["resource"], lambda item: store.read_bytes(item["bucket"], item["path"])))
    active_text = _text_summary(store, active_manifest)
    candidate_text = _text_summary(store, candidate_manifest)
    task["text_probe"] = {
        "identity": identity,
        "matches_active": False,
        "active_usable": active_text["usable"],
        "active_revision": active_text["revision"],
        "active_accepted": active_text["accepted"],
        "candidate_usable": candidate_text["usable"],
        "candidate_revision": candidate_text["revision"],
        "candidate_accepted": candidate_text["accepted"],
        "candidate_generation": candidate_manifest["generation"],
        "same_primary": _same_primary(active_manifest, candidate_manifest),
    }


def _same_primary(left, right):
    """Compare primary content identity, not the staging path chosen for it."""
    left = (left.get("primary") or {}).get("resource") or {}
    right = (right.get("primary") or {}).get("resource") or {}
    return bool(left.get("sha256") and left.get("sha256") == right.get("sha256")
                and left.get("bytes") == right.get("bytes"))


def _can_replace_unusable_text(task, active):
    """Allow one additive repair of a live layer with no usable characters.

    Never replaces accepted/effective corrections, a different source or
    primary, or an image-only candidate.
    """
    probe = task.get("text_probe")
    if not probe or probe.get("matches_active") or not active or task["spec"].get("preview_only"):
        return False
    identity = probe.get("identity") or []
    if len(identity) != 3 or identity[2] != active["reading_generation"]:
        # The live entry moved since this probe; never act on a stale comparison.
        return False
    if (active.get("source_sha256") != task["spec"]["source_sha256"]
            or active.get("components", {}).get("text") != "ready"):
        return False
    return bool(probe["candidate_usable"]
                and not probe["candidate_accepted"]
                and probe["candidate_revision"] != "effective"
                and probe["active_usable"] is False
                and not probe["active_accepted"]
                and probe["active_revision"] != "effective"
                and probe["same_primary"])


DERIVED_MANIFEST = "reader-index/derived_pdf_manifest.json"


def djvu_migration_enabled():
    return os.environ.get("READER_V3_DJVU_MIGRATION", "false").strip().lower() in {"1", "true", "yes"}


def _probe_djvu_migration(store, task, catalog):
    """Cache whether a native DjVu task may replace its live derived-PDF reading.

    Only the same source key, whose live primary is the recorded derived PDF of
    exactly this DjVu original, with raw (never accepted/effective) text.
    """
    spec = task["spec"]
    active = catalog["files"].get(spec["source_key"])
    if (not djvu_migration_enabled() or spec.get("primary_format") != "djvu" or spec.get("preview_only")
            or not active or active["source_sha256"] == spec["source_sha256"]):
        task.pop("djvu_migration", None)
        return
    probe = task.get("djvu_migration")
    if probe and probe.get("active_generation") == active["reading_generation"]:
        return
    try:
        manifest = publication.v3.decode(publication.v3.verified_read(
            active["resource"], lambda item: store.read_bytes(item["bucket"], item["path"])))
        derived = json.loads(store.read_bytes(shared.PDF_PAGES_BUCKET, DERIVED_MANIFEST))
        entries = [item for item in derived.get("files", []) if isinstance(item, dict)
                   and item.get("key") == spec["source_key"] and item.get("source_extension") == "djvu"]
        primary = manifest["primary"]["resource"]
        derived_match = any(item.get("source_sha256") == spec["source_sha256"]
                            and item.get("derived_sha256") == active["source_sha256"] == primary["sha256"]
                            and item.get("derived_bytes") == primary["bytes"] for item in entries)
        summary = _text_summary(store, manifest)
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        task.pop("djvu_migration", None)
        return
    task["djvu_migration"] = {"active_generation": active["reading_generation"],
                              "active_source_sha256": active["source_sha256"],
                              "allowed": bool(derived_match and manifest["primary"]["kind"] == "pdf"
                                              and not summary["accepted"] and summary["revision"] != "effective")}


def _can_migrate_djvu(task, active):
    probe = task.get("djvu_migration")
    return bool(djvu_migration_enabled() and probe and probe.get("allowed") and active
                and probe["active_generation"] == active["reading_generation"]
                and probe["active_source_sha256"] == active["source_sha256"])


def _eligible(state, catalog, budget, now, priority_source=None, *, skip_building=False, store=None):
    eligible = []
    for identity, task in state["tasks"].items():
        spec = task["spec"]
        active = catalog["files"].get(spec["source_key"])
        if store is not None:
            _probe_text(store, task, catalog)
            _probe_djvu_migration(store, task, catalog)
        text_upgrade = (active and active["source_sha256"] == spec["source_sha256"]
                        and active.get("components", {}).get("text") == "pending" and not spec.get("preview_only"))
        replacement = _can_replace_unusable_text(task, active) or _can_migrate_djvu(task, active)
        if active and not text_upgrade and not replacement:
            # A different raw recipe must never replace accepted or already published text.
            task["status"] = "published" if active["source_sha256"] == spec["source_sha256"] else "protected"
            task["active_generation"] = active["reading_generation"]
            continue
        if (text_upgrade or replacement) and task["status"] in {"published", "protected"}:
            # Older admission code treated any canonical entry as full-text
            # completion. The actual pending/unusable component, not that stale
            # label, controls this strictly additive text upgrade.
            task["status"] = "staged" if task.get("candidate") else "pending"
        if (task["status"] == "needs-review" and task.get("error_type") == "PublicationReviewRequired"
                and task.get("review_recipe") != REVIEW_RECIPE):
            # One rebuild under the native-text fallback recipe; a second review is final.
            task.update(status="pending", attempts=0, failures=0, review_recipe=REVIEW_RECIPE)
            task.pop("retry_at", None)
        if task["status"] in {"published", "protected", "failed", "needs-review", "superseded"}:
            continue
        if skip_building and task["status"] == "building" and not task.get("candidate"):
            continue
        if task["status"] == "dispatch-pending" and not task.get("candidate"):
            # A cancelled zero-job dispatch already paid for this attempt. Reuse
            # it even at the daily/attempt ceiling, without another charge.
            eligible.append((identity, task))
            continue
        if not task.get("candidate") and task["attempts"] >= MAX_ATTEMPTS:
            task["status"] = "failed"
            continue
        if task.get("retry_at") and task["retry_at"] > now.isoformat():
            continue
        if task.get("candidate"):
            eligible.append((identity, task))
        elif (task["attempts"] < MAX_ATTEMPTS and budget["attempts"] < DAILY_ATTEMPTS
              and budget["pages"] + task["page_count"] <= DAILY_PAGES):
            eligible.append((identity, task))
    eligible.sort(key=lambda pair: (pair[1]["spec"]["source_sha256"] != priority_source,
                                    not bool(pair[1].get("candidate")),
                                    not (catalog["files"].get(pair[1]["spec"]["source_key"], {}).get("components", {}).get("text") == "pending"
                                         and not pair[1]["spec"].get("preview_only")),
                                    pair[1]["status"] != "dispatch-pending", pair[1]["page_count"], pair[0]))
    return eligible


def build_order(eligible):
    """Share each bounded wave between new page streams and ready text packages."""
    pairs = [pair for pair in eligible if not pair[1].get("candidate")]
    previews = iter(pair for pair in pairs if pair[1]["spec"].get("preview_only"))
    complete = iter(pair for pair in pairs if not pair[1]["spec"].get("preview_only"))
    first, second = (previews, complete) if pairs and pairs[0][1]["spec"].get("preview_only") else (complete, previews)
    while True:
        a, b = next(first, None), next(second, None)
        if a is None and b is None:
            return
        if a is not None:
            yield a
        if b is not None:
            yield b


def _charge(task, budget, now):
    task.update(status="building", attempts=task["attempts"] + 1, started_at=now.isoformat())
    budget["attempts"] += 1
    budget["pages"] += task["page_count"]


def _promote(store, task):
    pointer, latest = publication.current(store)
    active = latest["files"].get(task["spec"]["source_key"])
    text_upgrade = (active
                    and active["source_sha256"] == task["spec"]["source_sha256"]
                    and active.get("components", {}).get("text") == "pending"
                    and not task["spec"].get("preview_only"))
    migration = _can_migrate_djvu(task, active)
    if active and not (text_upgrade or _can_replace_unusable_text(task, active) or migration):
        task["status"] = "protected"
    else:
        promoted = publication.promote(store, task["candidate"], pointer["generation"] if pointer else None, apply=True,
                                       **({"replace_source": active["source_sha256"]} if migration else {}))
        task.update(status="published", catalog_generation=promoted["generation"])
    task.pop("retry_at", None)
    task.pop("error_type", None)


def _fail(task, error_type, review, now):
    task["error_type"] = error_type
    task["failures"] = task.get("failures", 0) + 1
    if review:
        task["review_recipe"] = REVIEW_RECIPE
    task["status"] = ("needs-review" if review else
                      "failed" if task["failures"] >= MAX_ATTEMPTS else "retry")
    task["retry_at"] = (now + timedelta(hours=2 ** min(task["failures"], 5))).isoformat()


def build_path(identity):
    return f"{BUILDS}/{text.sha(identity)}.json"


def github(path):
    """Bounded, repository-scoped read; an API failure must not open admission."""
    if os.environ.get("GITHUB_REPOSITORY") != "anftm/pipeline":
        raise RuntimeError("v3 build queue inspection requires the central repository")
    result = subprocess.run(["gh", "api", "repos/anftm/pipeline/" + path],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("cannot verify v3 build queue; refusing new admission")
    return json.loads(result.stdout)


def build_jobs(run):
    if (run.get("repository", {}).get("full_name", "").lower() != "anftm/pipeline"
            or run.get("path") != ".github/workflows/reader-v3-build.yml"
            or run.get("head_branch") != "main"
            or run.get("event") != "workflow_dispatch"):
        raise ValueError("v3 build run identity mismatch")
    jobs, page = [], 1
    while True:
        data = github(f"actions/runs/{run['id']}/jobs?per_page=100&page={page}")
        jobs.extend(data["jobs"])
        if len(jobs) >= data["total_count"]:
            return jobs
        if not data["jobs"]:
            raise ValueError("incomplete v3 build job listing")
        page += 1


def active_builds():
    """Include queued waves, but not a finished wave's callback-only job."""
    runs = {}
    for status in ("pending", "queued", "in_progress"):
        page = 1
        while True:
            data = github(f"actions/workflows/reader-v3-build.yml/runs?status={status}&per_page=100&page={page}")
            runs.update((run["id"], run) for run in data["workflow_runs"])
            if len(data["workflow_runs"]) < 100:
                break
            page += 1
    active = []
    for run in runs.values():
        jobs = build_jobs(run)
        builders = [job for job in jobs if job["name"].startswith("build (")]
        if jobs and not builders:
            raise ValueError("cannot identify v3 build jobs")
        if not jobs or any(job["status"] != "completed" for job in builders):
            tasks = []
            for job in builders:
                match = re.fullmatch(r"build \(([1-9][0-9]*), ([0-9a-f]{64})\)", job["name"])
                if not match:
                    raise ValueError("cannot identify active v3 build task")
                tasks.append({"identity": match[2], "attempt": int(match[1])})
            active.append({"run_id": run["id"], "tasks": tasks})
    return active


def recover_dispatches(store, options, *, apply=False):
    """Return only verified cancelled/unstarted work to the bounded dispatcher.

    No source, candidate, failure history or charged attempt is discarded. A
    replacement dispatch uses the same attempt rather than calling model OCR.
    """
    if apply:
        publication.assert_writer(store)
    actor = os.environ.get("GITHUB_ACTOR", "").strip()
    runs, requested = options.get("cancelled_run_ids"), options.get("tasks")
    if (not actor or not isinstance(runs, list) or not runs or len(runs) > 256
            or any(type(run) is not int or run < 1 for run in runs) or len(set(runs)) != len(runs)
            or not isinstance(requested, list) or not requested or len(requested) > 256):
        raise ValueError("explicit cancelled dispatches and task attempts required")
    for run_id in runs:
        run = github(f"actions/runs/{run_id}")
        if (run.get("status") != "completed" or run.get("conclusion") != "cancelled"
                or run.get("run_attempt") != 1 or build_jobs(run)):
            raise ValueError("dispatch recovery requires cancelled first-attempt runs with zero jobs")
    active = active_builds()
    if any(not run["tasks"] for run in active):
        raise ValueError("unplanned build run still queued; refusing dispatch recovery")
    protected = {task["identity"] for run in active for task in run["tasks"]}
    state = copy.deepcopy(load_state(store))
    recovered, retained, seen = [], [], set()
    for item in requested:
        identity, attempt = text.sha(item.get("identity")), item.get("attempt")
        if identity in seen or type(attempt) is not int or attempt < 1 or identity in protected:
            raise ValueError("invalid, duplicate or active dispatch recovery task")
        seen.add(identity)
        task = state["tasks"][identity]
        if task.get("attempts") != attempt or task.get("status") != "building" or task.get("candidate"):
            raise ValueError("dispatch recovery task changed since inspection")
        try:
            record = json.loads(store.read_bytes(publication.ASSETS, build_path(identity)))
        except FileNotFoundError:
            record = None
        if record and record.get("attempt") == attempt:
            # The normal collector owns completed results, never reset them.
            retained.append(item)
            continue
        task["status"] = "dispatch-pending"
        recovered.append(item)
    report = {"applied": apply, "queue": "build-dispatch", "cancelled_run_ids": runs,
              "recovered": recovered, "retained_results": retained, "actor": actor, "at": clock().isoformat()}
    if apply:
        state.setdefault("dispatch_recoveries", []).append({k: v for k, v in report.items() if k != "applied"})
        save_state(store, state)
    return report


def recover_execution_timeouts(store, options, *, apply=False):
    """Compensate one false queue-time expiry, backed by the exact build jobs."""
    if apply:
        publication.assert_writer(store)
    runs, requested = options.get("build_run_ids"), options.get("tasks")
    actor = os.environ.get("GITHUB_ACTOR", "").strip()
    if (not actor or not isinstance(runs, list) or not runs or len(runs) > 32
            or any(type(run) is not int or run < 1 for run in runs)
            or not isinstance(requested, list) or not requested or len(requested) > 256):
        raise ValueError("explicit build runs and timed-out task attempts required")
    jobs = {}
    for run_id in runs:
        run = github(f"actions/runs/{run_id}")
        if run.get("conclusion") == "cancelled":
            raise ValueError("cancelled execution is not a live timeout recovery")
        for job in build_jobs(run):
            match = re.fullmatch(r"build \(([1-9][0-9]*), ([0-9a-f]{64})\)", job["name"])
            if match:
                jobs[(match[2], int(match[1]))] = job
    state = copy.deepcopy(load_state(store))
    recovered, seen = [], set()
    for item in requested:
        identity, attempt = text.sha(item.get("identity")), item.get("attempt")
        failures = item.get("failures")
        job = jobs.get((identity, attempt))
        task = state["tasks"][identity]
        if (identity in seen or type(attempt) is not int or type(failures) is not int or failures < 1
                or job is None or task.get("attempts") != attempt or task.get("failures") != failures
                or task.get("status") not in {"failed", "retry"}
                or task.get("error_type") != "BuildTimeout" or task.get("candidate")):
            raise ValueError("timed-out task no longer matches the inspected build execution")
        seen.add(identity)
        try:
            record = json.loads(store.read_bytes(publication.ASSETS, build_path(identity)))
        except FileNotFoundError:
            record = None
        status = ("building" if job["status"] != "completed" or record and record.get("attempt") == attempt
                  else "dispatch-pending")
        recovered.append({**item, "status": status, "job_id": job["id"],
                          "false_timeout": {k: task.get(k) for k in ("error_type", "retry_at", "failures")}})
        task.update(status=status, failures=failures - 1)
        task.pop("retry_at", None)
    report = {"applied": apply, "queue": "build-timeout", "build_run_ids": runs,
              "recovered": recovered, "actor": actor, "at": clock().isoformat()}
    if apply:
        state.setdefault("execution_timeout_recoveries", []).append({k: v for k, v in report.items() if k != "applied"})
        save_state(store, state)
    return report


def wave(store, *, apply=False, priority_source=None, now=None, wave_size=WAVE_SIZE,
         build_runs=None, source_keys=None, promotion_limit=8, preview_first=False):
    """Serialized step: collect parallel build results, promote them, plan the next wave.

    Runs under reader-sidecar. It never builds; ``build_task`` does that in the
    separate central build workflow, which cannot write the catalog or this state.
    """
    if type(wave_size) is not int or not 1 <= wave_size <= 32:
        raise ValueError("v3 build wave size must be 1..32")
    if type(promotion_limit) is not int or not 1 <= promotion_limit <= 8:
        raise ValueError("v3 promotion limit must be 1..8")
    if type(preview_first) is not bool:
        raise ValueError("preview-first admission must be boolean")
    if source_keys is not None and (not isinstance(source_keys, list) or not source_keys or len(source_keys) > 256
                                    or any(not isinstance(k, str) or not k for k in source_keys)):
        raise ValueError("invalid scoped v3 source keys")
    if apply:
        publication.assert_writer(store)
    if build_runs is None:
        # The CLI store can be __main__.CentralHubStore, not the separately
        # imported publication.CentralHubStore. Never use isinstance for this gate.
        build_runs = active_builds() if os.environ.get("GITHUB_ACTIONS") == "true" else []
    state = copy.deepcopy(load_state(store))
    rendered = None
    if preview_first:
        try:
            rendered = json.loads(store.read_bytes(publication.ASSETS, RENDER_REGISTRY))
        except FileNotFoundError:
            pass
    ignored = discover(state, json.loads(store.read_bytes(publication.ASSETS, REGISTRY)), rendered)
    now = now or clock()
    budget = state["days"].setdefault(now.date().isoformat(), {"attempts": 0, "pages": 0})
    collected = []
    for identity, task in state["tasks"].items():
        if task["status"] != "building" or task.get("candidate"):
            continue
        try:
            record = json.loads(store.read_bytes(publication.ASSETS, build_path(identity)))
        except FileNotFoundError:
            record = None
        if record is None or record.get("attempt") != task["attempts"]:
            started = datetime.fromisoformat(task.get("started_at") or "1970-01-01T00:00:00+00:00")
            # Admission time is not execution time. A matrix job may wait hours
            # behind its siblings; never expire or retry it while a builder runs.
            if not build_runs and now - started >= BUILD_TIMEOUT:
                _fail(task, "BuildTimeout", False, now)
                collected.append({"id": identity, "status": task["status"], "error_type": "BuildTimeout"})
            continue
        if record["status"] == "staged":
            task.update(status="staged", candidate=record["candidate"])
        else:
            _fail(task, record.get("error_type") or "BuildFailed", record["status"] == "needs-review", now)
        collected.append({"id": identity, "status": task["status"], "error_type": task.get("error_type")})
    _, catalog = publication.current(store)
    eligible = _eligible(state, catalog, budget, now, priority_source, skip_building=True, store=store)
    if source_keys is not None:
        eligible = [pair for pair in eligible if pair[1]["spec"]["source_key"] in source_keys]
    inflight = [identity for identity, task in state["tasks"].items()
                if task["status"] == "building" and not task.get("candidate")]
    blocked = bool(build_runs or inflight)
    report = {"applied": apply, "tasks": len(state["tasks"]), "ignored": ignored, "eligible": len(eligible),
              "budget": budget, "collected": collected, "processed": [], "wave": [], "changed": False,
              "inflight": len(inflight), "active_build_runs": [run["run_id"] for run in build_runs],
              "admission_blocked": blocked}
    if not apply:
        report["next"] = [] if blocked else [identity for identity, _ in list(build_order(eligible))[:wave_size]]
        return report
    save_state(store, state)
    for identity, task in [pair for pair in eligible if pair[1].get("candidate")][:promotion_limit]:
        try:
            _promote(store, task)
            report["changed"] = report["changed"] or task["status"] == "published"
        except Exception as error:
            _fail(task, type(error).__name__, isinstance(error, publication.PublicationReviewRequired), now)
        save_state(store, state)
        report["processed"].append({"id": identity, "status": task["status"], "error_type": task.get("error_type")})
    staged = sum(bool(task.get("candidate")) and task["status"] not in {"published", "protected", "failed", "needs-review", "superseded"}
                 for task in state["tasks"].values())
    capacity = max(0, wave_size - staged)
    for identity, task in ([] if blocked else build_order(eligible)):
        if len(report["wave"]) >= capacity:
            break
        if task["status"] == "dispatch-pending":
            task.update(status="building", started_at=now.isoformat())
        else:
            if budget["attempts"] >= DAILY_ATTEMPTS or budget["pages"] + task["page_count"] > DAILY_PAGES:
                continue
            _charge(task, budget, now)
        report["wave"].append({"identity": identity, "attempt": task["attempts"]})
    save_state(store, state)  # Charge before dispatch, including builds that never report.
    return report


def build_task(store, identity, attempt, *, build=None):
    """Parallel builder: stage one immutable candidate and record the outcome only."""
    publication.assert_stager(store)
    identity = text.sha(identity)
    task = load_state(store)["tasks"].get(identity)
    if not task or task["status"] != "building" or task["attempts"] != attempt or task.get("candidate"):
        return {"identity": identity, "attempt": attempt, "status": "stale"}
    try:
        prior = json.loads(store.read_bytes(publication.ASSETS, build_path(identity)))
    except FileNotFoundError:
        prior = None
    if prior and prior.get("attempt") == attempt:
        if (prior.get("identity") != identity or prior.get("kind") != "reader-v3-build"
                or prior.get("status") not in {"staged", "failed", "needs-review"}):
            raise ValueError("invalid existing v3 build result")
        return prior
    record = {"version": 1, "kind": "reader-v3-build", "identity": identity, "attempt": attempt,
              "run_id": os.environ.get("GITHUB_RUN_ID", ""), "resources": []}
    try:
        with tempfile.TemporaryDirectory(prefix="reader-v3-build-") as directory:
            spec = dict(task["spec"])
            if not spec.get("preview_only") and build is None:
                _, catalog = publication.current(store)
                active = catalog["files"].get(spec["source_key"])
                if (active and active["source_sha256"] == spec["source_sha256"]
                        and active.get("components", {}).get("text") == "pending"):
                    spec["reuse_reading"] = active["resource"]
            built = (build or publication.build_stage)(store, spec, Path(directory), apply=True)
        record.update(status="staged", candidate=built["candidate"])
        record["metrics"] = {key: built[key] for key in ("build", "timings", "reads") if key in built}
        # Keeps the candidate (and its whole closure) referenced until promotion.
        record["resources"] = [built["candidate"]]
    except Exception as error:
        record.update(status="needs-review" if isinstance(error, publication.PublicationReviewRequired) else "failed",
                      error_type=type(error).__name__, error=str(error)[:500])
    raw = publication.encode(record)
    store.put_bytes(publication.ASSETS, build_path(identity), raw)
    if store.read_bytes(publication.ASSETS, build_path(identity)) != raw:
        raise ValueError("build record readback mismatch")
    return record


def run(store, *, apply=False, priority_source=None, build=None, now=None, max_books=4,
        max_seconds=3600):
    if type(max_books) is not int or not 1 <= max_books <= 100:
        raise ValueError("v3 batch book limit must be 1..100")
    started = time.monotonic()
    reports = []
    for _ in range(max_books if apply else 1):
        if reports and time.monotonic() - started >= max_seconds:
            break
        report = _run_one(store, apply=apply, priority_source=priority_source, build=build, now=now)
        reports.append(report)
        if not apply or not report["processed"]:
            break
    result = reports[-1]
    result["processed"] = [item for report in reports for item in report["processed"]]
    result["elapsed_seconds"] = time.monotonic() - started
    return result


def _run_one(store, *, apply=False, priority_source=None, build=None, now=None):
    if apply:
        publication.assert_writer(store)
    state = copy.deepcopy(load_state(store))
    ignored = discover(state, json.loads(store.read_bytes(publication.ASSETS, REGISTRY)))
    now = now or clock()
    day = now.date().isoformat()
    budget = state["days"].setdefault(day, {"attempts": 0, "pages": 0})
    _, catalog = publication.current(store)
    eligible = _eligible(state, catalog, budget, now, priority_source, store=store)
    report = {"applied": apply, "tasks": len(state["tasks"]), "ignored": ignored,
              "eligible": len(eligible), "budget": budget, "processed": []}
    if not apply:
        report["next"] = eligible[0][0] if eligible else None
        return report
    save_state(store, state)
    if not eligible:
        return report
    identity, task = eligible[0]
    try:
        if not task.get("candidate"):
            _charge(task, budget, now)
            save_state(store, state)  # Charge before work, including interrupted attempts.
            with tempfile.TemporaryDirectory(prefix="reader-v3-auto-") as directory:
                built = (build or publication.build_stage)(store, task["spec"], Path(directory), apply=True)
            task.update(status="staged", candidate=built["candidate"])
            save_state(store, state)
        _promote(store, task)
    except Exception as error:
        _fail(task, type(error).__name__, isinstance(error, publication.PublicationReviewRequired), now)
    save_state(store, state)
    report["processed"].append({"id": identity, "status": task["status"], "error_type": task.get("error_type")})
    return report
