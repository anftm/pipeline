#!/usr/bin/env python3
"""Merge PDF shard bundles and publish them in one atomic commit."""

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

try:
    from . import pdf_assets
except ImportError:
    import pdf_assets


def merge_bundles(bundle_paths: list[Path], output: Path) -> list[dict]:
    results = []
    seen = set()
    output.mkdir(parents=True, exist_ok=True)
    for bundle in sorted(bundle_paths, key=lambda path: path.as_posix()):
        data = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("results"), list):
            raise ValueError(f"invalid PDF asset bundle: {bundle}")
        for result in data["results"]:
            key = result.get("key")
            if not key or key in seen:
                raise ValueError(f"duplicate PDF asset result: {key}")
            seen.add(key)
            results.append(result)
        for source in sorted(bundle.rglob("*")):
            if not source.is_file() or source.name in {"bundle.json", pdf_assets.MANIFEST_NAME}:
                continue
            relative = source.relative_to(bundle)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.read_bytes() != source.read_bytes():
                    raise ValueError(f"conflicting PDF asset artifact: {relative}")
            else:
                shutil.copyfile(source, destination)
    results.sort(key=lambda result: result["key"])
    return results


def result_chunks(results: list[dict], size: int) -> list[list[dict]]:
    if size < 1:
        raise ValueError("publication chunk size must be positive")
    return [results[index:index + size] for index in range(0, len(results), size)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles", type=Path, nargs="+", required=True)
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", pdf_assets.READER_ASSETS_REPO))
    parser.add_argument("--chunk-results", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as root:
        merged = Path(root)
        results = merge_bundles(args.bundles, merged)
        manifest, _ = pdf_assets.build_publish(pdf_assets.empty_manifest(), results, merged)
        (merged / pdf_assets.MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        if not args.dry_run:
            token = os.environ.get("HF_TOKEN")
            if not token:
                raise RuntimeError("HF_TOKEN is required")
            api = HfApi(token=token)
            chunks = result_chunks(results, args.chunk_results)
            for index, chunk in enumerate(chunks, 1):
                print(f"publishing PDF chunk {index}/{len(chunks)} ({len(chunk)} asset(s))")
                pdf_assets.publish(api, args.assets_repo, manifest, chunk, merged)
        print(f"published {len(results)} PDF asset(s) to {args.assets_repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
