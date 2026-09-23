#!/usr/bin/env python3
"""Build one OCR shard; upload only immutable objects and a small result file."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from huggingface_hub import hf_hub_download, sync_bucket
from huggingface_hub.errors import HfHubHTTPError

try:
    from . import pdf_ocr
except ImportError:
    import pdf_ocr


def source_path(item: dict) -> Path:
    for attempt in range(6):
        try:
            if item.get("source_kind") == "generated":
                return Path(hf_hub_download(
                    item["reader_assets_repo"], item["reader_assets_path"], repo_type="dataset",
                    revision=item["reader_assets_revision"], token=os.environ.get("HF_TOKEN")))
            return Path(hf_hub_download(
                item["repo"], item["path"], repo_type="dataset", revision=item["source_revision"],
                token=os.environ.get("HF_TOKEN")))
        except HfHubHTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status not in {429, 500, 502, 503, 504} or attempt == 5:
                raise
            time.sleep(min(60, 2 ** attempt))
    raise RuntimeError("source download retry limit reached")


def build_queue(queue_path: Path, shard: int, output: Path, sync_objects: bool = False) -> list[dict]:
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if queue.get("version") != 1 or queue.get("kind") != "pdf-ocr-queue":
        raise ValueError("invalid PDF OCR queue")
    shards = queue.get("shards")
    if not isinstance(shards, list) or not 0 <= shard < len(shards):
        raise ValueError("invalid PDF OCR shard")
    results = []
    for item in shards[shard].get("records", []):
        book = output / f"book-{len(results):04d}"
        book.mkdir(parents=True, exist_ok=True)
        try:
            result = pdf_ocr.build_item(item, source_path(item), book)
            result["bundle_root"] = book.name
            if sync_objects and result.get("status") == "ready":
                sync_bucket(str(book), "hf://buckets/vomebook/pdf-pages",
                            token=os.environ.get("HF_TOKEN"), include=["objects/**"], quiet=False)
        except Exception as exc:
            result = {**item, "status": "failed", "profile": pdf_ocr.asset_profile(),
                      "error": f"{type(exc).__name__}: {exc}"[:1000]}
        results.append(result)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--output", type=Path, default=Path("output/pdf-ocr/bundle"))
    parser.add_argument("--sync-bucket", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = build_queue(args.queue, args.shard, args.output, args.sync_bucket)
    (args.output / "results.json").write_text(
        json.dumps({"version": 1, "profile": pdf_ocr.asset_profile(), "results": results},
                   ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"built {len(results)} PDF OCR result(s)")
    return 0 if all(result.get("status") != "failed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
