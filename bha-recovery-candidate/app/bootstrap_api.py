import hashlib
import json
import threading
from typing import Any

import brotli
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import runtime, search_api
from .search_logic import SearchRequest
from .storage_lock import serving_lock


router = APIRouter()
# Two caches, two payloads: the first screen only needs the default results and
# the source lists. The three facet lists are ~62 KB of the old 85 KB payload
# and are fetched separately once the results are on screen.
bootstrap_cache: tuple[tuple[Any, ...], bytes, bytes] | None = None
facet_cache: tuple[tuple[Any, ...], bytes, bytes] | None = None
bootstrap_lock = threading.Lock()
BOOTSTRAP_BROTLI_QUALITY = 5


def generation_id(generation: tuple[Any, ...]) -> str:
    return hashlib.sha256(repr(generation).encode()).hexdigest()[:16]


def encode_payload(payload: dict[str, Any]) -> tuple[bytes, bytes]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return raw, brotli.compress(raw, quality=BOOTSTRAP_BROTLI_QUALITY)


def encoded_response(raw: bytes, compressed: bytes, accept_encoding: str) -> Response:
    headers = {"Cache-Control": "no-store", "Vary": "Accept-Encoding"}
    if "br" in accept_encoding:
        return Response(
            content=compressed,
            media_type="application/json",
            headers={**headers, "Content-Encoding": "br"},
        )
    return Response(content=raw, media_type="application/json", headers=headers)


def facet_payload() -> dict[str, Any]:
    with serving_lock():
        return {
            kind: runtime.cached_facet_payload(kind, 1, 200, "")
            for kind in ("source", "author", "tag")
        }


def _valid_search_response(response: Response) -> bool:
    # Keep the runtime guard: tests and alternate search implementations may
    # return a different response type even though today's endpoint is JSON.
    return isinstance(response, JSONResponse) and response.status_code == 200


def bootstrap(request: Request | None = None) -> Response:
    global bootstrap_cache
    accept_encoding = (request.headers.get("accept-encoding", "") if request is not None else "")
    with bootstrap_lock:
        generation = runtime.serving_generation_token()
        if runtime.index_status() == "ready" and bootstrap_cache and bootstrap_cache[0] == generation:
            return encoded_response(bootstrap_cache[1], bootstrap_cache[2], accept_encoding)
        if not runtime.serving_generation_ready():
            return JSONResponse({"error": "serving generation is not ready"}, status_code=503)

        for _attempt in range(2):
            generation = runtime.serving_generation_token()
            search_response: Response = search_api.search(SearchRequest())
            if not _valid_search_response(search_response):
                return JSONResponse({"error": "default search is unavailable"}, status_code=503)
            search_payload: object = json.loads(bytes(search_response.body))
            with serving_lock():
                sources = runtime.cached_sources_payload()
            if runtime.index_status() != "ready" or runtime.serving_generation_token() != generation:
                continue
            payload = {
                "generation": generation_id(generation),
                "search": search_payload,
                "sources": sources,
            }
            raw, compressed = encode_payload(payload)
            bootstrap_cache = (generation, raw, compressed)
            return encoded_response(raw, compressed, accept_encoding)

        return JSONResponse({"error": "serving generation changed during bootstrap"}, status_code=503)


@router.get("/api/bootstrap")
def bootstrap_route(request: Request) -> Response:
    return bootstrap(request)


def bootstrap_facets(request: Request | None = None) -> Response:
    global facet_cache
    accept_encoding = (request.headers.get("accept-encoding", "") if request is not None else "")
    with bootstrap_lock:
        generation = runtime.serving_generation_token()
        if runtime.index_status() == "ready" and facet_cache and facet_cache[0] == generation:
            return encoded_response(facet_cache[1], facet_cache[2], accept_encoding)
        if not runtime.serving_generation_ready():
            return JSONResponse({"error": "serving generation is not ready"}, status_code=503)

        for _attempt in range(2):
            generation = runtime.serving_generation_token()
            facets = facet_payload()
            if runtime.index_status() != "ready" or runtime.serving_generation_token() != generation:
                continue
            payload = {"generation": generation_id(generation), "facets": facets}
            raw, compressed = encode_payload(payload)
            facet_cache = (generation, raw, compressed)
            return encoded_response(raw, compressed, accept_encoding)

        return JSONResponse({"error": "serving generation changed during bootstrap"}, status_code=503)


@router.get("/api/bootstrap/facets")
def bootstrap_facets_route(request: Request) -> Response:
    return bootstrap_facets(request)
