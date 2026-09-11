#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

from huggingface_hub import hf_hub_download
from pdf_health import load_report, plan, records_from_sources


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", "vomebook/Reader-Assets"))
    parser.add_argument("--output", type=Path, default=Path("output/pdf-health/queue.json"))
    args = parser.parse_args()
    report = args.report
    if report is None:
        try:
            report = Path(hf_hub_download(args.assets_repo, "pdf_health.json.gz", repo_type="dataset",
                                          token=os.environ.get("HF_TOKEN")))
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) != 404:
                raise
    queue = plan(records_from_sources(args.search_data, args.revisions), load_report(report))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(queue, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"planned {queue['selected_records']} of {queue['pending_records']} pending PDF(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
