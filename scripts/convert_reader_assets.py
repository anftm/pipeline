#!/usr/bin/env python3
"""Download and convert one Reader Assets queue into a publishable bundle."""

import argparse
import concurrent.futures
import base64
import hashlib
import html
import mimetypes
import json
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import urllib.parse
import zipfile
import os
from pathlib import Path

from PIL import Image, ImageSequence

try:
    from .reader_assets import canonical_json, load_json
except ImportError:
    from reader_assets import canonical_json, load_json

MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_HTML_RESOURCE_BYTES = 16 * 1024 * 1024
MAX_HTML_RESOURCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_HTML_RESOURCES = 64
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
PASSWORD_RE = re.compile(r"(?:密码|password)\s*[：:]\s*([^\]〕】）)\s]+)", re.IGNORECASE)
OLE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
MIN_PAGE_CONTENT_RATIO = 0.0005
COMMAND_TIMEOUT_SECONDS = int(os.environ.get("READER_CONVERSION_COMMAND_TIMEOUT", "120"))
CONVERSION_WORKERS = max(1, int(os.environ.get("READER_CONVERSION_WORKERS", "1")))
READER_ASSETS_REPO = os.environ.get("READER_ASSETS_REPO", "vomebook/Reader-Assets")
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


def inline_html_resources(source: Path, source_url: str, work: Path) -> Path:
    """Inline safe same-tree images and stylesheets before the HTML is published."""
    text = source.read_text(encoding="utf-8", errors="replace")
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


def run_checked(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"conversion command timed out: {Path(command[0]).name}") from exc
    if result.returncode:
        raise RuntimeError(f"conversion command failed: {Path(command[0]).name}: {result.stderr[-500:]}")


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"validation command timed out: {Path(command[0]).name}") from exc
    if result.returncode:
        raise RuntimeError(f"validation command failed: {Path(command[0]).name}: {result.stderr[-500:]}")
    return result.stdout


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


def convert_file(item: dict, source: Path, target: Path, work: Path) -> None:
    ext = item["extension"]
    office_profile = (work / "libreoffice-profile").resolve().as_uri()
    if ext in {"htm", "html"}:
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
            match = PASSWORD_RE.search(item.get("path", ""))
            with source.open("rb") as handle:
                is_ole = handle.read(8) == OLE_SIGNATURE
            if match:
                import msoffcrypto
                with source.open("rb") as encrypted, target.open("wb") as decrypted:
                    document = msoffcrypto.OfficeFile(encrypted)
                    document.load_key(password=match.group(1))
                    document.decrypt(decrypted)
            elif is_ole:
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
    elif ext in {"mobi", "azw3"}:
        run_checked(["ebook-convert", str(source), str(target), "--flow-size", "0"])
    elif ext in {"tif", "tiff"}:
        convert_tiff(source, target, work)
    elif ext == "djvu":
        run_checked(["ddjvu", "-format=pdf", str(source), str(target)])
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
                else:
                    temporary = work / item["output_name"]
                    convert_file(item, source, temporary, work)
                    validate_output(temporary, item["reader_mode"])
                    if item["extension"] == "djvu":
                        validate_djvu_pdf(temporary, work)
                    if item["extension"] in {"doc", "docx", "htm", "html"} and item["reader_mode"] == "pdf":
                        validate_office_pdf(temporary, item, work, source)
                    shutil.move(temporary, target)
            else:
                validate_output(target, item["reader_mode"])
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
    reusable = queue_data.get("objects", {})
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
                    "error": type(exc).__name__,
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
