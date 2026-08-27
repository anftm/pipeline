#!/usr/bin/env python3
"""Download and convert one Reader Assets queue into a publishable bundle."""

import argparse
import concurrent.futures
import base64
import email.policy
import hashlib
import html
import mimetypes
import json
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import urllib.parse
import zipfile
import os
import posixpath
import xml.etree.ElementTree as ET
from email.parser import BytesParser
from pathlib import Path

from PIL import Image, ImageSequence

try:
    from .reader_assets import PASSWORD_RE, canonical_json, load_json, source_password
except ImportError:
    from reader_assets import PASSWORD_RE, canonical_json, load_json, source_password

MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_HTML_RESOURCE_BYTES = 16 * 1024 * 1024
MAX_HTML_RESOURCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_HTML_RESOURCES = 64
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
OLE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
MIN_PAGE_CONTENT_RATIO = 0.0005
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("READER_CONVERSION_COMMAND_TIMEOUT", "120"))
DJVU_COMMAND_TIMEOUT_SECONDS = int(os.environ.get(
    "READER_DJVU_COMMAND_TIMEOUT", str(max(600, COMMAND_TIMEOUT_SECONDS)),
))
MEDIA_COMMAND_TIMEOUT_SECONDS = int(os.environ.get(
    "READER_MEDIA_COMMAND_TIMEOUT", str(max(7200, COMMAND_TIMEOUT_SECONDS)),
))
POSTSCRIPT_COMMAND_TIMEOUT_SECONDS = int(os.environ.get(
    "READER_POSTSCRIPT_COMMAND_TIMEOUT", str(max(600, COMMAND_TIMEOUT_SECONDS)),
))
CONVERSION_WORKERS = max(1, int(os.environ.get("READER_CONVERSION_WORKERS", "1")))
READER_ASSETS_REPO = os.environ.get("READER_ASSETS_REPO", "vomebook/Reader-Assets")
CAJ2PDF_DIR = Path(os.environ.get("CAJ2PDF_DIR", "/opt/caj2pdf"))
ARTIFACT_LOCKS = {}
ARTIFACT_LOCKS_GUARD = threading.Lock()


def download_source(url: str, target: Path) -> tuple[str, int]:
    request = urllib.request.Request(url, headers={"User-Agent": "VoiceOfML-Reader-Assets/1.0"})
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise RuntimeError("source exceeds reader conversion size limit")
            digest.update(chunk)
            output.write(chunk)
    return digest.hexdigest(), size


def decode_html_source(source: Path) -> str:
    data = source.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    probe = data[:8192].decode("latin-1")
    match = re.search(r"charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)", probe, re.IGNORECASE)
    if match:
        encoding = match.group(1).lower().replace("_", "-")
        encoding = {
            "gb2312": "gb18030", "gb-2312": "gb18030", "gbk": "gb18030",
            "x-gbk": "gb18030", "x-sjis": "shift-jis", "windows-31j": "shift-jis",
        }.get(encoding, encoding)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gb18030", errors="replace")


def inline_html_resources(source: Path, source_url: str, work: Path) -> Path:
    """Inline safe same-tree images and stylesheets before the HTML is published."""
    text = decode_html_source(source)
    text = re.sub(
        r"(charset\s*=\s*)([\"']?)[^\s\"'/>;]+\2",
        lambda match: match.group(1) + ('"utf-8"' if match.group(2) else "utf-8"),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(<\?xml\b[^>]*\bencoding\s*=\s*)[\"'][^\"']+[\"']", r'\1"utf-8"', text, flags=re.IGNORECASE)
    if not re.search(r"<meta\b[^>]*\bcharset\s*=", text, re.IGNORECASE):
        text = re.sub(r"(<head\b[^>]*>)", r'\1<meta charset="utf-8">', text, count=1, flags=re.IGNORECASE)
        if not re.search(r"<meta\b[^>]*\bcharset\s*=", text, re.IGNORECASE):
            text = '<meta charset="utf-8">' + text
    base = source_url.rsplit("/", 1)[0] + "/"
    root = urllib.parse.urlsplit(base)
    root_path = root.path.rstrip("/") + "/"
    total = 0
    count = 0
    cache = {}

    def load_resource(raw: str, allowed: set[str]):
        nonlocal total, count
        raw = html.unescape(raw).strip()
        if not raw or raw.startswith(("#", "data:", "mailto:", "javascript:")):
            return None
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base, raw))
        if (parsed.scheme != "https" or parsed.netloc != root.netloc or parsed.query or
                not parsed.path.startswith(root_path) or parsed.path == root_path):
            return None
        path = parsed.path
        if path in cache:
            return cache[path]
        if count >= MAX_HTML_RESOURCES:
            return None
        suffix = Path(path).suffix.lower()
        mime = mimetypes.guess_type(path)[0] or ""
        if suffix not in allowed and mime not in allowed:
            return None
        target = work / "html-resources" / str(count)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            digest, size = download_source(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), target)
        except Exception:
            target.unlink(missing_ok=True)
            return None
        if size > MAX_HTML_RESOURCE_BYTES or total + size > MAX_HTML_RESOURCE_TOTAL_BYTES:
            target.unlink(missing_ok=True)
            return None
        data = target.read_bytes()
        total += size
        count += 1
        encoded = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        cache[path] = encoded
        return encoded

    def image(match):
        value = match.group(2)
        encoded = load_resource(value, {"image/gif", "image/jpeg", "image/png", "image/webp", ".gif", ".jpg", ".jpeg", ".png", ".webp"})
        return match.group(1) + (encoded or value) + match.group(3)

    text = re.sub(r"(\b(?:src|data-src)\s*=\s*[\"'])([^\"']+)([\"'])", image, text, flags=re.IGNORECASE)

    # Replace local stylesheet links with their downloaded, resource-checked CSS.
    def stylesheet_tag(match):
        value = match.group(2)
        raw = html.unescape(value).strip()
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base, raw))
        if parsed.path not in cache:
            load_resource(raw, {"text/css", ".css"})
        css_data = cache.get(parsed.path)
        if not css_data:
            return ""
        try:
            css = base64.b64decode(css_data.split(",", 1)[1]).decode("utf-8", "replace")
        except Exception:
            return ""
        css = re.sub(r"@import[^;]+;|url\s*\([^)]*\)", "", css, flags=re.IGNORECASE)
        return f"<style>{css}</style>"

    text = re.sub(r"<link\b[^>]*\brel\s*=\s*[\"']stylesheet[\"'][^>]*\bhref\s*=\s*([\"'])([^\"']+)\1[^>]*>\s*", stylesheet_tag, text, flags=re.IGNORECASE)
    text = re.sub(r"<link\b[^>]*\bhref\s*=\s*([\"'])([^\"']+)\1[^>]*\brel\s*=\s*[\"']stylesheet[\"'][^>]*>\s*", stylesheet_tag, text, flags=re.IGNORECASE)
    output = work / "inlined.html"
    output.write_text(text, encoding="utf-8")
    return output


def download_existing(url: str, target: Path, expected_sha256: str) -> None:
    temporary = target.with_name(f".{target.name}.download")
    try:
        digest, _ = download_source(url, temporary)
        if digest != expected_sha256:
            raise RuntimeError("reusable reader artifact digest mismatch")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_lock(path: Path) -> threading.Lock:
    with ARTIFACT_LOCKS_GUARD:
        return ARTIFACT_LOCKS.setdefault(str(path), threading.Lock())


class ReaderConversionTimeout(RuntimeError):
    pass


class ReaderConversionCommandError(RuntimeError):
    pass


def conversion_error_class(exc: Exception) -> str:
    if isinstance(exc, ReaderConversionTimeout):
        return "conversion-timeout"
    if isinstance(exc, ReaderConversionCommandError):
        return "conversion-command-failed"
    if isinstance(exc, urllib.error.URLError):
        return "source-download-failed"
    return type(exc).__name__


def run_checked(command: list[str], *, timeout_seconds: int | None = None, cwd: Path | None = None) -> None:
    timeout_seconds = COMMAND_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise ReaderConversionTimeout(f"conversion command timed out: {Path(command[0]).name}") from exc
    if result.returncode:
        detail = result.stderr.strip().replace("\n", " ")[-500:]
        raise ReaderConversionCommandError(f"conversion command failed: {Path(command[0]).name}: {detail}")


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"validation command timed out: {Path(command[0]).name}") from exc
    if result.returncode:
        raise RuntimeError(f"validation command failed: {Path(command[0]).name}: {result.stderr[-500:]}")
    return result.stdout


def media_probe(path: Path) -> dict:
    try:
        return json.loads(command_output([
            "ffprobe", "-v", "error", "-show_format", "-show_streams",
            "-print_format", "json", str(path),
        ]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("media probe returned invalid data") from exc


def validate_media_output(path: Path, reader_mode: str) -> None:
    probe = media_probe(path)
    streams = probe.get("streams") if isinstance(probe, dict) else None
    media_format = probe.get("format") if isinstance(probe, dict) else None
    if not isinstance(streams, list) or not isinstance(media_format, dict):
        raise RuntimeError("media output has no stream metadata")
    try:
        duration = float(media_format.get("duration") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("media output has invalid duration") from exc
    if not 0 < duration <= 24 * 60 * 60:
        raise RuntimeError("media output duration is outside limits")
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    unexpected = [stream for stream in streams if stream.get("codec_type") not in {"audio", "video"}]
    format_names = set(str(media_format.get("format_name") or "").split(","))
    if unexpected:
        raise RuntimeError("media output contains unsupported streams")
    if reader_mode == "audio":
        if len(audio) != 1 or video or audio[0].get("codec_name") != "mp3" or "mp3" not in format_names:
            raise RuntimeError("conversion output is not compatible MP3 audio")
        return
    if reader_mode != "video" or len(video) != 1 or len(audio) > 1:
        raise RuntimeError("conversion output has invalid video streams")
    width, height = int(video[0].get("width") or 0), int(video[0].get("height") or 0)
    if (video[0].get("codec_name") != "h264" or "mp4" not in format_names
            or not 0 < width <= 1920 or not 0 < height <= 1080
            or (audio and audio[0].get("codec_name") != "aac")):
        raise RuntimeError("conversion output is not compatible H.264/AAC video")


def embedded_pdf_fonts(path: Path) -> list[str]:
    fonts = command_output(["pdffonts", str(path)]).splitlines()[2:]
    return [line for line in fonts if len(line.split()) >= 6 and line.split()[-5].lower() == "yes"]


def outline_pdf_fonts(path: Path, work: Path) -> None:
    rewritten = work / "outlined-fonts.pdf"
    run_checked([
        "gs", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.7",
        "-dNoOutputFonts=true",
        f"-sOutputFile={rewritten}", str(path),
    ])
    if not rewritten.is_file():
        raise RuntimeError("Ghostscript produced no PDF")
    shutil.move(rewritten, path)


def convert_tiff(source: Path, target: Path, work: Path) -> None:
    pages = work / "tiff-pages"
    pages.mkdir()
    outputs = []
    with Image.open(source) as image:
        for index, frame in enumerate(ImageSequence.Iterator(image), start=1):
            output = pages / f"{index:08d}.pdf"
            converted = frame.copy().convert("RGB")
            converted.save(output, "PDF", resolution=150.0)
            converted.close()
            outputs.append(output)
    if not outputs:
        raise RuntimeError("TIFF has no frames")
    if len(outputs) == 1:
        shutil.move(outputs[0], target)
    else:
        run_checked(["pdfunite", *(str(output) for output in outputs), str(target)])


def mhtml_to_html(source: Path, target: Path) -> None:
    message = BytesParser(policy=email.policy.default).parsebytes(source.read_bytes())
    parts = list(message.walk()) if message.is_multipart() else [message]
    html_part = next((part for part in parts if part.get_content_type() == "text/html"), None)
    if html_part is None:
        raise RuntimeError("CHM MHTML has no HTML body")
    payload = html_part.get_payload(decode=True) or b""
    charset = html_part.get_content_charset() or "utf-8"
    try:
        document = payload.decode(charset)
    except (LookupError, UnicodeDecodeError):
        document = payload.decode("gb18030", "replace")
    base = str(html_part.get("Content-Location") or "")

    def safe_join(root: str, value: str) -> str:
        root, value = root.replace("\\", "/"), value.replace("\\", "/")
        root = re.sub(r"^file://([A-Za-z]:/)", r"file:///\1", root)
        if re.match(r"^[A-Za-z]:/", root):
            root = "file:///" + root
        try:
            return urllib.parse.urljoin(root, value)
        except ValueError:
            return ""
    resources = {}
    for part in parts:
        if part is html_part or part.is_multipart():
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        encoded = f"data:{part.get_content_type()};base64,{base64.b64encode(data).decode('ascii')}"
        location = str(part.get("Content-Location") or "")
        content_id = str(part.get("Content-ID") or "").strip("<>")
        if location:
            resources[location] = encoded
            joined = safe_join(base, location)
            if joined:
                resources[joined] = encoded
        if content_id:
            resources[f"cid:{content_id}"] = encoded

    def inline_resource(match):
        value = html.unescape(match.group(2)).strip()
        replacement = resources.get(value) or resources.get(safe_join(base, value))
        return match.group(1) + (replacement or value) + match.group(3)

    document = re.sub(
        r"(\b(?:src|href|poster)\s*=\s*[\"'])([^\"']+)([\"'])",
        inline_resource,
        document,
        flags=re.IGNORECASE,
    )
    document = sanitize_chm_html(document)
    document = re.sub(r"<meta\b[^>]*charset[^>]*>", "", document, flags=re.IGNORECASE)
    document = re.sub(r"(<head\b[^>]*>)", r'\1<meta charset="utf-8">', document, count=1, flags=re.IGNORECASE)
    target.write_text(document, encoding="utf-8")


def sanitize_chm_html(document: str) -> str:
    document = re.sub(r"<(script|object|iframe)\b[^>]*>.*?</\1\s*>", "", document, flags=re.IGNORECASE | re.DOTALL)
    document = re.sub(r"<(?:embed|base)\b[^>]*?/?>", "", document, flags=re.IGNORECASE)
    document = re.sub(r"\s+on[a-z0-9_-]+\s*=\s*([\"']).*?\1", "", document, flags=re.IGNORECASE | re.DOTALL)
    document = re.sub(r"\s+(?:href|src)\s*=\s*([\"'])\s*javascript:.*?\1", "", document, flags=re.IGNORECASE | re.DOTALL)
    document = re.sub(r"\s+(?:src|poster)\s*=\s*([\"'])\s*(?:https?:)?//.*?\1", "", document, flags=re.IGNORECASE | re.DOTALL)
    document = re.sub(r"@import\s+[^;]+;", "", document, flags=re.IGNORECASE)
    document = re.sub(r"url\(\s*([\"']?)(?:https?:)?//.*?\1\s*\)", "none", document, flags=re.IGNORECASE)
    return document


def sanitize_chm_epub(path: Path, work: Path) -> None:
    rewritten = work / "sanitized-chm.epub"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(rewritten, "w") as target:
        infos = sorted(source.infolist(), key=lambda info: info.filename != "mimetype")
        for info in infos:
            data = source.read(info.filename)
            suffix = Path(info.filename).suffix.lower()
            if suffix in {".htm", ".html", ".xhtml", ".css"}:
                data = sanitize_chm_html(data.decode("utf-8", "replace")).encode("utf-8")
            if info.filename == "mimetype":
                info.compress_type = zipfile.ZIP_STORED
            target.writestr(info, data)
    shutil.move(rewritten, path)


def convert_chm(source: Path, target: Path, work: Path) -> None:
    try:
        run_checked(["ebook-convert", str(source), str(target), "--flow-size", "0"])
        sanitize_chm_epub(target, work)
        validate_output(target, "epub")
        validate_chm_epub(target)
        return
    except RuntimeError as exc:
        initial_error = exc
    extracted = work / "chm-extracted"
    extracted.mkdir()
    run_checked(["7z", "x", "-y", f"-o{extracted}", str(source)])
    mhtml_files = sorted((*extracted.rglob("*.mht"), *extracted.rglob("*.mhtml")))
    html_files = sorted((*extracted.rglob("*.htm"), *extracted.rglob("*.html")))
    if not mhtml_files and not html_files:
        raise initial_error
    prepared = work / "chm-mhtml"
    prepared.mkdir()
    documents = []
    if mhtml_files:
        for index, mhtml_file in enumerate(mhtml_files):
            output = prepared / f"{index:04d}.html"
            mhtml_to_html(mhtml_file, output)
            documents.append(output)
    else:
        # Some CHM files contain ordinary HTML pages directly rather than MHTML.
        # Keep their relative asset paths so ebook-convert can collect images and
        # linked pages into the resulting EPUB.
        shutil.copytree(extracted, prepared, dirs_exist_ok=True)
        documents = [prepared / html_file.relative_to(extracted) for html_file in html_files]
    source_html = documents[0]
    if len(documents) > 1:
        source_html = prepared / "index.html"
        links = "".join(
            f'<li><a href="{urllib.parse.quote(item.relative_to(prepared).as_posix())}">'
            f'{html.escape(item.stem)}</a></li>'
            for item in documents
        )
        source_html.write_text(f'<meta charset="utf-8"><ul>{links}</ul>', encoding="utf-8")
    target.unlink(missing_ok=True)
    run_checked(["ebook-convert", str(source_html), str(target), "--flow-size", "0"])
    sanitize_chm_epub(target, work)
    validate_output(target, "epub")
    validate_chm_epub(target)


def detect_caj_family(source: Path) -> str:
    header = source.read_bytes()[:16]
    if header.startswith(b"%PDF-"):
        return "pdf"
    if header.startswith(b"KDH "):
        return "kdh"
    if header.startswith(b"CAJ"):
        return "caj"
    if header.startswith(b"HN"):
        return "hn"
    if header.startswith(b"\xc8"):
        return "c8"
    if header[:8] == b"\0" * 8:
        return "hn"
    raise RuntimeError("unsupported CAJ-family container")


def extract_kdh_pdf(source: Path, target: Path, work: Path) -> None:
    data = source.read_bytes()
    if len(data) <= 254:
        raise RuntimeError("KDH container is truncated")
    key = b"FZHMEI"
    decrypted = bytes(value ^ key[index % len(key)] for index, value in enumerate(data[254:]))
    end = decrypted.rfind(b"%%EOF")
    if end < 0:
        raise RuntimeError("KDH container has no embedded PDF terminator")
    raw = work / "kdh-raw.pdf"
    raw.write_bytes(decrypted[:end + 5])
    run_checked(["mutool", "clean", str(raw), str(target)])


def convert_caj_family(source: Path, target: Path, work: Path) -> None:
    kind = detect_caj_family(source)
    if kind == "pdf":
        shutil.copyfile(source, target)
        return
    if kind == "kdh":
        extract_kdh_pdf(source, target, work)
        return
    converter = CAJ2PDF_DIR / "caj2pdf"
    if not converter.is_file():
        raise RuntimeError("pinned caj2pdf converter is unavailable")
    for library in ("libjbigdec.so", "libjbig2codec.so"):
        source_library = CAJ2PDF_DIR / library
        if source_library.is_file():
            shutil.copyfile(source_library, work / library)
    run_checked(["python3", str(converter), "convert", str(source), "--output", str(target)], cwd=work)


def validate_djvu_pdf(path: Path, work: Path) -> None:
    info = command_output(["pdfinfo", str(path)])
    match = re.search(r"^Pages:\s+(\d+)\s*$", info, re.MULTILINE)
    if not match or int(match.group(1)) < 1:
        raise RuntimeError("converted DjVu PDF has no pages")
    page_count = int(match.group(1))
    for page in sorted({1, page_count}):
        prefix = work / f"djvu-page-{page}"
        run_checked([
            "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-png", "-r", "72",
            str(path), str(prefix),
        ])
        rendered = prefix.with_suffix(".png")
        if not rendered.is_file() or rendered.stat().st_size == 0:
            raise RuntimeError(f"converted DjVu PDF page {page} is not renderable")


def validate_pdf_content(path: Path, work: Path) -> None:
    info = command_output(["pdfinfo", str(path)])
    match = re.search(r"^Pages:\s+(\d+)\s*$", info, re.MULTILINE)
    if not match or int(match.group(1)) < 1:
        raise RuntimeError("converted PDF has no pages")
    page_count = int(match.group(1))
    if page_count <= 12:
        samples = list(range(1, page_count + 1))
    else:
        samples = sorted({1, 2, page_count // 4, page_count // 2, page_count * 3 // 4, page_count - 1, page_count})
    content_pages = []
    sample_dir = work / "pdf-content-samples"
    sample_dir.mkdir(exist_ok=True)
    for page in samples:
        prefix = sample_dir / f"page-{page}"
        run_checked([
            "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-png", "-r", "72",
            str(path), str(prefix),
        ])
        rendered = prefix.with_suffix(".png")
        if not rendered.is_file() or rendered.stat().st_size == 0:
            raise RuntimeError(f"converted PDF page {page} is not renderable")
        if page_content_ratio(rendered) >= MIN_PAGE_CONTENT_RATIO:
            content_pages.append(page)
    if not content_pages:
        raise RuntimeError("converted PDF has no visible page content")
    if page_count >= 4 and not any(page > page_count // 2 for page in content_pages):
        raise RuntimeError("converted PDF has no visible content after its midpoint")


def validate_pdf_structure(path: Path) -> None:
    info = command_output(["pdfinfo", str(path)])
    match = re.search(r"^Pages:\s+(\d+)\s*$", info, re.MULTILINE)
    if not match or int(match.group(1)) < 1:
        raise RuntimeError("converted PDF has no pages")


def rasterize_pdf(path: Path, work: Path) -> None:
    pages = work / "raster-pages"
    pages.mkdir()
    run_checked(["pdftoppm", "-png", "-r", "150", str(path), str(pages / "page")])
    images = sorted(pages.glob("page-*.png"), key=lambda item: int(item.stem.rsplit("-", 1)[-1]))
    if not images:
        raise RuntimeError("PDF rasterization produced no pages")
    sample = "".join(command_output(["tesseract", str(image), "stdout", "-l", "chi_sim"]) for image in images[:3])
    if not CJK_RE.search(sample):
        raise RuntimeError("rasterized office PDF has no recognizable CJK text")
    content = [page_content_ratio(image) >= MIN_PAGE_CONTENT_RATIO for image in images]
    for index in range(1, len(images) - 1):
        if content[index] or not any(content[:index]) or not any(content[index + 1:]):
            continue
        replacement = pages / f"ghostscript-{index + 1}.png"
        run_checked([
            "gs", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=png16m", "-r150",
            f"-dFirstPage={index + 1}", f"-dLastPage={index + 1}",
            f"-sOutputFile={replacement}", str(path),
        ])
        if not replacement.is_file() or page_content_ratio(replacement) < MIN_PAGE_CONTENT_RATIO:
            raise RuntimeError(f"rasterized office PDF has blank interior page {index + 1}")
        shutil.move(replacement, images[index])
    frames = [Image.open(image).convert("RGB") for image in images]
    rewritten = work / "rasterized.pdf"
    frames[0].save(rewritten, "PDF", save_all=True, append_images=frames[1:], resolution=150.0)
    for frame in frames:
        frame.close()
    shutil.move(rewritten, path)


def page_content_ratio(path: Path) -> float:
    with Image.open(path) as image:
        grayscale = image.convert("L")
        histogram = grayscale.histogram()
        pixels = grayscale.width * grayscale.height
    return sum(histogram[:245]) / pixels if pixels else 0.0


def epub_package(path: Path):
    archive = zipfile.ZipFile(path)
    try:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        opf_path = rootfile.attrib["full-path"]
        package = ET.fromstring(archive.read(opf_path))
    except (KeyError, ET.ParseError, AttributeError) as exc:
        archive.close()
        raise RuntimeError("converted EPUB has no package document") from exc
    return archive, package, opf_path


def validate_epub_content(path: Path) -> None:
    archive, package, opf_path = epub_package(path)
    with archive:
        names = set(archive.namelist())
        manifest = {item.attrib.get("id"): item for item in package.findall(".//{*}manifest/{*}item") if item.attrib.get("id")}
        spine = [item.attrib.get("idref") for item in package.findall(".//{*}spine/{*}itemref")]
        if not spine:
            raise RuntimeError("converted EPUB has no spine")
        base = posixpath.dirname(opf_path)
        meaningful = []
        for idref in spine:
            item = manifest.get(idref)
            if item is None or not item.attrib.get("href"):
                raise RuntimeError("converted EPUB has an invalid spine reference")
            href = urllib.parse.unquote(item.attrib["href"].split("#", 1)[0])
            document_path = posixpath.normpath(posixpath.join(base, href))
            if document_path.startswith("../"):
                raise RuntimeError("converted EPUB spine escapes its package directory")
            try:
                document = archive.read(document_path).decode("utf-8", "replace")
                root = ET.fromstring(document)
            except KeyError as exc:
                raise RuntimeError("converted EPUB spine document is missing") from exc
            except ET.ParseError as exc:
                raise RuntimeError("converted EPUB has malformed spine content") from exc
            if re.search(r"<(?:script|object|embed|iframe)\b", document, re.IGNORECASE):
                raise RuntimeError("converted EPUB contains active content")
            if re.search(r"\b(?:src|poster)\s*=\s*[\"']\s*(?:https?:)?//", document, re.IGNORECASE):
                raise RuntimeError("converted EPUB contains an external embedded resource")
            body = root.find(".//{*}body")
            text = html.unescape("".join(body.itertext())) if body is not None else ""
            has_text = len(re.sub(r"\s+", "", text)) >= 20
            has_image = False
            for image in root.findall(".//{*}img"):
                source = urllib.parse.unquote((image.attrib.get("src") or "").split("#", 1)[0])
                image_path = posixpath.normpath(posixpath.join(posixpath.dirname(document_path), source))
                if source and not image_path.startswith("../") and image_path in names:
                    has_image = True
                    break
            meaningful.append(has_text or has_image)
        if not any(meaningful):
            raise RuntimeError("converted EPUB has no readable content")
        if len(meaningful) >= 4 and not any(meaningful[len(meaningful) // 2:]):
            raise RuntimeError("converted EPUB has no readable content after its midpoint")


def validate_docx_content(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        try:
            document = ET.fromstring(archive.read("word/document.xml"))
        except (KeyError, ET.ParseError) as exc:
            raise RuntimeError("converted DOCX has malformed document content") from exc
        text = "".join(node.text or "" for node in document.findall(".//{*}t"))
        visible_objects = document.findall(".//{*}drawing") + document.findall(".//{*}pict") + document.findall(".//{*}object")
        if not re.sub(r"\s+", "", text) and not visible_objects:
            raise RuntimeError("converted DOCX has no readable content")


def validate_html_content(path: Path) -> None:
    document = path.read_text(encoding="utf-8", errors="replace")
    visible = re.sub(r"<(script|style|template|object|iframe)\b[^>]*>.*?</\1\s*>", "", document, flags=re.IGNORECASE | re.DOTALL)
    visible = html.unescape(re.sub(r"<[^>]+>", " ", visible))
    has_image = bool(re.search(r"<img\b[^>]*\bsrc\s*=\s*[\"']data:image/", document, re.IGNORECASE))
    if not re.sub(r"\s+", "", visible) and not has_image:
        raise RuntimeError("converted HTML has no readable content")


def validate_reader_content(path: Path, item: dict, work: Path) -> None:
    mode = item["reader_mode"]
    if mode == "pdf":
        if item["extension"] == "ps":
            validate_pdf_structure(path)
        else:
            validate_pdf_content(path, work)
    elif mode == "epub":
        validate_epub_content(path)
    elif mode == "docx":
        validate_docx_content(path)
    elif mode == "html":
        validate_html_content(path)


def convert_file(item: dict, source: Path, target: Path, work: Path) -> None:
    ext = item["extension"]
    office_profile = (work / "libreoffice-profile").resolve().as_uri()
    password = source_password(item.get("repo", ""), item.get("path", ""))
    if password and ext in {"doc", "docx", "ppt", "pptx", "pps", "xls", "xlsx"}:
        import msoffcrypto
        with source.open("rb") as encrypted:
            document = msoffcrypto.OfficeFile(encrypted)
            if document.is_encrypted():
                decrypted_source = work / f"decrypted.{ext}"
                document.load_key(password=password)
                with decrypted_source.open("wb") as decrypted:
                    document.decrypt(decrypted)
                source = decrypted_source
    if ext == "pdf":
        if not password:
            raise RuntimeError("protected PDF has no known password")
        run_checked(["qpdf", f"--password={password}", "--decrypt", str(source), str(target)])
    elif ext in {"htm", "html"}:
        source_url = item.get("source_url")
        prepared = inline_html_resources(source, source_url, work) if source_url else source
        shutil.copyfile(prepared, target)
    elif ext == "doc":
        out = work / "office"
        out.mkdir()
        with source.open("rb") as handle:
            prefix = handle.read(256).lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
        office_source = source
        if prefix.lower().startswith((b"<html", b"<!doctype html")):
            office_source = work / "source.html"
            shutil.copyfile(source, office_source)
            intermediate = work / "html-office"
            intermediate.mkdir()
            run_checked(["libreoffice", "--headless", f"-env:UserInstallation={office_profile}", "--convert-to", "odt", "--outdir", str(intermediate), str(office_source)])
            office_source = intermediate / "source.odt"
            if not office_source.is_file():
                raise RuntimeError("LibreOffice produced no ODT from HTML source")
        run_checked(["libreoffice", "--headless", f"-env:UserInstallation={office_profile}", "--convert-to", "docx", "--outdir", str(out), str(office_source)])
        produced = out / f"{office_source.stem}.docx"
        if not produced.exists():
            raise RuntimeError("LibreOffice produced no DOCX")
        shutil.move(produced, target)
    elif ext == "docx":
        try:
            with zipfile.ZipFile(source) as archive:
                archive.getinfo("word/document.xml")
            shutil.copyfile(source, target)
        except (zipfile.BadZipFile, KeyError):
            with source.open("rb") as handle:
                is_ole = handle.read(8) == OLE_SIGNATURE
            if is_ole:
                out = work / "mislabeled-office"
                out.mkdir()
                mislabeled = work / "mislabeled.doc"
                shutil.copyfile(source, mislabeled)
                run_checked(["libreoffice", "--headless", f"-env:UserInstallation={office_profile}", "--convert-to", "docx", "--outdir", str(out), str(mislabeled)])
                produced = out / "mislabeled.docx"
                if not produced.is_file():
                    raise RuntimeError("LibreOffice produced no DOCX from mislabeled source")
                shutil.move(produced, target)
            else:
                raise
    elif ext in {"mobi", "azw3", "fb2", "odt"}:
        run_checked(["ebook-convert", str(source), str(target), "--flow-size", "0"])
    elif ext == "rtf":
        try:
            run_checked(["ebook-convert", str(source), str(target), "--flow-size", "0"])
        except ReaderConversionCommandError:
            target.unlink(missing_ok=True)
            intermediate = work / "rtf-html"
            intermediate.mkdir()
            run_checked([
                "libreoffice", "--headless", f"-env:UserInstallation={office_profile}",
                "--convert-to", "html", "--outdir", str(intermediate), str(source),
            ])
            html_source = intermediate / f"{source.stem}.html"
            if not html_source.is_file():
                raise RuntimeError("LibreOffice produced no HTML from RTF")
            run_checked(["ebook-convert", str(html_source), str(target), "--flow-size", "0"])
    elif ext == "chm":
        convert_chm(source, target, work)
    elif ext in {"tif", "tiff"}:
        convert_tiff(source, target, work)
    elif ext == "djvu":
        run_checked(["ddjvu", "-format=pdf", str(source), str(target)], timeout_seconds=DJVU_COMMAND_TIMEOUT_SECONDS)
    elif ext in {"ppt", "pptx", "pps", "odp", "xls", "xlsx", "csv", "ods", "wps"}:
        out = work / "office-pdf"
        out.mkdir()
        office_source = source
        if ext == "xlsx" and source.read_bytes()[:8] == OLE_SIGNATURE:
            password = os.environ.get("READER_CONVERSION_PASSWORD", "")
            if password:
                import msoffcrypto
                with source.open("rb") as encrypted:
                    document = msoffcrypto.OfficeFile(encrypted)
                    office_source = work / "decrypted.xlsx"
                    document.load_key(password=password)
                    with office_source.open("wb") as decrypted:
                        document.decrypt(decrypted)
            else:
                office_source = work / "source.xls"
                shutil.copyfile(source, office_source)
        run_checked([
            "libreoffice", "--headless", f"-env:UserInstallation={office_profile}",
            "--convert-to", "pdf", "--outdir", str(out), str(office_source),
        ])
        produced = out / f"{office_source.stem}.pdf"
        if not produced.is_file():
            raise RuntimeError("LibreOffice produced no PDF")
        shutil.move(produced, target)
    elif ext in {"mht", "mhtml"}:
        mhtml_to_html(source, target)
    elif ext == "ps":
        run_checked([
            "gs", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.7",
            f"-sOutputFile={target}", str(source),
        ], timeout_seconds=POSTSCRIPT_COMMAND_TIMEOUT_SECONDS)
    elif ext in {"caj", "kdh"}:
        convert_caj_family(source, target, work)
    elif ext in {"ape", "wma", "amr"}:
        run_checked([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-map_metadata", "-1", "-c:a", "libmp3lame", "-q:a", "3", str(target),
        ], timeout_seconds=MEDIA_COMMAND_TIMEOUT_SECONDS)
    elif ext in {"flv", "f4v", "rm", "rmvb", "mkv", "avi", "mpg", "mpeg", "mts", "ts", "wmv"}:
        run_checked([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
            "-map_metadata", "-1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(target),
        ], timeout_seconds=MEDIA_COMMAND_TIMEOUT_SECONDS)
    else:
        raise ValueError(f"unsupported conversion extension: {ext}")


def normalized_office_pdf(source: Path, work: Path) -> Path:
    normalized = work / "normalized-office"
    normalized.mkdir()
    run_checked(["libreoffice", "--headless", "--convert-to", "docx", "--outdir", str(normalized), str(source)])
    docx = normalized / f"{source.stem}.docx"
    if not docx.is_file():
        raise RuntimeError("LibreOffice produced no normalized DOCX")
    pdf_dir = work / "normalized-pdf"
    pdf_dir.mkdir()
    run_checked(["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", str(pdf_dir), str(docx)])
    pdf = pdf_dir / f"{source.stem}.pdf"
    if not pdf.is_file():
        raise RuntimeError("LibreOffice produced no normalized PDF")
    return pdf


def validate_output(path: Path, reader_mode: str) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("conversion output is empty")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise RuntimeError("conversion output exceeds size limit")
    if reader_mode == "pdf" and not path.read_bytes()[:5] == b"%PDF-":
        raise RuntimeError("conversion output is not a PDF")
    if reader_mode == "html" and not path.read_bytes():
        raise RuntimeError("conversion output is empty HTML")
    if reader_mode == "epub":
        with zipfile.ZipFile(path) as archive:
            if archive.read("mimetype") != b"application/epub+zip":
                raise RuntimeError("conversion output is not an EPUB")
    if reader_mode == "docx":
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise RuntimeError("conversion output is not a DOCX")
            if not archive.read("word/document.xml").strip():
                raise RuntimeError("DOCX document body is empty")
    if reader_mode in {"audio", "video"}:
        validate_media_output(path, reader_mode)


def validate_chm_epub(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            opf_path = rootfile.attrib["full-path"]
            package = ET.fromstring(archive.read(opf_path))
        except (KeyError, ET.ParseError, AttributeError) as exc:
            raise RuntimeError("converted CHM EPUB has no package document") from exc

        manifest = {
            item.attrib.get("id"): item
            for item in package.findall(".//{*}manifest/{*}item")
            if item.attrib.get("id")
        }
        spine = [item.attrib.get("idref") for item in package.findall(".//{*}spine/{*}itemref")]
        if not spine:
            raise RuntimeError("converted CHM EPUB has no spine")

        readable_characters = 0
        base = posixpath.dirname(opf_path)
        for idref in spine:
            item = manifest.get(idref)
            if item is None or not item.attrib.get("href"):
                raise RuntimeError("converted CHM EPUB has an invalid spine reference")
            href = urllib.parse.unquote(item.attrib["href"].split("#", 1)[0])
            document_path = posixpath.normpath(posixpath.join(base, href))
            try:
                document = archive.read(document_path).decode("utf-8", "replace")
            except KeyError as exc:
                raise RuntimeError("converted CHM EPUB spine document is missing") from exc
            if re.search(r"<(?:script|object|embed|iframe)\b", document, re.IGNORECASE):
                raise RuntimeError("converted CHM EPUB contains active content")
            if re.search(r"\b(?:src|poster)\s*=\s*[\"']\s*(?:https?:)?//", document, re.IGNORECASE):
                raise RuntimeError("converted CHM EPUB contains an external embedded resource")
            try:
                root = ET.fromstring(document)
            except ET.ParseError as exc:
                raise RuntimeError("converted CHM EPUB has malformed spine content") from exc
            body = root.find(".//{*}body")
            if body is not None:
                text = html.unescape("".join(body.itertext()))
                readable_characters += len(re.sub(r"\s+", "", text))
        if readable_characters < 20:
            raise RuntimeError("converted CHM EPUB has no readable content")


def validate_office_pdf(path: Path, item: dict, work: Path, source: Path | None = None) -> None:
    text = command_output(["pdftotext", str(path), "-"])
    has_cjk_path = bool(CJK_RE.search(item.get("path", "")))
    if has_cjk_path and not CJK_RE.search(text):
        try:
            rasterize_pdf(path, work)
        except RuntimeError as exc:
            if source is None or item.get("profile") != "libreoffice-pdf-v3" or "blank interior page" not in str(exc):
                raise
            candidate = normalized_office_pdf(source, work)
            shutil.move(candidate, path)
            rasterize_pdf(path, work)
        validate_output(path, "pdf")
        return
    if not embedded_pdf_fonts(path):
        outline_pdf_fonts(path, work)
        validate_output(path, "pdf")


def convert_item(item: dict, bundle: Path, reusable: dict | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="reader-convert-") as root:
        work = Path(root)
        source = work / f"source.{item['extension']}"
        digest, source_bytes = download_source(item["source_url"], source)
        existing = (reusable or {}).get(f"{digest}\0{item['profile']}")
        object_path = existing["path"] if existing else f"objects/{digest[:2]}/{digest}/{item['profile']}/{item['output_name']}"
        target = bundle / object_path
        target.parent.mkdir(parents=True, exist_ok=True)
        reused = existing is not None
        with artifact_lock(target):
            if not target.exists():
                if existing:
                    asset_url = f"https://huggingface.co/datasets/{READER_ASSETS_REPO}/resolve/main/{existing['path']}"
                    download_existing(asset_url, target, existing["sha256"])
                    if target.stat().st_size != existing["bytes"]:
                        raise RuntimeError("reusable reader artifact size mismatch")
                    validate_output(target, item["reader_mode"])
                    if item["extension"] == "chm":
                        validate_chm_epub(target)
                    validate_reader_content(target, item, work)
                else:
                    temporary = work / item["output_name"]
                    convert_file(item, source, temporary, work)
                    validate_output(temporary, item["reader_mode"])
                    if item["extension"] == "chm":
                        validate_chm_epub(temporary)
                    if item["extension"] == "djvu":
                        validate_djvu_pdf(temporary, work)
                    if item["extension"] in {"doc", "docx", "htm", "html", "ppt", "pptx", "pps", "odp", "xls", "xlsx", "csv", "ods", "wps"} and item["reader_mode"] == "pdf":
                        validate_office_pdf(temporary, item, work, source)
                    validate_reader_content(temporary, item, work)
                    shutil.move(temporary, target)
            else:
                validate_output(target, item["reader_mode"])
                if item["extension"] == "chm":
                    validate_chm_epub(target)
                validate_reader_content(target, item, work)
                if existing and file_sha256(target) != existing["sha256"]:
                    raise RuntimeError("reusable reader artifact digest mismatch")
        return {
            "key": item["key"], "status": "ready", "source_revision": item["source_revision"],
            "source_sha256": digest, "source_bytes": source_bytes,
            "source_extension": item["extension"], "profile": item["profile"],
            "reader_mode": item["reader_mode"], "path": object_path, "bytes": target.stat().st_size,
            "sha256": file_sha256(target), "reused": reused,
        }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, default=Path("output/reader-assets/queue.json"))
    parser.add_argument("--bundle", type=Path, default=Path("output/reader-assets/bundle"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    queue_data = load_json(args.queue)
    queue = queue_data.get("items", [])
    reusable = {} if queue_data.get("force_rebuild") else queue_data.get("objects", {})
    if args.bundle.exists():
        shutil.rmtree(args.bundle)
    args.bundle.mkdir(parents=True)
    results = []
    if args.dry_run:
        print(f"dry run: would convert {len(queue)} reader asset(s)")
    else:
        def convert(item):
            try:
                return convert_item(item, args.bundle, reusable)
            except Exception as exc:
                print(f"failed: {item['repo']}/{item['path']}: {exc}")
                return {
                    "key": item["key"], "status": "failed", "source_revision": item["source_revision"],
                    "source_extension": item["extension"], "profile": item["profile"],
                    "error": conversion_error_class(exc),
                }
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONVERSION_WORKERS) as executor:
            results.extend(executor.map(convert, queue))
    bundle_data = {
        "version": 1,
        "results": results,
    }
    if queue_data.get("authoritative_snapshot") is True:
        bundle_data["active_keys"] = queue_data.get("active_keys", [])
        bundle_data["authoritative_snapshot"] = True
    (args.bundle / "bundle.json").write_bytes(canonical_json(bundle_data, pretty=True))
    failed = sum(item["status"] == "failed" for item in results)
    print(f"converted {len(results) - failed}; failed {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
