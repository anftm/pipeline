import asyncio
from collections.abc import Awaitable, Callable
import json
import random
import threading
import time
from typing import Any

from elasticsearch import Elasticsearch
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .bucket_snapshot import snapshot_status as bucket_snapshot_status
from .config import APP_ROOT, DATA_ROOT, ES_URL, INDEX_NAME
from .doc_store import DOC_DB, available_years, warmup_db
from .facet_store import FACET_DB, list_facets, sources_payload
from .indexer import sidecars_ready
from .search_logic import SearchRequest
from .storage_lock import serving_lock


router = APIRouter()
es = Elasticsearch(ES_URL, request_timeout=120)
INDEX_STATUS_PATH = DATA_ROOT / "index-status"
INDEX_SWITCH_STATE_PATH = DATA_ROOT / "index-switch.json"
STATUS_CACHE_TTL_SECONDS = 5
WARMUP_READY_POLL_SECONDS = 5
SEARCH_CACHE_TTL_SECONDS = 30
SEARCH_CACHE_MAX_ENTRIES = 128
LITERAL_CACHE_TTL_SECONDS = 300
LITERAL_CACHE_MAX_ENTRIES = 16
LITERAL_CACHE_MAX_TOTAL_IDS = 250000
FACET_FIELDS = {"source", "author", "tag", "type", "archive"}
FACET_CACHE_TTL_SECONDS = 600
active_user_requests = 0
last_user_activity = time.monotonic()
activity_lock = asyncio.Lock()
status_response_cache: tuple[float, dict[str, Any]] | None = None
search_response_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
literal_search_cache: dict[tuple[Any, ...], tuple[float, tuple[str, ...]]] = {}
search_cache_lock = threading.Lock()
search_inflight: dict[tuple[Any, ...], threading.Event] = {}
facet_response_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
sources_response_cache: tuple[float, dict[str, Any]] | None = None
sources_response_cache_generation: tuple[tuple[int, int, int, int] | None, ...] | None = None
_has_index_data = False


async def track_request(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    tracked = tracks_user_activity(request.url.path)
    if tracked:
        global active_user_requests, last_user_activity
        async with activity_lock:
            active_user_requests += 1
            last_user_activity = time.monotonic()
    try:
        return await call_next(request)
    finally:
        if tracked:
            async with activity_lock:
                active_user_requests = max(0, active_user_requests - 1)
                last_user_activity = time.monotonic()


def tracks_user_activity(path: str) -> bool:
    return (
        path == "/api/search"
        or path == "/api/random"
        or path.startswith("/api/preview/")
        or path.startswith("/api/download/")
    )


@router.get("/api/health")

def health():
    result = cached_status_payload(include_es=True)
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)

def warm_static_files() -> bool:
    paths = [APP_ROOT / "static" / name for name in ("index.html", "app.js", "style.css")]
    try:
        return all(path.read_bytes() for path in paths)
    except Exception:
        return False

def clear_response_caches() -> None:
    global sources_response_cache, sources_response_cache_generation, status_response_cache
    facet_response_cache.clear()
    with search_cache_lock:
        search_response_cache.clear()
        literal_search_cache.clear()
    sources_response_cache = None
    sources_response_cache_generation = None
    status_response_cache = None

def serving_generation_token() -> tuple[tuple[int, int, int, int] | None, ...]:
    values: list[tuple[int, int, int, int] | None] = []
    for path in (DOC_DB, FACET_DB):
        try:
            stat = path.stat()
            values.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except OSError:
            values.append(None)
    return tuple(values)


def search_cache_key(body: SearchRequest, page: int, page_size: int) -> tuple[Any, ...]:
    def values(plural: list[str], singular: str | None = None) -> tuple[str, ...]:
        selected = plural or ([singular] if singular else [])
        return tuple(sorted(set(str(item).strip() for item in selected if str(item).strip())))

    return (
        serving_generation_token(),
        body.q.strip(), page, page_size, body.exact, body.fulltext, body.sort,
        values(body.sources, body.source), values(body.exclude_sources),
        values(body.authors, body.author), values(body.exclude_authors),
        values(body.tags, body.tag), values(body.exclude_tags), body.archive_id,
        body.publication_type, body.date_from, body.date_to,
        tuple(tuple(sorted(term.items())) for term in body.date_terms),
    )


def literal_cache_key(body: SearchRequest) -> tuple[Any, ...]:
    return search_cache_key(body, 0, 0)


def cached_search_response(key: tuple[Any, ...]) -> dict[str, Any] | None:
    now = time.monotonic()
    with search_cache_lock:
        cached = search_response_cache.get(key)
        if cached and now - cached[0] < SEARCH_CACHE_TTL_SECONDS:
            return dict(cached[1])
    return None


def cached_search_total(key: tuple[Any, ...]) -> int | None:
    now = time.monotonic()
    query_key = key[:2] + key[4:]
    with search_cache_lock:
        for candidate_key, (stored_at, result) in search_response_cache.items():
            if now - stored_at >= SEARCH_CACHE_TTL_SECONDS or candidate_key[:2] + candidate_key[4:] != query_key:
                continue
            total = result.get("total")
            if type(total) is int and total >= 0 and result.get("index_status") == "ready":
                return total
    return None


def cached_literal_ids(key: tuple[Any, ...]) -> tuple[str, ...] | None:
    now = time.monotonic()
    with search_cache_lock:
        cached = literal_search_cache.get(key)
        if cached and now - cached[0] < LITERAL_CACHE_TTL_SECONDS:
            return cached[1]
        if cached:
            literal_search_cache.pop(key, None)
    return None


def store_literal_ids(key: tuple[Any, ...], doc_ids: list[str]) -> None:
    values = tuple(doc_ids)
    if len(values) > LITERAL_CACHE_MAX_TOTAL_IDS:
        return
    with search_cache_lock:
        literal_search_cache[key] = (time.monotonic(), values)
        while (
            len(literal_search_cache) > LITERAL_CACHE_MAX_ENTRIES
            or sum(len(item[1]) for item in literal_search_cache.values()) > LITERAL_CACHE_MAX_TOTAL_IDS
        ):
            oldest_key = min(literal_search_cache, key=lambda item: literal_search_cache[item][0])
            literal_search_cache.pop(oldest_key, None)


def begin_search(key: tuple[Any, ...]) -> tuple[bool, threading.Event]:
    with search_cache_lock:
        event = search_inflight.get(key)
        if event is not None:
            return False, event
        event = threading.Event()
        search_inflight[key] = event
        return True, event


def finish_search(key: tuple[Any, ...], event: threading.Event) -> None:
    with search_cache_lock:
        if search_inflight.get(key) is event:
            search_inflight.pop(key, None)
            event.set()


def store_search_response(key: tuple[Any, ...], result: dict[str, Any]) -> None:
    with search_cache_lock:
        search_response_cache[key] = (time.monotonic(), result)
        if len(search_response_cache) > SEARCH_CACHE_MAX_ENTRIES:
            oldest_key = min(search_response_cache, key=lambda item: search_response_cache[item][0])
            search_response_cache.pop(oldest_key, None)

def warmup_elasticsearch() -> bool:
    try:
        with serving_lock():
            es.search(
                index=INDEX_NAME,
                size=0,
                track_total_hits=False,
                query={"match_all": {}},
                source=False,
            )
        return True
    except Exception:
        return False

def run_auxiliary_warmup(full: bool = False) -> dict[str, Any]:
    elasticsearch_warmed = False
    preview_warmed = False
    facets_warmed = False
    static_warmed = False
    if full:
        clear_response_caches()
    try:
        elasticsearch_warmed = warmup_elasticsearch()
    except Exception:
        pass
    try:
        with serving_lock():
            preview_warmed = warmup_db()
    except Exception:
        pass
    try:
        with serving_lock():
            cached_sources_payload()
            available_years()
            for kind in ("source", "author", "tag"):
                cached_facet_payload(kind, 1, 200, "")
            facets_warmed = True
    except Exception:
        pass
    static_warmed = warm_static_files()
    ok = elasticsearch_warmed and preview_warmed and facets_warmed and static_warmed
    return {
        "ok": ok,
        "elasticsearch_warmed": elasticsearch_warmed,
        "preview_warmed": preview_warmed,
        "facets_warmed": facets_warmed,
        "static_warmed": static_warmed,
    }

async def user_is_active() -> bool:
    async with activity_lock:
        return active_user_requests > 0 or time.monotonic() - last_user_activity < 90

async def auxiliary_warmup_loop() -> None:
    while True:
        await asyncio.sleep(random.randint(300, 540))
        if not await asyncio.to_thread(serving_generation_ready):
            continue
        if await user_is_active():
            continue
        await asyncio.to_thread(run_auxiliary_warmup, False)

def serving_generation_ready() -> bool:
    return index_status() == "ready" and index_ready()

async def initialize_when_ready() -> None:
    while not await asyncio.to_thread(serving_generation_ready):
        await asyncio.sleep(WARMUP_READY_POLL_SECONDS)
    await asyncio.to_thread(run_auxiliary_warmup, True)


@router.get("/api/ping")
def ping():
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


def index_progress() -> dict[str, Any] | None:
    try:
        return json.loads((DATA_ROOT / "index-progress.json").read_text(encoding="utf-8"))
    except Exception:
        return None

def index_status() -> str:
    try:
        return INDEX_STATUS_PATH.read_text(encoding="utf-8").strip() or "unknown"
    except Exception:
        return "unknown"

def cached_status_payload(include_es: bool = False) -> dict[str, Any]:
    global status_response_cache
    now = time.monotonic()
    current_status = index_status()
    if (
        status_response_cache
        and now - status_response_cache[0] < STATUS_CACHE_TTL_SECONDS
        and status_response_cache[1].get("index_status") == current_status
    ):
        result = dict(status_response_cache[1])
    else:
        status = current_status
        ready = status == "ready" and index_ready() and FACET_DB.exists()
        result = {
            "ok": ready,
            "index_ready": ready,
            "document_count": document_count(),
            "index_status": status,
            "progress": index_progress(),
            "bucket_snapshot": bucket_snapshot_status(),
        }
        status_response_cache = (now, result)
        result = dict(result)
    if include_es:
        try:
            result["es"] = es.ping()
        except Exception:
            result["es"] = False
        result["ok"] = bool(result["ok"] and result["es"])
    return result

def index_ready() -> bool:
    try:
        data = es.get(index=INDEX_NAME, id="__meta__")
        return bool(data.get("found"))
    except Exception:
        return False

def serving_generation_valid() -> bool:
    for attempt in range(5):
        try:
            metadata = es.get(index=INDEX_NAME, id="__meta__").get("_source", {})
            if metadata and sidecars_ready(metadata):
                return True
        except Exception:
            pass
        if attempt < 4:
            time.sleep(0.2)
    return False

def document_count() -> int:
    try:
        if not es.indices.exists(index=INDEX_NAME):
            return 0
        return int(es.count(index=INDEX_NAME, query={"exists": {"field": "doc_id"}}).get("count", 0))
    except Exception:
        return 0

def has_index_data() -> bool:
    global _has_index_data
    if _has_index_data:
        return True
    try:
        if es.indices.exists(index=INDEX_NAME):
            _has_index_data = True
            return True
        return False
    except Exception:
        return False
def cached_facet_payload(kind: str, page: int, page_size: int, q: str) -> dict[str, Any]:
    now = time.monotonic()
    key = (serving_generation_token(), kind, page, page_size, q)
    cached = facet_response_cache.get(key)
    if cached and now - cached[0] < FACET_CACHE_TTL_SECONDS:
        return dict(cached[1])
    data = list_facets(kind, page, page_size, q)
    facet_response_cache[key] = (now, data)
    if len(facet_response_cache) > 300:
        oldest_key = min(facet_response_cache, key=lambda item: facet_response_cache[item][0])
        facet_response_cache.pop(oldest_key, None)
    return dict(data)

def cached_sources_payload() -> dict[str, Any]:
    global sources_response_cache, sources_response_cache_generation
    now = time.monotonic()
    generation = serving_generation_token()
    if (sources_response_cache and sources_response_cache_generation == generation
            and now - sources_response_cache[0] < FACET_CACHE_TTL_SECONDS):
        return dict(sources_response_cache[1])
    result = sources_payload()
    result["years"] = [{"name": str(year), "value": year, "count": 0} for year in available_years()]
    sources_response_cache = (now, result)
    sources_response_cache_generation = generation
    return dict(result)

@router.get("/api/facet/{kind}", response_model=None)

def facet(kind: str, page: int = 1, page_size: int = 200, q: str = "") -> JSONResponse | dict[str, Any]:
    if kind not in FACET_FIELDS:
        return JSONResponse({"error": "unknown facet"}, status_code=404)
    status_data = cached_status_payload()
    status = str(status_data.get("index_status") or "unknown")
    if status == "restoring" or not FACET_DB.exists():
        return {"items": [], "total": 0, "page": max(1, page), "page_size": page_size, "has_more": False, "indexing": status != "ready", "index_status": status}
    with serving_lock():
        data = cached_facet_payload(kind, max(1, page), min(max(1, page_size), 500), q.strip())
    data.update({"indexing": not bool(status_data.get("index_ready")), "index_status": status})
    return data
@router.get("/api/sources")

def sources() -> dict[str, Any]:
    status_data = cached_status_payload()
    status = str(status_data.get("index_status") or "unknown")
    if status == "restoring" or not FACET_DB.exists():
        return {"sources": [], "authors": [], "tags": [], "archives": [], "types": [], "years": [], "indexing": status != "ready", "index_status": status}
    with serving_lock():
        result = cached_sources_payload()
    result.update({"indexing": not bool(status_data.get("index_ready")), "index_status": status})
    return result
