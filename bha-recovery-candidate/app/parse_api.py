import hashlib
import ipaddress
import json
import os
import re
import threading
import time
from typing import Any, cast
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import parse_core, pipeline_dispatch, source_files
from .doc_store import parse_queue_docs
from .document_identity import content_hash
from .parse_core import ParseRequest
from .storage_lock import serving_lock


router = APIRouter()
GITHUB_TOKEN = os.environ.get("BHA_PROOFREAD_GITHUB_TOKEN", "")
PIPELINE_REPOSITORY = os.environ.get("BHA_PROOFREAD_PIPELINE_REPOSITORY", "anftm/pipeline")
PARSE_ARCHIVE_ID = 25
PARSE_QUEUE_PREFIX = "【文章待拆分】"
PARSE_OCR_BASE_URL = os.environ.get(
    "BHA_PARSE_OCR_BASE_URL",
    "https://raw.githubusercontent.com/banned-historical-archives/banned-historical-archives25/ocr_cache",
).rstrip("/")
PARSE_OCR_MAX_BYTES = 2 * 1024 * 1024
PARSE_MAX_PAYLOAD_CHARACTERS = 2_000_000
PARSE_RATE_LIMIT = 30
PARSE_RATE_WINDOW_SECONDS = 60 * 60
TRUST_PROXY = os.environ.get("BHA_TRUST_PROXY", "1" if os.environ.get("SPACE_ID") else "0") == "1"
def proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    try:
        return tuple(
            ipaddress.ip_network(value.strip())
            for value in os.environ.get("BHA_TRUST_PROXY_CIDRS", "").split(",")
            if value.strip()
        )
    except ValueError:
        return ()


def proxy_hops() -> int:
    try:
        return max(1, int(os.environ.get("BHA_TRUST_PROXY_HOPS", "1")))
    except ValueError:
        return 1


PROXY_NETWORKS = proxy_networks()
PROXY_HOPS = proxy_hops()
parse_requests: dict[str, list[float]] = {}
parse_rate_lock = threading.Lock()
parse_dispatches: dict[str, dict[str, Any]] = {}
parse_dispatch_lock = threading.Lock()


async def dispatch_parse(payload: dict[str, Any]) -> None:
    await pipeline_dispatch.dispatch(payload, "submit-parse", GITHUB_TOKEN, PIPELINE_REPOSITORY)


def parse_client_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    trusted_peer = False
    try:
        peer_address = ipaddress.ip_address(peer)
        trusted_peer = TRUST_PROXY and bool(PROXY_NETWORKS) and any(peer_address in network for network in PROXY_NETWORKS)
    except ValueError:
        pass
    forwarded = request.headers.get("x-forwarded-for", "") if trusted_peer else ""
    candidates = [value.strip() for value in forwarded.split(",") if value.strip()]
    if candidates and len(candidates) > PROXY_HOPS:
        candidate = candidates[-PROXY_HOPS - 1]
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    return peer


def parse_rate_error(request: Request) -> JSONResponse | None:
    key = parse_client_key(request)
    now = time.monotonic()
    with parse_rate_lock:
        recent = [value for value in parse_requests.get(key, []) if now - value < PARSE_RATE_WINDOW_SECONDS]
        if len(recent) >= PARSE_RATE_LIMIT:
            return JSONResponse({"error": "parse rate limit exceeded"}, status_code=429)
        recent.append(now)
        parse_requests[key] = recent
        if len(parse_requests) > 10000:
            for identity in list(parse_requests):
                parse_requests[identity] = [value for value in parse_requests[identity] if now - value < PARSE_RATE_WINDOW_SECONDS]
                if not parse_requests[identity]:
                    parse_requests.pop(identity, None)
            while len(parse_requests) > 10000:
                oldest = min(parse_requests, key=lambda identity: min(parse_requests[identity]))
                parse_requests.pop(oldest, None)
    request.state.parse_rate_key = key
    request.state.parse_rate_timestamp = now
    return None


def release_parse_rate(request: Request) -> None:
    key = getattr(request.state, "parse_rate_key", None)
    timestamp = getattr(request.state, "parse_rate_timestamp", None)
    if key is None or timestamp is None:
        return
    with parse_rate_lock:
        values = parse_requests.get(key, [])
        if timestamp in values:
            values.remove(timestamp)
        if values:
            parse_requests[key] = values
        else:
            parse_requests.pop(key, None)


def duplicate_parse_response(request_id: str) -> JSONResponse | None:
    now = time.time()
    with parse_dispatch_lock:
        for key, value in list(parse_dispatches.items()):
            if value["status"] == "accepted" and now - float(value["created_at"]) > 86400:
                parse_dispatches.pop(key, None)
        existing = parse_dispatches.get(request_id)
        if existing is not None:
            if existing["status"] != "accepted":
                return JSONResponse({"error": "该请求结果尚未确认，请保留草稿并勿重复提交。",
                                     "status": existing["status"], "request_id": request_id}, status_code=409)
            return JSONResponse({
                "ok": True,
                "status": "duplicate",
                "original_status": existing["status"],
                "request_id": request_id,
                "tasks": 1,
            })
        if len(parse_dispatches) >= 1000:
            return JSONResponse({"error": "暂时无法安全接收更多请求，请保留草稿。"}, status_code=503)
        parse_dispatches[request_id] = {"created_at": now, "status": "processing"}
    return None


def parse_queue_source(doc_id: str) -> dict[str, Any] | None:
    source = source_files.get_source_doc(doc_id)
    if not parse_core.is_queue_source(source, PARSE_ARCHIVE_ID, PARSE_QUEUE_PREFIX):
        return None
    return source

@router.get("/api/parse/queue")
async def parse_queue(page: int = 1, page_size: int = 20):
    if page < 1 or page_size < 1 or page_size > 100:
        return JSONResponse({"error": "invalid pagination"}, status_code=400)
    with serving_lock():
        total, sources = parse_queue_docs(PARSE_ARCHIVE_ID, PARSE_QUEUE_PREFIX, page, page_size)
    return {
        "archive_id": PARSE_ARCHIVE_ID,
        "prefix": PARSE_QUEUE_PREFIX,
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [parse_core.queue_item(source) for source in sources],
    }

@router.get("/api/parse/{doc_id}/ocr")
async def parse_ocr_page(doc_id: str, page: int = 1):
    source = parse_queue_source(doc_id)
    if not source:
        return JSONResponse({"error": "parse item not found"}, status_code=404)
    article: dict[str, Any] = source.get("article") or {}
    page_start = int(article.get("page_start") or 0)
    page_end = int(article.get("page_end") or 0)
    if page < page_start or page > page_end:
        return JSONResponse({"error": "page is outside the source range"}, status_code=404)
    publication_id = str(source.get("publication_id") or "")
    if not re.fullmatch(r"[0-9a-f-]{36}", publication_id, re.IGNORECASE):
        return JSONResponse({"error": "invalid publication id"}, status_code=500)
    url = f"{PARSE_OCR_BASE_URL}/{quote(publication_id, safe='')}/{page}.json"
    try:
        response = await source_files.source_client.get(url, follow_redirects=True)
        response.raise_for_status()
        if len(response.content) > PARSE_OCR_MAX_BYTES:
            return JSONResponse({"error": "OCR page is too large"}, status_code=502)
        payload = response.json()
    except Exception as exc:
        return JSONResponse({"error": f"OCR page unavailable: {exc}"}, status_code=502)
    if not isinstance(payload, dict) or not isinstance(cast(dict[str, object], payload).get("ocr_results"), list):
        return JSONResponse({"error": "OCR page is invalid"}, status_code=502)
    return JSONResponse({"publication_id": publication_id, "page": page, **payload}, headers={
        "Cache-Control": "public, max-age=300",
    })

def prepare_parse_payload(body: ParseRequest, source: dict[str, Any]) -> JSONResponse | dict[str, Any]:
    """Validate and normalize against one source observation; perform no writes.

    This observation is not a lock or a durable delivery acknowledgement.
    """
    if len(json.dumps(body.model_dump(), ensure_ascii=False)) > PARSE_MAX_PAYLOAD_CHARACTERS:
        return JSONResponse({"error": "parse payload is too large"}, status_code=413)
    if body.base_content_hash is None:
        return JSONResponse({"error": "缺少拆分预览基线，请保留草稿并重新核对原文。"}, status_code=409)
    if source.get("doc_id") != body.doc_id:
        return JSONResponse({"error": "parse item identity does not match"}, status_code=409)
    if (type(source.get("archive_id")) is not int
            or not parse_core.is_queue_source(source, PARSE_ARCHIVE_ID, PARSE_QUEUE_PREFIX)):
        return JSONResponse({"error": "parse item not found or already resolved"}, status_code=404)
    current_hash = content_hash(source)
    if body.base_content_hash != current_hash:
        return JSONResponse({"error": "parse item changed since preview", "content_hash": current_hash}, status_code=409)
    articles, error = parse_core.validate_articles(body.articles, source, PARSE_QUEUE_PREFIX)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    # Match the existing central worker's required source identity/files
    # before dispatch; a locally accepted but unprocessable parse loses time
    # and cannot be safely replayed. No external lookup or writes occur here.
    publication_id = source.get("publication_id")
    files = source.get("source_files")
    if not isinstance(publication_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", publication_id):
        return JSONResponse({"error": "parse source identity is invalid"}, status_code=409)
    if (not isinstance(files, list) or not 1 <= len(cast(list[object], files)) <= 1000
            or any(not isinstance(value, str) or not value or len(value) > 4096 for value in cast(list[object], files))):
        return JSONResponse({"error": "parse source files are invalid"}, status_code=409)
    article: dict[str, Any] = source.get("article") or {}
    payload: dict[str, Any] = {
        "doc_id": body.doc_id,
        "archive_id": source.get("archive_id"),
        "publication_id": source.get("publication_id"),
        # Pass the same baseline as proofreading so the receiving worker can
        # detect a changed preview before attempting any downstream write.
        "base_content_hash": current_hash,
        "kind": "parse",
        "body": {
            "articles": articles,
            "locator": {
                "title": article.get("title"),
                "page_start": article.get("page_start"),
                "page_end": article.get("page_end"),
            },
            "source_files": source.get("source_files") or [],
            "description": body.description,
        },
    }
    request_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    payload["request_id"] = request_id
    return payload


@router.post("/api/parse", status_code=202)
async def submit_parse(body: ParseRequest, request: Request):
    # Keep the submitted draft local rather than falsely accepting an event
    # that the disabled central workflow cannot process.
    if os.environ.get("BHA_PIPELINE_SUBMISSIONS_PAUSED", "1") != "0":
        return JSONResponse({"error": "文章拆分提交暂不可用，请保留草稿并稍后重试。"}, status_code=503)
    if not GITHUB_TOKEN:
        return JSONResponse({"error": "parsing is not configured"}, status_code=503)
    if len(json.dumps(body.model_dump(), ensure_ascii=False)) > PARSE_MAX_PAYLOAD_CHARACTERS:
        return JSONResponse({"error": "parse payload is too large"}, status_code=413)
    if body.base_content_hash is None:
        return JSONResponse({"error": "缺少拆分预览基线，请保留草稿并重新核对原文。"}, status_code=409)
    source = parse_queue_source(body.doc_id)
    if not source:
        return JSONResponse({"error": "parse item not found or already resolved"}, status_code=404)
    payload = prepare_parse_payload(body, source)
    if isinstance(payload, JSONResponse):
        return payload
    request_id = payload["request_id"]
    if duplicate := duplicate_parse_response(request_id):
        return duplicate
    if error := parse_rate_error(request):
        with parse_dispatch_lock:
            parse_dispatches.pop(request_id, None)
        return error
    try:
        await dispatch_parse(payload)
    except Exception:
        with parse_dispatch_lock:
            parse_dispatches[request_id] = {"created_at": time.time(), "status": "uncertain"}
        return JSONResponse({"error": "提交结果未确认，请保留草稿并勿重复提交。", "request_id": request_id}, status_code=503)
    with parse_dispatch_lock:
        parse_dispatches[request_id] = {"created_at": time.time(), "status": "accepted"}
    return {"ok": True, "status": "accepted", "request_id": request_id, "tasks": 1}
