#!/usr/bin/env python3
import argparse
import gzip
import json
import os
from pathlib import Path

from huggingface_hub import HfApi
from pdf_health import encode_report, publish, write_summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles", type=Path, nargs="+", required=True)
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", "vomebook/Reader-Assets"))
    parser.add_argument("--output", type=Path, default=Path("output/pdf-health/pdf_health.json.gz"))
    parser.add_argument("--current-keys", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    results = []
    for bundle in args.bundles:
        data = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError(f"invalid PDF health bundle: {bundle}")
        results.extend(data.get("results", []))
    if args.dry_run:
        report = {"version": 1, "files": {result["key"]: {k: v for k, v in result.items() if k != "key"} for result in results}}
    else:
        current_keys = None
        if args.current_keys:
            current_keys = set(json.loads(gzip.decompress(args.current_keys.read_bytes())))
        report = publish(HfApi(token=os.environ.get("HF_TOKEN")), args.assets_repo, results, current_keys)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encode_report(report))
    write_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
