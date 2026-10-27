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
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import warnings
from html.parser import HTMLParser
from pathlib import Path

try:
    from . import pdf_assets, reader_assets, shared, reader_page_codec
except ImportError:
    import pdf_assets
    import reader_assets
    import shared
    import reader_page_codec


OCR_MANIFEST_NAME = "ocr-manifest.json"
OCR_MANIFEST_VERSION = 1
OCR_PAGE_VERSION = 1
OCR_PROFILE = "pdf-ocr-v1-pp-ocrv6-medium"
OCR_LANG = os.environ.get("PDF_OCR_LANG", "auto").strip().lower() or "auto"
OCR_VERSION_OVERRIDE = os.environ.get("PDF_OCR_VERSION", "").strip() or None
OCR_BACKEND = os.environ.get("PDF_OCR_BACKEND", "rapidocr_onnxruntime").strip().lower() or "rapidocr_onnxruntime"
OCR_BACKENDS = frozenset({"paddle_static", "paddle_onnxruntime", "rapidocr_onnxruntime", "ndlocr_onnxruntime"})
PP_OCRV6_LANGS = frozenset({
    "ch", "chinese_cht", "en", "japan", "af", "az", "bs", "ca", "cs", "cy", "da", "de",
    "es", "et", "eu", "fi", "fr", "ga", "gl", "hr", "hu", "id", "is", "it", "ku", "la",
    "lb", "lt", "lv", "mi", "ms", "mt", "nl", "no", "oc", "pl", "pt", "qu", "rm", "ro",
    "rs_latin", "sk", "sl", "sq", "sv", "sw", "tl", "tr", "uz", "vi", "french", "german",
})
PP_OCRV5_LANGS = frozenset({
    "ch", "en", "fr", "de", "japan", "korean", "chinese_cht", "af", "it", "es", "bs", "pt",
    "cs", "cy", "da", "et", "ga", "hr", "hu", "rs_latin", "id", "oc", "is", "lt", "mi",
    "ms", "nl", "no", "pl", "sk", "sl", "sq", "sv", "sw", "tl", "tr", "uz", "la", "ru",
    "be", "uk", "th", "el", "az", "ku", "lv", "mt", "pi", "ro", "vi", "fi", "eu", "gl",
    "lb", "rm", "ca", "qu", "te", "sr", "bg", "mn", "ab", "ady", "kbd", "av", "dar", "inh",
    "ce", "lki", "lez", "tab", "kk", "ky", "tg", "mk", "tt", "cv", "ba", "mhr", "mo", "udm",
    "kv", "os", "bua", "xal", "tyv", "sah", "kaa", "ar", "fa", "ug", "ur", "ps", "sd", "bal",
    "hi", "mr", "ne", "bh", "mai", "ang", "bho", "mah", "sck", "new", "gom", "sa", "bgc", "ta",
})


def ocr_version() -> str:
    if OCR_LANG == 'japan' and OCR_BACKEND in {'rapidocr_onnxruntime', 'ndlocr_onnxruntime'}:
        return 'NDLOCR-Lite-202604'
    version = OCR_VERSION_OVERRIDE or ("PP-OCRv6" if OCR_LANG in PP_OCRV6_LANGS else "PP-OCRv5")
    if version not in {"PP-OCRv5", "PP-OCRv6"}:
        raise ValueError(f"unsupported PDF_OCR_VERSION: {version}")
    supported = PP_OCRV6_LANGS if version == "PP-OCRv6" else PP_OCRV5_LANGS
    if OCR_LANG not in supported:
        raise ValueError(f"language {OCR_LANG!r} is not supported by {version}")
    return version


if OCR_BACKEND not in OCR_BACKENDS:
    raise ValueError(f"unsupported PDF_OCR_BACKEND: {OCR_BACKEND}")
OCR_VERSION = ocr_version() if OCR_LANG != "auto" else "auto"
OCR_ENGINE = f"{'ndlocr_onnxruntime' if OCR_LANG == 'japan' and OCR_BACKEND == 'rapidocr_onnxruntime' else OCR_BACKEND} / {OCR_VERSION} / CPU / {OCR_LANG}"
OCR_DPI = int(os.environ.get("PDF_OCR_DPI", "300"))
WEBP_QUALITY = int(os.environ.get("PDF_WEBP_QUALITY", "85"))
WEBP_MAX_DIMENSION = int(os.environ.get("PDF_WEBP_MAX_DIMENSION", "1800"))
JXL_ENABLED = os.environ.get("PDF_JXL_ENABLED", "1").lower() in {"1", "true", "yes"}
MIN_NATIVE_PAGE_CHARS = int(os.environ.get("PDF_OCR_MIN_NATIVE_PAGE_CHARS", "48"))
NATIVE_PAGE_RATIO = float(os.environ.get("PDF_OCR_NATIVE_PAGE_RATIO", "0.90"))
MAX_PAGE_PIXELS = int(os.environ.get("PDF_OCR_MAX_PAGE_PIXELS", "50000000"))
COMMAND_TIMEOUT = int(os.environ.get("PDF_OCR_COMMAND_TIMEOUT", "600"))
OCR_TIMEOUT = int(os.environ.get("PDF_OCR_PAGE_TIMEOUT", "300"))
RAPID_MODEL_SIZE = os.environ.get('PDF_OCR_MODEL_SIZE', 'small').strip().lower()
RAPID_THREADS = int(os.environ.get('PDF_OCR_THREADS', '2'))
RAPID_REC_BATCH = int(os.environ.get('PDF_OCR_REC_BATCH', '16'))
TEXT_PROBE_BATCH_PAGES = 100
NATIVE_TEXT_BATCH_PAGES = 50
MI = 1024 * 1024
OCR_OBJECT_PATH_RE = re.compile(
    r"^objects/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{16}/"
    r"(?:ocr-manifest\.json|page-manifest\.json|render-manifest\.json|"
    r"text-layer-manifest\.json|text-review-manifest\.json|"
    r"text/(?:page-[0-9]{6}\.json\.gz|book-text\.json\.gz|search-[0-9]{6}-[0-9]{6}\.json\.gz|partition-[0-9]{6}-[0-9]{6}-manifest\.json)|"
    r"render-range-[0-9]{6}-[0-9]{6}\.json|"
    r"pages/page-[0-9]{6}\.(?:webp|jxl|png)|"
    r"ocr-input/page-[0-9]{6}\.png|"
    r"ocr/page-[0-9]{6}\.json\.gz|ocr/book-text\.json\.gz)$"
)


def resolve_ocr_config(language=None, backend=None):
    language = (language or OCR_LANG).strip().lower()
    backend = (backend or OCR_BACKEND).strip().lower()
    if language == "auto":
        language = "ch"
    if backend == "auto":
        backend = "rapidocr_onnxruntime"
    if language == 'japan' and backend == 'rapidocr_onnxruntime':
        backend = 'ndlocr_onnxruntime'
    if backend == 'ndlocr_onnxruntime' and language != 'japan':
        raise ValueError('NDLOCR models require Japanese page evidence')
    if backend not in OCR_BACKENDS:
        raise ValueError(f"unsupported PDF_OCR_BACKEND: {backend}")
    if backend == 'ndlocr_onnxruntime':
        return language, 'NDLOCR-Lite-202604', backend
    version = OCR_VERSION_OVERRIDE or ("PP-OCRv6" if language in PP_OCRV6_LANGS else "PP-OCRv5")
    supported = PP_OCRV6_LANGS if version == "PP-OCRv6" else PP_OCRV5_LANGS
    if version not in {"PP-OCRv5", "PP-OCRv6"} or language not in supported:
        raise ValueError(f"language {language!r} is not supported by {version}")
    return language, version, backend


def detect_language(value: str) -> str:
    # Only the file name is evidence. Repository and directory names are
    # Latin-heavy and used to turn Chinese books into "en" guesses.
    text = str(value or "").rsplit("\0", 1)[-1].replace('\\', '/').rsplit('/', 1)[-1]
    text = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", text)
    return detect_text_language(text)


def detect_text_language(text: str) -> str:
    counts = {
        "ch": sum("\u3400" <= c <= "\u9fff" for c in text),
        "japan": sum(("\u3040" <= c <= "\u30ff") for c in text),
        "korean": sum("\uac00" <= c <= "\ud7af" for c in text),
        "ru": sum(("\u0400" <= c <= "\u052f") for c in text),
        "ar": sum("\u0600" <= c <= "\u06ff" for c in text),
        "en": sum(("A" <= c <= "Z") or ("a" <= c <= "z") for c in text),
    }
    non_latin = {key: value for key, value in counts.items() if key != "en" and value}
    # Kana and Hangul disambiguate texts which also contain many Han characters.
    if counts['japan'] >= 1 and counts['japan'] >= counts['ch'] * .015:
        return 'japan'
    if counts['korean'] >= 2 and counts['korean'] >= counts['ch'] * .015:
        return 'korean'
    strongest = max(non_latin, key=non_latin.get) if non_latin else "en"
    if counts[strongest] < 2 and not non_latin:
        return "ch"
    if strongest == "ar":
        lowered = text.casefold()
        return "fa" if any(word in lowered for word in ("iran", "persian", "farsi", "ایران", "فارسی")) else "ar"
    return strongest


def book_ocr_config(book: dict):
    override = language_override(book.get("key", ""))
    language = ((override or {}).get('language') or (OCR_LANG if OCR_LANG != 'auto' else None)
                or book.get('ocr_language') or detect_language(book.get('key', '')))
    backend = ((override or {}).get("backend") or book.get("ocr_backend")
               or OCR_BACKEND)
    language, _, backend = resolve_ocr_config(language, backend)
    return language, backend


LANGUAGE_OVERRIDES_PATH = Path(os.environ.get(
    "PDF_OCR_LANGUAGE_OVERRIDES", "state/ocr_language_overrides.json"))
_LANGUAGE_OVERRIDES = None


def language_overrides() -> dict:
    """Exact-key language overrides; explicit evidence outranks filenames."""
    global _LANGUAGE_OVERRIDES
    if _LANGUAGE_OVERRIDES is None:
        try:
            payload = json.loads(LANGUAGE_OVERRIDES_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _LANGUAGE_OVERRIDES = {}
        else:
            if payload.get("version") != 1 or not isinstance(payload.get("overrides"), dict):
                raise ValueError("invalid OCR language override file")
            for key, value in payload["overrides"].items():
                if not isinstance(key, str) or not key or not isinstance(value, dict):
                    raise ValueError("invalid OCR language override entry")
                resolve_ocr_config(value.get("language"), value.get("backend"))
            _LANGUAGE_OVERRIDES = payload["overrides"]
    return _LANGUAGE_OVERRIDES


def language_override(key: str):
    return language_overrides().get(key)


def language_source(book: dict) -> str:
    if language_override(book.get("key", "")):
        return "override"
    if OCR_LANG != 'auto':
        return 'global'
    if book.get("ocr_language"):
        return "registry"
    return "filename"


def asset_profile(language=None, backend=None, revision=None) -> str:
    language, version, backend = resolve_ocr_config(language, backend)
    size = (RAPID_MODEL_SIZE if version == 'PP-OCRv6' else 'mobile') if backend == 'rapidocr_onnxruntime' else (
        'tiny' if backend == 'ndlocr_onnxruntime' else 'medium')
    model_profile = OCR_PROFILE if language == "ch" and version == "PP-OCRv6" and backend == "paddle_static" else (
        f"pdf-ocr-v1-{version.lower()}-{size}-lang-{language}")
    if backend != "paddle_static":
        model_profile += f"-backend-{backend.replace('_', '-')}"
    if backend == 'rapidocr_onnxruntime':
        model_profile += f'-models-det6{RAPID_MODEL_SIZE}-rec{version[-1]}{RAPID_MODEL_SIZE if version == "PP-OCRv6" else "mobile"}-rapid310'
        if rapid_language(language) in {'cyrillic', 'en', 'latin', 'de', 'fr', 'el'}:
            model_profile += '-horizontal-crops-v1'
    if backend == 'ndlocr_onnxruntime':
        try:
            from . import ndl_ocr
        except ImportError:
            import ndl_ocr
        model_profile += '-models-' + ndl_ocr.PROFILE
    if revision:
        model_profile += f"-rev-{revision}"
    return (f"{model_profile}-dpi-{OCR_DPI}-webp-{WEBP_QUALITY}-{WEBP_MAX_DIMENSION}"
            f"-native-{MIN_NATIVE_PAGE_CHARS}-{NATIVE_PAGE_RATIO:g}-maxpix-{MAX_PAGE_PIXELS}"
            f"-jxl-{int(JXL_ENABLED)}-adaptive")


def valid_published_profile(profile: str) -> bool:
    candidate = str(profile or "")
    base, separator, layout = candidate.partition("-layout-")
    if separator and not re.fullmatch(r"(?:layout-)?v[123]-[0-9a-f]{16}", layout):
        return False
    return bool(re.fullmatch(
        r"pdf-ocr-v1-(?:pp-ocrv[56]|ndlocr-lite-202604)-(?:medium|small|mobile|tiny)"
        r"(?:-lang-[a-z0-9_]+)?"
        r"(?:-backend-(?:paddle-onnxruntime|rapidocr-onnxruntime|ndlocr-onnxruntime))?"
        r"(?:-models-det6(?:small|medium)-rec[56](?:small|medium|mobile)-rapid310)?"
        r"(?:-horizontal-crops-v1)?"
        r"(?:-models-ndl202604-[0-9a-f]{12}-(?:adapter[1234]|adapter5-segmented|adapter6-folios-headings|adapter7-long-dash-pixels|adapter8-faint-dashes-sections|adapter9-edge-dashes-sections|adapter10-spaced-sections|adapter11-space-boundaries))?"
        r"(?:-rev-[a-z0-9._-]+)?"
        r"-dpi-[0-9]+-webp-[0-9]+-[0-9]+"
        r"-native-[0-9]+(?:\.[0-9]+)?-[0-9]+(?:\.[0-9]+)?"
        r"-maxpix-[0-9]+-jxl-[01]-(?:adaptive|[0-9]+(?:\.[0-9]+)?-[0-9]+)(?:-quality-v1)?",
        base,
    ))


def ocr_profile_without_jxl(profile: str) -> str:
    return str(profile or "").split("-jxl-", 1)[0]


def object_root(source_sha: str, key: str, language=None, backend=None, revision=None) -> Path:
    key_sha = hashlib.sha256(f"{key}\0{asset_profile(language, backend, revision)}".encode("utf-8")).hexdigest()[:16]
    return Path("objects") / source_sha[:2] / source_sha / key_sha


def _run(command: list[str], *, timeout: int = COMMAND_TIMEOUT, stderr: bool = False):
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
    if stderr:
        return result.stdout, result.stderr
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
        self.pages: list[dict] = []
        self._page: dict | None = None
        self._word: dict | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = dict(attrs)
        if tag == "page":
            self._page = {"width": float(attrs.get("width") or 0),
                          "height": float(attrs.get("height") or 0), "words": []}
            self.pages.append(self._page)
        elif tag == "word":
            self._word = {
                "x0": float(attrs.get("xmin") or 0), "y0": float(attrs.get("ymin") or 0),
                "x1": float(attrs.get("xmax") or 0), "y1": float(attrs.get("ymax") or 0),
            }
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._word is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "word" and self._word is not None:
            text = clean_text("".join(self._parts))
            if text and self._page is not None:
                self._page["words"].append(
                    {**self._word, "text": text, "confidence": 1.0, "source": "native"})
            self._word = None
            self._parts = []
        elif tag == "page":
            self._page = None


def _native_page_result(page: int, parsed: dict) -> dict:
    words = parsed["words"]
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
        "blocks": normalize_blocks(words, parsed["width"], parsed["height"]),
        "width": parsed["width"], "height": parsed["height"],
    }


def _native_pages_single(path: Path, start: int, end: int) -> dict[int, dict]:
    raw = _run(["pdftotext", "-bbox-layout", "-f", str(start), "-l", str(end), str(path), "-"])
    parser = _BBoxParser()
    parser.feed(raw)
    if len(parser.pages) != end - start + 1:
        raise RuntimeError("pdftotext returned an incomplete page range")
    return {number: _native_page_result(number, parsed)
            for number, parsed in zip(range(start, end + 1), parser.pages)}


def native_pages(path: Path, pages, batch_size: int = NATIVE_TEXT_BATCH_PAGES) -> dict[int, dict]:
    """Extract native text and coordinates in bounded Poppler process batches."""
    numbers = sorted(set(int(page) for page in pages))
    if not numbers:
        return {}
    if batch_size < 1 or numbers[0] < 1:
        raise ValueError("invalid native page batch")
    output = {}
    index = 0
    while index < len(numbers):
        start = end = numbers[index]
        index += 1
        while index < len(numbers) and numbers[index] == end + 1 and end - start + 1 < batch_size:
            end = numbers[index]
            index += 1
        try:
            output.update(_native_pages_single(path, start, end))
        except (RuntimeError, OSError):
            # Preserve per-page recovery when one malformed page breaks a batch.
            for page in range(start, end + 1):
                try:
                    output.update(_native_pages_single(path, page, page))
                except (RuntimeError, OSError):
                    raise
    if Path(path).is_file():
        try:
            from .pdf_bitmap_text import BitmapText
            from . import pdf_native_geometry, pdf_mixed_text
        except ImportError:
            from pdf_bitmap_text import BitmapText
            import pdf_native_geometry, pdf_mixed_text
        extractor = BitmapText(path)
        try:
            for number in numbers:
                if not any(info[2] == 'Type3' for info in extractor.document[number - 1].get_fonts()):
                    output[number] = pdf_native_geometry.refine(extractor.document[number - 1], output[number])
                    output[number]['raster_evidence'] = pdf_mixed_text.inspect(extractor.document[number - 1])
                    continue
                recovered = extractor.extract(number)
                original = output[number]
                if recovered:
                    output[number] = {**recovered, 'extracted_text': original['text'],
                                      'extracted_blocks': original['blocks']}
                else:
                    original['native_evidence'] = {'reasons': ['unverified-type3-font']}
        finally:
            extractor.close()
    return output


def native_page(path: Path, page: int) -> dict:
    """Extract words and coordinates from one PDF text page."""
    return native_pages(path, [page], batch_size=1)[page]


def page_text_probe(path: Path, page: int) -> int:
    raw = _run(["pdftotext", "-f", str(page), "-l", str(page), "-enc", "UTF-8", str(path), "-"])
    return len(re.sub(r"\s+", "", clean_text(raw)))


def _split_text_pages(raw: str, expected: int) -> list[str]:
    pages = raw.split("\f")
    if raw.endswith("\f"):
        pages.pop()
    if len(pages) != expected:
        raise ValueError("pdftotext returned an incomplete text page range")
    return pages


def probe_pdf(path: Path) -> dict:
    page_count = pdf_page_count(path)
    page_chars = []
    for start in range(1, page_count + 1, TEXT_PROBE_BATCH_PAGES):
        end = min(page_count, start + TEXT_PROBE_BATCH_PAGES - 1)
        try:
            raw = _run(["pdftotext", "-f", str(start), "-l", str(end), "-enc", "UTF-8", str(path), "-"])
            text_pages = _split_text_pages(raw, end - start + 1)
            page_chars.extend(len(re.sub(r"\s+", "", clean_text(text))) for text in text_pages)
        except (RuntimeError, ValueError, OSError):
            for page in range(start, end + 1):
                try:
                    page_chars.append(page_text_probe(path, page))
                except (RuntimeError, OSError):
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


def reader_presentation(path: Path) -> dict:
    """Inspect encoded images without rendering or recompressing the document."""
    import pymupdf
    inspected = 0
    binary_images = 0
    filters = set()
    try:
        with pymupdf.open(path) as document:
            for page in document:
                # Image-info includes inline images that get_images can omit.
                binary_images += sum(image.get("bpc") == 1 for image in page.get_image_info())
                filters.update(str(image[8]) for image in page.get_images(full=True)
                               if len(image) > 8 and image[8])
                inspected += 1
        if not inspected:
            raise ValueError("empty PDF")
        return {"strategy": "preserve-pdf", "policy": "djvu-only-page-stream-v1",
                "reason": "bitonal-raster" if binary_images else "primary-pdf-reader",
                "complete": True, "inspected_pages": inspected,
                "bitonal_images": binary_images, "filters": sorted(filters)}
    except Exception as error:
        return {"strategy": "preserve-pdf", "reason": "inspection-failed",
                "complete": False, "inspected_pages": inspected,
                "error_type": type(error).__name__}


def preserves_reader_pdf(item: dict) -> bool:
    return item.get("reader_presentation", {}).get("strategy") == "preserve-pdf"


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
                       "s": block.get("source", "ocr"),
                       **({"q": [[max(0, min(1, x / width)), max(0, min(1, y / height))]
                                 for x, y in block["polygon"]]} if block.get("polygon") else {})})
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
    def sequence(*names):
        for name in names:
            value = raw.get(name)
            if hasattr(value, "tolist"):
                value = value.tolist()
            if value is not None and len(value):
                return [value] if isinstance(value, str) else value
        return []
    texts = sequence("rec_texts", "rec_text")
    scores = sequence("rec_scores")
    polys = sequence("rec_polys", "dt_polys", "rec_boxes")
    blocks = []
    for index, text in enumerate(texts):
        text = clean_text(text)
        if not text:
            continue
        polygon = polys[index] if index < len(polys) else []
        if len(polygon) == 4 and all(isinstance(v, (float, int)) for v in polygon):
            x0, y0, x1, y1 = polygon
            polygon = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
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
                       "confidence": confidence, "source": "ocr", "polygon": points})
    return normalize_blocks(blocks, width, height)


def normalize_rapid_result(result, width: int, height: int) -> list[dict]:
    """Adapt RapidOCR's boxes/txts/scores output to the shared OCR contract."""
    if isinstance(result, tuple):
        result = result[0]
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if isinstance(result, dict):
        boxes = result.get("boxes", boxes)
        texts = result.get("txts", result.get("texts", texts))
        scores = result.get("scores", scores)
    if boxes is None and texts is None:
        # RapidOCR reports a normal blank page as an empty detection result.
        # Keep the page in the v2 index instead of turning it into a failed task.
        return []
    if boxes is None or texts is None or scores is None or not len(boxes) == len(texts) == len(scores):
        raise RuntimeError("RapidOCR detection/recognition result lengths differ")
    blocks = []
    score_values = scores if scores is not None else [1.0] * len(texts)
    for polygon, text, score in zip(boxes, texts, score_values):
        points = [[float(point[0]), float(point[1])] for point in polygon]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        blocks.append({"text": text, "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
                       "confidence": score, "source": "ocr", "polygon": points})
    return normalize_blocks(blocks, width, height)


_OCR_ENGINE_INSTANCES = {}
_MODEL_DIGESTS = {}
_VERTICAL_AUDITOR = None


def audit_vertical_recognition(engine, crops, detection, recognition):
    """Check Japanese vertical text with an independent recognizer on the same pixels.

    PP-OCRv6 can be highly confident while dropping small kana and punctuation.
    Keep both strings as evidence; disagreements never silently rewrite text.
    """
    import numpy as np
    from rapidocr import RapidOCR, ModelType, OCRVersion
    from rapidocr.ch_ppocr_rec import TextRecInput
    try:
        from . import ocr_quality
    except ImportError:
        import ocr_quality
    language = getattr(engine.cfg.Rec.lang_type, 'value', engine.cfg.Rec.lang_type)
    if language != 'japan':
        return None
    indices = []
    for index, box in enumerate(detection.boxes):
        width = max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3]))
        height = max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2]))
        if height > 3 * width and len(recognition.txts[index]) >= 4:
            indices.append(index)
    if not indices:
        return None
    global _VERTICAL_AUDITOR
    try:
        if _VERTICAL_AUDITOR is None:
            _VERTICAL_AUDITOR = RapidOCR(params={
                'Rec.lang_type': 'japan', 'Rec.ocr_version': OCRVersion.PPOCRV4,
                'Rec.model_type': ModelType.MOBILE, 'Rec.rec_batch_num': RAPID_REC_BATCH,
                'EngineConfig.onnxruntime.intra_op_num_threads': RAPID_THREADS,
                'EngineConfig.onnxruntime.inter_op_num_threads': 1})
        recognizer = _VERTICAL_AUDITOR._load_rec_model()
        output = recognizer(TextRecInput(img=[crops[i] for i in indices], return_word_box=False))
        path = Path(recognizer.session.session._model_path)
        rows = [{'region': i, 'primary': recognition.txts[i], 'secondary': text,
                 'primary_confidence': float(recognition.scores[i]), 'secondary_confidence': float(confidence)}
                for i, text, confidence in zip(indices, output.txts, output.scores)]
        return ocr_quality.compare_recognizers(rows, {'rec': {'file': path.name,
            'sha256': shared.hash_file(path)[0], 'version': 'PP-OCRv4', 'size': 'mobile'}})
    except Exception as exc:
        return {'status': 'review', 'issues': [ocr_quality.issue('vertical-recognition-audit-unavailable',
                'review', error=f'{type(exc).__name__}: {exc}'[:300])]}


def strict_rapid_steps(engine, image, operations):
    """RapidOCR 3.10 swallows stage errors; keep only genuine no-detection results."""
    from rapidocr.main import RapidOCRError
    from rapidocr.ch_ppocr_cls import TextClsOutput
    from rapidocr.ch_ppocr_det import TextDetOutput
    from rapidocr.ch_ppocr_rec import TextRecOutput
    engine.reader_diagnostics = {}
    engine.reader_secondary_models = {}
    try:
        crops, detection = engine.detect_and_crop(image, operations)
    except RapidOCRError as exc:
        if str(exc) != "The text detection result is empty":
            raise
        engine.reader_diagnostics = {"detected_regions": 0, "recognized_regions": 0}
        return TextDetOutput(), TextClsOutput(), TextRecOutput(), []
    recognition = engine.recognize_txt(crops)
    if recognition.txts is None or len(recognition.txts) != len(crops):
        raise RuntimeError("RapidOCR did not recognize every detected region")
    line_evidence = []
    if getattr(engine, 'reader_horizontal_crops', False):
        try:
            from . import ocr_line_geometry
        except ImportError:
            import ocr_line_geometry
        crops, line_evidence = ocr_line_geometry.split(crops, detection, recognition)
        if line_evidence:
            recognition = engine.recognize_txt(crops)
            if recognition.txts is None or len(recognition.txts) != len(crops):
                raise RuntimeError('RapidOCR did not recognize every separated line')
    engine.reader_diagnostics = {"detected_regions": len(crops),
                                 "recognized_regions": sum(bool(t.strip()) for t in recognition.txts),
                                 "empty_regions": [i for i, t in enumerate(recognition.txts) if not t.strip()]}
    if getattr(engine, 'reader_horizontal_crops', False):
        engine.reader_diagnostics['line_geometry'] = {'profile': ocr_line_geometry.PROFILE,
            'automatic_quarter_turn': False, 'splits': line_evidence}
    secondary = audit_vertical_recognition(engine, crops, detection, recognition)
    if secondary is not None:
        engine.reader_diagnostics['secondary_check'] = secondary
    return detection, TextClsOutput(), recognition, crops


def get_ocr_engine(language=None, backend=None, *, model_size=None):
    language, version, backend = resolve_ocr_config(language, backend)
    model_size=RAPID_MODEL_SIZE if model_size is None else model_size
    if backend == 'ndlocr_onnxruntime':
        try:
            from . import ndl_ocr
        except ImportError:
            import ndl_ocr
        return ndl_ocr.engine()
    cache_key = (language, version, backend, model_size, RAPID_THREADS, RAPID_REC_BATCH)
    if cache_key not in _OCR_ENGINE_INSTANCES:
        if backend == "rapidocr_onnxruntime":
            try:
                from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR
            except ImportError as exc:
                raise RuntimeError("RapidOCR ONNX dependencies are not installed") from exc
            rapid_version = getattr(OCRVersion, version.replace("-", "").upper())
            if model_size not in {'small', 'medium'} or not 1 <= RAPID_THREADS <= 16 or not 1 <= RAPID_REC_BATCH <= 64:
                raise ValueError('invalid RapidOCR model or CPU configuration')
            class DocumentOCR(RapidOCR):
                reader_horizontal_crops = rapid_language(language) in {'cyrillic', 'en', 'latin', 'de', 'fr', 'el'}

                def run_ocr_steps(self, image, operations):
                    return strict_rapid_steps(self, image, operations)

                def build_final_output(self, original, detection, classification, recognition, crops, operations):
                    # RapidOCR drops empty recognitions when building its output.
                    # Preserve their original page positions for graphic-rule
                    # classification and lossless failure evidence.
                    if detection.boxes is not None and recognition.txts is not None:
                        from rapidocr.main import map_boxes_to_original
                        h, w = original.shape[:2]
                        boxes = map_boxes_to_original(detection.boxes.copy(), operations, h, w)
                        self.reader_diagnostics['detection_regions'] = [
                            {'region': i, 'text': value,
                             'b': [float(box[:, 0].min())/w, float(box[:, 1].min())/h,
                                   float(box[:, 0].max())/w, float(box[:, 1].max())/h],
                             'q': [[float(x)/w, float(y)/h] for x, y in box]}
                            for i, (box, value) in enumerate(zip(boxes, recognition.txts))]
                    return super().build_final_output(original, detection, classification, recognition, crops, operations)

                def crop_text_regions(self, image, boxes):
                    if not self.reader_horizontal_crops:
                        return super().crop_text_regions(image, boxes)
                    try:
                        from . import ocr_line_geometry
                    except ImportError:
                        import ocr_line_geometry
                    return [ocr_line_geometry.crop(image, box) for box in boxes]

            _OCR_ENGINE_INSTANCES[cache_key] = DocumentOCR(params={
                "Det.engine_type": EngineType.ONNXRUNTIME, "Cls.engine_type": EngineType.ONNXRUNTIME,
                "Rec.engine_type": EngineType.ONNXRUNTIME, "Det.lang_type": 'ch',
                "Rec.lang_type": rapid_language(language), "Det.ocr_version": OCRVersion.PPOCRV6,
                "Rec.ocr_version": rapid_version,
                'Det.model_type': ModelType(model_size),
                'Rec.model_type': ModelType(model_size if version == 'PP-OCRv6' else 'mobile'),
                'Global.use_cls': False, 'Global.text_score': 0.0, 'Rec.rec_batch_num': RAPID_REC_BATCH,
                'EngineConfig.onnxruntime.intra_op_num_threads': RAPID_THREADS,
                'EngineConfig.onnxruntime.inter_op_num_threads': 1,
            })
        else:
            try:
                from paddleocr import PaddleOCR
            except ImportError as exc:
                raise RuntimeError("PaddleOCR dependencies are not installed") from exc
            _OCR_ENGINE_INSTANCES[cache_key] = PaddleOCR(
                lang=language, ocr_version=version, device="cpu",
                engine="onnxruntime" if backend == "paddle_onnxruntime" else "paddle_static",
                use_doc_orientation_classify=False, use_doc_unwarping=False,
                use_textline_orientation=False,
            )
    return _OCR_ENGINE_INSTANCES[cache_key]


def rapid_language(language):
    if language in PP_OCRV6_LANGS:
        return language
    if language in {'ar', 'fa', 'ug', 'ur', 'ps', 'sd', 'bal'}:
        return 'arabic'
    if language in {'ru', 'be', 'uk', 'sr', 'bg', 'mn', 'ab', 'ady', 'kbd', 'av', 'dar', 'inh',
                    'ce', 'lki', 'lez', 'tab', 'kk', 'ky', 'tg', 'mk', 'tt', 'cv', 'ba', 'mhr',
                    'mo', 'udm', 'kv', 'os', 'bua', 'xal', 'tyv', 'sah', 'kaa'}:
        return 'cyrillic'
    if language in {'hi', 'mr', 'ne', 'bh', 'mai', 'ang', 'bho', 'mah', 'sck', 'new', 'gom', 'sa', 'bgc', 'pi'}:
        return 'devanagari'
    return language


def actual_ocr_models(language=None, backend=None, *, model_size=None):
    """Record the loaded files rather than a configured marketing-size label."""
    engine = get_ocr_engine(language, backend) if model_size is None else get_ocr_engine(language,backend,model_size=model_size)
    if resolve_ocr_config(language, backend)[2] == 'ndlocr_onnxruntime':
        return engine.models()
    if not hasattr(engine, 'cfg'):
        return {'backend': resolve_ocr_config(language, backend)[2]}
    result = {'backend': 'rapidocr_onnxruntime', 'package_version': importlib.metadata.version('rapidocr'),
              'models': {}, 'diagnostics': dict(getattr(engine, 'reader_diagnostics', {}))}
    for name in ('Det', 'Rec'):
        config = getattr(engine.cfg, name)
        component = getattr(engine, 'text_det' if name == 'Det' else 'text_rec', None)
        session = getattr(getattr(component, 'session', None), 'session', None)
        model = getattr(session, '_model_path', None)
        entry = {'version': config.ocr_version.value, 'size': config.model_type.value,
                 'language': getattr(config.lang_type, 'value', config.lang_type)}
        if model and Path(model).is_file():
            path = Path(model)
            stat = path.stat()
            identity = (str(path), stat.st_size, stat.st_mtime_ns)
            if identity not in _MODEL_DIGESTS:
                _MODEL_DIGESTS[identity] = shared.hash_file(path)[0]
            entry.update(file=path.name, sha256=_MODEL_DIGESTS[identity], bytes=stat.st_size,
                         providers=session.get_providers())
        result['models'][name.lower()] = entry
    result['models'].update(getattr(engine, 'reader_secondary_models', {}))
    return result


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


def scan_reader_images(path: Path, start: int, end: int) -> dict[int, tuple[int, int]]:
    """Find unambiguous, nearly full-page raster scans and their source pixels."""
    try:
        listing = _run(["pdfimages", "-f", str(start), "-l", str(end), "-list", str(path)])
    except RuntimeError:
        return {}
    images: dict[int, list[tuple[int, int, int, int]]] = {}
    for line in listing.splitlines()[2:]:
        parts = line.split()
        if len(parts) < 14 or not parts[0].isdigit():
            continue
        page = int(parts[0])
        if not start <= page <= end:
            continue
        if parts[2] != "image":
            images.setdefault(page, []).append((0, 0, 0, 0))
            continue
        try:
            image = tuple(int(parts[index]) for index in (3, 4, 12, 13))
        except ValueError:
            continue
        images.setdefault(page, []).append(image)
    result = {}
    for page, candidates in images.items():
        if len(candidates) != 1:
            continue
        width, height, x_ppi, y_ppi = candidates[0]
        if min(width, height, x_ppi, y_ppi) < 1:
            continue
        try:
            info = _run(["pdfinfo", "-f", str(page), "-l", str(page), "-box", str(path)])
            match = re.search(r"Page(?:\s+\d+)? size:\s*([0-9.]+)\s+x\s+([0-9.]+)\s+pts", info)
            if not match:
                continue
            page_width, page_height = (float(value) / 72 for value in match.groups())
            coverage = (width / x_ppi / page_width, height / y_ppi / page_height)
            if all(0.85 <= value <= 1.1 for value in coverage):
                result[page] = (width, height)
        except (RuntimeError, ZeroDivisionError):
            continue
    return result


def reader_webp_quality(image, full_page_scan: bool) -> int:
    if not full_page_scan:
        return WEBP_QUALITY
    sample = image.resize((128, 128))
    pixels = sample.tobytes()
    bright = dark = colorful = 0
    for offset in range(0, len(pixels), 3):
        r, g, b = pixels[offset:offset + 3]
        bright += min(r, g, b) > 235
        dark += max(r, g, b) < 96
        colorful += max(r, g, b) - min(r, g, b) > 22
    count = sample.width * sample.height
    return min(WEBP_QUALITY, 80) if (bright / count >= .7 and dark / count <= .12
                                    and colorful / count <= .03) else WEBP_QUALITY


def render_page(path: Path, page: int, directory: Path,
                reader_pixels: tuple[int, int] | None = None, reader_jxl: bool = False,
                reader_images: bool = False) -> tuple[Path, int, int]:
    # PDF rendering exists only for OCR/correction evidence. Keep obsolete call
    # arguments accepted, but never let them recreate reading WebP/JXL/PPM files.
    prefix = directory / f"page-{page:06d}"
    _stdout, errors = _run([
        "pdftocairo", "-tiff", "-tiffcompression", "none", "-singlefile", "-r", str(_page_render_dpi(path, page)),
        "-f", str(page), "-l", str(page), str(path), str(prefix),
    ], timeout=COMMAND_TIMEOUT, stderr=True)
    # A PDF with non-embedded CJK CID fonts renders as an almost blank page when
    # Poppler lacks the required CMap/font packages, silently producing an
    # unusable OCR input. Fail loudly instead of publishing a symbol-only layer.
    if "some font thing failed" in errors or "Couldn't find a font" in errors:
        raise RuntimeError(
            "pdftocairo could not resolve a font; install poppler-data and CJK "
            "fonts before rendering (refusing an unreliable OCR page): "
            + errors.strip().splitlines()[0][:200])
    png = prefix.with_suffix(".png")
    raster = prefix.with_suffix('.tif')
    if not raster.is_file():
        raise RuntimeError(f"page {page} render missing")
    from PIL import Image
    previous_limit = Image.MAX_IMAGE_PIXELS
    try:
        # Poppler can emit a very large intermediate PNG even when the final
        # Reader/OCR contract caps it at MAX_PAGE_PIXELS. Let Pillow open that
        # intermediate so the explicit resize below enforces our bound instead
        # of treating Pillow's generic decompression warning as a PDF failure.
        Image.MAX_IMAGE_PIXELS = None
        with warnings.catch_warnings(), Image.open(raster) as image:
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            width, height = image.size
            rgb = image.convert("RGB")
            if width * height > MAX_PAGE_PIXELS:
                scale = (MAX_PAGE_PIXELS / (width * height)) ** 0.5
                rgb = rgb.resize((max(1, round(width * scale)), max(1, round(height * scale))))
                width, height = rgb.size
            # Cairo's PNG writer uses costly maximum compression. A temporary
            # uncompressed TIFF has identical Cairo pixels; level 6 preserves
            # every sample and avoids recompressing the same page twice.
            rgb.save(png, 'PNG', compress_level=6)
            rgb.close()
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
        raster.unlink(missing_ok=True)
    return png, width, height


def encode_jxl(png: Path, destination: Path) -> tuple[str, int]:
    return reader_page_codec.encode_png(png, destination)


def ocr_page(image: Path, width: int, height: int, language=None, backend=None) -> list[dict]:
    started = time.monotonic()
    _, _, backend = resolve_ocr_config(language, backend)
    engine = get_ocr_engine(language, backend)
    if backend == 'ndlocr_onnxruntime':
        result = engine.recognize(image)
        if time.monotonic() - started > OCR_TIMEOUT:
            raise RuntimeError('NDLOCR page timeout')
        return result
    if backend == "rapidocr_onnxruntime":
        language = resolve_ocr_config(language, backend)[0]
        mixed = language in {'ch', 'chinese_cht'}
        recognized = engine(str(image), use_det=True, use_cls=False, use_rec=True,
                            return_word_box=mixed, return_single_char_box=mixed)
        result = normalize_rapid_result(recognized, width, height)
        if mixed and result:
            try:
                from . import ocr_mixed_text
            except ImportError:
                import ocr_mixed_text
            model_evidence = actual_ocr_models(language, backend)
            result = ocr_mixed_text.recognize(recognized, width, height, model_evidence)
            engine.reader_diagnostics.update(model_evidence.get('diagnostics', {}))
            engine.reader_secondary_models = {k: v for k, v in model_evidence['models'].items()
                                             if k not in {'det', 'rec'}}
        if time.monotonic() - started > OCR_TIMEOUT:
            raise RuntimeError("RapidOCR page timeout")
        return result
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
    uri = f"hf://buckets/{shared.PDF_PAGES_BUCKET}/{path}"
    with HfFileSystem(token=os.environ.get("HF_TOKEN")).open(uri, "rb") as stream:
        return json.loads(stream.read())


def read_bucket_gzip_json(path: str) -> dict:
    from huggingface_hub import HfFileSystem
    uri = f"hf://buckets/{shared.PDF_PAGES_BUCKET}/{path}"
    with HfFileSystem(token=os.environ.get("HF_TOKEN")).open(uri, "rb") as stream:
        return json.loads(gzip.decompress(stream.read()))


def build_item(item: dict, source: Path, bundle: Path) -> dict:
    raise ValueError("legacy monolithic OCR is retired; use the independent plan-render/plan-ocr pipeline with quality evidence")


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
        if entry.get("status") not in {"ready", "failed", "skipped", "rendered"}:
            raise ValueError("invalid PDF OCR entry status")
        if entry.get("status") == "rendered":
            validate_ocr_object_path(entry["render_manifest"]["path"], "/render-manifest.json")
            if entry.get("page_manifest"):
                validate_ocr_object_path(entry["page_manifest"]["path"], "/page-manifest.json")
        if entry.get("status") == "ready":
            if (not valid_published_profile(entry.get("profile"))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("source_sha256", "")))):
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
                   repo: str = "") -> list[dict]:
    records = pdf_assets.load_records(search_data, revisions, repo, "pdf")
    try:
        from . import djvu_pages
    except ImportError:
        import djvu_pages
    # Gated: native DjVu originals enter the page pipeline directly instead of
    # through their derived PDF. Default off keeps production unchanged.
    native_djvu = {item["key"]: {**item, "source_format": "djvu"}
                   for item in pdf_assets.load_records(search_data, revisions, repo, "djvu")
                   if djvu_pages.native_enabled(item) and not djvu_pages.native_exception(item)} \
        if djvu_pages.native_enabled() else {}
    original_sizes = {item["key"]: item.get("source_bytes") for item in records}
    original_sizes.update({key: item.get("source_bytes") for key, item in native_djvu.items()})
    if assets_manifest:
        generated = pdf_assets.load_generated_records(
            assets_manifest, repo=repo, assets_revision=str(assets_manifest.get("revision", "main")),
            min_bytes=0,
        )
        for item in generated:
            records.append(item)
    # A repaired/generated Reader PDF is authoritative for OCR. In particular,
    # never extract text from the original GBK-encoded 林一章版 PDF when its
    # repaired asset is available. If the repair is not published yet, wait
    # for it instead of indexing the known-bad source text.
    records = [item for item in records if not (
        item.get("source_kind") == "upstream"
        and reader_assets.known_gbk_pdf(item.get("repo"), item.get("path"))
    )]
    by_key = {}
    for item in records:
        key = item["key"]
        if key not in by_key or item.get("source_kind") == "generated":
            if item.get("source_kind") == "generated" and key in original_sizes:
                item = {**item, "original_source_bytes": original_sizes[key]}
            by_key[key] = item
    for key, item in native_djvu.items():
        # One record per key: the native original replaces its derived PDF, so a
        # DjVu is never queued twice. The derivation is kept only for sources
        # whose bytes turn out to be a PDF (sniffed at planning time).
        derived = by_key.get(key)
        by_key[key] = {**item, **({"_derived_fallback": derived}
                                  if derived and derived.get("source_kind") == "generated" else {})}
    records = list(by_key.values())
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
