from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import runtime, source_files
from .doc_store import get_doc, random_doc, variants_for_title
from .document_identity import content_hash
from .storage_lock import serving_lock


router = APIRouter()


@router.get("/api/preview/{doc_id}")
async def preview(doc_id: str, request: Request):
    if runtime.index_status() == "restoring":
        return JSONResponse({"error": "index restore in progress"}, status_code=503)
    with serving_lock():
        stored = get_doc(doc_id)
    if stored:
        payload = await preview_payload(stored)
        content_hash = str(payload["content_hash"])
        etag = f'"{content_hash}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"})
        first_page = next((file.get("initial_page") or file.get("pages", [None])[0] for file in payload.get("source_files", []) if file.get("initial_page") or file.get("pages")), None)
        headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
        if first_page:
            headers["Link"] = f"<{first_page}>; rel=preload; as=image"
        return JSONResponse(payload, headers=headers)
    return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/api/random")
async def random_preview():
    if runtime.index_status() == "restoring":
        return JSONResponse({"error": "index restore in progress"}, status_code=503)
    with serving_lock():
        stored = random_doc()
    if stored:
        return await preview_payload(stored)
    return JSONResponse({"error": "not found"}, status_code=404)

async def preview_payload(source: dict[str, Any]) -> dict[str, Any]:
    doc_id = str(source.get("doc_id") or "")
    title = source.get("title")
    variants: list[dict[str, Any]] = []
    if title:
        with serving_lock():
            variants = variants_for_title(str(title), doc_id, source.get("publication_name"))
    source_items: list[dict[str, Any]] = []
    source_urls: list[str] = source.get("source_files") or []
    for index, url in enumerate(source_urls):
        source_url = str(url)
        hostname = (urlparse(source_url).hostname or "").lower()
        entry: dict[str, Any] | None = await source_files.source_mirror_entry(source_url) if hostname in {"raw.githubusercontent.com", "github.com"} else None
        page_paths: list[object] = entry.get("page_previews", {}).get("paths", []) if entry else []
        pages = [
            f"{source_files.SOURCE_MIRROR_BASE_URL}/{quote(path, safe='/')}"
            for path in page_paths
            if isinstance(path, str) and path.startswith("archives") and ".." not in Path(path).parts
        ]
        item: dict[str, Any] = {"index": index, "name": Path(source_url.split("?", 1)[0]).name or f"文件 {index + 1}", "url": source_url}
        mirror_path: object = entry.get("path") if entry else None
        if (not source_url.lower().split("?", 1)[0].endswith(".pdf")
                and isinstance(mirror_path, str) and mirror_path.startswith("archives")
                and ".." not in Path(mirror_path).parts):
            item["preview_url"] = f"{source_files.SOURCE_MIRROR_BASE_URL}/{quote(mirror_path, safe='/')}"
        if pages:
            item["page_count"] = len(page_paths)
            article: dict[str, Any] = source.get("article") or {}
            article_page = int(article.get("page_start") or 1)
            requested_numbers = [number for number in (article_page, article_page + 1) if 1 <= number <= len(page_paths)]
            item["pages"] = [
                f"{source_files.SOURCE_MIRROR_BASE_URL}/{quote(path, safe='/')}"
                for number in requested_numbers
                if isinstance((path := page_paths[number - 1]), str) and path.startswith("archives") and ".." not in Path(path).parts
            ]
            item["page_numbers"] = requested_numbers[:len(item["pages"])]
            if item["pages"]:
                item["initial_page"] = item["pages"][0]
                item["initial_page_number"] = item["page_numbers"][0]
        source_items.append(item)
    return {
        "doc_id": doc_id,
        "content_hash": content_hash(source),
        "title": title,
        "publication_name": source.get("publication_name"),
        "publication_author": source.get("publication_author"),
        "publication_type": source.get("publication_type"),
        "source_file_count": int(source.get("source_file_count") or len(source.get("source_files") or [])),
        "date_display": source.get("date_display") or [],
        "tag_names": source.get("tag_names") or [],
        "authors": source.get("authors") or [],
        "content": source.get("content") or "",
        "article_id": source.get("article_id"),
        "publication_id": source.get("publication_id"),
        "archive_id": source.get("archive_id"),
        "article": source.get("article") or {},
        "source_files": source_items,
        "variants": variants,
    }
