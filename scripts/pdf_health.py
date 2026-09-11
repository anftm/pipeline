#!/usr/bin/env python3
"""Incremental PDF health checks; deliberately does not render pages."""

import argparse
import gzip
import json
import os
import subprocess
import time
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.errors import HfHubHTTPError

try:
    from . import pdf_assets
    from . import shared
except ImportError:
    import pdf_assets
    import shared

REPORT_NAME = "pdf_health.json.gz"
REPORT_VERSION = 1
BATCH_SIZE = 500
SHARD_COUNT = 18
MAX_DIAGNOSTICS = 20
MAX_DIAGNOSTIC_CHARS = 500
COMMAND_TIMEOUT = 120


def _diagnostic(values: list[str], text: str) -> None:
    for line in text.splitlines():
        line = " ".join(line.split())
        if line and line not in values and len(values) < MAX_DIAGNOSTICS:
            values.append(line[:MAX_DIAGNOSTIC_CHARS])


def _run(args: list[str], timeout: int = COMMAND_TIMEOUT) -> tuple[int, str]:
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=timeout, check=False)
        return result.returncode, result.stdout or ""
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return 124, f"timeout after {timeout}s: {output}"
    except OSError as exc:
        return 127, str(exc)


def records_from_sources(search_data: Path, revisions: Path) -> list[dict]:
    records = pdf_assets.decode_search_payload(json.loads(search_data.read_text(encoding="utf-8")))
    revision_map = json.loads(revisions.read_text(encoding="utf-8"))
    selected = []
    keys = set()
    missing_revisions = set()
    for record in records:
        repo = str(record.get("Repo") or "")
        extension = str(record.get("Extension") or "").lower().lstrip(".")
        revision = str(revision_map.get(repo) or "")
        if extension != "pdf":
            continue
        if not revision:
            missing_revisions.add(repo)
            continue
        path = pdf_assets.relative_path(record)
        key = f"{repo}\0{path}"
        if key in keys:
            raise ValueError(f"duplicate upstream PDF: {key}")
        keys.add(key)
        selected.append({
            "key": key, "repo": repo, "path": path,
            "source_revision": revision, "declared_bytes": int(record.get("Size") or 0),
            "source_url": pdf_assets.source_url(repo, revision, path),
        })
    if missing_revisions:
        raise ValueError(f"missing pinned revision for PDF repositories: {', '.join(sorted(missing_revisions))}")
    return sorted(selected, key=lambda item: (item["repo"], item["path"]))


def load_report(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {"version": REPORT_VERSION, "files": {}}
    raw = gzip.decompress(path.read_bytes())
    report = json.loads(raw.decode("utf-8"))
    if report.get("version") != REPORT_VERSION or not isinstance(report.get("files"), dict):
        raise ValueError("invalid PDF health report")
    return report


def encode_report(report: dict) -> bytes:
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(payload, compresslevel=9, mtime=0)


def pending_records(records: list[dict], report: dict) -> list[dict]:
    files = report.get("files", {})
    pending = []
    for record in records:
        current = files.get(record["key"])
        if not isinstance(current, dict) or current.get("status") in {"download-failed", "tool-error"}:
            pending.append(record)
            continue
        same_revision = current.get("source_revision") == record["source_revision"]
        unchanged_healthy = (current.get("status") in {"healthy", "warning", "encrypted"}
                             and current.get("declared_bytes") == record.get("declared_bytes"))
        if not same_revision and not unchanged_healthy:
            pending.append(record)
    return pending


def weighted_shards(records: list[dict], count: int = SHARD_COUNT) -> list[list[dict]]:
    if count != SHARD_COUNT:
        raise ValueError("PDF health requires exactly 18 shards")
    return shared.weighted_shards(
        records, count,
        weight=lambda item: int(item.get("declared_bytes") or 0),
        order=lambda item: (-int(item.get("declared_bytes") or 0), item["key"]))


def plan(records: list[dict], report: dict) -> dict:
    pending = pending_records(records, report)
    selected = pending[:BATCH_SIZE]
    shards = weighted_shards(selected) if selected else [[]]
    return {"version": REPORT_VERSION, "kind": "pdf-health-queue",
            "batch_size": BATCH_SIZE, "shard_count": SHARD_COUNT,
            "total_records": len(records), "pending_records": len(pending),
            "selected_records": len(selected), "remaining_after_batch": max(0, len(pending) - len(selected)),
            "shard_ids": list(range(SHARD_COUNT)) if selected else [0],
            "shards": [{"index": i, "declared_bytes": sum(int(r.get("declared_bytes") or 0) for r in shard),
                        "records": shard} for i, shard in enumerate(shards)]}


def inspect_pdf(path: Path, declared_bytes: int, timeout: int = COMMAND_TIMEOUT) -> dict:
    diagnostics: list[str] = []
    corrupt_reasons: list[str] = []
    warning_reasons: list[str] = []
    actual_bytes = 0
    sha256 = ""
    try:
        sha256, actual_bytes = pdf_assets.digest(path)
        with path.open("rb") as stream:
            header = stream.read(1024)
            stream.seek(max(0, actual_bytes - 65536))
            tail = stream.read()
    except OSError as exc:
        return {"status": "download-failed", "reason": "read-failed", "diagnostics": [str(exc)[:MAX_DIAGNOSTIC_CHARS]]}

    header_offset = header.find(b"%PDF-")
    if header_offset < 0:
        corrupt_reasons.append("missing-pdf-header")
    eof = tail.rfind(b"%%EOF")
    trailing_zero_bytes = 0
    if eof < 0:
        corrupt_reasons.append("missing-eof")
    else:
        trailing = tail[eof + 5:]
        trailing_zero_bytes = trailing.count(b"\x00")
        if trailing.strip(b"\x00\r\n\t"):
            warning_reasons.append("non-zero-trailing-data")
            _diagnostic(diagnostics, trailing[:1000].decode(errors="replace"))
    if actual_bytes != declared_bytes:
        warning_reasons.append("declared-size-mismatch")

    info_code, info_text = _run(["pdfinfo", str(path)], timeout)
    if info_code in {124, 127}:
        _diagnostic(diagnostics, info_text)
        reason = "pdfinfo-timeout" if info_code == 124 else "pdfinfo-unavailable"
        return {"status": "tool-error", "reason": reason, "sha256": sha256,
                "actual_bytes": actual_bytes, "declared_bytes": declared_bytes,
                "trailing_zero_bytes": trailing_zero_bytes, "diagnostics": diagnostics}
    if info_code:
        _diagnostic(diagnostics, info_text)
        warning_reasons.append("pdfinfo-check-failed")
    info = {}
    for line in info_text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            info[key.strip().lower().replace(" ", "_")] = value.strip()
    encrypted = info.get("encrypted", "").lower().startswith("yes")
    pages = None
    try:
        pages = int(info["pages"])
    except (KeyError, ValueError):
        corrupt_reasons.append("missing-page-count")
    qpdf_code, qpdf_text = _run(["qpdf", "--check", str(path)], timeout)
    if qpdf_code == 127:
        _diagnostic(diagnostics, qpdf_text)
        return {"status": "tool-error", "reason": "qpdf-unavailable", "sha256": sha256,
                "actual_bytes": actual_bytes, "declared_bytes": declared_bytes, "diagnostics": diagnostics}
    if qpdf_code == 124:
        _diagnostic(diagnostics, qpdf_text)
        return {"status": "tool-error", "reason": "qpdf-timeout", "sha256": sha256,
                "actual_bytes": actual_bytes, "declared_bytes": declared_bytes, "diagnostics": diagnostics}
    qpdf_warning = qpdf_code == 3 or "warning" in qpdf_text.lower()
    qpdf_error = qpdf_code not in {0, 3}
    if qpdf_warning or qpdf_error:
        _diagnostic(diagnostics, qpdf_text)
    if qpdf_error:
        corrupt_reasons.append("qpdf-check-failed")
    sample = []
    if pages and pages > 0:
        sample = sorted(set((1, (pages + 1) // 2, pages)))
        for page in sample:
            text_code, text_output = _run(["pdftotext", "-f", str(page), "-l", str(page), str(path), "-"], timeout)
            if text_code in {124, 127}:
                _diagnostic(diagnostics, text_output)
                reason = "pdftotext-timeout" if text_code == 124 else "pdftotext-unavailable"
                return {"status": "tool-error", "reason": reason, "sha256": sha256,
                        "actual_bytes": actual_bytes, "declared_bytes": declared_bytes, "page_count": pages,
                        "trailing_zero_bytes": trailing_zero_bytes, "diagnostics": diagnostics}
            if text_code:
                _diagnostic(diagnostics, text_output)
                warning_reasons.append("structural-extraction-failed")
    if encrypted:
        status, reason = "encrypted", "encrypted-pdf"
    elif corrupt_reasons:
        status, reason = "corrupt", corrupt_reasons[0]
    elif qpdf_warning or warning_reasons:
        status, reason = "warning", (warning_reasons[0] if warning_reasons else "qpdf-warning")
    else:
        status, reason = "healthy", "ok"
    return {"status": status, "reason": reason, "sha256": sha256, "actual_bytes": actual_bytes,
            "declared_bytes": declared_bytes, "page_count": pages, "pdf_version": info.get("pdf_version"),
            "encrypted": encrypted, "sample_pages": sample, "pdf_header_offset": header_offset,
            "eof_present": eof >= 0, "trailing_zero_bytes": trailing_zero_bytes,
            "diagnostics": diagnostics, "reasons": corrupt_reasons + warning_reasons}


def audit_record(record: dict, source: Path, timeout: int = COMMAND_TIMEOUT) -> dict:
    try:
        result = inspect_pdf(source, int(record.get("declared_bytes") or 0), timeout)
    except (OSError, ValueError, RuntimeError) as exc:
        result = {"status": "tool-error", "reason": "audit-failed", "diagnostics": [str(exc)[:MAX_DIAGNOSTIC_CHARS]]}
    return {**record, **result}


def merge_report(remote: dict, results: list[dict], current_keys: set[str] | None = None) -> dict:
    files = {key: value for key, value in remote.get("files", {}).items()
             if current_keys is None or key in current_keys}
    for result in results:
        files[result["key"]] = {key: value for key, value in result.items() if key != "key"}
    return {"version": REPORT_VERSION, "files": dict(sorted(files.items()))}


def remote_report(api: HfApi, repo: str) -> dict:
    try:
        path = api.hf_hub_download(repo_id=repo, repo_type="dataset", filename=REPORT_NAME)
    except HfHubHTTPError as exc:
        if getattr(exc.response, "status_code", None) == 404:
            return {"version": REPORT_VERSION, "files": {}}
        raise
    return load_report(Path(path))


def publish(api: HfApi, repo: str, results: list[dict], current_keys: set[str] | None = None,
            max_attempts: int = 8) -> dict:
    if not results and current_keys is None:
        return remote_report(api, repo)
    for attempt in range(max_attempts):
        try:
            info = api.repo_info(repo_id=repo, repo_type="dataset")
            remote = remote_report(api, repo)
            merged = merge_report(remote, results, current_keys)
            if merged == remote:
                return remote
            api.create_commit(repo_id=repo, repo_type="dataset", parent_commit=info.sha,
                              commit_message="Publish PDF health audit",
                              operations=[CommitOperationAdd(path_in_repo=REPORT_NAME,
                                                             path_or_fileobj=encode_report(merged))])
            return merged
        except HfHubHTTPError as exc:
            if not shared.is_retryable_hf_status(shared.hf_status_code(exc), frozenset({409, 412, 429})):
                raise
            if attempt + 1 == max_attempts:
                raise
            time.sleep(shared.hf_retry_delay(attempt, max_shift=10))
        except (ConnectionError, OSError):
            if attempt + 1 == max_attempts:
                raise
            time.sleep(shared.hf_retry_delay(attempt, max_shift=10))
    raise RuntimeError("PDF health publication retry limit reached")


def write_summary(report: dict, output=None) -> None:
    issues = [(key, value) for key, value in report.get("files", {}).items()
              if value.get("status") not in {"healthy"}]
    lines = [f"### PDF health audit", f"Audited records: {len(report.get('files', {}))}",
             f"Issues: {len(issues)}"]
    summary_limit = 500
    for key, value in issues[:summary_limit]:
        lines.append(f"- `{key}`: {value.get('status')} ({value.get('reason')})")
    if len(issues) > summary_limit:
        lines.append(f"- {len(issues) - summary_limit} additional issue(s) are retained in `{REPORT_NAME}`.")
    target = output or os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        print("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=COMMAND_TIMEOUT)
    args = parser.parse_args()
    queue = json.loads(args.queue.read_text(encoding="utf-8"))
    records = queue["shards"][args.shard_index]["records"]
    results = []
    for record in records:
        try:
            source = (args.source_dir / record["repo"] / record["path"] if args.source_dir else
                      pdf_assets.download_hf_source(record["repo"], record["path"], record["source_revision"], os.environ.get("HF_TOKEN")))
            results.append(audit_record(record, Path(source), args.timeout))
        except Exception as exc:
            results.append({**record, "status": "download-failed", "reason": "download-failed",
                            "diagnostics": [str(exc)[:MAX_DIAGNOSTIC_CHARS]]})
    args.bundle.mkdir(parents=True, exist_ok=True)
    (args.bundle / "bundle.json").write_text(json.dumps({"version": 1, "results": results}, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
