#!/usr/bin/env python3
"""Resumable DJVU -> PDF derivatives with immutable checkpoint publication.

The derived manifest is the durable queue checkpoint. Each successful book is
uploaded together with its derived manifest and the merged index before the
next book is converted. A killed runner therefore resumes from the last
published source identity instead of losing the whole temporary batch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from huggingface_hub import HfFileSystem

try:
    from . import reader_lifecycle, shared
    from .convert_reader_assets import convert_file, validate_djvu_pdf
    from .reader_assets import bucket_conversion_contract, decode_search_payload, relative_path, source_url
    from .reader_bucket import index_path, publish_json, read_bytes as read_bucket_bytes
except ImportError:
    import reader_lifecycle, shared
    from convert_reader_assets import convert_file, validate_djvu_pdf
    from reader_assets import bucket_conversion_contract, decode_search_payload, relative_path, source_url
    from reader_bucket import index_path, publish_json, read_bytes as read_bucket_bytes


BUCKET = shared.PDF_PAGES_BUCKET
MANIFEST = "reader-index/derived_pdf_manifest.json"
PROFILE = "djvulibre-pdf-v3-resumable"
GENERATION = "weekly-djvu-resumable-v2"
MAX_SOURCE_BYTES = 512 * 1024 * 1024
DOWNLOAD_ATTEMPTS = 6


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--repo", default="")
    parser.add_argument("--path", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def empty_manifest() -> dict:
    return {"version": 1, "kind": "pdf-derived-source-index", "generation": GENERATION, "files": []}


def read_manifest(token: str) -> dict:
    fs = HfFileSystem(token=token)
    try:
        with fs.open(f"hf://buckets/{BUCKET}/{MANIFEST}", "rb") as stream:
            value = json.loads(stream.read().decode("utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return empty_manifest()
    if (value.get("version") != 1 or value.get("kind") != "pdf-derived-source-index"
            or not isinstance(value.get("files"), list)):
        raise ValueError("invalid DJVU derived-source manifest")
    return value


def source_records(search_data: Path, revisions: Path, *, repo_filter="", path_filter="") -> list[dict]:
    revision_map = json.loads(revisions.read_text(encoding="utf-8"))
    records = []
    for record in decode_search_payload(json.loads(search_data.read_text(encoding="utf-8"))):
        if str(record.get("Extension") or "").lower().lstrip(".") != "djvu":
            continue
        repo = str(record.get("Repo") or "")
        path = relative_path(record)
        if (repo_filter and repo != repo_filter) or (path_filter and path != path_filter):
            continue
        revision = str(revision_map.get(repo) or "")
        if repo and revision:
            records.append({"repo": repo, "path": path, "revision": revision,
                            "key": f"{repo}\0{path}", "source_bytes": int(record.get("Size") or 0)})
    return sorted(records, key=lambda item: (item["repo"], item["path"]))


def download(url: str, target: Path, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=180) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_SOURCE_BYTES:
                    raise ValueError("DJVU source exceeds the conversion limit")
                with target.open("wb") as output:
                    total = 0
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_SOURCE_BYTES:
                            raise ValueError("DJVU source exceeds the conversion limit")
                        output.write(chunk)
                return
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt + 1 == DOWNLOAD_ATTEMPTS:
                raise
            retry_after = error.headers.get("Retry-After")
            delay = min(120, int(retry_after)) if retry_after and retry_after.isdigit() else min(60, 2 ** attempt)
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt + 1 == DOWNLOAD_ATTEMPTS:
                raise
            delay = min(60, 2 ** attempt)
        time.sleep(delay)
    raise RuntimeError("DJVU source download retry limit reached")


def processing_path(key: str, revision: str, pdf_sha: str) -> str:
    identity = hashlib.sha256(f"{key}\0{revision}\0{pdf_sha}".encode()).hexdigest()[:32]
    return f"reader-index/processing/djvu-{identity}.json"


def build_one(item: dict, work: Path, token: str) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source.djvu"
    target = work / "document.pdf"
    download(source_url(item["repo"], item["revision"], item["path"]), source, token)
    source_sha = shared.hash_file(source)[0]
    contract = bucket_conversion_contract(item["repo"], item["path"], "djvu", source.stat().st_size)
    if not contract:
        raise ValueError("no DJVU conversion contract")
    convert_file({"extension": "djvu", "profile": contract[0], "reader_mode": "pdf",
                  "output_name": "document.pdf", "path": item["path"]}, source, target, work)
    validate_djvu_pdf(target, work)
    pdf_sha, pdf_bytes = shared.hash_file(target)
    object_id = hashlib.sha256(f"{item['key']}\0{item['revision']}\0{pdf_sha}".encode()).hexdigest()[:32]
    root = f"derived/weekly-djvu/{object_id}"
    remote = f"{root}/document.pdf"
    manifest_path = f"{root}/derived-manifest.json"
    processing = processing_path(item["key"], item["revision"], pdf_sha)
    entry = {"key": item["key"], "repo": item["repo"], "source_path": item["path"],
             "source_extension": "djvu", "source_revision": item["revision"],
             "source_sha256": source_sha, "source_bytes": source.stat().st_size,
             "derived_sha256": pdf_sha, "derived_bytes": pdf_bytes, "profile": PROFILE,
             "path": remote, "new_path": remote, "manifest_path": manifest_path,
             "bucket": BUCKET, "generation": GENERATION, "status": "ready",
             "processing_roots": [processing]}
    manifest = (json.dumps({"version": 1, "kind": "pdf-derived-source", **entry},
                           ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    return {"entry": entry, "pdf": target, "remote": remote, "manifest_path": manifest_path,
            "manifest": manifest, "processing_path": processing, "root": root}


def failure_entry(item: dict, error: Exception, previous: dict | None = None) -> dict:
    if isinstance(previous, dict):
        prior_ready = (previous.get("status") == "ready"
                       or (previous.get("status") is None and previous.get("new_path")
                           and previous.get("derived_sha256") and previous.get("derived_bytes")))
        if prior_ready:
            # Never hide an already-readable derivative because an explicit
            # replacement attempt failed. Keep the failed revision recorded;
            # retry it only by explicit retry or exact-path force.
            return {**previous, "last_attempt_source_revision": item["revision"],
                    "last_attempt_status": "failed", "last_attempts": int(previous.get("last_attempts", 0)) + 1,
                    "last_attempt_error": f"{type(error).__name__}: {error}"[:1000]}
    return {"key": item["key"], "repo": item["repo"], "source_path": item["path"],
            "source_extension": "djvu", "source_revision": item["revision"],
            "status": "failed", "profile": PROFILE, "attempts": int((previous or {}).get("attempts", 0)) + 1,
            "error": f"{type(error).__name__}: {error}"[:1000]}


def publish_checkpoint(results: dict[str, dict], additions: list[tuple[object, str]], token: str) -> None:
    payload = {"version": 1, "kind": "pdf-derived-source-index", "generation": GENERATION,
               "files": [results[key] for key in sorted(results)]}
    additions = list(additions)
    additions.append(((json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(), MANIFEST))
    shared.batch_bucket_files_with_retry(BUCKET, additions, token, max_attempts=8)
    remote = read_bucket_bytes(MANIFEST, token, bucket=BUCKET)
    if json.loads(remote) != payload:
        raise ValueError("DJVU checkpoint manifest readback mismatch")


def pending_records(records, existing, *, retry_failed=False, force=False):
    pending = []
    for item in records:
        prior = existing.get(item["key"])
        if not isinstance(prior, dict):
            pending.append(item)
            continue
        if force:
            pending.append(item)
            continue
        prior_ready = (prior.get("status") == "ready"
                       or (prior.get("status") is None and prior.get("new_path")
                           and prior.get("derived_sha256") and prior.get("derived_bytes")))
        if prior_ready:
            # HF dataset revisions are repository-wide. An unrelated commit is
            # not evidence that this DJVU changed; keep its immutable derivative.
            # Rebuild a ready key only with an exact-path --force request. A
            # failed exact-source attempt is retried only when requested.
            retry_revision = prior.get("last_attempt_source_revision")
            if prior.get("last_attempt_status") == "failed" and retry_revision == item["revision"]:
                if retry_failed:
                    pending.append(item)
            continue
        if prior.get("source_revision") == item["revision"] and prior.get("status") == "failed" and not retry_failed:
            continue
        pending.append(item)
    return pending


def main() -> int:
    options = args()
    if options.limit < 0:
        raise ValueError("limit must be non-negative")
    if bool(options.repo) != bool(options.path):
        raise ValueError("--repo and --path must be supplied together")
    if options.force and not options.path:
        raise ValueError("--force requires an exact --repo and --path")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN is required")
    records = source_records(options.search_data, options.revisions,
                             repo_filter=options.repo, path_filter=options.path)
    manifest = read_manifest(token)
    results = {item["key"]: item for item in manifest["files"] if isinstance(item, dict) and item.get("key")}
    pending = pending_records(records, results, retry_failed=options.retry_failed, force=options.force)
    if options.limit:
        pending = pending[:options.limit]
    print(f"djvu_sources={len(records)} pending={len(pending)} checkpoint_entries={len(results)}")
    if not options.apply:
        print("report-only; pass --apply to convert and publish resumable checkpoints")
        return 0
    successes = failures = 0
    with tempfile.TemporaryDirectory(prefix="djvu-weekly-") as temp:
        for number, item in enumerate(pending):
            previous = results.get(item["key"])
            try:
                built = build_one(item, Path(temp) / str(number), token)
                processing = reader_lifecycle.processing_record(
                    [{"bucket": BUCKET, "root": built["root"]}], "djvu-object-upload",
                    {"repository": os.environ.get("GITHUB_REPOSITORY", ""),
                     "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                     "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")})
                publish_json(built["processing_path"], processing, token)
                results[item["key"]] = built["entry"]
                publish_checkpoint(results, [(str(built["pdf"]), built["remote"]),
                                             (built["manifest"], built["manifest_path"])], token)
                uploaded = reader_lifecycle.heartbeat_processing(processing)
                uploaded["status"] = "uploaded"
                publish_json(built["processing_path"], uploaded, token)
                successes += 1
                print(f"checkpoint ready {item['repo']}/{item['path']}", flush=True)
            except Exception as error:
                result = failure_entry(item, error, previous)
                results[item["key"]] = result
                # Persist failure state so --retry-failed is explicit and a
                # broken source cannot monopolize every future run.
                publish_checkpoint(results, [], token)
                failures += 1
                print(f"checkpoint failed {item['repo']}/{item['path']}: {type(error).__name__}: {error}", flush=True)
    print(f"published_checkpoints={successes} failures={failures} entries={len(results)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
