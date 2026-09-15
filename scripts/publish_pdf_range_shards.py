#!/usr/bin/env python3
"""Merge parallel PDF assessment bundles and publish one atomic state update."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

from huggingface_hub import HfApi

try:
    from . import pdf_range_assets, pdf_range_state, reader_assets
except ImportError:
    import pdf_range_assets, pdf_range_state, reader_assets


def merge_results(bundles):
    results = {}
    merged = Path(tempfile.mkdtemp(prefix="pdf-range-merge-"))
    for bundle in sorted(bundles):
        result_files = sorted(bundle.rglob("results.json"))
        if not result_files:
            raise ValueError(f"bundle has no results: {bundle}")
        for result_file in result_files:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            for result in data:
                key = result.get("key")
                if not key or key in results:
                    raise ValueError(f"duplicate PDF range result: {key}")
                results[key] = result
            objects = result_file.parent / "objects"
            if not objects.is_dir():
                continue
            for source in sorted(objects.rglob("*")):
                if not source.is_file():
                    continue
                target = merged / "objects" / source.relative_to(objects)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copyfile(source, target)
    # qpdf may emit different document IDs in parallel workers. The path
    # identity is already content/profile based, so retain one valid artifact
    # and make all aliases reference its digest.
    canonical = {}
    for result in results.values():
        if result.get("status") != "optimized" or not result.get("path"):
            continue
        winner = canonical.setdefault(result["path"], result)
        result["sha256"] = winner.get("sha256")
        result["bytes"] = winner.get("bytes")
    return merged, list(results.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles", type=Path, nargs="+", required=True)
    parser.add_argument("--assets-repo", default=reader_assets.READER_ASSETS_REPO)
    args = parser.parse_args()
    api = HfApi()
    revision = api.repo_info(repo_id=args.assets_repo, repo_type="dataset").sha
    baseline = pdf_range_state.remote_state(api, args.assets_repo, revision)
    bundle, results = merge_results(args.bundles)
    state = {"version": 1, "files": dict(baseline.get("files", {})),
             "inventories": dict(baseline.get("inventories", {}))}
    fresh_results = []
    for result in results:
        previous = state["files"].get(result["key"])
        if previous and previous.get("identity") != result.get("identity"):
            # Another run already assessed this key. Do not overwrite its newer
            # state or fail the rest of an otherwise valid parallel batch.
            continue
        state["files"][result["key"]] = result
        fresh_results.append(result)
    published = pdf_range_assets.publish(api, args.assets_repo, baseline, state, bundle, fresh_results)
    print(f"published {len(fresh_results)} PDF range result(s) at {published}; skipped {len(results) - len(fresh_results)} stale result(s)", flush=True)
    shutil.rmtree(bundle, ignore_errors=True)


if __name__ == "__main__":
    main()
