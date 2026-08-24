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
from pathlib import Path

from PIL import Image, ImageSequence

try:
    from .reader_assets import canonical_json, load_json
except ImportError:
    from reader_assets import canonical_json, load_json

MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


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
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"conversion command failed: {Path(command[0]).name}: {result.stderr[-500:]}")


def command_output(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"validation command failed: {Path(command[0]).name}: {result.stderr[-500:]}")
    return result.stdout


def convert_tiff(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        frames = [frame.copy().convert("RGB") for frame in ImageSequence.Iterator(image)]
    if not frames:
        raise RuntimeError("TIFF has no frames")
    first, rest = frames[0], frames[1:]
    first.save(target, "PDF", save_all=True, append_images=rest, resolution=150.0)
    for frame in frames:
        frame.close()


def convert_file(item: dict, source: Path, target: Path, work: Path) -> None:
    ext = item["extension"]
    if ext in {"doc", "docx"}:
        out = work / "office"
        out.mkdir()
        run_checked(["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", str(out), str(source)])
        produced = out / f"{source.stem}.pdf"
        if not produced.exists():
            raise RuntimeError("LibreOffice produced no PDF")
        shutil.move(produced, target)
    elif ext in {"mobi", "azw3"}:
        run_checked(["ebook-convert", str(source), str(target)])
    elif ext in {"tif", "tiff"}:
        convert_tiff(source, target)
    else:
        raise ValueError(f"unsupported conversion extension: {ext}")


def validate_output(path: Path, reader_mode: str) -> None:
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("conversion output is empty")
    if reader_mode == "pdf" and not path.read_bytes()[:5] == b"%PDF-":
        raise RuntimeError("conversion output is not a PDF")
    if reader_mode == "epub":
        import zipfile
        with zipfile.ZipFile(path) as archive:
            if archive.read("mimetype") != b"application/epub+zip":
                raise RuntimeError("conversion output is not an EPUB")


def validate_office_pdf(path: Path, item: dict) -> None:
    fonts = command_output(["pdffonts", str(path)]).splitlines()[2:]
    if not fonts or any(len(line.split()) < 6 or line.split()[-5].lower() != "yes" for line in fonts):
        raise RuntimeError("office PDF has missing or unembedded fonts")
    text = command_output(["pdftotext", str(path), "-"])
    if CJK_RE.search(item.get("path", "")) and not CJK_RE.search(text):
        raise RuntimeError("office PDF has no extractable CJK text")


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
            if item["extension"] in {"doc", "docx"}:
                validate_office_pdf(temporary, item)
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
