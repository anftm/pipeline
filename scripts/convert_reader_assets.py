#!/usr/bin/env python3
"""Download and convert one Reader Assets queue into a publishable bundle."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from PIL import Image, ImageSequence

try:
    from .reader_assets import canonical_json, load_json
except ImportError:
    from reader_assets import canonical_json, load_json

MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
PASSWORD_RE = re.compile(r"(?:密码|password)\s*[：:]\s*([^\]〕】）)\s]+)", re.IGNORECASE)
OLE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
MIN_PAGE_CONTENT_RATIO = 0.0005


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_checked(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"conversion command timed out: {Path(command[0]).name}") from exc
    if result.returncode:
        raise RuntimeError(f"conversion command failed: {Path(command[0]).name}: {result.stderr[-500:]}")


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
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


def convert_tiff(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        frames = [frame.copy().convert("RGB") for frame in ImageSequence.Iterator(image)]
    if not frames:
        raise RuntimeError("TIFF has no frames")
    first, rest = frames[0], frames[1:]
    first.save(target, "PDF", save_all=True, append_images=rest, resolution=150.0)
    for frame in frames:
        frame.close()


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
    if ext == "doc":
        out = work / "office"
        out.mkdir()
        run_checked(["libreoffice", "--headless", "--convert-to", "docx", "--outdir", str(out), str(source)])
        produced = out / f"{source.stem}.docx"
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
                run_checked(["libreoffice", "--headless", "--convert-to", "docx", "--outdir", str(out), str(mislabeled)])
                produced = out / "mislabeled.docx"
                if not produced.is_file():
                    raise RuntimeError("LibreOffice produced no DOCX from mislabeled source")
                shutil.move(produced, target)
            else:
                raise
    elif ext in {"mobi", "azw3"}:
        run_checked(["ebook-convert", str(source), str(target)])
    elif ext in {"tif", "tiff"}:
        convert_tiff(source, target)
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


def convert_item(item: dict, bundle: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="reader-convert-") as root:
        work = Path(root)
        source = work / f"source.{item['extension']}"
        digest, source_bytes = download_source(item["source_url"], source)
        object_path = f"objects/{digest[:2]}/{digest}/{item['profile']}/{item['output_name']}"
        target = bundle / object_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = work / item["output_name"]
            convert_file(item, source, temporary, work)
            validate_output(temporary, item["reader_mode"])
            if item["extension"] in {"doc", "docx"} and item["reader_mode"] == "pdf":
                validate_office_pdf(temporary, item, work, source)
            shutil.move(temporary, target)
        else:
            validate_output(target, item["reader_mode"])
        return {
            "key": item["key"], "status": "ready", "source_revision": item["source_revision"],
            "source_sha256": digest, "source_bytes": source_bytes,
            "source_extension": item["extension"], "profile": item["profile"],
            "reader_mode": item["reader_mode"], "path": object_path, "bytes": target.stat().st_size,
            "sha256": file_sha256(target),
        }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, default=Path("output/reader-assets/queue.json"))
    parser.add_argument("--bundle", type=Path, default=Path("output/reader-assets/bundle"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    queue = load_json(args.queue).get("items", [])
    if args.bundle.exists():
        shutil.rmtree(args.bundle)
    args.bundle.mkdir(parents=True)
    results = []
    if args.dry_run:
        print(f"dry run: would convert {len(queue)} reader asset(s)")
    else:
        for item in queue:
            try:
                results.append(convert_item(item, args.bundle))
            except Exception as exc:
                results.append({
                    "key": item["key"], "status": "failed", "source_revision": item["source_revision"],
                    "source_extension": item["extension"], "profile": item["profile"],
                    "error": type(exc).__name__,
                })
                print(f"failed: {item['repo']}/{item['path']}: {exc}")
    (args.bundle / "bundle.json").write_bytes(canonical_json({"version": 1, "results": results}, pretty=True))
    failed = sum(item["status"] == "failed" for item in results)
    print(f"converted {len(results) - failed}; failed {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
