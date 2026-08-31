#!/usr/bin/env python3
"""Plan a deterministic, page-weighted PDF asset build queue."""

import argparse
import json
import os
from pathlib import Path

from huggingface_hub import hf_hub_download

try:
    from . import pdf_assets
except ImportError:
    import pdf_assets


def source_path(item: dict, source_dir: Path | None, assets_repo: str) -> Path:
    if item.get("source_kind") == "generated":
        return Path(hf_hub_download(item["reader_assets_repo"], item["reader_assets_path"],
                                    repo_type="dataset", token=os.environ.get("HF_TOKEN")))
    if source_dir:
        return source_dir / item["repo"] / item["path"]
    return Path(hf_hub_download(item["repo"], item["path"], repo_type="dataset",
                                revision=item["source_revision"], token=os.environ.get("HF_TOKEN")))


def plan(records: list[dict], source_dir: Path | None, assets_repo: str, shard_count: int) -> dict:
    selected = []
    for item in records:
        pages = pdf_assets._pages(source_path(item, source_dir, assets_repo))
        if pages < 1:
            raise ValueError(f"invalid page count for {item['key']}")
        selected.append({**item, "page_count": pages})
    shards = pdf_assets.weighted_shards(selected, shard_count)
    return {"version": 1, "kind": "pdf-assets-queue", "shard_count": shard_count,
            "total_records": len(selected),
            "total_pages": sum(item["page_count"] for item in selected),
            "shards": [{"index": index, "page_count": sum(item["page_count"] for item in shard),
                        "records": shard} for index, shard in enumerate(shards)]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--repo", default="")
    parser.add_argument("--source", choices=("upstream", "generated", "all"), default="all")
    parser.add_argument("--reader-assets-manifest", type=Path)
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", pdf_assets.READER_ASSETS_REPO))
    parser.add_argument("--limit", type=int, required=True, help="Total PDFs in this checkpoint")
    parser.add_argument("--checkpoint", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=10)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/pdf-assets/queue.json"))
    args = parser.parse_args()
    records = []
    if args.source in {"upstream", "all"}:
        records.extend(pdf_assets.load_records(args.search_data, args.revisions, args.repo, "pdf"))
    if args.source in {"generated", "all"}:
        if not args.reader_assets_manifest:
            manifest = hf_hub_download(args.assets_repo, "manifest.json", repo_type="dataset",
                                       token=os.environ.get("HF_TOKEN"))
            args.reader_assets_manifest = Path(manifest)
        records.extend(pdf_assets.load_generated_records(args.reader_assets_manifest, args.assets_repo, args.repo))
    records.sort(key=lambda item: (0 if item.get("source_extension") in {"caj", "kdh"} else 1,
                                   item["repo"], item["path"], item["source_kind"]))
    selected = pdf_assets.queue(records, args.limit, args.checkpoint)
    planned = plan(selected, args.source_dir, args.assets_repo, args.shard_count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(planned, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"planned {planned['total_records']} PDF asset(s) across {args.shard_count} shard(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
