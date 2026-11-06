import hmac
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import IO, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import runtime
from .bucket_snapshot import PublicationIncomplete, publish as publish_bucket_snapshot, publish_delay as bucket_snapshot_publish_delay, publish_due as bucket_snapshot_publish_due, publish_marker_path, read_manifest as read_bucket_manifest, snapshot_status as bucket_snapshot_status, write_convergence_plan, write_snapshot_status
from .config import ARCHIVE_END, ARCHIVE_START, DATA_ROOT, INDEX_NAME
from .indexer import acquire_index_lock, ensure_index


router = APIRouter()
snapshot_publish_timer: threading.Timer | None = None
snapshot_publish_timer_lock = threading.Lock()
snapshot_publish_worker_lock = threading.Lock()
REINDEX_COOLDOWN_SECONDS = 10 * 60
REINDEX_LAST_START = DATA_ROOT / "reindex-last-start"
REINDEX_JOB_PATH = DATA_ROOT / "reindex-job.json"
REINDEX_TOKEN = os.environ.get("BHA_REINDEX_TOKEN", "")
BUCKET_DIR = Path(os.environ["BHA_BUCKET_DIR"]) if os.environ.get("BHA_BUCKET_DIR") else None
SNAPSHOT_MIN_INTERVAL_SECONDS = int(os.environ.get("BHA_SNAPSHOT_MIN_INTERVAL_SECONDS", "1800"))


class ReindexRequest(BaseModel):
    archive_commits: dict[str, str]
    archive_revisions: dict[str, str]
    changed_archives: list[int] = Field(default_factory=list[int], max_length=32)


def admin_auth_error(request: Request) -> JSONResponse | None:
    if not REINDEX_TOKEN:
        return JSONResponse(
            {"error": "administrative API is not configured"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    scheme, _, supplied_token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not supplied_token or not hmac.compare_digest(supplied_token, REINDEX_TOKEN):
        return JSONResponse(
            {"error": "invalid administrative credentials"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
    return None



def write_reindex_job(state: str, job_id: str, **values: Any) -> None:
    previous = read_reindex_job() or {}
    payload = {
        **(previous if previous.get("job_id") == job_id else {}),
        "job_id": job_id, "state": state, "updated_at": int(time.time()), **values,
    }
    REINDEX_JOB_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = REINDEX_JOB_PATH.with_name(f".{REINDEX_JOB_PATH.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, REINDEX_JOB_PATH)

def read_reindex_job() -> dict[str, Any] | None:
    try:
        value = json.loads(REINDEX_JOB_PATH.read_text(encoding="utf-8"))
        return cast(dict[str, Any], value) if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None

def update_reindex_job(state: str, job_id: str, **values: Any) -> bool:
    if not job_id:
        return False
    try:
        # Reject a missing or already-replaced job observed at this point.
        # This read followed by a write is not a cross-writer atomic CAS.
        current = read_reindex_job()
        if not current or current.get("job_id") != job_id:
            print("reindex_job_status_failed=job_identity_changed", flush=True)
            return False
        write_reindex_job(state, job_id, **values)
        return True
    except OSError as exc:
        print(f"reindex_job_status_failed={type(exc).__name__}", flush=True)
        return False



def publish_bucket_snapshot_worker() -> None:
    global snapshot_publish_timer
    # Serialize the scheduler itself as well as publication. If a new index
    # finishes while a snapshot is being copied, the later worker must recheck
    # the latest manifest and schedule another publication rather than drop it.
    with snapshot_publish_worker_lock:
        with snapshot_publish_timer_lock:
            snapshot_publish_timer = None
        _publish_bucket_snapshot_worker_locked()


def _publish_bucket_snapshot_worker_locked() -> None:
    try:
        if BUCKET_DIR is None:
            return
        if publish_marker_path().exists():
            print("bucket_snapshot_publish_stopped=capacity_review_required", flush=True)
            return
        previous_status = bucket_snapshot_status() or {}
        if previous_status.get("state") in {"publishing", "publish_failed"}:
            print("bucket_snapshot_publish_stopped=capacity_review_required", flush=True)
            return
        if bucket_snapshot_publish_due(BUCKET_DIR, SNAPSHOT_MIN_INTERVAL_SECONDS):
            # publish() writes an independent marker under the filesystem lock
            # before copying. Its survival across Space restarts is unknown.
            publish_bucket_snapshot(BUCKET_DIR)
            return
        delay = bucket_snapshot_publish_delay(BUCKET_DIR, SNAPSHOT_MIN_INTERVAL_SECONDS)
        write_snapshot_status("pending_publish", retry_at=int(time.time()) + delay)
        print(f"bucket_snapshot_publish_scheduled=delay_{delay}", flush=True)
        schedule_bucket_snapshot_publish(delay)
    except PublicationIncomplete:
        print("bucket_snapshot_publish_stopped=capacity_review_required", flush=True)
    except Exception as exc:
        if BUCKET_DIR is not None:
            if publish_marker_path().exists():
                print("bucket_snapshot_publish_stopped=capacity_review_required", flush=True)
                return
            if (bucket_snapshot_status() or {}).get("state") == "publish_failed":
                print("bucket_snapshot_publish_stopped=capacity_review_required", flush=True)
                return
            try:
                write_snapshot_status(
                    "publish_failed", stage="scheduler", reason=type(exc).__name__,
                    detail=str(exc)[:300],
                )
            except Exception:
                pass
        print(f"bucket_snapshot_publish_failed={exc}")

def schedule_bucket_snapshot_publish(delay: int) -> None:
    global snapshot_publish_timer
    with snapshot_publish_timer_lock:
        if snapshot_publish_timer is not None and snapshot_publish_timer.is_alive():
            return
        snapshot_publish_timer = threading.Timer(max(1, delay), publish_bucket_snapshot_worker)
        snapshot_publish_timer.daemon = True
        snapshot_publish_timer.start()

def resume_pending_snapshot_publish() -> None:
    if BUCKET_DIR is None:
        return
    if publish_marker_path().exists():
        print("bucket_snapshot_publish_stopped=capacity_review_required", flush=True)
        return
    status = bucket_snapshot_status() or {}
    if status.get("state") != "pending_publish":
        return
    try:
        delay = max(0, int(status.get("retry_at", 0)) - int(time.time()))
    except (TypeError, ValueError):
        delay = 0
    schedule_bucket_snapshot_publish(delay)

def run_reindex(
    lock_file: IO[str],
    reset: bool = False,
    expected_commits: dict[int, str] | None = None,
    expected_revisions: dict[int, str] | None = None,
    changed_archives: list[int] | None = None,
    job_id: str = "",
) -> None:
    # Refuse an orphaned worker without changing serving status or touching a
    # newer job's record. The local lock is not a cross-host writer fence.
    current_job = read_reindex_job()
    if not job_id or not current_job or current_job.get("job_id") != job_id:
        print("reindex_failed=job_identity_changed", flush=True)
        lock_file.close()
        return
    try:
        if not update_reindex_job("running", job_id, started_at=int(time.time())):
            raise RuntimeError("cannot record running reindex job")
        runtime.INDEX_STATUS_PATH.write_text("rebuilding", encoding="utf-8")
        runtime.clear_response_caches()
        if expected_commits is None and expected_revisions is None:
            changed = ensure_index(reset=reset, lock_file=lock_file)
        else:
            if expected_commits is None or expected_revisions is None:
                raise ValueError("commits and revisions must be supplied together")
            write_convergence_plan(
                {str(key): value for key, value in expected_commits.items()},
                expected_revisions,
                changed_archives or [],
            )
            changed = ensure_index(
                reset=reset, lock_file=lock_file,
                expected_commits=expected_commits, expected_revisions=expected_revisions,
                changed_archives=changed_archives,
            )
        runtime.INDEX_STATUS_PATH.write_text("ready", encoding="utf-8")
        update_reindex_job(
            "succeeded", job_id, changed=bool(changed), index_status="ready", finished_at=int(time.time()),
        )
        if changed and BUCKET_DIR is not None:
            threading.Thread(target=publish_bucket_snapshot_worker, daemon=True).start()
    except Exception as exc:
        safe_ready = not runtime.INDEX_SWITCH_STATE_PATH.exists() and runtime.serving_generation_valid()
        runtime.INDEX_STATUS_PATH.write_text("ready" if safe_ready else "failed", encoding="utf-8")
        update_reindex_job(
            "failed", job_id, error=f"{type(exc).__name__}: {str(exc)[:500]}",
            index_status="ready" if safe_ready else "failed", finished_at=int(time.time()),
        )
        print(f"reindex_failed={exc}")
    finally:
        runtime.clear_response_caches()
        lock_file.close()

def start_reindex_worker(
    lock_file: IO[str],
    reset: bool = False,
    expected_commits: dict[int, str] | None = None,
    expected_revisions: dict[int, str] | None = None,
    changed_archives: list[int] | None = None,
    job_id: str = "",
) -> None:
    threading.Thread(
        target=run_reindex,
        args=(lock_file, reset, expected_commits, expected_revisions, changed_archives, job_id),
        daemon=True,
    ).start()

@router.post("/api/reindex", status_code=202)

def reindex(request: Request, full: bool = False, payload: ReindexRequest | None = None):
    auth_error = admin_auth_error(request)
    if auth_error is not None:
        return auth_error
    lock_file = acquire_index_lock(blocking=False)
    if lock_file is None:
        return JSONResponse({"error": "indexing already in progress"}, status_code=409)
    now = int(time.time())
    expected_commits = None
    expected_revisions = None
    if payload is not None:
        expected_keys = {str(value) for value in range(ARCHIVE_START, ARCHIVE_END + 1)}
        if set(payload.archive_commits) != expected_keys or set(payload.archive_revisions) != expected_keys:
            lock_file.close()
            return JSONResponse({"error": "archive identity must cover archives 0 through 31"}, status_code=400)
        if any(not re.fullmatch(r"[0-9a-f]{40}", value) for value in (*payload.archive_commits.values(), *payload.archive_revisions.values())):
            lock_file.close()
            return JSONResponse({"error": "invalid archive identity"}, status_code=400)
        if any(value < ARCHIVE_START or value > ARCHIVE_END for value in payload.changed_archives):
            lock_file.close()
            return JSONResponse({"error": "invalid changed archive"}, status_code=400)
        expected_commits = {int(key): value for key, value in payload.archive_commits.items()}
        expected_revisions = {int(key): value for key, value in payload.archive_revisions.items()}
        changed_archives = sorted(set(payload.changed_archives))
    else:
        changed_archives = None
    try:
        last_start = int(REINDEX_LAST_START.read_text(encoding="utf-8").strip())
    except Exception:
        last_start = 0
    retry_after = REINDEX_COOLDOWN_SECONDS - (now - last_start)
    if retry_after > 0:
        lock_file.close()
        return JSONResponse(
            {"error": "reindex cooldown active", "retry_after": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    try:
        REINDEX_LAST_START.parent.mkdir(parents=True, exist_ok=True)
        REINDEX_LAST_START.write_text(str(now), encoding="utf-8")
        job_id = uuid.uuid4().hex
        write_reindex_job(
            "accepted", job_id, mode="full" if full else "incremental",
            changed_archives=changed_archives or [],
            target_archive_commits=expected_commits or {},
            target_archive_revisions=expected_revisions or {},
            accepted_at=now,
        )
        if payload is None:
            start_reindex_worker(lock_file, reset=full, job_id=job_id)
        else:
            start_reindex_worker(
                lock_file, reset=full,
                expected_commits=expected_commits, expected_revisions=expected_revisions,
                changed_archives=changed_archives, job_id=job_id,
            )
    except Exception:
        lock_file.close()
        REINDEX_LAST_START.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "status": "accepted",
        "mode": "full" if full else "incremental",
        "job_id": job_id,
        "cooldown_seconds": REINDEX_COOLDOWN_SECONDS,
    }

@router.get("/api/reindex/status")
def reindex_status(request: Request):
    auth_error = admin_auth_error(request)
    if auth_error is not None:
        return auth_error
    metadata: dict[str, Any] = {}
    try:
        metadata = runtime.es.get(index=INDEX_NAME, id="__meta__").get("_source", {})
    except Exception:
        pass
    snapshot_manifest = None
    if BUCKET_DIR is not None:
        snapshot_manifest = read_bucket_manifest(BUCKET_DIR)
    return JSONResponse({
        "index_status": runtime.index_status(),
        "progress": runtime.index_progress(),
        "job": read_reindex_job(),
        "switch_pending": runtime.INDEX_SWITCH_STATE_PATH.exists(),
        "archive_revisions": metadata.get("archive_revisions") or {},
        "build_fingerprint": metadata.get("build_fingerprint"),
        # Publication happens asynchronously after index success. Expose only
        # validated manifest metadata, never infer snapshot success from the
        # index job's state alone.
        "bucket_snapshot_configured": BUCKET_DIR is not None,
        "bucket_snapshot_marker_present": publish_marker_path().exists(),
        "bucket_snapshot": bucket_snapshot_status(),
        "snapshot_manifest": ({
            "snapshot": snapshot_manifest.get("snapshot"),
            "generation": snapshot_manifest.get("generation"),
            "archive_revisions": snapshot_manifest.get("index_metadata", {}).get("archive_revisions"),
            "build_fingerprint": snapshot_manifest.get("index_metadata", {}).get("build_fingerprint"),
        } if snapshot_manifest is not None else None),
    }, headers={"Cache-Control": "no-store"})
