"""Publish and restore a coherent BHA Elasticsearch and SQLite snapshot."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, TypedDict, cast

from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError

from .config import ARCHIVE_END, ARCHIVE_START, DATA_ROOT, ES_URL, INDEX_NAME
from .data_loader import (
    parsed_archive_commits,
    parsed_archive_revisions,
    parsed_archive_tree_revisions,
    parsed_corpus_fingerprint,
    object_dict,
)
from .doc_store import DOC_DB
from .facet_store import FACET_DB
from .indexer import build_fingerprint, desired_metadata, sidecar_metadata_matches
from .storage_lock import serving_lock, snapshot_publish_lock

MANIFEST_NAME = "current.json"
CONVERGENCE_PLAN_NAME = "bucket-snapshot-convergence.json"
PUBLISH_MARKER_NAME = ".bucket-snapshot-publication-incomplete"
REPOSITORY = "bha_bucket"
GENERATIONS_ROOT = "generations"
REPOSITORY_DIRNAME = "elasticsearch"
SIDECARS = {"docs": DOC_DB, "facets": FACET_DB}
SNAPSHOT_NAME_RE = re.compile(r"^bha-[a-z0-9-]+$")
RESTORE_RECOVERY_MAX_BYTES_PER_SEC = os.environ.get(
    "BHA_RESTORE_RECOVERY_MAX_BYTES_PER_SEC", "160mb"
).strip().lower()


def snapshot_status_path() -> Path:
    return DATA_ROOT / "bucket-snapshot-status.json"


class PublicationIncomplete(RuntimeError):
    """An earlier attempt requires inspection before another publication."""


def publish_marker_path() -> Path:
    # Independent of snapshot_status: startup restore updates that status.
    # This remains local DATA_ROOT state, not a cross-host or durable fence.
    return DATA_ROOT / PUBLISH_MARKER_NAME


def create_publish_marker() -> Path:
    """Fail closed until the current local publication finishes normally."""
    marker = publish_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as target:
            target.write("publication incomplete; operator review required\n")
            target.flush()
            os.fsync(target.fileno())
    except FileExistsError as exc:
        raise PublicationIncomplete("previous snapshot publication requires review") from exc
    return marker

def convergence_plan_path() -> Path:
    return DATA_ROOT / CONVERGENCE_PLAN_NAME


def write_snapshot_status(state: str, **values: Any) -> None:
    payload = {"state": state, "updated_at": int(time.time()), **values}
    path = snapshot_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def snapshot_status() -> dict[str, Any] | None:
    try:
        value = json.loads(snapshot_status_path().read_text(encoding="utf-8"))
        return cast(dict[str, Any], value) if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None

def publish_due(bucket_dir: Path, minimum_interval_seconds: int) -> bool:
    return publish_delay(bucket_dir, minimum_interval_seconds) <= 0

def publish_delay(bucket_dir: Path, minimum_interval_seconds: int) -> int:
    if minimum_interval_seconds <= 0:
        return 0
    try:
        manifest = json.loads(manifest_path(bucket_dir).read_text(encoding="utf-8"))
        created_at = int(manifest.get("created_at", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0
    return max(0, int(minimum_interval_seconds - (time.time() - created_at)))


def local_repository() -> Path:
    return DATA_ROOT / "es-repositories" / "production"


def clear_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

def link_or_copy(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    copied = 0
    with source.open("rb") as source_file, destination.open("wb") as destination_file:
        for block in iter(lambda: source_file.read(4 * 1024 * 1024), b""):
            destination_file.write(block)
            digest.update(block)
            copied += len(block)
    return copied, digest.hexdigest()


class SidecarFileMetadata(TypedDict):
    filename: str
    bytes: int
    sha256: str


def file_metadata(path: Path) -> SidecarFileMetadata:
    return {"filename": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}


def repository(client: Elasticsearch) -> None:
    client.snapshot.create_repository(
        name=REPOSITORY,
        repository={"type": "fs", "settings": {"location": str(local_repository())}},
        verify=True,
    )


def manifest_path(bucket_dir: Path) -> Path:
    return bucket_dir / MANIFEST_NAME


def read_manifest(bucket_dir: Path) -> dict[str, Any] | None:
    try:
        raw_data: object = json.loads(manifest_path(bucket_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data = object_dict(raw_data)
    if data is None or data.get("schema") != 3:
        return None
    index_metadata = object_dict(data.get("index_metadata"))
    if index_metadata is None or index_metadata.get("build_fingerprint") != build_fingerprint():
        return None
    snapshot = data.get("snapshot")
    generation = data.get("generation")
    physical_index = data.get("physical_index")
    source_fingerprint = index_metadata.get("source_fingerprint")
    raw_revisions = object_dict(index_metadata.get("archive_revisions"))
    raw_commits = object_dict(data.get("archive_commits"))
    sidecars = object_dict(data.get("sidecars"))
    if not isinstance(snapshot, str) or not SNAPSHOT_NAME_RE.fullmatch(snapshot):
        return None
    if not isinstance(generation, str) or not SNAPSHOT_NAME_RE.fullmatch(generation):
        return None
    if not isinstance(physical_index, str) or not physical_index.startswith(f"{INDEX_NAME}-"):
        return None
    if not isinstance(source_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint):
        return None
    if raw_revisions is None:
        return None
    try:
        revisions = {int(key): str(value) for key, value in raw_revisions.items()}
    except (TypeError, ValueError):
        return None
    if set(revisions) != set(range(ARCHIVE_START, ARCHIVE_END + 1)):
        return None
    if any(not re.fullmatch(r"[0-9a-f]{40}", revision) for revision in revisions.values()):
        return None
    if raw_commits is None or set(raw_commits) != {str(value) for value in range(ARCHIVE_START, ARCHIVE_END + 1)}:
        return None
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) for value in raw_commits.values()):
        return None
    if index_metadata != desired_metadata(source_fingerprint, revisions):
        return None
    if sidecars is None or set(sidecars) != set(SIDECARS):
        return None
    for key, target in SIDECARS.items():
        value = object_dict(sidecars.get(key))
        if value is None or value.get("filename") != target.name:
            return None
    return data

def refresh_manifest_commits(bucket_dir: Path, manifest: dict[str, Any], commits: dict[str, str]) -> None:
    """Record equivalent remote commit identities without changing the snapshot generation."""
    payload = dict(manifest)
    payload["archive_commits"] = dict(sorted(commits.items()))
    temporary = bucket_dir / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, manifest_path(bucket_dir))
    finally:
        temporary.unlink(missing_ok=True)

def write_convergence_plan(
    commits: dict[str, str], revisions: dict[int, str], changed_archives: list[int],
) -> None:
    path = convergence_plan_path()
    payload = {
        "archive_commits": dict(sorted(commits.items())),
        "archive_revisions": {str(key): value for key, value in sorted(revisions.items())},
        "changed_archives": sorted(changed_archives),
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def read_convergence_plan() -> dict[str, Any] | None:
    try:
        value = json.loads(convergence_plan_path().read_text(encoding="utf-8"))
        return cast(dict[str, Any], value) if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def physical_index(client: Elasticsearch) -> str:
    aliases = client.indices.get_alias(name=INDEX_NAME)
    names = sorted(aliases)
    if len(names) != 1 or not names[0].startswith(f"{INDEX_NAME}-"):
        raise RuntimeError("expected one physical index behind the article alias")
    return names[0]


def backup_sqlite(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as destination_conn:
        source_conn.backup(destination_conn)


def copy_validated(source: Path, destination: Path, metadata: dict[str, Any], attempts: int = 3) -> None:
    last_error = "validation failed"
    for attempt in range(1, attempts + 1):
        destination.unlink(missing_ok=True)
        try:
            if not source.is_file():
                raise RuntimeError("source file is missing")
            copied_bytes, copied_sha256 = copy_and_hash(source, destination)
            if copied_bytes != int(metadata["bytes"]):
                raise RuntimeError("size mismatch")
            if copied_sha256 != str(metadata["sha256"]):
                raise RuntimeError("checksum mismatch")
            return
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(attempt * 2)
    raise RuntimeError(f"sidecar copy failed after {attempts} attempts ({last_error})")


def publish(bucket_dir: Path) -> None:
    # Readers can continue while publication snapshots one stable generation;
    # generation activation takes the exclusive lock and waits for this copy.
    with snapshot_publish_lock():
        with serving_lock():
            # Never let a rejected attempt overwrite another publisher's
            # status or remove a marker belonging to a previous attempt.
            marker = create_publish_marker()
            try:
                _publish(bucket_dir)
            except Exception as exc:
                # Keep the serving lock through local repository cleanup so
                # generation activation cannot race this cleanup. This local
                # lock does not fence every restore or external writer.
                cleanup_failed_publication(bucket_dir)
                detail = str(exc).replace("\n", " ")[:500]
                write_snapshot_status("publish_failed", stage="publish", reason=type(exc).__name__, detail=detail)
                raise
            marker.unlink()

def cleanup_failed_publication(bucket_dir: Path) -> None:
    # The bucket may contain prior or concurrently published generations.
    # Preserve them, including temporary manifests, until a separate reviewed
    # retention procedure can establish ownership and safe capacity limits.
    try:
        clear_directory(local_repository())
    except OSError:
        pass

def _publish(bucket_dir: Path) -> None:
    if not bucket_dir.is_dir():
        raise RuntimeError(f"bucket directory is unavailable: {bucket_dir}")
    if not all(path.is_file() for path in SIDECARS.values()):
        raise RuntimeError("cannot snapshot missing SQLite sidecars")
    client = Elasticsearch(ES_URL, request_timeout=3600)
    metadata = client.get(index=INDEX_NAME, id="__meta__").get("_source", {})
    plan = read_convergence_plan()
    try:
        revisions = parsed_archive_revisions()
        commits = parsed_archive_commits()
    except (OSError, RuntimeError):
        if not plan or not isinstance(plan.get("archive_commits"), dict) or not isinstance(plan.get("archive_revisions"), dict):
            raise
        commits = {str(key): str(value) for key, value in plan["archive_commits"].items()}
        revisions = {int(key): str(value) for key, value in plan["archive_revisions"].items()}
    source_fingerprint = parsed_corpus_fingerprint(revisions=revisions)
    if metadata != desired_metadata(source_fingerprint, revisions):
        raise RuntimeError("cannot snapshot an index with mismatched build or source metadata")
    if not all(sidecar_metadata_matches(path, metadata) for path in SIDECARS.values()):
        raise RuntimeError("cannot snapshot mismatched SQLite sidecars")
    physical_name = physical_index(client)
    snapshot = f"bha-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    generation = snapshot
    write_snapshot_status("publishing", stage="snapshot_elasticsearch", snapshot=snapshot)
    try:
        client.snapshot.delete_repository(name=REPOSITORY)
    except NotFoundError:
        pass
    clear_directory(local_repository())
    repository(client)
    started = time.monotonic()
    client.snapshot.create(
        repository=REPOSITORY,
        snapshot=snapshot,
        indices=physical_name,
        include_global_state=False,
        wait_for_completion=True,
    )
    write_snapshot_status("publishing", stage="copy_generation", snapshot=snapshot)
    generation_dir = bucket_dir / GENERATIONS_ROOT / generation
    generation_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(local_repository(), generation_dir / REPOSITORY_DIRNAME, copy_function=link_or_copy)
    sidecars: dict[str, SidecarFileMetadata] = {}
    with tempfile.TemporaryDirectory(dir=DATA_ROOT, prefix="bha-snapshot-") as temporary_dir:
        for key, source in SIDECARS.items():
            local_copy = Path(temporary_dir) / source.name
            backup_sqlite(source, local_copy)
            destination = generation_dir / source.name
            shutil.copyfile(local_copy, destination)
            sidecars[key] = file_metadata(destination)
    payload = {
        "schema": 3,
        "index_name": INDEX_NAME,
        "index_metadata": metadata,
        "archive_commits": {str(key): value for key, value in sorted(commits.items())},
        "physical_index": physical_name,
        "snapshot": snapshot,
        "generation": generation,
        "document_count": int(client.count(index=INDEX_NAME, query={"exists": {"field": "doc_id"}})["count"]),
        "sidecars": sidecars,
        "created_at": int(time.time()),
    }
    temporary_manifest = bucket_dir / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    temporary_manifest.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary_manifest, manifest_path(bucket_dir))
    convergence_plan_path().unlink(missing_ok=True)
    write_snapshot_status("published", stage="complete", snapshot=snapshot)
    # Keep prior generations and legacy snapshots; this candidate does not
    # assume a process-local lock fences every bucket writer.
    try:
        clear_directory(local_repository())
    except OSError as exc:
        print(f"bha_bucket_snapshot=local_repository_cleanup_failed error={type(exc).__name__}", flush=True)
    print(f"bha_bucket_snapshot=published snapshot={snapshot} elapsed_seconds={time.monotonic() - started:.2f}", flush=True)


def prepare_restored_sidecars(
    bucket_dir: Path, manifest: dict[str, Any],
) -> list[tuple[Path, Path]]:
    generation_dir = bucket_dir / GENERATIONS_ROOT / str(manifest["generation"])
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_paths: list[tuple[Path, Path]] = []
    try:
        for key, target in SIDECARS.items():
            metadata = manifest["sidecars"][key]
            source = generation_dir / str(metadata["filename"])
            temporary = DATA_ROOT / f".{target.name}.{uuid.uuid4().hex}.tmp"
            temporary_paths.append((temporary, target))
            copy_validated(source, temporary, metadata)
        return temporary_paths
    except Exception:
        discard_prepared_sidecars(temporary_paths)
        raise


def discard_prepared_sidecars(temporary_paths: list[tuple[Path, Path]]) -> None:
    for temporary, _ in temporary_paths:
        temporary.unlink(missing_ok=True)


def activate_restored_sidecars(temporary_paths: list[tuple[Path, Path]]) -> None:
    replaced_targets: list[Path] = []
    try:
        with serving_lock(exclusive=True):
            backups: list[tuple[Path, Path]] = []
            try:
                for _, target in temporary_paths:
                    if target.exists():
                        with sqlite3.connect(target) as connection:
                            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            connection.execute("PRAGMA journal_mode=DELETE")
                        backup = DATA_ROOT / f".{target.name}.{uuid.uuid4().hex}.previous"
                        os.replace(target, backup)
                        backups.append((backup, target))
                    Path(f"{target}-wal").unlink(missing_ok=True)
                    Path(f"{target}-shm").unlink(missing_ok=True)
                for temporary, target in temporary_paths:
                    os.replace(temporary, target)
                    replaced_targets.append(target)
            except Exception:
                for target in replaced_targets:
                    target.unlink(missing_ok=True)
                for backup, target in backups:
                    os.replace(backup, target)
                raise
            for backup, _ in backups:
                backup.unlink(missing_ok=True)
    finally:
        discard_prepared_sidecars(temporary_paths)


def restore_sidecars(bucket_dir: Path, manifest: dict[str, Any]) -> None:
    activate_restored_sidecars(prepare_restored_sidecars(bucket_dir, manifest))


def configure_restore_recovery(client: Elasticsearch) -> bool:
    value = RESTORE_RECOVERY_MAX_BYTES_PER_SEC
    if not re.fullmatch(r"[1-9][0-9]*(?:kb|mb|gb)", value):
        print(f"bha_bucket_snapshot=recovery_limit_invalid value={value}", flush=True)
        return False
    try:
        client.cluster.put_settings(
            transient={"indices.recovery.max_bytes_per_sec": value}
        )
        return True
    except Exception as exc:
        print(f"bha_bucket_snapshot=recovery_limit_unavailable reason={type(exc).__name__}", flush=True)
        return False


def clear_restore_recovery(client: Elasticsearch, applied: bool) -> None:
    if not applied:
        return
    try:
        client.cluster.put_settings(
            transient={"indices.recovery.max_bytes_per_sec": None}
        )
    except Exception as exc:
        print(f"bha_bucket_snapshot=recovery_limit_cleanup_failed reason={type(exc).__name__}", flush=True)


def validate_restored_sidecars(manifest: dict[str, Any]) -> None:
    for key, target in SIDECARS.items():
        metadata = manifest["sidecars"][key]
        if not target.is_file():
            raise RuntimeError(f"restored {key} sidecar is missing")
        actual_bytes = target.stat().st_size
        expected_bytes = int(metadata["bytes"])
        if actual_bytes != expected_bytes:
            raise RuntimeError(f"restored {key} sidecar size mismatch: expected {expected_bytes}, got {actual_bytes}")
    with sqlite3.connect(DOC_DB) as conn:
        docs = int(conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
    with sqlite3.connect(FACET_DB) as conn:
        facets = int(conn.execute("SELECT COUNT(*) FROM facets").fetchone()[0])
    expected_docs = int(manifest["document_count"])
    if docs != expected_docs:
        raise RuntimeError(f"restored docs count mismatch: expected {expected_docs}, got {docs}")
    if facets <= 0:
        raise RuntimeError(f"restored facets count is invalid: {facets}")
    metadata = manifest["index_metadata"]
    if not all(sidecar_metadata_matches(path, metadata) for path in SIDECARS.values()):
        raise RuntimeError("restored SQLite sidecars have mismatched generation metadata")


def restore(bucket_dir: Path, allow_stale: bool = True) -> bool:
    convergence_plan_path().unlink(missing_ok=True)
    manifest = read_manifest(bucket_dir)
    if manifest is None:
        write_snapshot_status("unavailable", stage="read_manifest", reason="missing_or_invalid_manifest")
        print("bha_bucket_snapshot=unavailable reason=missing_or_invalid_manifest", flush=True)
        return False
    source_stale = False
    convergence_plan: tuple[dict[str, str], dict[int, str], list[int]] | None = None
    try:
        remote_commits = {str(key): value for key, value in parsed_archive_commits(remote=True).items()}
        if manifest["archive_commits"] != remote_commits:
            changed_ids = [
                int(key) for key, value in remote_commits.items()
                if manifest["archive_commits"].get(key) != value
            ]
            remote_trees = parsed_archive_tree_revisions(
                {archive_id: remote_commits[str(archive_id)] for archive_id in changed_ids},
                changed_ids,
            )
            stored_trees = {
                int(key): value
                for key, value in manifest["index_metadata"]["archive_revisions"].items()
            }
            if all(stored_trees.get(archive_id) == tree for archive_id, tree in remote_trees.items()):
                refresh_manifest_commits(bucket_dir, manifest, remote_commits)
            else:
                target_trees = {**stored_trees, **remote_trees}
                changed_archives = [
                    archive_id for archive_id, tree in target_trees.items()
                    if stored_trees.get(archive_id) != tree
                ]
                if not allow_stale:
                    write_snapshot_status("unavailable", stage="validate_source", reason="source_revision_mismatch")
                    print("bha_bucket_snapshot=unavailable reason=source_revision_mismatch", flush=True)
                    return False
                source_stale = True
                convergence_plan = (remote_commits, target_trees, changed_archives)
                write_snapshot_status("stale_source", stage="validate_source", reason="source_revision_mismatch")
                print("bha_bucket_snapshot=stale_source reason=source_revision_mismatch", flush=True)
    except Exception as exc:
        if allow_stale:
            source_stale = True
            write_snapshot_status("source_fingerprint_unverified", stage="validate_source", reason=type(exc).__name__)
            print(f"bha_bucket_snapshot=source_fingerprint_unverified reason={type(exc).__name__}", flush=True)
        else:
            write_snapshot_status(
                "source_fingerprint_unverified", stage="validate_source", reason=type(exc).__name__
            )
            print(f"bha_bucket_snapshot=source_fingerprint_unverified reason={type(exc).__name__}", flush=True)
            return False
    client = Elasticsearch(ES_URL, request_timeout=3600)
    if client.indices.exists(index=INDEX_NAME):
        write_snapshot_status("skipped", stage="preflight", reason="index_exists")
        print("bha_bucket_snapshot=skipped reason=index_exists", flush=True)
        return False
    started = time.monotonic()
    stage = "prepare_local_repository"
    stage_started = started
    stage_seconds: dict[str, float] = {}
    parallel_seconds: dict[str, float] = {}
    recovery_limit_applied = False
    recovery_limit_active = False
    sidecar_executor: concurrent.futures.ThreadPoolExecutor | None = None
    sidecar_future: concurrent.futures.Future[tuple[list[tuple[Path, Path]], float]] | None = None
    prepared_sidecars: list[tuple[Path, Path]] = []

    def enter_stage(next_stage: str) -> None:
        nonlocal stage, stage_started
        now = time.monotonic()
        stage_seconds[stage] = round(now - stage_started, 2)
        stage = next_stage
        stage_started = now
        write_snapshot_status(
            "restoring", stage=stage, snapshot=str(manifest["snapshot"]),
            stage_seconds=dict(stage_seconds),
            parallel_seconds=dict(parallel_seconds),
            recovery_max_bytes_per_sec=RESTORE_RECOVERY_MAX_BYTES_PER_SEC,
            recovery_limit_applied=recovery_limit_applied,
        )

    write_snapshot_status(
        "restoring", stage=stage, snapshot=str(manifest["snapshot"]), stage_seconds={},
        parallel_seconds={},
        recovery_max_bytes_per_sec=RESTORE_RECOVERY_MAX_BYTES_PER_SEC,
        recovery_limit_applied=False,
    )
    try:
        clear_directory(local_repository())
        enter_stage("copy_snapshot_repository")
        repository_source = bucket_dir / GENERATIONS_ROOT / str(manifest["generation"]) / REPOSITORY_DIRNAME
        if not repository_source.is_dir():
            raise RuntimeError("bucket snapshot repository is missing")
        shutil.copytree(repository_source, local_repository(), dirs_exist_ok=True)
        enter_stage("register_snapshot_repository")
        repository(client)

        def prepare_sidecars_timed() -> tuple[list[tuple[Path, Path]], float]:
            prepare_started = time.monotonic()
            paths = prepare_restored_sidecars(bucket_dir, manifest)
            return paths, round(time.monotonic() - prepare_started, 2)

        sidecar_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        sidecar_future = sidecar_executor.submit(prepare_sidecars_timed)
        enter_stage("configure_recovery")
        recovery_limit_applied = configure_restore_recovery(client)
        recovery_limit_active = recovery_limit_applied
        enter_stage("restore_elasticsearch")
        try:
            client.snapshot.restore(
                repository=REPOSITORY,
                snapshot=str(manifest["snapshot"]),
                indices=str(manifest["physical_index"]),
                include_global_state=False,
                include_aliases=True,
                wait_for_completion=True,
            )
        finally:
            clear_restore_recovery(client, recovery_limit_active)
            recovery_limit_active = False
        if not client.indices.exists_alias(name=INDEX_NAME):
            client.indices.put_alias(index=str(manifest["physical_index"]), name=INDEX_NAME)
        enter_stage("validate_elasticsearch")
        metadata = client.get(index=INDEX_NAME, id="__meta__").get("_source", {})
        count = int(client.count(index=INDEX_NAME, query={"exists": {"field": "doc_id"}})["count"])
        if metadata != manifest["index_metadata"] or count != int(manifest["document_count"]):
            raise RuntimeError("restored Elasticsearch snapshot failed validation")
        enter_stage("wait_sidecars")
        prepared_sidecars, parallel_seconds["prepare_sidecars"] = sidecar_future.result()
        sidecar_executor.shutdown(wait=True)
        sidecar_executor = None
        enter_stage("activate_sidecars")
        activate_restored_sidecars(prepared_sidecars)
        prepared_sidecars = []
        enter_stage("validate_sidecars")
        validate_restored_sidecars(manifest)
        enter_stage("cleanup_local_repository")
        clear_directory(local_repository())
        stage_seconds[stage] = round(time.monotonic() - stage_started, 2)
    except Exception as exc:
        clear_restore_recovery(client, recovery_limit_active)
        if sidecar_future is not None and not prepared_sidecars:
            try:
                prepared_sidecars, parallel_seconds["prepare_sidecars"] = sidecar_future.result()
            except Exception:
                pass
        if sidecar_executor is not None:
            sidecar_executor.shutdown(wait=True)
        discard_prepared_sidecars(prepared_sidecars)
        for target in (INDEX_NAME, str(manifest["physical_index"])):
            try:
                client.indices.delete(index=target, ignore_unavailable=True)
            except Exception:
                pass
        clear_directory(local_repository())
        detail = str(exc).replace("\n", " ")[:500]
        failed_stage_seconds = {
            **stage_seconds,
            stage: round(time.monotonic() - stage_started, 2),
        }
        write_snapshot_status(
            "unavailable", stage=stage, reason=type(exc).__name__, detail=detail,
            snapshot=str(manifest["snapshot"]), stage_seconds=failed_stage_seconds,
            parallel_seconds=parallel_seconds,
            recovery_max_bytes_per_sec=RESTORE_RECOVERY_MAX_BYTES_PER_SEC,
            recovery_limit_applied=recovery_limit_applied,
        )
        print(
            f"bha_bucket_snapshot=unavailable stage={stage} reason={type(exc).__name__} detail={detail}",
            flush=True,
        )
        return False
    if convergence_plan is not None:
        write_convergence_plan(*convergence_plan)
    write_snapshot_status(
        "restored", stage="complete", snapshot=str(manifest["snapshot"]),
        elapsed_seconds=round(time.monotonic() - started, 2), source_stale=source_stale,
        stage_seconds=stage_seconds,
        parallel_seconds=parallel_seconds,
        recovery_max_bytes_per_sec=RESTORE_RECOVERY_MAX_BYTES_PER_SEC,
        recovery_limit_applied=recovery_limit_applied,
    )
    print(f"bha_bucket_snapshot=restored snapshot={manifest['snapshot']} elapsed_seconds={time.monotonic() - started:.2f}", flush=True)
    return True


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"publish", "restore"}:
        raise SystemExit("usage: python -m app.bucket_snapshot publish|restore <bucket-dir>")
    action, directory = sys.argv[1:]
    bucket_dir = Path(directory)
    if action == "publish":
        try:
            publish(bucket_dir)
        except PublicationIncomplete:
            print("bha_bucket_snapshot=publish_blocked reason=previous_incomplete", flush=True)
            raise
        except Exception as exc:
            detail = getattr(exc, "body", None)
            print(f"bha_bucket_snapshot=publish_failed error={type(exc).__name__} detail={detail}", flush=True)
            raise
        return 0
    return 0 if restore(bucket_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
