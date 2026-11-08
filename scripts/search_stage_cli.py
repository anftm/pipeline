"""Build a verified local search snapshot and optionally stage immutable objects.

This command never advances the published pointer or starts consumers. A
successful stage confirms exact object readback, not successful deployment.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .search_artifact_s3 import S3SearchArtifactStore
    from .search_artifact_store import stage_generation
    from .search_generation import build_generation
except ImportError:
    from search_artifact_s3 import S3SearchArtifactStore
    from search_artifact_store import stage_generation
    from search_generation import build_generation


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--revisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", action="store_true", help="conditionally create immutable S3 objects")
    args = parser.parse_args(argv)
    report = build_generation(args.source, args.tree, args.browser, args.revisions, args.output)
    result = {"generation": report["generation"], "record_count": report["record_count"],
              "staged": False, "pointer_written": False, "consumers_verified": False}
    if args.stage:
        staging = stage_generation(args.output, S3SearchArtifactStore())
        result.update(staging)
        result["staged"] = True
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
