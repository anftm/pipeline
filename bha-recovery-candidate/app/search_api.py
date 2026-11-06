from typing import Any, Mapping, cast

from elasticsearch.exceptions import BadRequestError
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import runtime, search_logic
from .config import INDEX_NAME
from .search_logic import SearchRequest
from .search_store import SearchStoreError, get_search_docs, literal_match_ids
from .storage_lock import serving_lock


router = APIRouter()
MAX_RESULT_WINDOW = 500000
LITERAL_CANDIDATE_BATCH_SIZE = 5000
LITERAL_ROW_ORDER_MIN_CANDIDATES = 10000
LITERAL_ROW_BUFFER_MAX_IDS = 250000
TOTAL_REUSE_MIN_HITS = 10000


class IncompleteSearchError(RuntimeError):
    pass


def complete_search(**kwargs: Any) -> Mapping[str, Any]:
    data = runtime.es.search(**kwargs)
    if data.get("timed_out") or data.get("terminated_early") or data.get("_shards", {}).get("failed", 0):
        raise IncompleteSearchError("Elasticsearch returned partial results")
    if kwargs.get("track_total_hits"):
        total = data.get("hits", {}).get("total")
        if total is None or (isinstance(total, dict) and cast(dict[str, Any], total).get("relation", "eq") != "eq"):
            raise IncompleteSearchError("Elasticsearch did not return an exact total")
    return {key: data[key] for key in data}


def literal_sort(sort: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    values = list(sort or [{"_score": {"order": "desc"}}])
    values.append({"doc_id": {"order": "asc"}})
    return values


def verified_literal_ids(search_kwargs: dict[str, Any], query: str) -> list[str]:
    candidate_kwargs = {
        **search_kwargs,
        "from_": 0,
        "size": LITERAL_CANDIDATE_BATCH_SIZE,
        "sort": literal_sort(search_kwargs.get("sort")),
    }
    verified: list[str] = []
    seen = 0
    total = None
    buffered_ids: list[str] | None = None
    while total is None or seen < total:
        data = complete_search(**candidate_kwargs)
        hits: list[dict[str, Any]] = data.get("hits", {}).get("hits", [])
        if total is None:
            total_payload = data.get("hits", {}).get("total", {})
            total = total_payload if isinstance(total_payload, int) else int(total_payload.get("value", 0))
            if LITERAL_ROW_ORDER_MIN_CANDIDATES <= total <= LITERAL_ROW_BUFFER_MAX_IDS:
                buffered_ids = []
        if not hits:
            if seen < total:
                raise IncompleteSearchError("Literal candidate scan ended before its total")
            break
        candidate_ids = [doc_id for hit in hits if isinstance((doc_id := hit.get("_id")), str) and doc_id]
        if len(candidate_ids) != len(hits):
            raise IncompleteSearchError("Literal candidate is missing its document ID")
        if buffered_ids is not None:
            if len(buffered_ids) + len(candidate_ids) > LITERAL_ROW_BUFFER_MAX_IDS:
                # Fall back to complete streaming verification if the count changes.
                for offset in range(0, len(buffered_ids), LITERAL_CANDIDATE_BATCH_SIZE):
                    batch = buffered_ids[offset:offset + LITERAL_CANDIDATE_BATCH_SIZE]
                    literal_ids = literal_match_ids(batch, query)
                    verified.extend(doc_id for doc_id in batch if doc_id in literal_ids)
                buffered_ids = None
            else:
                buffered_ids.extend(candidate_ids)
        if buffered_ids is None:
            literal_ids = literal_match_ids(candidate_ids, query)
            verified.extend(doc_id for doc_id in candidate_ids if doc_id in literal_ids)
        seen += len(hits)
        sort_values = hits[-1].get("sort")
        if seen >= total:
            break
        if not sort_values or sort_values == candidate_kwargs.get("search_after"):
            raise IncompleteSearchError("Literal candidate scan cannot advance")
        candidate_kwargs.pop("from_", None)
        candidate_kwargs["search_after"] = sort_values
        candidate_kwargs["track_total_hits"] = False
    if buffered_ids is not None:
        literal_ids = literal_match_ids(buffered_ids, query, row_order=True)
        verified = [doc_id for doc_id in buffered_ids if doc_id in literal_ids]
    return verified


def request_literal_ids(body: SearchRequest, search_kwargs: dict[str, Any], ready: bool) -> tuple[str, ...]:
    if not ready:
        return tuple(verified_literal_ids(search_kwargs, body.q.strip()))
    key = runtime.literal_cache_key(body)
    inflight_key = ("literal", key)
    while True:
        cached = runtime.cached_literal_ids(key)
        if cached is not None:
            return cached
        leader, event = runtime.begin_search(inflight_key)
        if leader:
            break
        event.wait()
    try:
        cached = runtime.cached_literal_ids(key)
        if cached is not None:
            return cached
        verified = tuple(verified_literal_ids(search_kwargs, body.q.strip()))
        runtime.store_literal_ids(key, list(verified))
        return verified
    finally:
        runtime.finish_search(inflight_key, event)


@router.post("/api/search")

def api_search(body: SearchRequest):
    return search(body, normalize_exact=True)


def search(body: SearchRequest, *, normalize_exact: bool = False):
    # The HTTP layer keeps the request/response contract and the UI toggle
    # unchanged, but runs punctuation queries through the normal (fuzzy) path.
    # Literal verification for those queries would otherwise scan every ES
    # candidate against SQLite. Internal callers keep their requested mode.
    if normalize_exact:
        body = body.model_copy(update={"exact": search_logic.effective_exact(body.q, body.exact)})
    status = runtime.index_status()
    if status == "unknown":
        status = str(runtime.cached_status_payload().get("index_status") or status)
    has_data = status not in {"switching", "restoring", "failed"} and runtime.has_index_data()
    if not has_data:
        return JSONResponse({"results": [], "total": 0, "page": max(1, body.page), "page_size": body.page_size, "indexing": status != "ready", "index_status": status})
    page = max(1, body.page)
    page_size = min(max(1, body.page_size), 100)
    if (page - 1) * page_size >= MAX_RESULT_WINDOW:
        return JSONResponse({"error": "result window exceeded", "max_result_window": MAX_RESULT_WINDOW}, status_code=400)
    cache_key = runtime.search_cache_key(body, page, page_size)
    if status == "ready":
        cached = runtime.cached_search_response(cache_key)
        if cached is not None:
            return JSONResponse(cached)
    search_kwargs: dict[str, Any] = {
        "index": INDEX_NAME,
        "from_": (page - 1) * page_size,
        "size": page_size,
        "query": search_logic.build_query(body),
        "sort": search_logic.sort_clause(body.sort),
        "track_total_hits": True,
        "allow_partial_search_results": False,
        "source": False,
    }
    if not body.q.strip() and body.sort == "relevance":
        search_kwargs["sort"] = [{"doc_id": {"order": "asc"}}]
    inflight_event = None
    if status == "ready":
        while True:
            cached = runtime.cached_search_response(cache_key)
            if cached is not None:
                return JSONResponse(cached)
            leader, event = runtime.begin_search(cache_key)
            if leader:
                inflight_event = event
                break
            event.wait()
    cacheable_response = status == "ready"
    try:
        with serving_lock():
            if search_logic.requires_literal_verification(body.q, body.exact):
                verified_ids = request_literal_ids(body, search_kwargs, status == "ready")
                total_value = len(verified_ids)
                start = (page - 1) * page_size
                hits = [{"_id": doc_id} for doc_id in verified_ids[start:start + page_size]]
            else:
                cached_total = None
                cacheable_response = status == "ready" and runtime.index_status() == "ready" and cache_key[0] == runtime.serving_generation_token()
                if cacheable_response:
                    cached_total = runtime.cached_search_total(cache_key)
                reuse_total = cached_total is not None and cached_total >= TOTAL_REUSE_MIN_HITS
                if reuse_total:
                    # Ready serving generations are immutable; the count is already exact.
                    search_kwargs["track_total_hits"] = False
                data = complete_search(**search_kwargs)
                total = data.get("hits", {}).get("total", {})
                total_value = cached_total if reuse_total else (total if isinstance(total, int) else total.get("value", 0))
                hits = data.get("hits", {}).get("hits", [])
            hit_ids = [doc_id for hit in hits if isinstance((doc_id := hit.get("_id")), str) and doc_id]
            stored_docs = get_search_docs(hit_ids, body.q, body.fulltext, body.exact)
            if len(stored_docs) != len(hit_ids):
                raise SearchStoreError("search sidecar generation does not contain every Elasticsearch hit")
        result = {"results": [search_logic.trim_hit(hit, stored_docs[doc_id], body.q, body.fulltext, body.exact)
                              for hit in hits if isinstance((doc_id := hit.get("_id")), str) and stored_docs.get(doc_id)],
                  "total": total_value, "page": page, "page_size": page_size, "indexing": status != "ready", "index_status": status}
        if cacheable_response:
            runtime.store_search_response(cache_key, result)
        return JSONResponse(result)
    except BadRequestError as exc:
        return JSONResponse({"error": "invalid search query", "detail": str(exc)}, status_code=400)
    except SearchStoreError:
        return JSONResponse({"error": "search storage unavailable"}, status_code=503)
    except IncompleteSearchError:
        return JSONResponse({"error": "search results incomplete"}, status_code=503)
    finally:
        if inflight_event is not None:
            runtime.finish_search(cache_key, inflight_event)
