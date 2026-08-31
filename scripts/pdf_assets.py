#!/usr/bin/env python3
"""Build and atomically publish the independent large-PDF asset collection."""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.errors import HfHubHTTPError

try:
    from .reader_assets import READER_ASSETS_REPO, decode_search_payload, relative_path, source_url
except ImportError:
    from reader_assets import READER_ASSETS_REPO, decode_search_payload, relative_path, source_url

MANIFEST_NAME = "pdf_manifest.json"
MANIFEST_VERSION = 1
MI = 1024 * 1024
MIN_BYTES = 50 * MI
LARGE_BYTES = 100 * MI
WEBP_QUALITY = int(os.environ.get("PDF_WEBP_QUALITY", "85"))
WEBP_MAX_DIMENSION = int(os.environ.get("PDF_WEBP_MAX_DIMENSION", "2400"))
SAMPLE_PAGES = int(os.environ.get("PDF_SAMPLE_PAGES", "3"))
WEBP_MAX_RATIO = float(os.environ.get("PDF_WEBP_MAX_RATIO", "0.9"))
PDF_PROFILE = f"pdf-pages-v1-{WEBP_QUALITY}-{WEBP_MAX_DIMENSION}"
SOURCE_PROFILES = {
    "upstream": "pdf-assets-upstream-v1",
    "generated": "pdf-assets-reader-generated-v1",
}


def digest(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def load_records(search_data: Path, revisions: Path, repo: str = "", extension: str = "pdf") -> list[dict]:
    records = decode_search_payload(json.loads(search_data.read_text(encoding="utf-8")))
    revision_map = json.loads(revisions.read_text(encoding="utf-8"))
    selected = []
    for record in records:
        source_repo = str(record.get("Repo") or "")
        source_extension = str(record.get("Extension") or "").lower().lstrip(".")
        revision = str(revision_map.get(source_repo) or "")
        if source_extension != extension or (repo and source_repo != repo) or not revision:
            continue
        path = relative_path(record)
        selected.append({
            "key": f"{source_repo}\0{path}", "repo": source_repo, "path": path,
            "extension": source_extension, "source_revision": revision, "source_bytes": int(record.get("Size") or 0),
            "source_url": source_url(source_repo, revision, path),
            "source_kind": "upstream", "profile": SOURCE_PROFILES["upstream"],
            "source_extension": source_extension,
        })
    selected.sort(key=lambda item: (item["repo"], item["path"]))
    return selected


def load_generated_records(manifest: Path | dict, assets_repo: str = READER_ASSETS_REPO,
                           repo: str = "") -> list[dict]:
    data = manifest if isinstance(manifest, dict) else json.loads(manifest.read_text(encoding="utf-8"))
    selected = []
    for key, entry in data.get("files", {}).items():
        source_repo, separator, source_path = str(key).partition("\0")
        artifact = str(entry.get("path") or "") if isinstance(entry, dict) else ""
        artifact_bytes = entry.get("bytes") if isinstance(entry, dict) else None
        if (not separator or (repo and source_repo != repo) or entry.get("status", "ready") != "ready"
                or entry.get("reader_mode") != "pdf" or not artifact.endswith("/document.pdf")
                or not isinstance(artifact_bytes, int) or artifact_bytes < MIN_BYTES):
            continue
        selected.append({
            "key": key, "repo": source_repo, "path": source_path,
            "source_revision": str(entry.get("source_revision") or ""),
            "source_bytes": artifact_bytes, "source_url": "", "extension": "pdf",
            "source_extension": Path(source_path).suffix.lower().lstrip("."),
            "source_kind": "generated", "profile": SOURCE_PROFILES["generated"],
            "reader_assets_repo": assets_repo, "reader_assets_path": artifact,
        })
    selected.sort(key=lambda item: (0 if item.get("source_extension") in {"caj", "kdh"} else 1,
                                    item["repo"], item["path"]))
    return selected


def queue(records: list[dict], limit: int = 0, checkpoint: int = 0) -> list[dict]:
    if limit < 0 or checkpoint < 0:
        raise ValueError("limit and checkpoint must be non-negative")
    if not limit:
        return records
    start = checkpoint * limit
    return records[start:start + limit]


def shard_records(records: list[dict], shard_count: int, shard_index: int) -> list[dict]:
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid PDF asset shard")
    return [item for item in records
             if int.from_bytes(hashlib.sha256(item["key"].encode()).digest()[:8], "big") % shard_count == shard_index]


def weighted_shards(records: list[dict], shard_count: int = 10) -> list[list[dict]]:
    """Assign records to the least-loaded shard, using page counts as weight."""
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    shards = [[] for _ in range(shard_count)]
    loads = [0] * shard_count
    for item in sorted(records, key=lambda value: (-int(value["page_count"]), value["key"])):
        shard = min(range(shard_count), key=lambda index: (loads[index], index))
        shards[shard].append(item)
        loads[shard] += int(item["page_count"])
    return shards


def load_planned_shard(queue_file: Path, shard_count: int, shard_index: int) -> list[dict]:
    data = json.loads(queue_file.read_text(encoding="utf-8"))
    if data.get("version") != 1 or data.get("shard_count") != shard_count:
        raise ValueError("invalid PDF asset queue")
    shards = data.get("shards")
    if not isinstance(shards, list) or len(shards) != shard_count:
        raise ValueError("invalid PDF asset shard queue")
    shard = shards[shard_index]
    if not isinstance(shard, dict) or shard.get("index") != shard_index or not isinstance(shard.get("records"), list):
        raise ValueError("invalid PDF asset shard queue")
    return shard["records"]


def _run(args: list[str], *, text: bool = False) -> str:
    result = subprocess.run(args, check=True, stdout=subprocess.PIPE if text else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, text=text)
    return result.stdout.strip() if text else ""


def _pages(pdf: Path, extension: str = "pdf") -> int:
    if extension == "djvu":
        output = _run(["djvused", str(pdf), "-e", "n"], text=True)
        return int(output.split()[-1])
    output = _run(["pdfinfo", str(pdf)], text=True)
    for line in output.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise RuntimeError("missing page count")


def _render(pdf: Path, page: int, directory: Path, extension: str = "pdf") -> Path:
    stem = directory / f"page-{page:06d}"
    if extension == "djvu":
        _run(["ddjvu", "-format=ppm", f"-page={page}", str(pdf), str(stem.with_suffix(".ppm"))])
    else:
        _run(["pdftocairo", "-png", "-singlefile", "-f", str(page), "-l", str(page), str(pdf), str(stem)])
    png = stem.with_suffix(".png") if extension != "djvu" else stem.with_suffix(".ppm")
    webp = stem.with_suffix(".webp")
    args = ["cwebp", "-quiet", "-q", str(WEBP_QUALITY)]
    if WEBP_MAX_DIMENSION > 0:
        args += ["-resize", str(WEBP_MAX_DIMENSION), str(WEBP_MAX_DIMENSION)]
    _run([*args, str(png), "-o", str(webp)])
    png.unlink(missing_ok=True)
    return webp


def build_item(item: dict, source: Path, bundle: Path) -> dict:
    source_sha, actual_bytes = digest(source)
    base = {**item, "source_sha256": source_sha, "source_bytes": actual_bytes}
    if item.get("extension") != "djvu" and actual_bytes < MIN_BYTES:
        return {**base, "status": "skipped", "reason": "below-minimum-50-mib", "strategy": "none"}
    object_root = Path("objects") / source_sha[:2] / source_sha
    if item.get("extension") != "djvu" and actual_bytes < LARGE_BYTES:
        output = bundle / object_root / "linearized.pdf"
        output.parent.mkdir(parents=True, exist_ok=True)
        _run(["qpdf", "--linearize", str(source), str(output)])
        _run(["qpdf", "--check-linearization", str(output)])
        output_sha, output_bytes = digest(output)
        return {**base, "status": "ready", "strategy": "linearized-pdf",
                "path": (object_root / "linearized.pdf").as_posix(),
                "bytes": output_bytes, "sha256": output_sha,
                "pdf": {"source_bytes": actual_bytes, "output_bytes": output_bytes}}

    pages = _pages(source, str(item.get("extension") or "pdf"))
    with tempfile.TemporaryDirectory(dir=bundle) as temp:
        sample = sorted(set([1, max(1, pages // 2), pages]))[:max(1, SAMPLE_PAGES)]
        sample_sizes = []
        sample_end = min(pages, max(sample))
        if item.get("extension") == "djvu":
            classification = "djvu-image"
        else:
            text = _run(["pdftotext", "-f", "1", "-l", str(sample_end), str(source), "-"], text=True)
            classification = "native-text" if len("".join(text.split())) >= max(100, sample_end * 40) else "scan"
        metadata = {"pages": pages, "classification": classification, "sample_pages": sample}
        if classification == "native-text":
            return {**base, "status": "skipped", "reason": "native-text-pdf",
                    "strategy": "native-text", "pdf": metadata}
        for page in sample:
            sample_sizes.append(_render(source, page, Path(temp), str(item.get("extension") or "pdf")).stat().st_size)
        estimated = int(round(sum(sample_sizes) / len(sample_sizes) * pages))
        metadata.update({"sample_webp_bytes": sample_sizes,
                    "estimated_webp_bytes": estimated})
        if item.get("extension") != "djvu" and estimated > actual_bytes * WEBP_MAX_RATIO:
            return {**base, "status": "skipped", "reason": "estimated-webp-over-90-percent",
                    "strategy": "sampled-webp", "pdf": metadata}
        page_entries = []
        for page in range(1, pages + 1):
            rendered = _render(source, page, Path(temp), str(item.get("extension") or "pdf"))
            page_sha, page_bytes = digest(rendered)
            destination = bundle / object_root / "pages" / f"page-{page:06d}.webp"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(rendered, destination)
            page_entries.append({"page": page, "path": (object_root / "pages" / destination.name).as_posix(),
                                 "sha256": page_sha, "bytes": page_bytes})
    page_manifest = {
        "version": 1, "kind": "pdf-pages", "source_sha256": source_sha,
        "profile": PDF_PROFILE, "pages": page_entries,
    }
    manifest_path = bundle / object_root / "page-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(page_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    manifest_sha, manifest_bytes = digest(manifest_path)
    return {**base, "path": "", "status": "ready", "strategy": "sampled-webp", "pdf": metadata,
            "pages": page_entries, "page_manifest": {
                "path": (object_root / "page-manifest.json").as_posix(),
                "sha256": manifest_sha, "bytes": manifest_bytes,
            }}


def empty_manifest() -> dict:
    return {"version": MANIFEST_VERSION, "files": {}}


def build_publish(manifest: dict, results: list[dict], bundle: Path) -> tuple[dict, list[CommitOperationAdd]]:
    files = dict(manifest.get("files", {}))
    artifacts = {}
    for result in results:
        entry = {k: v for k, v in result.items() if k != "key"}
        if result["status"] == "ready":
            paths = [result["path"]] if result.get("path") else [page["path"] for page in result["pages"]]
            if result.get("page_manifest"):
                paths.append(result["page_manifest"]["path"])
            for path in paths:
                artifact = bundle / path
                if not artifact.is_file():
                    raise ValueError("missing PDF asset artifact")
                artifacts[path] = str(artifact)
        files[result["key"]] = entry
    updated = {"version": MANIFEST_VERSION, "files": dict(sorted(files.items()))}
    operations = [CommitOperationAdd(path_in_repo=path, path_or_fileobj=artifacts[path])
                  for path in sorted(artifacts)]
    operations.append(CommitOperationAdd(
        path_in_repo=MANIFEST_NAME,
        path_or_fileobj=json.dumps(updated, ensure_ascii=False, sort_keys=True, indent=2).encode(),
    ))
    return updated, operations


def remote_manifest(api: HfApi, repo: str) -> dict:
    try:
        path = api.hf_hub_download(repo_id=repo, repo_type="dataset", filename=MANIFEST_NAME)
    except HfHubHTTPError as exc:
        if getattr(exc.response, "status_code", None) != 404:
            raise
        return empty_manifest()
    return json.loads(Path(path).read_text(encoding="utf-8"))


def failed_source_keys(api: HfApi, repo: str, extension: str) -> set[str]:
    try:
        path = api.hf_hub_download(repo_id=repo, repo_type="dataset", filename="manifest.json")
    except HfHubHTTPError as exc:
        if getattr(exc.response, "status_code", None) == 404:
            return set()
        raise
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        key for key, entry in manifest.get("files", {}).items()
        if entry.get("status") == "failed" and entry.get("source_extension") == extension
    }


def remote_sidecar(api: HfApi, repo: str) -> dict:
    try:
        path = api.hf_hub_download(repo_id=repo, repo_type="dataset", filename="reader_assets.json.gz")
    except HfHubHTTPError as exc:
        if getattr(exc.response, "status_code", None) != 404:
            raise
        return {"v": 1, "f": {}}
    return json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))


def update_sidecar(sidecar: dict, results: list[dict]) -> bytes:
    updated = {"v": 1, "f": dict(sidecar.get("f", {}))}
    for result in results:
        if result.get("status") != "ready":
            continue
        path = result.get("path") or result.get("page_manifest", {}).get("path")
        if not path:
            continue
        updated["f"][result["key"]] = {"s": 2, "m": "p", "p": path}
    payload = json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(payload, compresslevel=9, mtime=0)


def bundle_is_published(manifest: dict, results: list[dict]) -> bool:
    entries = manifest.get("files", {})
    for result in results:
        current = entries.get(result.get("key"))
        if not current or current.get("status") != result.get("status"):
            return False
        for field in ("source_revision", "source_sha256", "source_extension", "profile", "strategy"):
            if current.get(field) != result.get(field):
                return False
        if result.get("status") == "ready":
            if current.get("path") != result.get("path", ""):
                return False
            if result.get("page_manifest") and current.get("page_manifest") != result["page_manifest"]:
                return False
    return bool(results)


def publish(api: HfApi, repo: str, manifest: dict, results: list[dict], bundle: Path,
            max_attempts: int = 20) -> None:
    if not results:
        return
    baseline = None
    for attempt in range(max_attempts):
        info = api.repo_info(repo_id=repo, repo_type="dataset")
        current = remote_manifest(api, repo)
        sidecar = remote_sidecar(api, repo)
        sidecar_keys = sidecar.get("f", {})
        sidecar_matches = all(
            result.get("status") != "ready"
            or sidecar_keys.get(result["key"], {}).get("p") == (
                result.get("path") or result.get("page_manifest", {}).get("path")
            )
            for result in results
        )
        if bundle_is_published(current, results) and sidecar_matches:
            return
        keys = {result["key"] for result in results}
        current_entries = {key: current.get("files", {}).get(key) for key in keys}
        if baseline is None:
            baseline = current_entries
        elif current_entries != baseline:
            raise RuntimeError("PDF asset key changed during publication retry")
        updated, operations = build_publish(current, results, bundle)
        operations.append(CommitOperationAdd(
            path_in_repo="reader_assets.json.gz",
            path_or_fileobj=update_sidecar(sidecar, results),
        ))
        try:
            api.create_commit(repo_id=repo, repo_type="dataset", operations=operations,
                              commit_message="Publish independent PDF assets", parent_commit=info.sha)
            return
        except HfHubHTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status not in {409, 412} and not (status == 429 or 500 <= (status or 0) < 600):
                raise
            if attempt + 1 == max_attempts:
                raise
            time.sleep(min(60, 2 ** min(attempt, 5)))
    raise RuntimeError("PDF asset publication retry limit reached")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--repo", default="")
    parser.add_argument("--extension", choices=("pdf", "djvu"), default="pdf")
    parser.add_argument("--failed-only", action="store_true")
    parser.add_argument("--source", choices=("upstream", "generated", "all"), default="all")
    parser.add_argument("--reader-assets-manifest", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--checkpoint", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--queue-file", type=Path, help="Weighted queue produced by plan_pdf_assets.py")
    parser.add_argument("--source-dir", type=Path, help="Local source mirror, keyed by dataset/path")
    parser.add_argument("--bundle", type=Path, default=Path("output/pdf-assets/bundle"))
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", READER_ASSETS_REPO))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.queue_file:
        records = load_planned_shard(args.queue_file, args.shard_count, args.shard_index)
    else:
        records = []
        if args.source in {"upstream", "all"}:
            records.extend(load_records(args.search_data, args.revisions, args.repo, args.extension))
        if args.source in {"generated", "all"}:
            if args.reader_assets_manifest:
                manifest_path = args.reader_assets_manifest
            else:
                from huggingface_hub import hf_hub_download
                manifest_path = Path(hf_hub_download(args.assets_repo, "manifest.json", repo_type="dataset",
                                                     token=os.environ.get("HF_TOKEN")))
            records.extend(load_generated_records(manifest_path, args.assets_repo, args.repo))
        records.sort(key=lambda item: (0 if item.get("source_extension") in {"caj", "kdh"} else 1,
                                       item["repo"], item["path"], item["source_kind"]))
        if args.failed_only:
            token = os.environ.get("HF_TOKEN")
            if not token:
                raise RuntimeError("HF_TOKEN is required for --failed-only")
            failed = failed_source_keys(HfApi(token=token), args.assets_repo, args.extension)
            records = [item for item in records if item["key"] in failed]
        records = shard_records(records, args.shard_count, args.shard_index)
        records = queue(records, args.limit, args.checkpoint)
    args.bundle.mkdir(parents=True, exist_ok=True)
    if not records:
        (args.bundle / "bundle.json").write_text(
            json.dumps({"version": 1, "results": []}, sort_keys=True) + "\n", encoding="utf-8")
        print("processed 0 PDF asset(s)")
        return 0
    results = []
    built_by_sha = {}
    for item in records:
        if item.get("source_kind") == "generated":
            from huggingface_hub import hf_hub_download
            source = Path(hf_hub_download(item["reader_assets_repo"], item["reader_assets_path"],
                                          repo_type="dataset", token=os.environ.get("HF_TOKEN")))
        elif args.source_dir:
            source = args.source_dir / item["repo"] / item["path"]
        else:
            from huggingface_hub import hf_hub_download
            source = Path(hf_hub_download(item["repo"], item["path"], repo_type="dataset",
                                          revision=item["source_revision"], token=os.environ.get("HF_TOKEN")))
        try:
            source_sha, source_bytes = digest(source)
            if source_sha in built_by_sha:
                result = {**built_by_sha[source_sha], **item,
                          "source_sha256": source_sha, "source_bytes": source_bytes}
            else:
                result = build_item(item, source, args.bundle)
                built_by_sha[source_sha] = {k: v for k, v in result.items()
                                            if k not in {"key", "repo", "path", "source_revision", "source_url"}}
            results.append(result)
        except (OSError, subprocess.CalledProcessError, ValueError, RuntimeError):
            source_sha = ""
            try:
                source_sha, source_bytes = digest(source)
            except OSError:
                source_bytes = item.get("source_bytes", 0)
            results.append({**item, "source_sha256": source_sha, "source_bytes": source_bytes,
                            "status": "failed", "reason": "tool-error", "strategy": "none"})
    for result in results:
        if result.get("status") == "ready":
            paths = [result.get("path")] if result.get("path") else [page["path"] for page in result.get("pages", [])]
            if result.get("page_manifest"):
                paths.append(result["page_manifest"]["path"])
            for path in paths:
                if not (args.bundle / path).is_file():
                    raise RuntimeError(f"missing built artifact: {path}")
    manifest, _ = build_publish(empty_manifest(), results, args.bundle)
    (args.bundle / MANIFEST_NAME).write_bytes(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode())
    (args.bundle / "bundle.json").write_text(
        json.dumps({"version": 1, "results": results}, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    if not args.dry_run and not args.build_only:
        token = os.environ.get("HF_TOKEN")
        if not token:
            raise RuntimeError("HF_TOKEN is required")
        publish(HfApi(token=token), args.assets_repo, manifest, results, args.bundle)
    print(f"processed {len(results)} PDF asset(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
