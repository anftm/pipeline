import asyncio
import hashlib
import ipaddress
import json
import os
import threading
import time
from collections.abc import Mapping
from typing import NotRequired, TypedDict, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import pipeline_dispatch, proofread_core, source_files
from .document_identity import content_hash


router = APIRouter()
GITHUB_TOKEN = os.environ.get("BHA_PROOFREAD_GITHUB_TOKEN", "")
PIPELINE_REPOSITORY = os.environ.get("BHA_PROOFREAD_PIPELINE_REPOSITORY", "anftm/pipeline")
RATE_LIMIT = 30
RATE_WINDOW_SECONDS = 60 * 60
MAX_PAYLOAD_CHARACTERS = 2_000_000
DISPATCH_MAX_CHARACTERS = 60_000
DISPATCH_MAX_PROPERTIES = 10
BLOB_CHUNK_CHARACTERS = 60_000
CHANGE_PREVIEW_BUDGET_CHARACTERS = 48_000
TAG_TYPES = {"文稿大类", "文稿类型", "地点", "人物", "出版方/发行方", "主题/事件", "记录", "审核", "翻译", "翻印/传抄"}
PART_TYPES = {
    "appellation", "title", "authors", "place", "subtitle", "subtitle2", "subtitle3",
    "subtitle4", "subtitle5", "subdate", "paragraph", "quotation", "signature", "image",
    "image_description",
}
AUTO_MERGE_MAX_DIFF_COST = 500
AUTO_MERGE_MAX_NET_PARAGRAPH_DELTA = 3
AUTO_MERGE_POLICY = {
    "max_diff_cost": AUTO_MERGE_MAX_DIFF_COST,
    "max_net_paragraph_delta": AUTO_MERGE_MAX_NET_PARAGRAPH_DELTA,
}
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


class ArticleDate(TypedDict, total=False):
    year: int
    month: int
    day: int


class Tag(TypedDict):
    name: str
    type: str


class ArticleMetadata(TypedDict, total=False):
    title: str
    authors: list[str]
    dates: list[ArticleDate]
    tags: list[Tag]


class SourceMetadata(TypedDict, total=False):
    name: str
    author: str
    type: str
    files: list[str]


class ProofreadMetadata(TypedDict, total=False):
    article: ArticleMetadata
    source: SourceMetadata


class DispatchState(TypedDict):
    created_at: float
    status: str
    auto_merge: NotRequired[bool]


class ProofreadBody(TypedDict):
    description: str
    patch: NotRequired[proofread_core.ProofreadPatch]
    metadata: NotRequired[ProofreadMetadata]
    locator: NotRequired[dict[str, object]]


def object_dict(value: object) -> dict[object, object] | None:
    return cast(dict[object, object], value) if isinstance(value, dict) else None


proofread_requests: dict[str, list[float]] = {}
proofread_rate_lock = threading.Lock()
proofread_dispatches: dict[str, DispatchState] = {}
proofread_dispatch_lock = threading.Lock()


class ProofreadRequest(BaseModel):
    doc_id: str = Field(min_length=1, max_length=512)
    patch: dict[str, object] | None = None
    metadata: dict[str, object] | None = None
    description: str = Field(default="", max_length=2000)
    base_content_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")


def proofread_rate_error(request: Request) -> JSONResponse | None:
    peer = request.client.host if request.client else "unknown"
    trusted_peer = False
    try:
        peer_address = ipaddress.ip_address(peer)
        trusted_peer = TRUST_PROXY and bool(PROXY_NETWORKS) and any(peer_address in network for network in PROXY_NETWORKS)
    except ValueError:
        pass
    forwarded = request.headers.get("x-forwarded-for", "") if trusted_peer else ""
    candidates = [value.strip() for value in forwarded.split(",") if value.strip()]
    forwarded_client = ""
    if candidates and len(candidates) > PROXY_HOPS:
        candidate = candidates[-PROXY_HOPS - 1]
        try:
            if ipaddress.ip_address(candidate):
                forwarded_client = candidate
        except ValueError:
            pass
    key = forwarded_client or peer
    now = time.monotonic()
    with proofread_rate_lock:
        recent = [value for value in proofread_requests.get(key, []) if now - value < RATE_WINDOW_SECONDS]
        if len(recent) >= RATE_LIMIT:
            return JSONResponse({"error": "proofread rate limit exceeded"}, status_code=429)
        recent.append(now)
        proofread_requests[key] = recent
        if len(proofread_requests) > 10000:
            expired: list[str] = []
            for identity, values in proofread_requests.items():
                values[:] = [value for value in values if now - value < RATE_WINDOW_SECONDS]
                if not values:
                    expired.append(identity)
            for identity in expired:
                proofread_requests.pop(identity, None)
            while len(proofread_requests) > 10000:
                oldest = min(proofread_requests, key=lambda identity: min(proofread_requests[identity]))
                proofread_requests.pop(oldest, None)
    try:
        request.state.proofread_rate_key = key
        request.state.proofread_rate_timestamp = now
    except Exception:
        pass
    return None

def release_proofread_rate(request: Request) -> None:
    try:
        key = request.state.proofread_rate_key
        timestamp = request.state.proofread_rate_timestamp
    except Exception:
        return
    with proofread_rate_lock:
        values = proofread_requests.get(key, [])
        try:
            values.remove(timestamp)
        except ValueError:
            return
        if values:
            proofread_requests[key] = values
        else:
            proofread_requests.pop(key, None)

def clean_string_list(value: object, field: str, limit: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list with at most {limit} entries")
    items = cast(list[object], value)
    if len(items) > limit:
        raise ValueError(f"{field} must be a list with at most {limit} entries")
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"{field} entries must be strings")
        cleaned.append(item.strip())
    if any(not item or len(item) > 300 for item in cleaned):
        raise ValueError(f"{field} contains an invalid entry")
    return cleaned

def clean_metadata(value: Mapping[str, object]) -> ProofreadMetadata:
    allowed_groups = {"article", "source"}
    if set(value) - allowed_groups:
        raise ValueError("metadata contains unsupported groups")
    result: ProofreadMetadata = {}
    if "article" in value:
        article = object_dict(value["article"])
        if article is None or set(article) - {"title", "authors", "dates", "tags"}:
            raise ValueError("article metadata contains unsupported fields")
        cleaned: ArticleMetadata = {}
        if "title" in article:
            raw_title = article["title"]
            if not isinstance(raw_title, str):
                raise ValueError("title is invalid")
            title = raw_title.strip()
            if not title or len(title) > 500:
                raise ValueError("title is invalid")
            cleaned["title"] = title
        if "authors" in article:
            cleaned["authors"] = clean_string_list(article["authors"], "authors", 50)
        if "dates" in article:
            dates = article["dates"]
            if not isinstance(dates, list):
                raise ValueError("dates must be a list with at most 50 entries")
            date_items = cast(list[object], dates)
            if len(date_items) > 50:
                raise ValueError("dates must be a list with at most 50 entries")
            clean_dates: list[ArticleDate] = []
            for raw_date in date_items:
                date = object_dict(raw_date)
                if date is None or set(date) - {"year", "month", "day"}:
                    raise ValueError("date contains unsupported fields")
                for key, minimum, maximum in (("year", -9999, 9999), ("month", 1, 12), ("day", 1, 31)):
                    number = date.get(key)
                    if key in date and (type(number) is not int or not minimum <= number <= maximum):
                        raise ValueError(f"date {key} is invalid")
                if not any(key in date for key in ("year", "month", "day")):
                    raise ValueError("date must contain a value")
                # The key and exact-int checks above establish this JSON shape.
                clean_dates.append(cast(ArticleDate, dict(date)))
            cleaned["dates"] = clean_dates
        if "tags" in article:
            tags = article["tags"]
            if not isinstance(tags, list):
                raise ValueError("tags must be a list with at most 100 entries")
            tag_items = cast(list[object], tags)
            if len(tag_items) > 100:
                raise ValueError("tags must be a list with at most 100 entries")
            clean_tags: list[Tag] = []
            for raw_tag in tag_items:
                tag = object_dict(raw_tag)
                if tag is None or set(tag) != {"name", "type"}:
                    raise ValueError("each tag requires name and type")
                name, kind = tag["name"], tag["type"]
                if not isinstance(name, str) or not isinstance(kind, str):
                    raise ValueError("tag name and type must be strings")
                if not name.strip() or not kind.strip():
                    raise ValueError("tag name and type cannot be empty")
                if kind.strip() not in TAG_TYPES:
                    raise ValueError("tag type is invalid")
                clean_tags.append({"name": name.strip(), "type": kind.strip()})
            cleaned["tags"] = clean_tags
        result["article"] = cleaned
    if "source" in value:
        source = object_dict(value["source"])
        if source is None or set(source) - {"name", "author", "type", "files"}:
            raise ValueError("source metadata contains unsupported fields")
        source_cleaned: SourceMetadata = {}
        for key in ("name", "author", "type"):
            if key in source:
                raw_text = source[key]
                if not isinstance(raw_text, str):
                    raise ValueError(f"source {key} is invalid")
                text = raw_text.strip()
                if len(text) > 500 or (key in {"name", "type"} and not text):
                    raise ValueError(f"source {key} is invalid")
                if key == "name":
                    source_cleaned["name"] = text
                elif key == "author":
                    source_cleaned["author"] = text
                else:
                    source_cleaned["type"] = text
        if "files" in source:
            files = clean_string_list(source["files"], "source files", source_files.MAX_SOURCE_FILES)
            if any(source_files.validate_source_url(url) for url in files):
                raise ValueError("source files contain an invalid URL")
            source_cleaned["files"] = files
        result["source"] = source_cleaned
    if not any(result.values()):
        raise ValueError("metadata contains no changes")
    return result

def metadata_is_safe_to_merge(metadata: ProofreadMetadata | None, _article: Mapping[str, object]) -> bool:
    return metadata is None

async def dispatch_proofread(payload: dict[str, object]) -> None:
    await pipeline_dispatch.dispatch(
        payload, "submit-proofread", GITHUB_TOKEN, PIPELINE_REPOSITORY,
        DISPATCH_MAX_CHARACTERS, DISPATCH_MAX_PROPERTIES,
        BLOB_CHUNK_CHARACTERS,
    )

def proofread_request_id(doc_id: str, patch: proofread_core.ProofreadPatch | None, metadata: ProofreadMetadata | None, description: str, base_content_hash: str) -> str:
    value = {
        "doc_id": doc_id,
        "patch": patch,
        "metadata": metadata,
        "description": description,
        "base_content_hash": base_content_hash,
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def duplicate_proofread_response(request_id: str) -> JSONResponse | None:
    now = time.time()
    with proofread_dispatch_lock:
        expired = [key for key, item in proofread_dispatches.items()
                   if item["status"] == "accepted" and now - float(item["created_at"]) > 86400]
        for key in expired:
            proofread_dispatches.pop(key, None)
        existing = proofread_dispatches.get(request_id)
        if existing is None:
            if len(proofread_dispatches) >= 1000:
                return JSONResponse({"error": "暂时无法安全接收更多请求，请保留草稿。"}, status_code=503)
            proofread_dispatches[request_id] = {"created_at": now, "status": "processing"}
            return None
        if existing["status"] != "accepted":
            return JSONResponse({"error": "该请求结果尚未确认，请保留草稿并勿重复提交。",
                                 "status": existing["status"], "request_id": request_id}, status_code=409)
        return JSONResponse({
            "ok": True,
            "status": "duplicate",
            "original_status": existing["status"],
            "request_id": request_id,
            "tasks": 1,
            "auto_merge": existing.get("auto_merge", False),
        }, status_code=200)

def prepare_proofread_payload(body: ProofreadRequest, source: dict[str, object]) -> JSONResponse | dict[str, object]:
    """Validate and normalize against one source observation; perform no writes.

    Callers must supply the source for the requested document, control lookup
    and rate limiting, and independently protect delivery and later changes.
    """
    if len(json.dumps(body.model_dump(), ensure_ascii=False)) > MAX_PAYLOAD_CHARACTERS:
        return JSONResponse({"error": "proofread payload is too large"}, status_code=413)
    if body.base_content_hash is None:
        return JSONResponse({"error": "缺少校订预览基线，请保留草稿并重新核对原文。"}, status_code=409)
    if source.get("doc_id") != body.doc_id:
        return JSONResponse({"error": "document identity does not match"}, status_code=409)
    current_content_hash = content_hash(source)
    if body.base_content_hash != current_content_hash:
        return JSONResponse({"error": "document changed since preview", "content_hash": current_content_hash}, status_code=409)
    body_fields: ProofreadBody = {"description": body.description}
    payload: dict[str, object] = {
        "doc_id": body.doc_id,
        "archive_id": source.get("archive_id"),
        "article_id": source.get("article_id"),
        "publication_id": source.get("publication_id"),
        "title": f"校订 {source.get('title') or source.get('article_id')}",
        "kind": "proofread",
        "body": body_fields,
    }
    article_value: object = source.get("article") or {}
    raw_article = object_dict(article_value) or {}
    article = {key: value for key, value in raw_article.items() if isinstance(key, str)}
    if body.patch:
        try:
            normalized_patch = proofread_core.validate_proofread_patch(
                body.patch, article, PART_TYPES,
            )
            body_fields["patch"] = normalized_patch
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    if body.metadata:
        try:
            metadata = clean_metadata(body.metadata)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        body_fields["metadata"] = metadata
        body_fields["locator"] = {
            "title": article.get("title"),
            "authors": article.get("authors") or [],
            "dates": article.get("dates") or [],
            "is_range_date": bool(article.get("is_range_date")),
            "page_start": article.get("page_start"),
            "page_end": article.get("page_end"),
        }
    if "patch" not in body_fields and "metadata" not in body_fields:
        return JSONResponse({"error": "no proofreading changes supplied"}, status_code=400)
    request_id = proofread_request_id(body.doc_id, body_fields.get("patch"), body_fields.get("metadata"), body.description, current_content_hash)
    payload["request_id"] = request_id
    payload["base_content_hash"] = current_content_hash
    payload["changed"] = proofread_core.proofread_change_preview(
        body_fields.get("patch"), article, body_fields.get("metadata"), source,
    )
    if not payload["changed"]:
        return JSONResponse({"error": "no effective proofreading changes supplied"}, status_code=400)
    archive = source.get("archive_id")
    article_id = source.get("article_id")
    publication_id = source.get("publication_id")
    if (type(archive) is not int or not 0 <= archive <= 31
            or any(not isinstance(value, str) or not value
                   or not all(char.isalnum() or char in "._-" for char in value)
                   for value in (article_id, publication_id))):
        return JSONResponse({"error": "proofread source identity is invalid"}, status_code=409)
    if len(json.dumps(payload, ensure_ascii=False)) > CHANGE_PREVIEW_BUDGET_CHARACTERS:
        payload["changed"] = proofread_core.proofread_change_preview(
            body_fields.get("patch"), article, body_fields.get("metadata"), source,
            clip=2000,
        )
    return payload


@router.post("/api/proofread", status_code=202, response_model=None)
async def submit_proofread(body: ProofreadRequest, request: Request) -> JSONResponse | dict[str, object]:
    # Fail closed while the central Actions receiver is paused. A GitHub
    # dispatch 204 is not evidence that a disabled workflow accepted the work.
    if os.environ.get("BHA_PIPELINE_SUBMISSIONS_PAUSED", "1") != "0":
        return JSONResponse({"error": "校订提交暂不可用，请保留草稿并稍后重试。"}, status_code=503)
    if not GITHUB_TOKEN:
        return JSONResponse({"error": "proofreading is not configured"}, status_code=503)
    # These checks precede lookup as before; the pure preparer repeats them
    # so an independent caller cannot accidentally skip the baseline check.
    if len(json.dumps(body.model_dump(), ensure_ascii=False)) > MAX_PAYLOAD_CHARACTERS:
        return JSONResponse({"error": "proofread payload is too large"}, status_code=413)
    if body.base_content_hash is None:
        return JSONResponse({"error": "缺少校订预览基线，请保留草稿并重新核对原文。"}, status_code=409)
    source = await asyncio.to_thread(source_files.get_source_doc, body.doc_id)
    if not source:
        return JSONResponse({"error": "document not found"}, status_code=404)
    payload = prepare_proofread_payload(body, source)
    if isinstance(payload, JSONResponse):
        return payload
    request_id = cast(str, payload["request_id"])
    if duplicate := duplicate_proofread_response(request_id):
        return duplicate
    if error := proofread_rate_error(request):
        with proofread_dispatch_lock:
            proofread_dispatches.pop(request_id, None)
        return error
    # The recovery candidate routes all proofreading PRs to human review.
    # Do not ask even an older worker to merge or delete a branch automatically.
    try:
        await dispatch_proofread(payload)
    except Exception:
        with proofread_dispatch_lock:
            proofread_dispatches[request_id] = {"created_at": time.time(), "status": "uncertain"}
        return JSONResponse({"error": "提交结果未确认，请保留草稿并勿重复提交。", "request_id": request_id}, status_code=503)
    auto_merge = False
    response: dict[str, object] = {"ok": True, "status": "accepted", "request_id": request_id, "tasks": 1, "auto_merge": auto_merge}
    with proofread_dispatch_lock:
        proofread_dispatches[request_id] = {"created_at": time.time(), "status": "accepted", "auto_merge": auto_merge}
    return response
