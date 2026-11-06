import argparse
import fcntl
import hashlib
import json
import importlib.metadata
import os
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import closing
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import IO, Any, Iterable, NotRequired, TypedDict
from elasticsearch import Elasticsearch, helpers
from elasticsearch.exceptions import NotFoundError
from .config import DATA_ROOT, ARCHIVE_END, ARCHIVE_START, ES_URL, INDEX_NAME, INDEX_VERSION, RESET_INDEX
from .data_loader import DOCUMENT_BUILD_VERSION, ensure_parsed_data, iter_document_ids, iter_documents, object_dict, object_list, parsed_archive_commits, parsed_archive_revisions, parsed_corpus_fingerprint
from .doc_store import DOC_DB, DOC_STORE_SCHEMA_VERSION, connect as connect_doc_db, delete_docs_to_conn, doc_row, init_db as init_doc_db, reset_db, row_to_doc, update_years_to_conn, write_docs_to_conn
from .facet_store import FACET_DB, FACET_STORE_SCHEMA_VERSION, apply_facet_deltas, connect as connect_facet_db, init_db as init_facet_db, reset_db as reset_facet_db, write_facets
from .storage_lock import serving_lock
MAX_RESULT_WINDOW = 500000
BULK_CHUNK_SIZE = 100
BULK_MAX_BYTES = 16 * 1024 * 1024
PROGRESS_PATH = DATA_ROOT / "index-progress.json"
INDEX_LOCK_PATH = DATA_ROOT / "index.lock"
INDEX_STATUS_PATH = DATA_ROOT / "index-status"
SWITCH_STATE_PATH = DATA_ROOT / "index-switch.json"
INDEX_BUILD_SCHEMA_VERSION = "1"

def acquire_index_lock(blocking: bool = True) -> IO[str] | None:
    INDEX_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = INDEX_LOCK_PATH.open("a+")
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(lock_file.fileno(), operation)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file

def write_progress(**values: object) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": INDEX_VERSION,
        "archive_start": ARCHIVE_START,
        "archive_end": ARCHIVE_END,
        "updated_at": int(time.time()),
    }
    payload.update(values)
    PROGRESS_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

def client() -> Elasticsearch:
    return Elasticsearch(ES_URL, request_timeout=120)

class IndexSettings(TypedDict):
    max_result_window: int
    number_of_replicas: int
    refresh_interval: str


class IndexSettingsEnvelope(TypedDict):
    index: IndexSettings


class IndexSourceMapping(TypedDict):
    excludes: list[str]


class IndexFieldMapping(TypedDict):
    _source: IndexSourceMapping
    properties: dict[str, object]


class IndexMapping(TypedDict):
    settings: IndexSettingsEnvelope
    mappings: IndexFieldMapping


def index_mapping() -> IndexMapping:
    return {
        "settings": {
            "index": {
                "max_result_window": MAX_RESULT_WINDOW,
                "number_of_replicas": 0,
                "refresh_interval": "30s",
            },
        },
        "mappings": {
            "_source": {"excludes": ["content", "source_files", "*_words", "*_literal"]},
            "properties": {
                "doc_id": {"type": "keyword"},
                "article_id": {"type": "keyword"},
                "publication_id": {"type": "keyword"},
                "publication_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}, "cjk": {"type": "text", "analyzer": "cjk"}, "literal": {"type": "wildcard"}}},
                "publication_name_words": {"type": "text", "analyzer": "whitespace"},
                "publication_author": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "publication_type": {"type": "keyword"},
                "source_files": {"type": "keyword"},
                "archive_id": {"type": "integer"},
                "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}, "cjk": {"type": "text", "analyzer": "cjk"}, "literal": {"type": "wildcard"}}},
                "title_words": {"type": "text", "analyzer": "whitespace"},
                "authors": {"type": "text", "fields": {"keyword": {"type": "keyword"}, "literal": {"type": "wildcard"}}},
                "authors_words": {"type": "text", "analyzer": "whitespace"},
                "tag_names": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "tag_names_words": {"type": "text", "analyzer": "whitespace"},
                "tag_types": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "date_min": {"type": "integer"},
                "date_max": {"type": "integer"},
                "date_display": {"type": "keyword"},
                "dates": {"type": "integer"},
                "date_years": {"type": "integer"},
                "date_months": {"type": "integer"},
                "date_days": {"type": "integer"},
                "date_month_days": {"type": "keyword"},
                "date_year_months": {"type": "keyword"},
                "date_year_days": {"type": "keyword"},
                "date_year_month_days": {"type": "keyword"},
                "content": {"type": "text", "analyzer": "cjk", "index_phrases": True},
                "path": {"type": "keyword"},
            },
        },
    }

def build_fingerprint() -> str:
    contract = {
        "mapping": index_mapping(),
        "index_build": INDEX_BUILD_SCHEMA_VERSION,
        "documents": DOCUMENT_BUILD_VERSION,
        "doc_store": DOC_STORE_SCHEMA_VERSION,
        "facet_store": FACET_STORE_SCHEMA_VERSION,
        "jieba": importlib.metadata.version("jieba"),
    }
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

class IndexMetadata(TypedDict):
    version: str
    archive_start: int
    archive_end: int
    build_fingerprint: str
    source_fingerprint: NotRequired[str]
    archive_revisions: NotRequired[dict[str, str]]


def desired_metadata(source_fingerprint: str | None = None, archive_revisions: dict[int, str] | None = None) -> IndexMetadata:
    metadata: IndexMetadata = {
        "version": INDEX_VERSION,
        "archive_start": ARCHIVE_START,
        "archive_end": ARCHIVE_END,
        "build_fingerprint": build_fingerprint(),
    }
    if source_fingerprint is not None:
        metadata["source_fingerprint"] = source_fingerprint
    if archive_revisions is not None:
        metadata["archive_revisions"] = {str(k): v for k, v in sorted(archive_revisions.items())}
    return metadata

def metadata_matches(es: Elasticsearch, source_fingerprint: str, archive_revisions: dict[int, str] | None = None) -> bool:
    try:
        data = es.get(index=INDEX_NAME, id="__meta__")
    except Exception:
        return False
    source = data.get("_source", {})
    return source == desired_metadata(source_fingerprint, archive_revisions)

class IncrementalUnsafe(RuntimeError):
    pass

def copy_sqlite(source: Path, target: Path) -> None:
    target.unlink(missing_ok=True)
    with closing(sqlite3.connect(source)) as source_conn, closing(sqlite3.connect(target)) as target_conn:
            source_conn.backup(target_conn)

def write_sidecar_metadata(conn: sqlite3.Connection, metadata: Mapping[str, object]) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS build_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT OR REPLACE INTO build_meta(key, value) VALUES ('index_metadata', ?)",
        (json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True),),
    )

def write_facet_sidecar_metadata(db_path: Path, metadata: Mapping[str, object]) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS build_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT OR REPLACE INTO build_meta(key, value) VALUES ('index_metadata', ?)",
            (json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True),),
        )
        conn.commit()

def sidecar_metadata_matches(db_path: Path, metadata: Mapping[str, object]) -> bool:
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute("SELECT value FROM build_meta WHERE key = 'index_metadata'").fetchone()
        return bool(row) and json.loads(row[0]) == metadata
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False

def serving_physical_index(es: Elasticsearch) -> str | None:
    try:
        aliases = es.indices.get_alias(name=INDEX_NAME)
    except Exception:
        return None
    indices = [name for name, data in aliases.items() if INDEX_NAME in data.get("aliases", {})]
    return indices[0] if len(indices) == 1 else None

def clone_serving_index(es: Elasticsearch, source: str, target: str) -> None:
    try:
        es.indices.put_settings(index=source, settings={"index.blocks.write": True})
        response = es.indices.clone(
            index=source,
            target=target,
            body={"settings": {"index.blocks.write": False}},
            wait_for_active_shards="1",
            timeout="120s",
        )
        if not response.get("acknowledged"):
            raise IncrementalUnsafe("incremental clone was not acknowledged")
        health = es.cluster.health(
            index=target,
            wait_for_status="yellow",
            wait_for_no_initializing_shards=True,
            timeout="120s",
        )
        if health.get("timed_out"):
            raise IncrementalUnsafe("incremental clone shard did not become ready")
    finally:
        es.indices.put_settings(index=source, settings={"index.blocks.write": False})

def empty_facet_counters() -> dict[str, Counter[str]]:
    return {
        "source": Counter(), "author": Counter(), "tag": Counter(),
        "type": Counter(), "archive": Counter(),
    }

def elasticsearch_document(doc: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in doc.items() if key != "article"}

def incremental_build(
    es: Elasticsearch,
    source_fingerprint: str,
    revisions: dict[int, str],
    old_meta: Mapping[str, object],
    changed_paths: dict[int, set[str]] | None = None,
) -> None:
    source_index = serving_physical_index(es)
    old_revisions: dict[str, object] | None = object_dict(old_meta.get("archive_revisions"))
    expected_static = desired_metadata()
    if (
        not source_index
        or old_revisions is None
        or set(old_revisions) != {str(archive_id) for archive_id in revisions}
        or any(old_meta.get(key) != value for key, value in expected_static.items())
    ):
        raise IncrementalUnsafe("incremental state is unavailable")
    changed = [
        archive_id for archive_id in revisions
        if old_revisions.get(str(archive_id)) != revisions[archive_id]
    ]
    if not changed:
        raise IncrementalUnsafe("source fingerprint changed without an archive revision change")

    generation = uuid.uuid4().hex[:12]
    build_index = f"{INDEX_NAME}-{generation}"
    temp_doc_db = DOC_DB.with_name(f"{DOC_DB.name}.{generation}.tmp")
    temp_facet_db = FACET_DB.with_name(f"{FACET_DB.name}.{generation}.tmp")
    activated = False
    try:
        copy_sqlite(DOC_DB, temp_doc_db)
        copy_sqlite(FACET_DB, temp_facet_db)
        if not sidecar_metadata_matches(DOC_DB, old_meta) or not sidecar_metadata_matches(FACET_DB, old_meta):
            raise IncrementalUnsafe("serving sidecar generation is unavailable")
        removed_docs: list[dict[str, Any]] = []
        upsert_docs: list[dict[str, Any]] = []
        scan_archives = changed
        if changed_paths is not None:
            # A changed-path filter cannot identify deletions without seeing the
            # complete current archive. Empty diffs are safe to skip.
            scan_archives = [
                archive_id for archive_id in changed
                if changed_paths.get(archive_id) is None or changed_paths[archive_id]
            ]
        with closing(connect_doc_db(temp_doc_db)) as conn:
            init_doc_db(conn)
            remaining_ids: set[str] = set()
            if scan_archives:
                placeholders = ",".join("?" for _ in scan_archives)
                old_ids = {
                    row[0] for row in conn.execute(
                        f"SELECT doc_id FROM docs WHERE archive_id IN ({placeholders})", scan_archives
                    )
                }
                current_ids = set(iter_document_ids(scan_archives))
                remaining_ids = old_ids - current_ids
            seen_ids: set[str] = set()
            # File paths identify deletions cheaply; only changed files need
            # JSON normalization and tokenization.
            for doc in iter_documents(scan_archives, changed_paths=changed_paths):
                doc_id = doc["doc_id"]
                if doc_id in seen_ids:
                    raise IncrementalUnsafe(f"duplicate incremental doc_id: {doc_id}")
                seen_ids.add(doc_id)
                remaining_ids.discard(doc_id)
                row = conn.execute("SELECT * FROM docs WHERE doc_id = ?", (doc_id,)).fetchone()
                if row is None:
                    upsert_docs.append(dict(doc))
                elif tuple(row[key] for key in row.keys()) != doc_row(dict(doc)):
                    removed_docs.append(row_to_doc(row))
                    upsert_docs.append(dict(doc))
            deleted = sorted(remaining_ids)
            if deleted:
                delete_placeholders = ",".join("?" for _ in deleted)
                removed_docs.extend(
                    row_to_doc(row) for row in conn.execute(
                        f"SELECT * FROM docs WHERE doc_id IN ({delete_placeholders})", deleted
                    )
                )
            delete_docs_to_conn(conn, deleted)
            write_docs_to_conn(conn, upsert_docs)
            update_years_to_conn(conn, [*removed_docs, *upsert_docs])
            conn.commit()
        removed_facets = empty_facet_counters()
        added_facets = empty_facet_counters()
        for doc in removed_docs:
            update_facets(removed_facets, doc)
        for doc in upsert_docs:
            update_facets(added_facets, doc)
        apply_facet_deltas(removed_facets, added_facets, temp_facet_db)

        clone_serving_index(es, source_index, build_index)
        actions: list[dict[str, Any]] = []
        for doc_id in deleted:
            actions.append({"_op_type": "delete", "_index": build_index, "_id": doc_id})
        for doc in upsert_docs:
            actions.append({"_op_type": "index", "_index": build_index, "_id": doc["doc_id"], "_source": elasticsearch_document(doc)})
        errors = [item for ok, item in helpers.streaming_bulk(
            es.options(request_timeout=120), actions, chunk_size=BULK_CHUNK_SIZE,
            max_chunk_bytes=BULK_MAX_BYTES, raise_on_error=False,
            raise_on_exception=False, retry_on_status=(429, 502, 503, 504),
            max_retries=8, initial_backoff=2, max_backoff=60,
        ) if not ok]
        if errors:
            raise IncrementalUnsafe(f"incremental bulk update failed: {errors[0]}")
        es.index(index=build_index, id="__meta__", document=desired_metadata(source_fingerprint, revisions))
        with closing(connect_doc_db(temp_doc_db)) as conn:
            write_sidecar_metadata(conn, desired_metadata(source_fingerprint, revisions))
            conn.commit()
        write_facet_sidecar_metadata(temp_facet_db, desired_metadata(source_fingerprint, revisions))
        es.indices.refresh(index=build_index)
        with closing(connect_doc_db(temp_doc_db)) as conn:
            sqlite_count = int(conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
        es_count = int(es.count(index=build_index, query={"exists": {"field": "doc_id"}}).get("count", -1))
        if es_count != sqlite_count:
            raise IncrementalUnsafe(
                f"incremental document count mismatch: elasticsearch={es_count} sqlite={sqlite_count}"
            )
        INDEX_STATUS_PATH.write_text("switching", encoding="utf-8")
        activate_build(es, build_index, temp_doc_db, temp_facet_db)
        activated = True
        INDEX_STATUS_PATH.write_text("ready", encoding="utf-8")
    except Exception as exc:
        uncertain = activation_uncertain(es, build_index)
        if uncertain or isinstance(exc, IncrementalUnsafe):
            raise
        INDEX_STATUS_PATH.write_text("ready", encoding="utf-8")
        raise IncrementalUnsafe(f"incremental build failed safely: {exc}") from exc
    finally:
        uncertain = activation_uncertain(es, build_index)
        if not activated and not uncertain:
            es.indices.delete(index=build_index, ignore_unavailable=True)
            reset_db(temp_doc_db)
            reset_facet_db(temp_facet_db)

def sidecars_ready(metadata: Mapping[str, object] | None = None) -> bool:
    if not DOC_DB.exists() or not FACET_DB.exists():
        return False
    try:
        with closing(connect_doc_db()) as conn:
            init_doc_db(conn)
            doc_count = int(conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
            meta_count = int(conn.execute("SELECT COUNT(*) FROM build_meta").fetchone()[0])
        with closing(sqlite3.connect(FACET_DB)) as conn:
            facet_count = int(conn.execute("SELECT COUNT(*) FROM facets").fetchone()[0])
        docs_match = metadata is None or sidecar_metadata_matches(DOC_DB, metadata)
        facets_match = metadata is None or sidecar_metadata_matches(FACET_DB, metadata)
        return doc_count > 0 and facet_count > 0 and meta_count > 0 and docs_match and facets_match
    except Exception:
        return False

def serving_data_available(es: Elasticsearch) -> bool:
    try:
        metadata = es.get(index=INDEX_NAME, id="__meta__").get("_source", {})
        return bool(es.indices.exists(index=INDEX_NAME)) and int(es.count(index=INDEX_NAME).get("count", 0)) > 0 and sidecars_ready(metadata)
    except Exception:
        return False

def start_progressive_build(es: Elasticsearch, build_index: str) -> None:
    with serving_lock(exclusive=True):
        reset_facet_db(FACET_DB)
        reset_db(DOC_DB)
        with closing(connect_doc_db()) as conn:
            init_doc_db(conn)
        with closing(connect_facet_db()) as conn:
            init_facet_db(conn)
        activate_index(es, build_index)

def replace_db(temp_path: Path, target_path: Path) -> None:
    # Close/checkpoint happens before this call; replace keeps readers on the old inode
    # while new requests immediately see the complete database.
    os.replace(temp_path, target_path)

def checkpoint_for_replace(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")

def remove_db_companions(db_path: Path) -> None:
    Path(f"{db_path}-wal").unlink(missing_ok=True)
    Path(f"{db_path}-shm").unlink(missing_ok=True)

def discard_progressive_build(es: Elasticsearch, build_index: str) -> None:
    with serving_lock(exclusive=True):
        try:
            es.indices.delete(index=build_index, ignore_unavailable=True)
        finally:
            reset_db(DOC_DB)
            reset_facet_db(FACET_DB)

def write_switch_state(payload: Mapping[str, object]) -> None:
    temp_path = SWITCH_STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(dict(payload)), encoding="utf-8")
    os.replace(temp_path, SWITCH_STATE_PATH)

def alias_points_to(es: Elasticsearch, index_name: str) -> bool:
    try:
        aliases = es.indices.get_alias(index=index_name)
    except NotFoundError:
        return False
    return INDEX_NAME in aliases.get(index_name, {}).get("aliases", {})

def activation_uncertain(es: Elasticsearch, index_name: str) -> bool:
    if SWITCH_STATE_PATH.exists():
        return True
    try:
        return alias_points_to(es, index_name)
    except Exception:
        return False

def recover_interrupted_switch(es: Elasticsearch) -> None:
    if not SWITCH_STATE_PATH.exists():
        return
    with serving_lock(exclusive=True):
        INDEX_STATUS_PATH.write_text("restoring", encoding="utf-8")
        try:
            state = json.loads(SWITCH_STATE_PATH.read_text(encoding="utf-8"))
            build_index = str(state["build_index"])
            had_docs = bool(state["had_docs"])
            had_facets = bool(state["had_facets"])
        except Exception as exc:
            raise RuntimeError("invalid index switch state") from exc
        docs_backup = DOC_DB.with_name(f"{DOC_DB.name}.previous")
        facets_backup = FACET_DB.with_name(f"{FACET_DB.name}.previous")
        if alias_points_to(es, build_index):
            docs_backup.unlink(missing_ok=True)
            facets_backup.unlink(missing_ok=True)
            SWITCH_STATE_PATH.unlink(missing_ok=True)
            INDEX_STATUS_PATH.write_text("ready", encoding="utf-8")
            return
        if had_docs and docs_backup.exists():
            os.replace(docs_backup, DOC_DB)
        elif not had_docs:
            DOC_DB.unlink(missing_ok=True)
        if had_facets and facets_backup.exists():
            os.replace(facets_backup, FACET_DB)
        elif not had_facets:
            FACET_DB.unlink(missing_ok=True)
        es.indices.delete(index=build_index, ignore_unavailable=True)
        SWITCH_STATE_PATH.unlink(missing_ok=True)

def activate_build(es: Elasticsearch, build_index: str, temp_doc_db: Path, temp_facet_db: Path) -> None:
    backups: list[tuple[Path, Path]] = []
    had_docs = DOC_DB.exists()
    had_facets = FACET_DB.exists()
    try:
        checkpoint_for_replace(temp_doc_db)
        checkpoint_for_replace(temp_facet_db)
        with serving_lock(exclusive=True):
            write_switch_state({
                "build_index": build_index,
                "had_docs": had_docs,
                "had_facets": had_facets,
            })
            for target in (DOC_DB, FACET_DB):
                if target.exists():
                    checkpoint_for_replace(target)
                    remove_db_companions(target)
                    backup = target.with_name(f"{target.name}.previous")
                    backup.unlink(missing_ok=True)
                    os.link(target, backup)
                    backups.append((target, backup))
            replace_db(temp_doc_db, DOC_DB)
            replace_db(temp_facet_db, FACET_DB)
            activate_index(es, build_index)
    except Exception:
        try:
            if alias_points_to(es, build_index):
                for _, backup in backups:
                    backup.unlink(missing_ok=True)
                SWITCH_STATE_PATH.unlink(missing_ok=True)
                return
        except Exception:
            # Preserve switch state and backups for recover_interrupted_switch().
            raise
        DOC_DB.unlink(missing_ok=True)
        FACET_DB.unlink(missing_ok=True)
        remove_db_companions(DOC_DB)
        remove_db_companions(FACET_DB)
        for target, backup in backups:
            os.replace(backup, target)
        SWITCH_STATE_PATH.unlink(missing_ok=True)
        raise
    for _, backup in backups:
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            print(f"sidecar_backup_cleanup_failed={exc}")
    SWITCH_STATE_PATH.unlink(missing_ok=True)

def activate_index(es: Elasticsearch, new_index: str) -> None:
    try:
        aliases = list(es.indices.get_alias(name=INDEX_NAME))
    except NotFoundError:
        aliases = []
    legacy_index = bool(es.indices.exists(index=INDEX_NAME)) and not bool(es.indices.exists_alias(name=INDEX_NAME))
    actions: list[dict[str, Any]] = [
        {"remove": {"index": index_name, "alias": INDEX_NAME}}
        for index_name in aliases
        if index_name != new_index and not (index_name == INDEX_NAME and legacy_index)
    ]
    if legacy_index:
        actions.append({"remove_index": {"index": INDEX_NAME}})
    actions.append({"add": {"index": new_index, "alias": INDEX_NAME}})
    es.indices.update_aliases(actions=actions)
    old_indices = [index_name for index_name in aliases if index_name != new_index]
    if old_indices:
        try:
            es.indices.delete(index=old_indices, ignore_unavailable=True)
        except Exception as exc:
            print(f"old_index_cleanup_failed={exc}")

def ensure_index(
    reset: bool = False,
    lock_file: IO[str] | None = None,
    expected_commits: dict[int, str] | None = None,
    expected_revisions: dict[int, str] | None = None,
    changed_archives: Iterable[int] | None = None,
) -> bool:
    owned_lock = lock_file is None
    if lock_file is None:
        lock_file = acquire_index_lock()
    if lock_file is None:
        raise RuntimeError("failed to acquire index lock")
    try:
        if expected_commits is None and expected_revisions is None:
            return _ensure_index(reset)
        return _ensure_index(reset, expected_commits, expected_revisions, changed_archives)
    finally:
        if owned_lock:
            lock_file.close()

def _ensure_index(
    reset: bool = False,
    expected_commits: dict[int, str] | None = None,
    expected_revisions: dict[int, str] | None = None,
    changed_archives: Iterable[int] | None = None,
) -> bool:
    started_at = time.time()
    write_progress(status="preparing", mode="unknown", indexed=0, errors=0, elapsed_seconds=0)
    es = client()
    recover_interrupted_switch(es)
    has_serving_data = serving_data_available(es)
    old_meta: dict[str, Any] = es.get(index=INDEX_NAME, id="__meta__").get("_source", {}) if has_serving_data else {}
    sync_archive_ids: list[int] | None = None
    old_revisions = object_dict(old_meta.get("archive_revisions"))
    if expected_commits is not None and expected_revisions is not None and (
        old_revisions is not None
        and set(old_revisions) == {str(archive_id) for archive_id in expected_revisions}
        and all(old_meta.get(key) == value for key, value in desired_metadata().items())
    ):
        requested_archives = set(changed_archives) if changed_archives is not None else None
        derived_archives = [
            archive_id for archive_id, revision in expected_revisions.items()
            if old_revisions.get(str(archive_id)) != revision
        ]
        if requested_archives is not None and not set(derived_archives).issubset(requested_archives):
            print("changed_archives_hint_incomplete=using_revision_difference", flush=True)
            requested_archives = None
        sync_archive_ids = [
            archive_id for archive_id, revision in expected_revisions.items()
            if old_revisions.get(str(archive_id)) != revision
            and (requested_archives is None or archive_id in requested_archives)
        ]
    changed_paths = ensure_parsed_data(lambda current, total: write_progress(
        status="syncing",
        indexed=0,
        errors=0,
        archive_current=current,
        archive_total=total,
        elapsed_seconds=int(time.time() - started_at),
    ), expected_commits=expected_commits, archive_ids=sync_archive_ids)
    if not isinstance(changed_paths, dict):
        changed_paths = None
    if expected_commits is not None:
        local_commits = parsed_archive_commits(archive_ids=sync_archive_ids)
        if local_commits != {
            archive_id: expected_commits[archive_id]
            for archive_id in (sync_archive_ids if sync_archive_ids is not None else expected_commits)
        }:
            raise RuntimeError("checked-out parsed commits do not match the reindex request")
    local_revisions = parsed_archive_revisions(archive_ids=sync_archive_ids)
    if expected_revisions is not None and local_revisions != {
        archive_id: expected_revisions[archive_id]
        for archive_id in (sync_archive_ids if sync_archive_ids is not None else expected_revisions)
    }:
        raise RuntimeError("checked-out parsed trees do not match the reindex request")
    if sync_archive_ids is not None:
        if expected_revisions is None:
            raise RuntimeError("missing expected revisions for partial synchronization")
        archive_revisions = expected_revisions
    else:
        archive_revisions = local_revisions
    source_fingerprint = parsed_corpus_fingerprint(revisions=archive_revisions)
    force_rebuild = reset or RESET_INDEX
    if es.indices.exists(index=INDEX_NAME):
        count = es.count(index=INDEX_NAME).get("count", 0)
        if not force_rebuild and count and metadata_matches(es, source_fingerprint, archive_revisions) and sidecars_ready(desired_metadata(source_fingerprint, archive_revisions)):
            write_progress(status="ready", indexed=count, errors=0, elapsed_seconds=int(time.time() - started_at))
            return False
    if not force_rebuild and has_serving_data:
        try:
            write_progress(status="incremental", mode="incremental", indexed=0, errors=0,
                           elapsed_seconds=int(time.time() - started_at))
            if changed_paths is None:
                incremental_build(es, source_fingerprint, archive_revisions, old_meta)
            else:
                incremental_build(es, source_fingerprint, archive_revisions, old_meta, changed_paths=changed_paths)
            write_progress(
                status="ready", mode="incremental",
                indexed=es.count(index=INDEX_NAME).get("count", 0),
                errors=0,
                elapsed_seconds=int(time.time() - started_at),
            )
            return True
        except IncrementalUnsafe as exc:
            print(f"incremental_index_unavailable={exc}; keeping serving generation", flush=True)
            raise
    progressive = not has_serving_data
    write_progress(status="full", mode="full", indexed=0, errors=0,
                   elapsed_seconds=int(time.time() - started_at))
    generation = uuid.uuid4().hex[:12]
    build_index = f"{INDEX_NAME}-{generation}"
    temp_doc_db = DOC_DB if progressive else DOC_DB.with_name(f"{DOC_DB.name}.{generation}.tmp")
    temp_facet_db = FACET_DB if progressive else FACET_DB.with_name(f"{FACET_DB.name}.{generation}.tmp")
    if not progressive:
        reset_facet_db(temp_facet_db)
        reset_db(temp_doc_db)
    try:
        es.indices.create(index=build_index, body=dict(index_mapping()))
        if progressive:
            start_progressive_build(es, build_index)
    except Exception:
        if progressive:
            discard_progressive_build(es, build_index)
        else:
            es.indices.delete(index=build_index, ignore_unavailable=True)
            reset_db(temp_doc_db)
            reset_facet_db(temp_facet_db)
        raise
    facet_counters: dict[str, Counter[str]] = {
        "source": Counter(),
        "author": Counter(),
        "tag": Counter(),
        "type": Counter(),
        "archive": Counter(),
    }

    def actions() -> Iterator[dict[str, Any]]:
        pending_docs: list[dict[str, Any]] = []
        doc_conn = connect_doc_db(temp_doc_db)
        init_doc_db(doc_conn)
        try:
            for doc in iter_documents():
                update_facets(facet_counters, doc)
                pending_docs.append(dict(doc))
                if len(pending_docs) >= 500:
                    write_docs_to_conn(doc_conn, pending_docs)
                    doc_conn.commit()
                    pending_docs.clear()
                yield {"_index": build_index, "_id": doc["doc_id"], "_source": elasticsearch_document(doc)}
            if pending_docs:
                write_docs_to_conn(doc_conn, pending_docs)
                doc_conn.commit()
        finally:
            doc_conn.close()
    activation_committed = False
    try:
        ok = 0
        error_count = 0
        error_samples: list[dict[str, Any]] = []
        last_facet_write = 0
        for success, item in helpers.streaming_bulk(
            es.options(request_timeout=120),
            actions(),
            chunk_size=BULK_CHUNK_SIZE,
            max_chunk_bytes=BULK_MAX_BYTES,
            max_retries=8,
            initial_backoff=2,
            max_backoff=60,
            retry_on_status=(429, 502, 503, 504),
            raise_on_error=False,
            raise_on_exception=False,
        ):
            if success:
                ok += 1
            else:
                error_count += 1
                if len(error_samples) < 5:
                    error_samples.append(item)
            if (ok + error_count) % 500 == 0:
                write_progress(status="indexing", indexed=ok, errors=error_count, elapsed_seconds=int(time.time() - started_at))
            if ok - last_facet_write >= 5000:
                write_facets(facet_counters, temp_facet_db)
                last_facet_write = ok
        for error in error_samples:
            print(f"bulk_error_sample={error}")
        if error_count:
            write_progress(status="failed", indexed=ok, errors=error_count, elapsed_seconds=int(time.time() - started_at))
            raise RuntimeError(f"bulk indexing failed with {error_count} errors")
        with closing(connect_doc_db(temp_doc_db)) as conn:
            sqlite_count = int(conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0])
        if sqlite_count != ok:
            raise RuntimeError(
                f"full build document count mismatch: indexed={ok} sqlite={sqlite_count}; duplicate doc_id likely"
            )
        write_facets(facet_counters, temp_facet_db)
        es.index(index=build_index, id="__meta__", document=desired_metadata(source_fingerprint, archive_revisions))
        with closing(connect_doc_db(temp_doc_db)) as conn:
            write_sidecar_metadata(conn, desired_metadata(source_fingerprint, archive_revisions))
            conn.commit()
        write_facet_sidecar_metadata(temp_facet_db, desired_metadata(source_fingerprint, archive_revisions))
        es.indices.refresh(index=build_index)
        try:
            es.options(request_timeout=3600).indices.forcemerge(index=build_index, max_num_segments=1)
        except Exception as exc:
            print(f"forcemerge failed (non-fatal): {exc}")
        if progressive:
            activation_committed = True
        else:
            INDEX_STATUS_PATH.write_text("switching", encoding="utf-8")
            activate_build(es, build_index, temp_doc_db, temp_facet_db)
            activation_committed = True
        INDEX_STATUS_PATH.write_text("ready", encoding="utf-8")
        write_progress(
            status="ready", indexed=ok, errors=0, elapsed_seconds=int(time.time() - started_at),
        )
    except Exception:
        if progressive:
            discard_progressive_build(es, build_index)
        elif not activation_committed and not SWITCH_STATE_PATH.exists():
            es.indices.delete(index=build_index, ignore_unavailable=True)
            reset_db(temp_doc_db)
            reset_facet_db(temp_facet_db)
        raise
    return True

def update_facets(counters: dict[str, Counter[str]], doc: Mapping[str, object]) -> None:
    if doc.get("publication_name"):
        counters["source"][str(doc["publication_name"])] += 1
    if doc.get("publication_type"):
        counters["type"][str(doc["publication_type"])] += 1
    if doc.get("archive_id") is not None:
        counters["archive"][str(doc["archive_id"])] += 1
    for author in object_list(doc.get("authors")):
        if str(author).strip():
            counters["author"][str(author).strip()] += 1
    for tag in object_list(doc.get("tag_names")):
        if str(tag).strip():
            counters["tag"][str(tag).strip()] += 1

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if args.ensure or args.reset:
        if args.plan is None:
            ensure_index(reset=args.reset)
            return
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        expected_commits = {int(key): str(value) for key, value in plan["archive_commits"].items()}
        expected_revisions = {int(key): str(value) for key, value in plan["archive_revisions"].items()}
        changed_archives = [int(value) for value in plan.get("changed_archives", [])]
        ensure_index(
            reset=args.reset,
            expected_commits=expected_commits,
            expected_revisions=expected_revisions,
            changed_archives=changed_archives,
        )

if __name__ == "__main__":
    main()
