#!/usr/bin/env python3
"""Stable page-level PDF OCR assets for the VoiceOfML Reader.

The module is intentionally usable without PaddleOCR installed.  Planning,
manifest validation and native PDF text extraction are kept in the small
pipeline environment; only ``ocr_page`` imports PaddleOCR.  This lets queue
planning and publication tests run without a model download.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

try:
    from . import pdf_assets, shared
except ImportError:
    import pdf_assets
    import shared


OCR_MANIFEST_NAME = "ocr-manifest.json"
OCR_MANIFEST_VERSION = 1
OCR_PAGE_VERSION = 1
OCR_PROFILE = "pdf-ocr-v1-pp-ocrv6-medium"
OCR_ENGINE = "PaddleOCR 3.7.0 / PP-OCRv6 medium / CPU"
OCR_DPI = int(os.environ.get("PDF_OCR_DPI", "300"))
WEBP_QUALITY = int(os.environ.get("PDF_WEBP_QUALITY", "85"))
WEBP_MAX_DIMENSION = int(os.environ.get("PDF_WEBP_MAX_DIMENSION", "1800"))
JXL_ENABLED = os.environ.get("PDF_JXL_ENABLED", "0").lower() in {"1", "true", "yes"}
JXL_DISTANCE = float(os.environ.get("PDF_JXL_DISTANCE", "1.5"))
JXL_EFFORT = int(os.environ.get("PDF_JXL_EFFORT", "7"))
MIN_NATIVE_PAGE_CHARS = int(os.environ.get("PDF_OCR_MIN_NATIVE_PAGE_CHARS", "48"))
NATIVE_PAGE_RATIO = float(os.environ.get("PDF_OCR_NATIVE_PAGE_RATIO", "0.90"))
MAX_PAGE_PIXELS = int(os.environ.get("PDF_OCR_MAX_PAGE_PIXELS", "50000000"))
COMMAND_TIMEOUT = int(os.environ.get("PDF_OCR_COMMAND_TIMEOUT", "600"))
OCR_TIMEOUT = int(os.environ.get("PDF_OCR_PAGE_TIMEOUT", "300"))
MI = 1024 * 1024
OCR_OBJECT_PATH_RE = re.compile(
    r"^objects/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{16}/"
    r"(?:ocr-manifest\.json|page-manifest\.json|"
    r"pages/page-[0-9]{6}\.(?:webp|jxl)|"
    r"ocr-input/page-[0-9]{6}\.png|"
    r"ocr/page-[0-9]{6}\.json\.gz|ocr/book-text\.json\.gz)$"
)


def asset_profile() -> str:
    return (f"{OCR_PROFILE}-dpi-{OCR_DPI}-webp-{WEBP_QUALITY}-{WEBP_MAX_DIMENSION}"
            f"-native-{MIN_NATIVE_PAGE_CHARS}-{NATIVE_PAGE_RATIO:g}-maxpix-{MAX_PAGE_PIXELS}"
            f"-jxl-{int(JXL_ENABLED)}-{JXL_DISTANCE:g}-{JXL_EFFORT}")


def ocr_profile_without_jxl(profile: str) -> str:
    return str(profile or "").split("-jxl-", 1)[0]


def object_root(source_sha: str, key: str) -> Path:
    key_sha = hashlib.sha256(f"{key}\0{asset_profile()}".encode("utf-8")).hexdigest()[:16]
    return Path("objects") / source_sha[:2] / source_sha / key_sha


def _run(command: list[str], *, timeout: int = COMMAND_TIMEOUT) -> str:
    try:
        result = subprocess.run(
            command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timeout: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()[-1000:]
        raise RuntimeError(f"command failed: {command[0]}: {detail}") from exc
    return result.stdout


def pdf_page_count(path: Path) -> int:
    for line in _run(["pdfinfo", str(path)]).splitlines():
        if line.startswith("Pages:"):
            value = int(line.split(":", 1)[1].strip())
            if value < 1:
                break
            return value
    raise RuntimeError("PDF page count unavailable")


def clean_text(value: str) -> str:
    value = html.unescape(str(value or "")).replace("\x00", "")
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


class _BBoxParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.words: list[dict] = []
        self.page_width = 0.0
        self.page_height = 0.0
        self._word: dict | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = dict(attrs)
        if tag == "page":
            self.page_width = float(attrs.get("width") or 0)
            self.page_height = float(attrs.get("height") or 0)
        elif tag == "word":
            self._word = {
                "x0": float(attrs.get("xMin") or 0), "y0": float(attrs.get("yMin") or 0),
                "x1": float(attrs.get("xMax") or 0), "y1": float(attrs.get("yMax") or 0),
            }
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._word is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "word" and self._word is not None:
            text = clean_text("".join(self._parts))
            if text:
                self.words.append({**self._word, "text": text, "confidence": 1.0, "source": "native"})
            self._word = None
            self._parts = []


def native_page(path: Path, page: int) -> dict:
    """Extract words and coordinates from the PDF text layer."""
    raw = _run(["pdftotext", "-bbox-layout", "-f", str(page), "-l", str(page), str(path), "-"])
    parser = _BBoxParser()
    parser.feed(raw)
    words = parser.words
    # pdftotext's y origin is top-left, matching the browser overlay.
    lines: list[str] = []
    previous_y = None
    current: list[str] = []
    for word in words:
        y = word["y0"]
        if previous_y is not None and abs(y - previous_y) > 4 and current:
            lines.append(" ".join(current))
            current = []
        current.append(word["text"])
        previous_y = y
    if current:
        lines.append(" ".join(current))
    text = "\n".join(lines).strip()
    return {
        "page": page, "status": "ready", "source": "native", "text": text,
        "blocks": normalize_blocks(words, parser.page_width, parser.page_height),
        "width": parser.page_width, "height": parser.page_height,
    }


def page_text_probe(path: Path, page: int) -> int:
    raw = _run(["pdftotext", "-f", str(page), "-l", str(page), "-enc", "UTF-8", str(path), "-"])
    return len(re.sub(r"\s+", "", clean_text(raw)))


def probe_pdf(path: Path) -> dict:
    page_count = pdf_page_count(path)
    page_chars = []
    for page in range(1, page_count + 1):
        try:
            page_chars.append(page_text_probe(path, page))
        except RuntimeError:
            page_chars.append(0)
    native_pages = sum(chars >= MIN_NATIVE_PAGE_CHARS for chars in page_chars)
    ratio = native_pages / page_count
    if native_pages == page_count:
        classification = "native-text"
    elif native_pages == 0:
        classification = "scan"
    else:
        classification = "mixed"
    return {
        "page_count": page_count, "page_chars": page_chars,
        "native_pages": native_pages, "native_page_ratio": ratio,
        "classification": classification,
    }


def normalize_blocks(blocks, width: float, height: float) -> list[dict]:
    width = max(1.0, float(width or 1))
    height = max(1.0, float(height or 1))
    output = []
    for block in blocks or []:
        text = clean_text(block.get("text", ""))
        if not text:
            continue
        x0 = max(0.0, min(1.0, float(block.get("x0", 0)) / width))
        y0 = max(0.0, min(1.0, float(block.get("y0", 0)) / height))
        x1 = max(x0, min(1.0, float(block.get("x1", 0)) / width))
        y1 = max(y0, min(1.0, float(block.get("y1", 0)) / height))
        output.append({"t": text, "b": [x0, y0, x1, y1],
                       "c": round(max(0.0, min(1.0, float(block.get("confidence", 1)))), 4),
                       "s": block.get("source", "ocr")})
    output.sort(key=lambda item: (item["b"][1], item["b"][0]))
    return output


def validate_object_path(path: str) -> str:
    if (not isinstance(path, str) or not path.startswith("objects/") or "\\" in path
            or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("invalid PDF OCR object path")
    return path


def validate_ocr_object_path(path: str, suffix: str | None = None) -> str:
    """Validate an OCR object path before it is written to a manifest."""
    validate_object_path(path)
    if not OCR_OBJECT_PATH_RE.fullmatch(path) or (suffix and not path.endswith(suffix)):
        raise ValueError("invalid PDF OCR object path")
    return path


def normalize_ocr_result(result, width: int, height: int) -> list[dict]:
    """Normalize PaddleOCR 3.x result objects and JSON fixtures alike."""
    raw = getattr(result, "json", result)
    if callable(raw):
        raw = raw()
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict) and isinstance(raw.get("res"), dict):
        raw = raw["res"]
    if not isinstance(raw, dict):
        raise RuntimeError("PP-OCRv6 returned an invalid result")
    texts = raw.get("rec_texts") or raw.get("rec_text") or []
    scores = raw.get("rec_scores") or raw.get("rec_scores") or []
    polys = raw.get("rec_polys") or raw.get("dt_polys") or raw.get("rec_boxes") or []
    blocks = []
    for index, text in enumerate(texts):
        text = clean_text(text)
        if not text:
            continue
        polygon = polys[index] if index < len(polys) else []
        points = []
        for point in polygon or []:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                points.append((float(point[0]), float(point[1])))
        if points:
            x0, y0 = min(point[0] for point in points), min(point[1] for point in points)
            x1, y1 = max(point[0] for point in points), max(point[1] for point in points)
        else:
            x0 = y0 = x1 = y1 = 0
        confidence = scores[index] if index < len(scores) else 0.0
        blocks.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                       "confidence": confidence, "source": "ocr"})
    return normalize_blocks(blocks, width, height)


_OCR_ENGINE_INSTANCE = None


def get_ocr_engine():
    global _OCR_ENGINE_INSTANCE
    if _OCR_ENGINE_INSTANCE is None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError("PP-OCRv6 dependencies are not installed") from exc
        _OCR_ENGINE_INSTANCE = PaddleOCR(
            lang="ch", ocr_version="PP-OCRv6", device="cpu", engine="paddle_static",
            use_doc_orientation_classify=False, use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return _OCR_ENGINE_INSTANCE


def _page_render_dpi(path: Path, page: int) -> int:
    """Choose a DPI that keeps unusually large PDF pages within the OCR budget."""
    try:
        info = _run(["pdfinfo", "-f", str(page), "-l", str(page), "-box", str(path)])
        match = re.search(r"Page size:\s*([0-9.]+)\s+x\s+([0-9.]+)\s+pts", info)
        if not match:
            return OCR_DPI
        width_points, height_points = (float(value) for value in match.groups())
        area = width_points * height_points
        if area <= 0:
            return OCR_DPI
        max_dpi = int(72 * (MAX_PAGE_PIXELS / area) ** 0.5)
        return max(24, min(OCR_DPI, max_dpi))
    except Exception:
        return OCR_DPI


def render_page(path: Path, page: int, directory: Path) -> tuple[Path, int, int]:
    prefix = directory / f"page-{page:06d}"
    _run([
        "pdftocairo", "-png", "-singlefile", "-r", str(_page_render_dpi(path, page)),
        "-f", str(page), "-l", str(page), str(path), str(prefix),
    ], timeout=COMMAND_TIMEOUT)
    png = prefix.with_suffix(".png")
    if not png.is_file():
        raise RuntimeError(f"page {page} render missing")
    from PIL import Image
    with Image.open(png) as image:
        width, height = image.size
        rgb = image.convert("RGB")
        if width * height > MAX_PAGE_PIXELS:
            scale = (MAX_PAGE_PIXELS / (width * height)) ** 0.5
            rgb = rgb.resize((max(1, round(width * scale)), max(1, round(height * scale))))
            width, height = rgb.size
            rgb.save(png, "PNG")
        if WEBP_MAX_DIMENSION and max(width, height) > WEBP_MAX_DIMENSION:
            scale = WEBP_MAX_DIMENSION / max(width, height)
            rgb = rgb.resize((max(1, round(width * scale)), max(1, round(height * scale))))
        webp = prefix.with_suffix(".webp")
        rgb.save(webp, "WEBP", quality=WEBP_QUALITY, method=6)
        rgb.close()
    return png, width, height


def encode_jxl(png: Path, destination: Path) -> tuple[str, int]:
    """Encode an optional future-format stream from the lossless OCR render."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(["cjxl", str(png), str(destination), "-d", str(JXL_DISTANCE), "-e", str(JXL_EFFORT)],
         timeout=COMMAND_TIMEOUT)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("cjxl produced no output")
    return shared.hash_file(destination)


def ocr_page(image: Path, width: int, height: int) -> list[dict]:
    started = time.monotonic()
    engine = get_ocr_engine()
    results = list(engine.predict(str(image)))
    if time.monotonic() - started > OCR_TIMEOUT:
        raise RuntimeError("PP-OCRv6 page timeout")
    if len(results) != 1:
        raise RuntimeError("PP-OCRv6 returned an unexpected page count")
    return normalize_ocr_result(results[0], width, height)


def page_payload(page: int, width: float, height: float, blocks: list[dict], source: str) -> dict:
    text = "\n".join(block["t"] for block in blocks).strip()
    return {
        "version": OCR_PAGE_VERSION, "kind": "pdf-ocr-page", "page": page,
        "width": round(float(width), 3), "height": round(float(height), 3),
        "source": source, "text": text, "blocks": blocks,
    }


def write_gzip_json(path: Path, payload: dict) -> tuple[str, int]:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(encoded, compresslevel=9, mtime=0))
    return shared.hash_file(path)


def write_json(path: Path, payload: dict) -> tuple[str, int]:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return shared.hash_file(path)


def read_bucket_json(path: str) -> dict:
    from huggingface_hub import HfFileSystem
    uri = f"hf://buckets/vomebook/pdf-pages/{path}"
    with HfFileSystem(token=os.environ.get("HF_TOKEN")).open(uri, "rb") as stream:
        return json.loads(stream.read())


def read_bucket_gzip_json(path: str) -> dict:
    from huggingface_hub import HfFileSystem
    uri = f"hf://buckets/vomebook/pdf-pages/{path}"
    with HfFileSystem(token=os.environ.get("HF_TOKEN")).open(uri, "rb") as stream:
        return json.loads(gzip.decompress(stream.read()))


def build_item(item: dict, source: Path, bundle: Path) -> dict:
    source_sha, source_bytes = shared.hash_file(source)
    if item.get("source_sha256") and item["source_sha256"] != source_sha:
        raise ValueError("PDF source changed after planning")
    bundle.mkdir(parents=True, exist_ok=True)
    probe = item.get("probe") or probe_pdf(source)
    pages = int(probe["page_count"])
    root = object_root(source_sha, item["key"])
    public_item = {key: value for key, value in item.items() if not key.startswith("_")}
    if probe["classification"] == "native-text":
        # A native PDF already has a browser-readable text layer.  Do not spend
        # time producing duplicate page images or OCR JSON for it.
        return {
            **public_item, "source_sha256": source_sha, "source_bytes": source_bytes,
            "status": "skipped", "reason": "native-text-pdf", "profile": asset_profile(),
            "classification": probe["classification"], "page_count": pages, "stream": False,
        }
    previous = item.get("_previous_ocr") if isinstance(item.get("_previous_ocr"), dict) else None
    previous_manifest = None
    previous_book = None
    previous_profile = str(previous.get("profile", "")) if previous else ""
    reuse_previous = bool(
        previous and previous.get("source_sha256") == source_sha
        and ocr_profile_without_jxl(previous_profile) == ocr_profile_without_jxl(asset_profile())
    )
    reencode_jxl = reuse_previous and JXL_ENABLED and previous_profile != asset_profile()
    if reuse_previous:
        try:
            previous_manifest = read_bucket_json(previous["ocr_manifest"])
            old_pages = previous_manifest.get("pages")
            if (previous_manifest.get("source_sha256") != source_sha
                    or previous_manifest.get("page_count") != pages
                    or not isinstance(old_pages, list) or len(old_pages) != pages
                    or not previous_manifest.get("page_manifest")):
                raise ValueError("incomplete previous OCR manifest")
            for number, old in enumerate(old_pages, 1):
                if old.get("p") != number:
                    raise ValueError("invalid previous OCR page order")
                for field, suffix in (("o", ".json.gz"), ("w", ".webp"), ("i", ".png")):
                    if field not in old:
                        continue
                    validate_ocr_object_path(old.get(field))
                    if not old[field].endswith(f"page-{number:06d}{suffix}"):
                        raise ValueError("invalid previous OCR page path")
            previous_book = read_bucket_gzip_json(previous_manifest["book_text"]["path"])
            if (previous_book.get("kind") != "pdf-ocr-book-text"
                    or len(previous_book.get("pages", [])) != pages):
                raise ValueError("incomplete previous OCR book text")
        except Exception:
            # A missing or stale old manifest is recoverable: fall back to a full OCR build.
            reuse_previous = False
            previous_manifest = previous_book = None
    previous_pages = previous_manifest.get("pages", []) if reuse_previous else []
    page_results = []
    book_pages = []
    with tempfile.TemporaryDirectory(dir=bundle) as temp_name:
        temp = Path(temp_name)
        for page in range(1, pages + 1):
            old_page = previous_pages[page - 1] if reuse_previous else None
            if old_page:
                page_entry = dict(old_page)
                if probe["classification"] != "native-text":
                    if not JXL_ENABLED:
                        for field in ("j", "js", "jb"):
                            page_entry.pop(field, None)
                    elif reencode_jxl:
                        with tempfile.TemporaryDirectory(dir=temp) as page_temp:
                            rendered, width, height = render_page(source, page, Path(page_temp))
                            jxl_path = bundle / root / "pages" / f"page-{page:06d}.jxl"
                            jxl_sha, jxl_bytes = encode_jxl(rendered, jxl_path)
                            page_entry.update({"j": (root / "pages" / jxl_path.name).as_posix(),
                                               "js": jxl_sha, "jb": jxl_bytes})
                page_results.append(page_entry)
                continue
            native_chars = int(probe.get("page_chars", [0] * pages)[page - 1])
            source_kind = "native" if native_chars >= MIN_NATIVE_PAGE_CHARS else "ocr"
            webp_path = None
            input_png_path = None
            if probe["classification"] != "native-text":
                rendered, width, height = render_page(source, page, temp)
                if source_kind == "native":
                    native = native_page(source, page)
                    blocks = native["blocks"]
                    page_text = native["text"]
                    width, height = native["width"] or width, native["height"] or height
                    source_kind = "native"
                else:
                    blocks = ocr_page(rendered, width, height)
                    page_text = "\n".join(block["t"] for block in blocks).strip()
                # Empty recognition is a valid page result (blank/image-only pages).
                if probe["classification"] != "native-text":
                    webp_path = bundle / root / "pages" / f"page-{page:06d}.webp"
                    webp_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(temp / f"page-{page:06d}.webp", webp_path)
                if source_kind == "ocr":
                    # Keep the high-quality PNG used by PaddleOCR so a later
                    # OCR-only workflow can consume it without downloading or
                    # rendering the source PDF again.  WebP remains the
                    # Reader delivery image; PNG is an internal OCR input.
                    input_png_path = bundle / root / "ocr-input" / f"page-{page:06d}.png"
                    input_png_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(rendered, input_png_path)
            else:
                native = native_page(source, page)
                width, height, blocks = native["width"], native["height"], native["blocks"]
                page_text = native["text"]
            payload = page_payload(page, width, height, blocks, source_kind)
            payload["text"] = page_text
            ocr_path = bundle / root / "ocr" / f"page-{page:06d}.json.gz"
            ocr_sha, ocr_bytes = write_gzip_json(ocr_path, payload)
            book_pages.append({"page": page, "text": page_text})
            page_entry = {
                "p": page, "o": (root / "ocr" / ocr_path.name).as_posix(),
                "os": ocr_sha, "ob": ocr_bytes, "source": source_kind,
                "chars": len(payload["text"]),
            }
            if webp_path:
                webp_sha, webp_bytes = shared.hash_file(webp_path)
                page_entry.update({"w": (root / "pages" / webp_path.name).as_posix(),
                                   "ws": webp_sha, "wb": webp_bytes})
                if JXL_ENABLED:
                    jxl_path = bundle / root / "pages" / f"page-{page:06d}.jxl"
                    jxl_sha, jxl_bytes = encode_jxl(rendered, jxl_path)
                    page_entry.update({"j": (root / "pages" / jxl_path.name).as_posix(),
                                       "js": jxl_sha, "jb": jxl_bytes})
            if input_png_path:
                input_sha, input_bytes = shared.hash_file(input_png_path)
                page_entry.update({"i": (root / "ocr-input" / input_png_path.name).as_posix(),
                                   "is": input_sha, "ib": input_bytes})
            page_results.append(page_entry)
            for suffix in (".png", ".webp"):
                (temp / f"page-{page:06d}{suffix}").unlink(missing_ok=True)
    # Keep the searchable representation compact and derive it from page files.
    if reuse_previous and previous_book:
        book_text = previous_book
        previous_book_meta = previous_manifest["book_text"]
        book_sha, book_bytes = previous_book_meta["sha256"], previous_book_meta["bytes"]
        book_path = Path(previous_book_meta["path"])
    else:
        book_text = {"version": 1, "kind": "pdf-ocr-book-text", "profile": OCR_PROFILE,
                     "pages": book_pages}
        book_path = bundle / root / "ocr" / "book-text.json.gz"
        book_sha, book_bytes = write_gzip_json(book_path, book_text)
    page_manifest_meta = previous_manifest["page_manifest"] if reuse_previous else None
    image_pages = [
        {"page": entry["p"], "path": entry["w"], "sha256": entry["ws"], "bytes": entry["wb"]}
        for entry in page_results if entry.get("w")
    ]
    if image_pages and not reuse_previous:
        page_manifest = pdf_assets.compact_page_manifest(
            source_sha, asset_profile(), image_pages, manifest_dir=root,
        )
        page_manifest_path = bundle / root / "page-manifest.json"
        page_manifest_sha, page_manifest_bytes = write_json(page_manifest_path, page_manifest)
        page_manifest_meta = {
            "path": page_manifest_path.relative_to(bundle).as_posix(),
            "sha256": page_manifest_sha, "bytes": page_manifest_bytes,
            "version": pdf_assets.PAGE_MANIFEST_VERSION,
        }
    manifest = {
        "version": OCR_MANIFEST_VERSION, "kind": "pdf-ocr",
        "source_sha256": source_sha, "source_bytes": source_bytes,
        "source_revision": item.get("source_revision", ""), "profile": asset_profile(),
        "engine": OCR_ENGINE, "dpi": OCR_DPI, "classification": probe["classification"],
        "page_count": pages, "complete": True, "pages": page_results,
        "book_text": {"path": previous_manifest["book_text"]["path"] if reuse_previous else (root / "ocr" / book_path.name).as_posix(),
                       "sha256": book_sha, "bytes": book_bytes},
    }
    if page_manifest_meta:
        manifest["page_manifest"] = page_manifest_meta
    manifest_path = bundle / root / OCR_MANIFEST_NAME
    # The browser manifest is deliberately plain JSON; page text is compressed.
    manifest_sha, manifest_bytes = write_json(manifest_path, manifest)
    return {
        **public_item, "source_sha256": source_sha, "source_bytes": source_bytes,
        "status": "ready", "profile": asset_profile(), "ocr_manifest": (root / OCR_MANIFEST_NAME).as_posix(),
        "ocr_manifest_sha256": manifest_sha, "ocr_manifest_bytes": manifest_bytes,
        "classification": probe["classification"], "page_count": pages,
        "stream": True, **({"page_manifest": page_manifest_meta} if page_manifest_meta else {}),
    }


def empty_manifest() -> dict:
    return {"version": 1, "profile": asset_profile(), "files": {}}


def load_manifest(path: Path | None) -> dict:
    if not path or not path.is_file():
        return empty_manifest()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("files"), dict):
        raise ValueError("invalid PDF OCR manifest")
    return data


def is_current(entry: dict | None, item: dict) -> bool:
    return bool(
        isinstance(entry, dict) and entry.get("status") == "ready"
        and entry.get("profile") == asset_profile()
        and entry.get("source_revision") == item.get("source_revision")
        and entry.get("source_sha256") == item.get("source_sha256")
    )


def validate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("invalid PDF OCR manifest")
    for key, entry in manifest["files"].items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValueError("invalid PDF OCR entry")
        if entry.get("status") not in {"ready", "failed", "skipped"}:
            raise ValueError("invalid PDF OCR entry status")
        if entry.get("status") == "ready":
            if entry.get("profile") not in {OCR_PROFILE, asset_profile()} or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("source_sha256", ""))):
                raise ValueError("invalid PDF OCR ready entry")
            if not isinstance(entry.get("page_count"), int) or entry["page_count"] < 1:
                raise ValueError("invalid PDF OCR page count")
            validate_ocr_object_path(entry["ocr_manifest"], "/ocr-manifest.json")
            page_manifest = entry.get("page_manifest")
            if page_manifest is not None:
                if not isinstance(page_manifest, dict):
                    raise ValueError("invalid PDF OCR page manifest metadata")
                validate_ocr_object_path(page_manifest.get("path"), "/page-manifest.json")
                if not isinstance(page_manifest.get("bytes"), int) or page_manifest["bytes"] <= 0:
                    raise ValueError("invalid PDF OCR page manifest size")
    return manifest


def source_records(search_data: Path, revisions: Path, assets_manifest: dict | None = None,
                   repo: str = "", range_manifest: dict | None = None) -> list[dict]:
    records = pdf_assets.load_records(search_data, revisions, repo, "pdf")
    if assets_manifest:
        generated = pdf_assets.load_generated_records(
            assets_manifest, repo=repo, assets_revision=str(assets_manifest.get("revision", "main")),
            min_bytes=0,
        )
        for item in generated:
            # Structure-optimized PDFs are delivery artifacts in a separate
            # Bucket. OCR the ordinary Reader-Assets PDF instead of downloading
            # or reprocessing the range artifact.
            records.append(item)
    records.sort(key=lambda item: (item.get("repo", ""), item.get("path", ""), item.get("source_kind", "")))
    return records


def queue(records: list[dict], limit: int, checkpoint: int) -> list[dict]:
    if limit < 1 or checkpoint < 0:
        raise ValueError("limit must be positive and checkpoint must be non-negative")
    return records[checkpoint * limit:(checkpoint + 1) * limit]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_item({"key": args.key}, args.source, args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
