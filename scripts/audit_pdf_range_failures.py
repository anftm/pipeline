#!/usr/bin/env python3
"""Read-only inventory of failed/unsupported PDF layout assessments."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from huggingface_hub import HfApi

try:
    from . import pdf_range_state, reader_assets
except ImportError:
    import pdf_range_state, reader_assets


def failure_group(row):
    reason = row.get("reason", "")
    errors = " ".join(value.get("error", "") for value in row.get("candidates", {}).values())
    if row.get("status") == "unsupported":
        if "password" in reason or "encrypted" in reason:
            return "encryption"
        if "catalog structures" in reason:
            return "catalog-equivalence"
        if "annotations" in reason:
            return "annotation-equivalence"
        if "digital signatures" in reason:
            return "digital-signature"
        return "other-unsupported"
    if "signature differs" in errors:
        return "content-mismatch"
    if "qpdf exit 2" in errors:
        return "qpdf-error"
    if "qpdf exit 3" in errors:
        return "legacy-qpdf-warning"
    if "UnknownErrorException" in reason:
        return "pdfjs-worker-error"
    if "cyclic page" in reason.lower():
        return "cyclic-page-tree"
    return row.get("error_category", "other-failed")


def audit(state):
    files = []
    fields = ("repo", "source_path", "source_kind", "source_bytes", "input_repo", "input_path",
              "input_revision", "input_token", "input_sha256", "input_profile", "identity",
              "status", "reason", "error_category")
    for key, row in sorted(state.get("files", {}).items()):
        if row.get("status") not in {"failed", "unsupported"}:
            continue
        files.append({"key": key, **{field: row[field] for field in fields if field in row},
                      "group": failure_group(row),
                      "candidate_errors": {method: value["error"] for method, value in row.get("candidates", {}).items()
                                           if value.get("error")}})
    return {"counts": dict(Counter(row.get("status", "unknown") for row in state.get("files", {}).values())),
            "groups": dict(Counter(row["group"] for row in files).most_common()), "files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-repo", default=reader_assets.READER_ASSETS_REPO)
    parser.add_argument("--revision", help="Read one fixed HF dataset revision; defaults to current head")
    parser.add_argument("--manifest", type=Path, help="Use a local canonical manifest instead of HF")
    parser.add_argument("--output", type=Path, help="Save the complete audit JSON; stdout always shows totals")
    args = parser.parse_args()
    if args.manifest:
        state = json.loads(args.manifest.read_text(encoding="utf-8"))
        source = {"local_manifest": str(args.manifest)}
    else:
        api = HfApi()
        revision = args.revision or api.repo_info(args.assets_repo, repo_type="dataset").sha
        state = pdf_range_state.remote_state(api, args.assets_repo, revision)
        source = {"assets_repo": args.assets_repo, "revision": revision}
    report = {"source": source, "observed_at": datetime.now(timezone.utc).isoformat(), **audit(state)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
