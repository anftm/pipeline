#!/usr/bin/env python3
"""Build and publish sharded native/transcoded media assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

try:
    from .convert_reader_assets import convert_item
    from .reader_assets import bucket_conversion_contract, decode_search_payload, relative_path, source_url
    from .reader_stream_index import (
        completed_revisions, merge_indexes, publish_checkpoint, read_index, source_failure, unchanged_sources,
    )
except ImportError:
    from convert_reader_assets import convert_item
    from reader_assets import bucket_conversion_contract, decode_search_payload, relative_path, source_url
    from reader_stream_index import (
        completed_revisions, merge_indexes, publish_checkpoint, read_index, source_failure, unchanged_sources,
    )


TARGET_BUCKET = "vomebook/reader-assets-v2"
MEDIA_EXTENSIONS = {
    "mp3", "wav", "m4a", "flac", "mpga", "ape", "wma", "amr",
    "mp4", "mov", "asx", "flv", "f4v", "rm", "rmvb", "mkv", "avi",
    "mpg", "mpeg", "mts", "ts", "wmv", "swf",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--kind", choices=("audio", "video", "swf"), required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--bucket", default=TARGET_BUCKET)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def existing_index(bucket: str, token: str | None, root: str, name: str) -> dict:
    return read_index(bucket, f"{root}/{name}", token, "media-stream-index")


def shard_for(key: str, count: int) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % count


def select_records(path: Path, revisions: dict, kind: str) -> list[dict]:
    records = decode_search_payload(json.loads(path.read_text(encoding="utf-8")))
    selected = []
    for record in records:
        extension = str(record.get("Extension") or "").lower().lstrip(".")
        if extension not in MEDIA_EXTENSIONS:
            continue
        contract = bucket_conversion_contract(
            str(record.get("Repo") or ""), relative_path(record), extension,
            int(record.get("Size") or 0),
        )
        if not contract or contract[1] != kind:
            if not (kind == "swf" and extension == "swf"):
                continue
        repo = str(record.get("Repo") or "")
        revision = str(revisions.get(repo) or "")
        if repo and revision:
            selected.append({
                "repo": repo, "path": relative_path(record), "extension": extension,
                "revision": revision, "source_bytes": int(record.get("Size") or 0),
            })
    selected.sort(key=lambda item: (item["extension"], item["repo"], item["path"]))
    return selected


def build_one(item: dict, work: Path, bucket: str) -> tuple[dict, dict[str, str]]:
    extension = item["extension"]
    contract = bucket_conversion_contract(item["repo"], item["path"], extension, item["source_bytes"])
    if contract is None:
        raise ValueError(f"no media contract for {extension}")
    profile, mode, output_name = contract
    source_url_value = source_url(item["repo"], item["revision"], item["path"])
    queue_item = {
        "key": f"{item['repo']}\0{item['path']}", "repo": item["repo"], "path": item["path"],
        "extension": extension, "source_revision": item["revision"], "source_url": source_url_value,
        "source_bytes": item["source_bytes"], "profile": profile, "reader_mode": mode,
        "output_name": output_name,
    }
    bundle = work / "bundle"
    result = convert_item(queue_item, bundle)
    artifact = bundle / result["path"]
    if not artifact.is_file():
        raise ValueError(f"converted artifact missing: {result['path']}")
    source_digest = result["source_sha256"]
    # Codec probing can change the format, filename and even ASX's media kind.
    # Publish the converter's actual artifact rather than the input contract.
    mode = result["reader_mode"]
    output_name = artifact.name
    root_kind = "swf" if mode == "swf" else mode
    remote_root = f"media/{root_kind}/{extension}/{result['sha256']}"
    remote_path = f"{remote_root}/{output_name}"
    target = work / output_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(artifact, target)
    entry = {
        "key": queue_item["key"], "repo": item["repo"], "path": item["path"],
        "extension": extension, "source_revision": item["revision"],
        "source_sha256": source_digest, "source_bytes": result.get("source_bytes", item["source_bytes"]),
        "mode": mode, "profile": result["profile"], "bucket": bucket,
        "object": remote_path, "bytes": target.stat().st_size,
        "sha256": result["sha256"],
    }
    return entry, {remote_path: str(target)}


def main() -> int:
    args = parse_args()
    if args.limit < 0 or args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        raise ValueError("invalid limit or shard")
    token = os.environ.get("HF_TOKEN")
    revisions = json.loads(args.revisions.read_text(encoding="utf-8"))
    root = f"media/{args.kind}"
    index_name = "index.json" if args.shard_count == 1 else f"index-{args.shard_index:02d}.json"
    previous = existing_index(args.bucket, token, root, index_name)
    canonical = existing_index(args.bucket, token, root, "index.json")
    # Historical shards contained complete canonical copies; retain only this
    # shard's keys and let the canonical newer checkpoint win on conflicts.
    previous = merge_indexes(previous, canonical.get("files", []), canonical.get("failures", []), "media-stream-index")
    previous = {**previous, **{field: [e for e in previous.get(field, [])
                  if shard_for(e["key"], args.shard_count) == args.shard_index] for field in ("files", "failures")}}
    completed = completed_revisions(previous, args.retry_failures)
    candidates = [item for item in select_records(args.search_data, revisions, args.kind)
                  if shard_for(f"{item['repo']}\0{item['path']}", args.shard_count) == args.shard_index]
    unchanged = unchanged_sources(candidates, previous, token)
    selected = [item for item in candidates if f"{item['repo']}\0{item['path']}" not in unchanged
                and completed.get(f"{item['repo']}\0{item['path']}") != item["revision"]]
    print(f"unchanged_by_source_sha={len(unchanged)} pending={len(selected)}", flush=True)
    if args.limit:
        selected = selected[:args.limit]
    if not selected:
        print(f"no pending {args.kind} media sources")
        return 0
    converted, failed = 0, 0
    with tempfile.TemporaryDirectory(prefix=f"reader-{args.kind}-") as directory:
        work = Path(directory)
        # A large or broken later source must not lose earlier successful work.
        for index, item in enumerate(selected):
            entries, failures, uploads = [], [], {}
            checkpoint_work = work / str(index)
            checkpoint_work.mkdir(parents=True, exist_ok=True)
            try:
                entry, uploads = build_one(item, checkpoint_work, args.bucket)
                entries.append(entry)
                converted += 1
            except Exception as error:
                failures.append(source_failure(item, error))
                failed += 1
                print(f"failed: {item['repo']}/{item['path']}: {type(error).__name__}: {error}", flush=True)
            previous = publish_checkpoint(args.bucket, f"{root}/{index_name}", previous,
                                          entries, failures, uploads, checkpoint_work, token,
                                          apply=args.apply, kind="media-stream-index")
            print(f"checkpoint={root}/{index_name} converted={converted} failures={failed}", flush=True)
            shutil.rmtree(checkpoint_work)
    return 1 if args.strict and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
