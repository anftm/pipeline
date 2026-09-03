#!/usr/bin/env python3
"""Plan a deterministic, page-weighted PDF asset build queue."""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
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


def plan(records: list[dict], source_dir: Path | None, assets_repo: str, shard_count: int,
         workers: int = 8) -> dict:
    def inspect(item: dict) -> dict:
        pages = pdf_assets._pages(source_path(item, source_dir, assets_repo))
        if pages < 1:
            raise ValueError(f"invalid page count for {item['key']}")
        return {**item, "page_count": pages}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        selected = list(executor.map(inspect, records))
    tasks = []
    for item in selected:
        if item["page_count"] <= pdf_assets.MAX_PAGES_PER_TASK:
            tasks.append(item)
            continue
        for start in range(1, item["page_count"] + 1, pdf_assets.MAX_PAGES_PER_TASK):
            end = min(item["page_count"], start + pdf_assets.MAX_PAGES_PER_TASK - 1)
            tasks.append({**item, "task_key": f"{item['key']}#pages-{start:06d}-{end:06d}",
                          "page_start": start, "page_end": end,
                          "range_page_count": end - start + 1})
    shards = pdf_assets.weighted_shards(tasks, shard_count)
    return {"version": 1, "kind": "pdf-assets-queue", "shard_count": shard_count,
            "total_records": len(selected), "total_tasks": len(tasks),
            "total_pages": sum(item["page_count"] for item in selected),
            "shards": [{"index": index, "page_count": sum(item.get("range_page_count", item["page_count"])
                                                               for item in shard),
                        "records": shard} for index, shard in enumerate(shards)]}


def pending_records(records: list[dict], manifest: dict) -> list[dict]:
    done = {}
    for key, entry in manifest.get("files", {}).items():
        if isinstance(entry, dict) and entry.get("status") in {"ready", "skipped"}:
            done[key] = entry
    pending = []
    for item in records:
        if item.get("source_kind") == "generated":
            if item["key"] not in done:
                pending.append(item)
            continue
        if int(item.get("source_bytes") or 0) >= pdf_assets.MIN_BYTES and item["key"] not in done:
            pending.append(item)
    return pending


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
    parser.add_argument("--workers", type=int, default=8)
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
    try:
        manifest_path = hf_hub_download(args.assets_repo, "pdf_manifest.json", repo_type="dataset",
                                        token=os.environ.get("HF_TOKEN"))
        pdf_manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        pdf_manifest = {"files": {}}
    records = pending_records(records, pdf_manifest)
    selected = pdf_assets.queue(records, args.limit, args.checkpoint)
    planned = plan(selected, args.source_dir, args.assets_repo, args.shard_count, args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(planned, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"planned {planned['total_records']} PDF asset(s) across {args.shard_count} shard(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
