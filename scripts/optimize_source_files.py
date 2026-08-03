#!/usr/bin/env python3
"""Preprocess mirrored BHA source files for faster browser rendering.

Each already-mirrored source file is transformed in place on the Hugging Face
mirror dataset:

  - PDF               -> qpdf --linearize (lossless; enables byte-range
                         streaming render in the browser and pdf.js)
  - jpg/jpeg/png      -> cwebp WebP (jpg/jpeg lossy at WEBP_QUALITY, png
                         lossless; far smaller than the original scans)
  - everything else   -> left untouched

Inputs are read from the mirror itself (manifest + resolve endpoint), never
from GitHub, so the pass is a pure remap of what the site already serves.
Transformed files replace the old blob under a new content-addressed path, the
manifest entry is updated (path/sha256/bytes) and flagged ``"optimized": true``
so reruns are no-ops. Old blob paths that no URL references anymore are deleted
from the dataset to reclaim storage.

The app reads only ``path``/``sha256``/``bytes`` per entry and serves WebP
through the source URL unchanged, so no frontend/app change is required for the
preprocessing itself.
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

from scripts import mirror_source_files as mirror

HF_REPO = mirror.HF_REPO
HF_TOKEN = mirror.HF_TOKEN
MANIFEST_NAME = mirror.MANIFEST_NAME
MAX_FILE_BYTES = mirror.MAX_FILE_BYTES
REPO_START = mirror.REPO_START
REPO_END = mirror.REPO_END

WEBP_QUALITY = int(os.environ.get("WEBP_QUALITY", "85"))
CONCURRENCY = int(os.environ.get("OPTIMIZE_CONCURRENCY", "2"))
PDF_SUFFIX = ".pdf"
RASTER_SUFFIXES = {".jpg", ".jpeg", ".png"}
LOSSLESS_SUFFIXES = {".png"}


def resolve_url(path: str) -> str:
    return f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/{urllib.parse.quote(path, safe='/')}"


def download_mirrored(path: str, target: Path) -> tuple[str, int]:
    request = urllib.request.Request(resolve_url(path), headers={"User-Agent": "anftm-source-optimize/1.0"})
    if HF_TOKEN:
        request.add_header("Authorization", f"Bearer {HF_TOKEN}")
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=300) as response, target.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise RuntimeError(f"file exceeds {MAX_FILE_BYTES} bytes")
            digest.update(chunk)
            output.write(chunk)
    return digest.hexdigest(), size


def transformation_for(url: str) -> tuple[str, str] | None:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix == PDF_SUFFIX:
        return ".pdf", "pdf-linearize"
    if suffix in RASTER_SUFFIXES:
        kind = "webp-lossless" if suffix in LOSSLESS_SUFFIXES else f"webp-q{WEBP_QUALITY}"
        return ".webp", kind
    return None


def optimized_path(archive_id: int, digest: str, suffix: str) -> str:
    return f"archives{archive_id}/{digest[:2]}/{digest}{suffix}"


def sha256_of(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def looks_like_pdf(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(5) == b"%PDF-"


def looks_like_webp(path: Path) -> bool:
    with path.open("rb") as handle:
        head = handle.read(12)
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def transform_file(kind: str, source: Path, target: Path) -> None:
    if kind == "pdf-linearize":
        returncode, _out, err = mirror.run(["qpdf", "--linearize", str(source), str(target)])
        if returncode != 0:
            raise RuntimeError(f"qpdf failed: {err or 'unknown error'}")
        if not looks_like_pdf(target):
            raise RuntimeError("qpdf output is not a valid PDF")
    else:
        args = ["cwebp"]
        if kind == "webp-lossless":
            args += ["-lossless", "-z", "9"]
        else:
            args += ["-q", str(WEBP_QUALITY)]
        args += [str(source), "-o", str(target)]
        returncode, _out, err = mirror.run(args)
        if returncode != 0:
            raise RuntimeError(f"cwebp failed: {err or 'unknown error'}")
        if not looks_like_webp(target):
            raise RuntimeError("cwebp output is not valid WebP")


def prepare_file(work: Path, url: str, meta: dict) -> dict:
    """Download one mirrored file and transform it.

    Returns a result dict with status in
    {"changed", "unchanged", "transform-failed", "download-failed"} plus the
    updated entry metadata when the file was processed.
    """
    result = {"url": url, "status": None, "meta": dict(meta)}
    transform = transformation_for(url)
    if transform is None:
        result["status"] = "pass-through"
        return result
    suffix, kind = transform

    source = work / "in"
    try:
        _digest, _size = download_mirrored(meta["path"], source)
    except urllib.error.HTTPError as exc:
        result["status"] = f"download-failed:{exc.code}"
        return result

    target = work / "out"
    try:
        transform_file(kind, source, target)
    except Exception as exc:
        result["status"] = "transform-failed"
        result["error"] = str(exc)
        result["meta"]["optimized"] = True
        return result

    digest, size = sha256_of(target)
    result["meta"]["optimized"] = True
    result["meta"]["sha256"] = digest
    result["meta"]["bytes"] = size
    result["output"] = target
    if suffix:
        result["meta"]["path"] = optimized_path(meta["archive_id"], digest, suffix)
    if result["meta"]["path"] == meta["path"]:
        result["status"] = "unchanged"
    else:
        result["status"] = "changed"
        result["old_path"] = meta["path"]
    return result


def pace_seconds(operations: int) -> int:
    if operations <= 0 or mirror.PACE_OBJECTS_PER_MINUTE <= 0:
        return 0
    expected = float(operations) / (float(mirror.PACE_OBJECTS_PER_MINUTE) / 60.0)
    return min(int(expected), mirror.PACE_MAX_SLEEP_SECONDS)


def commit_archive(
    api: HfApi,
    archive_id: int,
    files: dict[str, dict],
    staged: dict[str, dict],
    deletions: set[str],
    temp_dir: Path,
) -> None:
    operations = [
        CommitOperationAdd(path_in_repo=meta["path"], path_or_fileobj=str(temp_dir / meta["path"]))
        for meta in staged.values()
    ]
    operations += [CommitOperationDelete(path_in_repo=path) for path in sorted(deletions)]
    manifest_bytes = (
        json.dumps({"version": 1, "files": dict(sorted(files.items()))}, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    operations.append(CommitOperationAdd(path_in_repo=MANIFEST_NAME, path_or_fileobj=manifest_bytes))
    wait = pace_seconds(len(operations))
    if wait:
        print(
            f"archive{archive_id}: pacing upload by {wait}s for {len(operations)} operation(s) "
            "below the HF rate limit",
            flush=True,
        )
        time.sleep(wait)
    print(f"archive{archive_id}: committing {len(operations)} operation(s) to {HF_REPO} via Hub API", flush=True)
    api.create_commit(
        repo_id=HF_REPO,
        repo_type="dataset",
        operations=operations,
        commit_message=f"Optimize BHA source files archive{archive_id}",
    )
    print(f"archive{archive_id}: commit complete", flush=True)


def cleanup_archive_dir(temp_dir: Path, archive_id: int) -> None:
    directory = temp_dir / f"archives{archive_id}"
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


def optimize_archive(api: HfApi, temp_dir: Path, archive_id: int, files: dict[str, dict]) -> int:
    entries = [
        (url, meta)
        for url, meta in files.items()
        if meta.get("archive_id") == archive_id and not meta.get("optimized")
    ]
    total = sum(1 for meta in files.values() if meta.get("archive_id") == archive_id)
    if not entries:
        print(f"archive{archive_id}: {total} files already processed", flush=True)
        return 0

    print(f"archive{archive_id}: {len(entries)} file(s) to process", flush=True)
    work_root = temp_dir / f"archives{archive_id}"
    work_root.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    if CONCURRENCY > 1:
        print(f"archive{archive_id}: processing with {CONCURRENCY} concurrent workers", flush=True)
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {pool.submit(prepare_file, work_root / str(index), url, meta): url for index, (url, meta) in enumerate(entries)}
            completed = 0
            for future in as_completed(futures):
                url = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    pool.shutdown(cancel_futures=True)
                    raise RuntimeError(f"archive{archive_id}: processing failed for {url}: {exc}") from exc
                completed += 1
                if completed % 100 == 0 or completed == len(entries):
                    print(f"archive{archive_id}: [{completed}/{len(entries)}] files processed", flush=True)
    else:
        for index, (url, meta) in enumerate(entries):
            results.append(prepare_file(work_root / str(index), url, meta))

    updates: dict[str, dict] = {}
    staged: dict[str, dict] = {}
    created_paths: list[Path] = []
    try:
        for result in results:
            if result["status"] not in ("changed", "unchanged", "transform-failed"):
                continue
            updates[result["url"]] = result["meta"]
            output = result.get("output")
            if output is None or result["status"] == "unchanged":
                continue
            target = temp_dir / result["meta"]["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.move(str(output), str(target))
                created_paths.append(target)
            staged[result["url"]] = result["meta"]
    except Exception:
        for target in created_paths:
            target.unlink(missing_ok=True)
        cleanup_archive_dir(temp_dir, archive_id)
        raise

    files.update(updates)
    referenced = {meta["path"] for meta in files.values()}
    deletions = {result["old_path"] for result in results if result.get("old_path")} - referenced
    if not updates and not deletions:
        print(f"archive{archive_id}: no file changed", flush=True)
        return 0

    commit_archive(api, archive_id, files, staged, deletions, temp_dir)
    cleanup_archive_dir(temp_dir, archive_id)
    return len(updates)


def main() -> int:
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is required")
    if shutil.which("qpdf") is None:
        raise RuntimeError("qpdf is required (apt-get install qpdf)")
    if shutil.which("cwebp") is None:
        raise RuntimeError("cwebp is required (apt-get install webp)")
    api = HfApi(token=HF_TOKEN)
    with tempfile.TemporaryDirectory(prefix="bha-source-optimize-") as temp_dir:
        manifest = mirror.remote_manifest(api)
        files = manifest.setdefault("files", {})
        failures: list[str] = []
        for archive_id in range(REPO_START, REPO_END + 1):
            try:
                processed = optimize_archive(api, Path(temp_dir), archive_id, files)
                print(f"archive{archive_id}: processed {processed} file(s)", flush=True)
            except Exception as exc:
                failures.append(f"archive{archive_id}: {exc}")
                print(f"archive{archive_id} failed: {exc}", flush=True)
            cleanup_archive_dir(Path(temp_dir), archive_id)
        if failures:
            print("\n".join(failures))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
