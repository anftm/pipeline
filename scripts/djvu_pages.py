#!/usr/bin/env python3
"""Native DjVu page source: probe, page map, lossless renders and hidden text.

Rendering uses DjVuLibre (``ddjvu``/``djvutxt``) directly on the original DjVu
bytes, never an intermediate PDF. Page geometry is read from the IFF ``INFO``
chunks without external tools so v3 verification is deterministic. Identity is
always the original DjVu SHA-256; render profiles use a separate ``djvu-native``
namespace so they cannot collide with PDF renders of a derived document.
"""

from __future__ import annotations

import os
import json
import re
import struct
import subprocess
import tempfile
import warnings
from functools import lru_cache
from pathlib import Path

try:
    from . import pdf_ocr, pdf_text_layer as text
except ImportError:
    import pdf_ocr
    import pdf_text_layer as text


PROFILE_VERSION = "djvu-native-v1"
BITONAL_READER_PROFILE = "djvu-bitonal-reader-png-v1"
DIRECT_READER_PROFILE = "djvu-primary-ocr-only-v1"
MAGIC = b"AT&TFORM"
PDF_MAGIC = b"%PDF-"
COMMAND_TIMEOUT = int(os.environ.get("PDF_DJVU_COMMAND_TIMEOUT", "600"))
# Rotation flags from the INFO chunk mapped to clockwise display rotation, as
# applied by ddjvu (verified against DjVuLibre 3.5.28).
ROTATION_DEGREES = {1: 0, 6: 270, 2: 180, 5: 90}
COLOR_CHUNKS = frozenset({b"BG44", b"BGjp", b"BG2k", b"FG44", b"FGjp", b"FG2k", b"FGbz"})
MASK_CHUNKS = frozenset({b"Sjbz", b"Smmr"})
TEXT_CHUNKS = frozenset({b"TXTa", b"TXTz"})


def native_enabled(item: dict | None = None) -> bool:
    """``PDF_DJVU_NATIVE``: off (default), ``true``/``all`` or ``pilot``.

    Pilot mode admits only exact ``repo\\0path`` keys listed (newline or ``|``
    separated) in ``PDF_DJVU_NATIVE_KEYS`` so a rollout can start with 1-3 books.
    """
    mode = os.environ.get("PDF_DJVU_NATIVE", "false").strip().lower()
    if mode in {"", "0", "false", "no", "off"}:
        return False
    if mode in {"1", "true", "yes", "all"}:
        return True
    if mode == "pilot":
        if item is None:
            return True
        keys = {value.strip() for value in re.split(r"[|\n]", os.environ.get("PDF_DJVU_NATIVE_KEYS", ""))
                if value.strip()}
        return item.get("key") in keys or f"{item.get('repo')}/{item.get('path')}" in keys
    raise ValueError("PDF_DJVU_NATIVE must be false, true/all or pilot")


def use_text_layer() -> bool:
    return os.environ.get("PDF_DJVU_TEXT_LAYER", "true").strip().lower() not in {"0", "false", "no", "off"}


def sniff(path_or_bytes) -> str:
    """Classify by magic bytes, never by the source filename."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        head = bytes(path_or_bytes[:8])
    else:
        with Path(path_or_bytes).open("rb") as stream:
            head = stream.read(8)
    if head.startswith(MAGIC):
        return "djvu"
    if head.startswith(PDF_MAGIC):
        return "pdf"
    return "unknown"


def is_native_item(item: dict) -> bool:
    return item.get("source_format") == "djvu"


EXCEPTIONS = Path(__file__).resolve().parents[1] / "djvu-native-exceptions.json"


def native_exceptions() -> dict[str, dict]:
    """Historical DjVu decode failures, retained for audit, not routing."""
    return _native_exceptions()


@lru_cache(maxsize=1)
def _native_exceptions() -> dict[str, dict]:
    try:
        value = json.loads(EXCEPTIONS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if value.get("version") != 1 or value.get("kind") != "djvu-native-exceptions":
        raise ValueError("invalid native DjVu exception list")
    books = value.get("books")
    if not isinstance(books, list):
        raise ValueError("invalid native DjVu exception list")
    result = {}
    for entry in books:
        key = entry.get("key")
        if not isinstance(key, str) or "\0" not in key:
            raise ValueError("native DjVu exception lacks an exact key")
        result[key] = entry
    return result


def native_exception(item: dict | None) -> dict | None:
    return native_exceptions().get(str((item or {}).get("key") or ""))


def render_profile(base: str) -> str:
    """Distinct namespace on top of the PDF profile's shared delivery settings."""
    return (f"{base}-{PROFILE_VERSION}-native-dpi-maxpix-{pdf_ocr.MAX_PAGE_PIXELS}"
             f"-textlayer-{int(use_text_layer())}-{DIRECT_READER_PROFILE}")


def _chunks(raw: bytes, start: int, end: int):
    offset = start
    while offset + 8 <= end:
        name = raw[offset:offset + 4]
        size = struct.unpack(">I", raw[offset + 4:offset + 8])[0]
        body = offset + 8
        if body + size > end:
            raise ValueError("truncated DjVu chunk")
        yield name, body, size
        offset = body + size + (size & 1)


def _form(raw: bytes, offset: int):
    if raw[offset:offset + 4] != b"FORM":
        raise ValueError("DjVu component is not an IFF FORM")
    size = struct.unpack(">I", raw[offset + 4:offset + 8])[0]
    end = offset + 8 + size
    if end > len(raw) or size < 4:
        raise ValueError("truncated DjVu FORM")
    return raw[offset + 8:offset + 12], offset + 12, end


def _page_info(raw: bytes, start: int, end: int, includes: dict[int, set]) -> dict:
    info, names = None, set()
    for name, body, size in _chunks(raw, start, end):
        names.add(name)
        if name == b"INFO":
            if size < 5:
                raise ValueError("invalid DjVu INFO chunk")
            data = raw[body:body + size]
            width, height = struct.unpack(">HH", data[:4])
            dpi = struct.unpack("<H", data[6:8])[0] if size >= 8 else 300
            flags = data[9] if size >= 10 else 0
            info = {"width": width, "height": height,
                    "dpi": dpi if 25 <= dpi <= 6000 else 300,
                    "rotation": ROTATION_DEGREES.get(flags & 7, 0)}
        elif name == b"INCL":
            names.update(includes.get(-1, set()))
    if not info or info["width"] < 1 or info["height"] < 1:
        raise ValueError("DjVu page lacks INFO geometry")
    has_color = bool(names & COLOR_CHUNKS)
    has_mask = bool(names & MASK_CHUNKS)
    info["layers"] = "bitonal" if has_mask and not has_color else "color" if has_color else "empty"
    info["has_text"] = bool(names & TEXT_CHUNKS)
    return info


def inspect(raw: bytes) -> list[dict]:
    """Read every page's INFO chunk from a bundled or single-page DjVu."""
    if not raw.startswith(MAGIC):
        raise ValueError("not a DjVu document")
    kind, start, end = _form(raw, 4)
    if kind == b"DJVU":
        return [_page_info(raw, start, end, {})]
    if kind != b"DJVM":
        raise ValueError("unsupported DjVu FORM type")
    chunks = list(_chunks(raw, start, end))
    if not chunks or chunks[0][0] != b"DIRM":
        raise ValueError("DjVu document lacks a directory")
    _, body, size = chunks[0]
    flags = raw[body]
    if not flags & 0x80:
        raise ValueError("indirect DjVu documents are not self-contained")
    count = struct.unpack(">H", raw[body + 1:body + 3])[0]
    if size < 3 + 4 * count:
        raise ValueError("truncated DjVu directory")
    offsets = struct.unpack(f">{count}I", raw[body + 3:body + 3 + 4 * count])
    # Shared dictionaries (DJVI) can hold the JB2 shapes used by every page;
    # their chunk names classify an INCL-ing page as well.
    shared_names = set()
    components = []
    for offset in offsets:
        component, cstart, cend = _form(raw, offset)
        components.append((component, cstart, cend))
        if component == b"DJVI":
            shared_names.update(name for name, _, _ in _chunks(raw, cstart, cend))
    pages = [_page_info(raw, cstart, cend, {-1: shared_names})
             for component, cstart, cend in components if component == b"DJVU"]
    if not pages:
        raise ValueError("DjVu document has no pages")
    return pages


def page_map(raw: bytes, source_sha256: str) -> dict:
    """Page map in the same shape as ``pdf_reading_v3.page_map``.

    ``width``/``height`` are displayed points after rotation; boxes are the
    unrotated page at its native resolution, as for PDF ``/Rotate`` pages.
    """
    pages = []
    for number, info in enumerate(inspect(raw), 1):
        scale = 72 / info["dpi"]
        width, height = info["width"] * scale, info["height"] * scale
        if info["rotation"] in {90, 270}:
            shown = (height, width)
        else:
            shown = (width, height)
        geometry = {"page": number, "media_box": [0.0, 0.0, width, height],
                    "crop_box": [0.0, 0.0, width, height], "rotation": info["rotation"],
                    "width": shown[0], "height": shown[1],
                    "classification": "bitonal-image" if info["layers"] == "bitonal" else
                                      "raster-or-mixed" if info["layers"] == "color" else "vector-or-empty",
                    "djvu": {"pixels": [info["width"], info["height"]], "dpi": info["dpi"]}}
        geometry["identity"] = text.digest({"source_sha256": source_sha256, **geometry})
        pages.append(geometry)
    return {"version": 1, "kind": "pdf-page-map", "source_sha256": source_sha256,
            "source_format": "djvu", "pages": pages}


def _run(command, *, timeout=COMMAND_TIMEOUT, binary=False):
    try:
        result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timeout: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()[-1000:]
        raise RuntimeError(f"command failed: {command[0]}: {detail}") from exc
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def page_count(path: Path) -> int:
    value = int(_run(["djvused", str(path), "-e", "n"]).split()[-1])
    if value < 1:
        raise RuntimeError("DjVu page count unavailable")
    return value


def displayed_pixels(info: dict) -> tuple[int, int]:
    width, height = info["width"], info["height"]
    return (height, width) if info["rotation"] in {90, 270} else (width, height)


def render(path: Path, page: int, destination: Path, *, info: dict, dpi: int | None = None,
           max_pixels: int | None = None):
    """Render one page at native (or capped) resolution into a lossless PNG.

    A native-resolution bitonal page is the exact JB2 bitmap (``mode=1``);
    other pages are 8-bit gray/RGB as produced by ddjvu. No resample happens
    unless the dpi or pixel cap requires one.
    """
    from PIL import Image
    max_pixels = max_pixels or pdf_ocr.MAX_PAGE_PIXELS
    width, height = displayed_pixels(info)
    scale = min(1.0, (max_pixels / (width * height)) ** 0.5)
    if dpi is not None:
        scale = min(scale, dpi / info["dpi"])
    command = ["ddjvu", "-format=pnm", f"-page={page}"]
    if scale < 1:
        target = (max(1, int(width * scale)), max(1, int(height * scale)))
        command.append(f"-size={target[0]}x{target[1]}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        pnm = Path(temp) / "page.pnm"
        _run([*command, str(path), str(pnm)])
        previous = Image.MAX_IMAGE_PIXELS
        try:
            Image.MAX_IMAGE_PIXELS = None
            with warnings.catch_warnings(), Image.open(pnm) as image:
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                image.load()
                if image.width * image.height > max_pixels:
                    raise RuntimeError("DjVu render exceeds the pixel budget")
                mode = image.mode if image.mode in {"1", "L", "RGB"} else "RGB"
                converted = image if mode == image.mode else image.convert(mode)
                converted.save(destination, "PNG", optimize=mode == "1")
                return destination, converted.width, converted.height, mode, scale
        finally:
            Image.MAX_IMAGE_PIXELS = previous


def render_page(path: Path, page: int, directory: Path, *, info: dict,
                reader_jxl: bool = False, reader_images: bool = False):
    """Produce a lossless, bounded OCR input; never produce reading images."""
    if reader_jxl or reader_images:
        raise ValueError('DjVu document reading images are retired')
    from PIL import Image
    prefix = directory / f"page-{page:06d}"
    png = prefix.with_suffix(".png")
    _, width, height, mode, _ = render(path, page, png, info=info)
    if mode == "1":
        with Image.open(png) as image:
            image.convert("L").save(png, "PNG")
    return png, width, height


_TOKEN = re.compile(rb'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_ESCAPE = re.compile(rb"\\(?:([0-7]{1,3})|(.))", re.S)
_SIMPLE = {b"n": b"\n", b"t": b"\t", b"r": b"\r", b"b": b"\b", b"f": b"\f", b"v": b"\v", b"a": b"\a"}


def _string(raw: bytes) -> str:
    def replace(match):
        if match[1]:
            return bytes([int(match[1], 8) & 0xFF])
        return _SIMPLE.get(match[2], match[2])
    return _ESCAPE.sub(replace, raw).decode("utf-8", "replace")


def parse_sexprs(raw: bytes) -> list:
    """Parse djvutxt/djvused hidden-text S-expressions (one per page)."""
    stack, output, position = [[]], [], 0
    while position < len(raw):
        if raw[position:].strip() == b"":
            break
        match = _TOKEN.match(raw, position)
        if not match:
            raise ValueError("invalid DjVu text expression")
        position = match.end()
        if match[1]:
            stack.append([])
        elif match[2]:
            if len(stack) < 2:
                raise ValueError("unbalanced DjVu text expression")
            value = stack.pop()
            stack[-1].append(value)
        elif match[3] is not None:
            stack[-1].append(_string(match[3]))
        else:
            atom = match[4].decode("ascii", "replace")
            stack[-1].append(int(atom) if re.fullmatch(r"-?[0-9]+", atom) else atom)
    if len(stack) != 1:
        raise ValueError("unbalanced DjVu text expression")
    return stack[0]


_ZONES = ("page", "column", "region", "para", "line", "word", "char")


def _zone_text(zone) -> str:
    if len(zone) >= 6 and isinstance(zone[5], str):
        return zone[5]
    children = [child for child in zone[5:] if isinstance(child, list)]
    separator = " " if zone[0] == "line" else "\n"
    return separator.join(filter(None, (_zone_text(child) for child in children)))


def _line_zones(zone):
    """Yield line-level zones; a leaf above line level counts as one line."""
    if not isinstance(zone, list) or len(zone) < 5 or zone[0] not in _ZONES:
        return
    if zone[0] in {"line", "word", "char"} or not any(isinstance(child, list) for child in zone[5:]):
        yield zone
        return
    for child in zone[5:]:
        yield from _line_zones(child)


def _rotate_box(box, rotation):
    """Normalized top-left box in the unrotated frame -> displayed frame."""
    x0, y0, x1, y1 = box
    if rotation == 90:
        return [1 - y1, x0, 1 - y0, x1]
    if rotation == 180:
        return [1 - x1, 1 - y1, 1 - x0, 1 - y0]
    if rotation == 270:
        return [y0, 1 - x1, y1, 1 - x0]
    return [x0, y0, x1, y1]


def page_text(expression, info: dict) -> dict:
    """Native-text payload (``pdf_ocr.native_pages`` shape) in displayed pixels."""
    width, height = info["width"], info["height"]
    shown_width, shown_height = displayed_pixels(info)
    blocks, lines = [], []
    if isinstance(expression, list) and expression and expression[0] == "page":
        for zone in _line_zones(expression):
            value = pdf_ocr.clean_text(_zone_text(zone))
            if not value:
                continue
            x0, y0, x1, y1 = (int(v) for v in zone[1:5])
            box = [max(0, min(1, x0 / width)), max(0, min(1, 1 - y1 / height)),
                   max(0, min(1, x1 / width)), max(0, min(1, 1 - y0 / height))]
            rx0, ry0, rx1, ry1 = _rotate_box(box, info["rotation"])
            lines.append(value)
            blocks.append({"text": value, "x0": rx0 * shown_width, "y0": ry0 * shown_height,
                           "x1": rx1 * shown_width, "y1": ry1 * shown_height,
                           "confidence": 1.0, "source": "native"})
    return {"text": "\n".join(lines).strip(), "width": shown_width, "height": shown_height,
            "blocks": pdf_ocr.normalize_blocks(blocks, shown_width, shown_height)}


def text_pages(path: Path, infos: list[dict]) -> dict[int, dict]:
    """Hidden text for every page; empty pages yield empty payloads."""
    if not use_text_layer():
        return {number: {"text": "", "width": displayed_pixels(info)[0],
                         "height": displayed_pixels(info)[1], "blocks": []}
                for number, info in enumerate(infos, 1)}
    expressions = parse_sexprs(_run(["djvutxt", "--detail=line", str(path)], binary=True))
    if len(expressions) != len(infos):
        expressions = []
        for number in range(1, len(infos) + 1):
            values = parse_sexprs(_run(["djvutxt", "--detail=line", f"--page={number}", str(path)], binary=True))
            expressions.append(values[0] if values else [])
    return {number: page_text(expression, info)
            for number, (expression, info) in enumerate(zip(expressions, infos), 1)}


def probe(path: Path) -> dict:
    """``pdf_ocr.probe_pdf`` equivalent; hidden text counts as native text."""
    infos = inspect(Path(path).read_bytes())
    count = page_count(path)
    if count != len(infos):
        raise ValueError("DjVu page count differs from the document directory")
    texts = text_pages(path, infos)
    page_chars = [len(re.sub(r"\s+", "", texts[number]["text"])) for number in range(1, count + 1)]
    native = sum(chars >= pdf_ocr.MIN_NATIVE_PAGE_CHARS for chars in page_chars)
    return {"page_count": count, "page_chars": page_chars, "native_pages": native,
            "native_page_ratio": native / count, "source_format": "djvu",
            "classification": "native-text" if native == count else "scan" if native == 0 else "mixed",
            "pages": [{key: info[key] for key in ("width", "height", "dpi", "rotation", "layers")}
                      for info in infos]}


def reader_presentation(_path: Path | None = None) -> dict:
    return {"strategy": "preserve-djvu", "reason": "djvu-primary-browser-decoding", "complete": True,
            "source_format": "djvu"}
