#!/usr/bin/env python3
"""Safely embed local Markdown images as data URIs before Reader publication.

Only images inside the source document's own directory subtree are accepted.
Traversal, absolute paths, remote URLs, non-image data and missing files fail
closed: the caller must not publish a Markdown document with broken resources.
"""

from __future__ import annotations

import base64
import io
import mimetypes
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
MAX_IMAGE_PIXELS = 50_000_000

INLINE_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(\s*(?:<([^>\n]+)>|([^\s)\n]+))(?:\s+[\"'][^\"']*[\"'])?\s*\)"
)
HTML_IMAGE_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))",
    re.IGNORECASE,
)


class MarkdownResourceError(ValueError):
    """A local Markdown resource is unsafe, missing or unsupported."""


@dataclass(frozen=True)
class EmbeddedImage:
    reference: str
    path: str
    mime: str
    bytes: int


def _local_path(reference: str, base: str) -> str:
    value = unquote(reference.strip())
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith(("//", "/", "\\")):
        raise MarkdownResourceError(f"external or absolute image reference: {reference}")
    if parsed.query or parsed.fragment:
        raise MarkdownResourceError(f"image reference has query/fragment: {reference}")
    if "\\" in parsed.path or "\0" in parsed.path:
        raise MarkdownResourceError(f"unsafe image path: {reference}")
    normalized = posixpath.normpath(posixpath.join(base, parsed.path))
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise MarkdownResourceError(f"image escapes document directory: {reference}")
    if base and not normalized.startswith(base + "/"):
        raise MarkdownResourceError(f"image escapes document directory: {reference}")
    return normalized


def _load_image(path: str, read_bytes) -> tuple[str, bytes]:
    suffix = Path(path).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise MarkdownResourceError(f"unsupported image type: {path}")
    data = read_bytes(path)
    if len(data) > MAX_IMAGE_BYTES:
        raise MarkdownResourceError(f"image exceeds per-file limit: {path}")
    if suffix == ".bmp":
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise MarkdownResourceError(f"image exceeds pixel limit: {path}")
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                output = io.BytesIO()
                image.save(output, "PNG", optimize=True)
        except (UnidentifiedImageError, OSError, ValueError) as error:
            raise MarkdownResourceError(f"invalid BMP image: {path}") from error
        converted = output.getvalue()
        if len(converted) > MAX_IMAGE_BYTES:
            raise MarkdownResourceError(f"converted image exceeds per-file limit: {path}")
        return "image/png", converted
    mime = mimetypes.guess_type(path)[0]
    if mime not in {"image/gif", "image/jpeg", "image/png", "image/webp"}:
        raise MarkdownResourceError(f"unsupported image MIME: {path}")
    return mime, data


def embed_markdown_images(text: str, source_path: str, read_bytes) -> tuple[str, list[EmbeddedImage]]:
    """Return Markdown with same-subtree images replaced by data URIs.

    ``read_bytes`` receives a normalized repository-relative POSIX path and must
    return the image bytes or raise ``FileNotFoundError``. It is intentionally
    injected so the caller controls authenticated downloads and caching.
    """
    base = posixpath.dirname(source_path)
    embedded: list[EmbeddedImage] = []
    cache: dict[str, tuple[str, bytes]] = {}
    total = 0

    def load(reference: str) -> tuple[str, bytes, str]:
        nonlocal total
        path = _local_path(reference, base)
        if path not in cache:
            try:
                mime, data = _load_image(path, read_bytes)
            except FileNotFoundError as error:
                raise MarkdownResourceError(f"missing image: {path}") from error
            if total + len(data) > MAX_TOTAL_IMAGE_BYTES:
                raise MarkdownResourceError("Markdown images exceed the total embedding limit")
            total += len(data)
            cache[path] = (mime, data)
        mime, data = cache[path]
        embedded.append(EmbeddedImage(reference=reference, path=path, mime=mime, bytes=len(data)))
        return mime, data, path

    def data_uri(mime: str, data: bytes) -> str:
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    def inline(match: re.Match[str]) -> str:
        reference = match.group(2) or match.group(3)
        mime, data, _path = load(reference)
        return f"![{match.group(1)}]({data_uri(mime, data)})"

    def html(match: re.Match[str]) -> str:
        group = 1 if match.group(1) is not None else 2 if match.group(2) is not None else 3
        reference = match.group(group)
        mime, data, _path = load(reference)
        start, end = match.span(group)
        whole = match.group(0)
        return whole[:start - match.start()] + data_uri(mime, data) + whole[end - match.start():]

    text = INLINE_IMAGE_RE.sub(inline, text)
    text = HTML_IMAGE_RE.sub(html, text)
    return text, embedded
