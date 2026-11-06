import asyncio
import ipaddress
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse
from zipfile import ZIP_DEFLATED, ZipFile

import httpx2 as httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .doc_store import get_doc
from .storage_lock import serving_lock


router = APIRouter()
SOURCE_MIRROR_MANIFEST_URL = os.environ.get(
    "BHA_SOURCE_MIRROR_MANIFEST_URL",
    "https://huggingface.co/datasets/vomebook/BHA-Source-Files/resolve/main/manifest.json",
)
SOURCE_MIRROR_BASE_URL = os.environ.get(
    "BHA_SOURCE_MIRROR_BASE_URL",
    "https://huggingface.co/datasets/vomebook/BHA-Source-Files/resolve/main",
).rstrip("/")
SOURCE_MIRROR_TTL_SECONDS = int(os.environ.get("BHA_SOURCE_MIRROR_TTL_SECONDS", "86400"))
MAX_SOURCE_REDIRECTS = 5
MAX_SOURCE_FILES = 100
MAX_SOURCE_ZIP_BYTES = 512 * 1024 * 1024
INLINE_SOURCE_MEDIA_TYPES = {
    "application/pdf", "image/bmp", "image/gif", "image/jpeg", "image/png",
    "image/tiff", "image/webp",
}
SOURCE_INLINE_CACHE_CONTROL = "public, max-age=86400, s-maxage=86400"
source_client = httpx.AsyncClient(
    follow_redirects=False,
    trust_env=False,
    timeout=httpx.Timeout(120, connect=10, pool=10),
    limits=httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=600),
)
source_mirror_manifest: dict[str, Any] | None = None
source_mirror_loaded_at = 0.0
source_mirror_etag: str | None = None
source_mirror_refresh_task: asyncio.Task[None] | None = None
source_mirror_lock = asyncio.Lock()


async def source_page_url(doc_id: str, file: int, page: int) -> str | None:
    source = await asyncio.to_thread(get_source_doc, doc_id)
    if not source:
        return None
    files: list[object] = source.get("source_files") or []
    if file < 0 or file >= len(files) or page < 1:
        return None
    entry = await source_mirror_entry(str(files[file]))
    paths: list[object] = entry.get("page_previews", {}).get("paths", []) if entry else []
    if page > len(paths):
        return None
    path = paths[page - 1]
    if not isinstance(path, str) or not path.startswith("archives") or ".." in Path(path).parts:
        return None
    return f"{SOURCE_MIRROR_BASE_URL}/{quote(path, safe='/')}"


@router.get("/api/preview/source-page/{doc_id}")
async def preview_source_page(doc_id: str, file: int = 0, page: int = 1):
    url = await source_page_url(doc_id, file, page)
    if not url:
        return JSONResponse({"error": "source page not available"}, status_code=404)
    try:
        resp = await open_mirrored_response(url)
        if resp.status_code >= 400:
            await resp.aclose()
            return JSONResponse({"error": f"upstream HTTP {resp.status_code}"}, status_code=502)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    async def stream():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    media_type = source_media_type(url, resp.headers.get("content-type"))
    headers = {
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": SOURCE_INLINE_CACHE_CONTROL,
    }
    for header in ("accept-ranges", "content-length"):
        value = resp.headers.get(header)
        if value:
            headers[header.title()] = value
    return StreamingResponse(stream(), status_code=resp.status_code, media_type=media_type, headers=headers)

@router.get("/api/preview/source/{doc_id}")
async def preview_source(request: Request, doc_id: str, file: int = 0):
    return await download_source(request, doc_id, file, True)


def safe_filename(name: str, extension: str) -> str:
    cleaned = "".join(ch if ch not in '\\/:*?"<>|\r\n\t' else "_" for ch in name).strip(" ._")
    if not cleaned:
        cleaned = "download"
    if not cleaned.lower().endswith(extension.lower()):
        cleaned += extension
    return cleaned

def content_disposition(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"

def source_filename_base(source: dict[str, Any], fallback: str) -> str:
    return str(source.get("publication_name") or source.get("publication_id") or fallback)

def get_source_doc(doc_id: str) -> dict[str, Any] | None:
    with serving_lock():
        return get_doc(doc_id)

def validate_source_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "source URL must be http or https"
    host = parsed.hostname.strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        return "source URL host is not allowed"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_global:
        return "source URL IP is not allowed"
    return None

def source_media_type(url: str, content_type: str | None) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type and media_type != "application/octet-stream":
        return media_type
    suffix = Path(urlparse(url).path).suffix.lower()
    return {
        ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".bmp": "image/bmp",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp",
    }.get(suffix, media_type or "application/octet-stream")

async def validate_source_target(url: str) -> str | None:
    if error := validate_source_url(url):
        return error
    host = urlparse(url).hostname
    if not host:
        return "source URL host is not allowed"
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except OSError:
        return "source URL host could not be resolved"
    for entry in addresses:
        address = ipaddress.ip_address(entry[4][0])
        if not address.is_global:
            return "source URL resolves to a disallowed IP"
    return None

async def open_source_response(url: str, request_headers: dict[str, str] | None = None):
    current_url = url
    for _ in range(MAX_SOURCE_REDIRECTS + 1):
        if error := await validate_source_target(current_url):
            raise ValueError(error)
        request = source_client.build_request("GET", current_url, headers=request_headers or {})
        response = await source_client.send(request, stream=True, follow_redirects=False)
        network_stream = response.extensions.get("network_stream")
        peer = network_stream.get_extra_info("server_addr") if network_stream else None
        try:
            peer_address = ipaddress.ip_address(peer[0]) if peer else None
        except (ValueError, TypeError):
            peer_address = None
        if peer_address is None or not peer_address.is_global:
            await response.aclose()
            raise ValueError("source connection used a disallowed IP")
        if response.is_redirect:
            location = response.headers.get("location")
            next_request = response.next_request
            await response.aclose()
            if not location or next_request is None:
                raise ValueError("invalid source redirect")
            current_url = str(next_request.url)
            continue
        return response
    raise ValueError("too many source redirects")


async def refresh_source_mirror_manifest() -> None:
    global source_mirror_manifest, source_mirror_loaded_at, source_mirror_etag
    try:
        headers = {"If-None-Match": source_mirror_etag} if source_mirror_etag else None
        response = await source_client.get(SOURCE_MIRROR_MANIFEST_URL, headers=headers, follow_redirects=True)
        if response.status_code == 304:
            return
        response.raise_for_status()
        payload = await asyncio.to_thread(response.json)
        if not isinstance(payload, dict) or not isinstance(cast(dict[str, Any], payload).get("files"), dict):
            raise ValueError("source mirror manifest is invalid")
        source_mirror_manifest = cast(dict[str, Any], payload)
        etag = response.headers.get("etag")
        source_mirror_etag = etag if isinstance(etag, str) and etag else None
    except Exception:
        pass
    finally:
        source_mirror_loaded_at = time.monotonic()


async def refresh_source_mirror_manifest_locked() -> None:
    async with source_mirror_lock:
        await refresh_source_mirror_manifest()


async def source_mirror_warmup_loop() -> None:
    if not SOURCE_MIRROR_MANIFEST_URL:
        return
    while True:
        await refresh_source_mirror_manifest_locked()
        await asyncio.sleep(SOURCE_MIRROR_TTL_SECONDS)


async def source_mirror_entry(url: str) -> dict[str, Any] | None:
    global source_mirror_refresh_task
    if not SOURCE_MIRROR_MANIFEST_URL:
        return None
    now = time.monotonic()
    if source_mirror_manifest is None:
        await refresh_source_mirror_manifest_locked()
    elif now - source_mirror_loaded_at > SOURCE_MIRROR_TTL_SECONDS:
        if source_mirror_refresh_task is None or source_mirror_refresh_task.done():
            source_mirror_refresh_task = asyncio.create_task(refresh_source_mirror_manifest_locked())
    files: dict[str, object] = (source_mirror_manifest.get("files") or {}) if source_mirror_manifest else {}
    entry = files.get(url)
    return cast(dict[str, Any], entry) if isinstance(entry, dict) else None


async def mirrored_source_url(url: str) -> str | None:
    entry = await source_mirror_entry(url)
    path = entry.get("path") if entry else None
    if not isinstance(path, str) or not path.startswith("archives") or ".." in Path(path).parts:
        return None
    return f"{SOURCE_MIRROR_BASE_URL}/{quote(path, safe='/')}"


def etag_matches(if_none_match: str, etag: str) -> bool:
    quoted = f'"{etag}"'
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate in (quoted, etag):
            return True
    return False


async def open_mirrored_response(url: str, request_headers: dict[str, str] | None = None):
    mirror_host = urlparse(SOURCE_MIRROR_BASE_URL).hostname
    if not mirror_host or urlparse(url).hostname != mirror_host:
        raise ValueError("mirror URL host is not allowed")
    request = source_client.build_request("GET", url, headers=request_headers or {})
    return await source_client.send(request, stream=True, follow_redirects=True)


async def open_mirrored_source_response(url: str, request_headers: dict[str, str] | None = None):
    hostname = (urlparse(url).hostname or "").lower()
    if hostname not in {"raw.githubusercontent.com", "github.com"}:
        response = await open_source_response(url, request_headers) if request_headers else await open_source_response(url)
        return response, False
    mirror_url = await mirrored_source_url(url)
    if mirror_url:
        try:
            response = await open_mirrored_response(mirror_url, request_headers)
            if response.status_code < 400:
                return response, True
            await response.aclose()
        except Exception:
            pass
    response = await open_source_response(url, request_headers) if request_headers else await open_source_response(url)
    return response, False

async def download_source_to_path(url: str, target: Path, remaining_bytes: int) -> int:
    response = await open_source_response(url)
    if response.status_code >= 400:
        status = response.status_code
        await response.aclose()
        raise RuntimeError(f"upstream HTTP {status}")
    written = 0
    try:
        with target.open("wb") as output:
            async for chunk in response.aiter_bytes():
                written += len(chunk)
                if written > remaining_bytes:
                    raise ValueError("source files exceed download limit")
                output.write(chunk)
    finally:
        await response.aclose()
    return written

def build_source_zip(zip_path: Path, files: list[tuple[Path, str]]) -> None:
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as archive:
        for path, filename in files:
            archive.write(path, filename)

@router.get("/api/download/txt/{doc_id}")

def download_txt(doc_id: str):
    source = get_source_doc(doc_id)
    if not source:
        return JSONResponse({"error": "not found"}, status_code=404)
    title = str(source.get("title") or doc_id)
    header = f"标题：{title}\n来源：{source.get('publication_name') or ''}\n作者：{', '.join(source.get('authors') or [])}\n\n"
    body = header + str(source.get("content") or "")
    filename = safe_filename(title, ".txt")
    return Response(body, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": content_disposition(filename)})
@router.get("/api/download/source/{doc_id}")

async def download_source(request: Request, doc_id: str, file: int = 0, inline: bool = False):
    source = await asyncio.to_thread(get_source_doc, doc_id)
    if not source:
        return JSONResponse({"error": "not found"}, status_code=404)
    files: list[object] = source.get("source_files") or []
    if not files or file < 0 or file >= len(files):
        return JSONResponse({"error": "source file not available"}, status_code=404)
    url = str(files[file])
    suffix = Path(url.split("?", 1)[0]).suffix or ".bin"
    filename = safe_filename(source_filename_base(source, doc_id), suffix)
    hostname = (urlparse(url).hostname or "").lower()
    entry = await source_mirror_entry(url) if inline and hostname in {"raw.githubusercontent.com", "github.com"} else None
    etag = entry.get("sha256") if entry else None
    if inline and etag and not request.headers.get("range") and etag_matches(request.headers.get("if-none-match") or "", etag):
        headers = {
            "ETag": f'"{etag}"',
            "Cache-Control": SOURCE_INLINE_CACHE_CONTROL,
            "Vary": "Range",
            "X-Content-Type-Options": "nosniff",
            "X-BHA-Source-Mirror": "huggingface",
        }
        return Response(status_code=304, headers=headers)
    try:
        requested_range = request.headers.get("range")
        upstream_headers = {"Range": requested_range} if requested_range else None
        if inline:
            resp, mirrored = await open_mirrored_source_response(url, upstream_headers)
        else:
            resp = await open_source_response(url, upstream_headers) if upstream_headers else await open_source_response(url)
            mirrored = False
        if resp.status_code >= 400:
            await resp.aclose()
            return JSONResponse({"error": f"upstream HTTP {resp.status_code}"}, status_code=502)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    content_length = resp.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_SOURCE_ZIP_BYTES:
        await resp.aclose()
        return JSONResponse({"error": "source file exceeds download limit"}, status_code=413)

    async def stream():
        written = 0
        try:
            async for chunk in resp.aiter_bytes():
                written += len(chunk)
                if written > MAX_SOURCE_ZIP_BYTES:
                    raise ValueError("source file exceeds download limit")
                yield chunk
        finally:
            await resp.aclose()
    media_type = source_media_type(url, resp.headers.get("content-type"))
    normalized_media_type = media_type.split(";", 1)[0].strip().lower()
    if inline and normalized_media_type not in INLINE_SOURCE_MEDIA_TYPES:
        await resp.aclose()
        return JSONResponse({"error": "source type cannot be previewed inline"}, status_code=415)
    disposition = f"inline; filename*=UTF-8''{quote(filename)}" if inline else content_disposition(filename)
    headers = {"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"}
    if mirrored:
        headers["X-BHA-Source-Mirror"] = "huggingface"
    for header in ("content-range", "accept-ranges", "content-length"):
        value = resp.headers.get(header)
        if value:
            headers[header.title()] = value
    if inline:
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        headers["Cache-Control"] = SOURCE_INLINE_CACHE_CONTROL
        headers["Vary"] = "Range"
        if etag:
            headers["ETag"] = f'"{etag}"'
        for header in ("etag", "last-modified"):
            if any(existing.lower() == header for existing in headers):
                continue
            value = resp.headers.get(header)
            if value:
                headers[header.title()] = value
    return StreamingResponse(stream(), status_code=resp.status_code, media_type=media_type, headers=headers)
@router.get("/api/download/source-zip/{doc_id}")

async def download_source_zip(doc_id: str):
    source = await asyncio.to_thread(get_source_doc, doc_id)
    if not source:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw_files: list[object] = source.get("source_files") or []
    files = [str(item) for item in raw_files]
    if not files:
        return JSONResponse({"error": "source file not available"}, status_code=404)
    if len(files) > MAX_SOURCE_FILES:
        return JSONResponse(
            {"error": "too many source files", "max_files": MAX_SOURCE_FILES, "file_count": len(files)},
            status_code=413,
        )
    source_name = source_filename_base(source, doc_id)
    temp_dir = Path(tempfile.mkdtemp(prefix="bha-source-"))
    zip_path = temp_dir / "source.zip"
    downloaded: list[tuple[Path, str]] = []
    total_bytes = 0
    try:
        for index, url in enumerate(files, start=1):
            source_path = temp_dir / f"source-{index:03d}"
            size = await download_source_to_path(url, source_path, MAX_SOURCE_ZIP_BYTES - total_bytes)
            total_bytes += size
            suffix = Path(url.split("?", 1)[0]).suffix or ".bin"
            downloaded.append((source_path, safe_filename(f"{source_name}-{index:03d}", suffix)))
    except asyncio.CancelledError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except ValueError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return JSONResponse({"error": f"source file unavailable: {exc}"}, status_code=502)
    if not downloaded:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return JSONResponse({"error": "source files unavailable"}, status_code=502)
    try:
        await asyncio.to_thread(build_source_zip, zip_path, downloaded)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return JSONResponse({"error": "failed to build source archive"}, status_code=500)
    filename = safe_filename(source_name, ".zip")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )
