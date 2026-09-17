#!/usr/bin/env python3
"""One PDF assessment per subprocess, bounded by the parent process group."""
import argparse
import json
from pathlib import Path

try:
    from . import pdf_range
    from .pdf_range_assets import compact_report
except ImportError:
    import pdf_range
    from pdf_range_assets import compact_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--vendor", type=Path, required=True)
    args = parser.parse_args()
    try:
        report, _ = pdf_range.assess(args.source, args.work, args.vendor)
        report = compact_report(report)
    except pdf_range.UnsupportedPDF as error:
        report = {"status": "unsupported", "reason": str(error)[:400]}
    except Exception as error:
        details = pdf_range.failure_details(error)
        report = {"status": "failed", "reason": details["error"], "error_category": details["error_category"]}
    (args.work / "assessment.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
