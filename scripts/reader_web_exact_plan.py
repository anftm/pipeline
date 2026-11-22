#!/usr/bin/env python3
"""Preflight one exact web repair from local files and print a JSON report.

Supply the current canonical snapshot with an independently pinned raw SHA-256,
its full expected old row, the original bytes, and the producer's output row,
manifest and HTML. This checks local evidence only: it cannot establish remote
freshness, retention, sidecar/v3 state or publication readiness.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
import stat
import sys

if __package__:
    from .reader_stream_checkpoint_cas import merge_observed
else:
    from reader_stream_checkpoint_cas import merge_observed

BUCKET = "vomebook/reader-assets-v2"
KIND = "web-document-stream-index"
EXTENSIONS = {"html", "htm", "mht", "mhtml"}
MAX_CANONICAL = 8 * 1024 * 1024
MAX_ROW = 64 * 1024
MAX_MANIFEST = 16 * 1024
MAX_HTML = 64 * 1024 * 1024
MAX_ORIGINAL = 256 * 1024 * 1024
HEX_SHA = re.compile(r"[0-9a-f]{64}")
ROW_FIELDS = {"key", "repo", "path", "extension", "source_revision", "source_sha256",
              "mode", "bucket", "object", "manifest", "bytes", "sha256"}


@contextmanager
def _bounded_file(path, limit, label):
    # Open nonblocking so a mistaken FIFO input cannot hang the preflight.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{label} must be a local regular file")
            if info.st_size > limit:
                raise ValueError(f"{label} exceeds byte limit")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                yield stream
        finally:
            os.close(fd)
    except OSError:
        raise ValueError(f"cannot read {label}") from None


def _read(path, limit, label):
    with _bounded_file(path, limit, label) as stream:
        raw = stream.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError(f"{label} is empty or exceeds byte limit")
    return raw


def _original_sha(path):
    digest = sha256()
    remaining = MAX_ORIGINAL
    with _bounded_file(path, MAX_ORIGINAL, "original") as stream:
        while chunk := stream.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("original exceeds byte limit")
            digest.update(chunk)
    if remaining == MAX_ORIGINAL:
        raise ValueError("original is empty")
    return digest.hexdigest()


def _json(raw, label):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    def finite(token):
        value = float(token)
        if not isfinite(value):
            raise ValueError("non-finite number")
        return value

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_float=finite, parse_constant=finite)
    except (ValueError, RecursionError):
        raise ValueError(f"invalid {label} JSON") from None


def _json_equal(a, b):
    # In particular, JSON true, 1 and 1.0 are not interchangeable here.
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _is_sha(value):
    return isinstance(value, str) and HEX_SHA.fullmatch(value) is not None


def _rows(index, field):
    rows = index.get(field) if isinstance(index, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"invalid canonical {field} list")
    result = {}
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else None
        if not isinstance(key, str) or not key or key in result:
            raise ValueError(f"invalid or duplicate canonical {field} key")
        result[key] = row
    return result


def _web_row(row, *, old):
    label = "old row" if old else "candidate row"
    extras = {"profile"} | ({"checkpointed_at", "processing_roots"} if old else set())
    if not isinstance(row, dict) or not ROW_FIELDS <= row.keys() <= ROW_FIELDS | extras:
        raise ValueError(f"invalid {label} fields")
    if "profile" in row and (not isinstance(row["profile"], str)
                             or re.fullmatch(r"web-utf8-typography-v[1-9][0-9]{0,5}", row["profile"]) is None):
        raise ValueError(f"invalid {label} profile")
    repo, path, ext = row["repo"], row["path"], row["extension"]
    if (not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo, re.ASCII)
            or not isinstance(path, str) or any(c in path for c in "\0\\\r\n")
            or any(part in {"", ".", ".."} for part in (repo + "/" + path).split("/"))
            or not isinstance(ext, str) or ext not in EXTENSIONS or not path.lower().endswith("." + ext)
            or row["key"] != f"{repo}\0{path}"):
        raise ValueError(f"invalid {label} repo/path key or extension")
    if (row["bucket"] != BUCKET or row["mode"] != "document-stream"
            or not isinstance(row["source_revision"], str) or not row["source_revision"]
            or not _is_sha(row["source_sha256"]) or not _is_sha(row["sha256"])
            or type(row["bytes"]) is not int or row["bytes"] <= 0):
        raise ValueError(f"invalid {label} web stream metadata")
    base = f"documents/web/{ext}"
    if old:
        # Retain the exact observed old location, even for legacy hash schemes.
        match = (re.fullmatch(rf"({base}/[0-9a-f]{{64}})/document\.html", row["object"])
                 if isinstance(row["object"], str) else None)
        if match is None:
            raise ValueError("invalid old object path")
        root = match[1]
        if "checkpointed_at" in row and not isinstance(row["checkpointed_at"], str):
            raise ValueError("invalid old checkpoint watermark")
        refs = row.get("processing_roots", [])
        if (not isinstance(refs, list) or any(not isinstance(ref, str) or not re.fullmatch(
                r"reader-index/processing/stream-upload-[0-9a-f]{32}\.json", ref) for ref in refs)
                or len(set(refs)) != len(refs)):
            raise ValueError("invalid old processing root references")
    else:
        bundle = sha256(f"{row['source_sha256']}\0{row['sha256']}".encode("ascii")).hexdigest()
        root = f"{base}/{bundle}"
    if row["object"] != f"{root}/document.html" or row["manifest"] != f"{root}/document-manifest.json":
        raise ValueError(f"{label} does not use its exact immutable paths")
    return root


def plan(*, canonical: Path, canonical_sha256: str, old_row: Path, candidate_row: Path,
         manifest: Path, html: Path, original: Path) -> dict:
    """Check exact local evidence; return only metadata, never a writer payload."""
    canonical_raw = _read(canonical, MAX_CANONICAL, "canonical index")
    if not _is_sha(canonical_sha256) or sha256(canonical_raw).hexdigest() != canonical_sha256:
        raise ValueError("canonical raw SHA-256 mismatch")
    parent = _json(canonical_raw, "canonical index")
    if (not isinstance(parent, dict) or type(parent.get("version")) is not int
            or parent["version"] != 1 or parent.get("kind") != KIND
            or parent.keys() - {"version", "kind", "files", "failures", "checkpoint_operation"}):
        raise ValueError("invalid canonical web index schema")
    before = _rows(parent, "files")
    _rows(parent, "failures")

    old = _json(_read(old_row, MAX_ROW, "old row"), "old row")
    old_root = _web_row(old, old=True)
    key = old["key"]
    if key not in before or not _json_equal(before[key], old):
        raise ValueError("exact old row does not match canonical index")
    candidate = _json(_read(candidate_row, MAX_ROW, "candidate row"), "candidate row")
    new_root = _web_row(candidate, old=False)
    if candidate["key"] != key:
        raise ValueError("candidate does not match exact old repo/path key")
    source_sha = _original_sha(original)
    if source_sha != old["source_sha256"] or source_sha != candidate["source_sha256"]:
        raise ValueError("original SHA-256 differs from old or candidate source")
    if new_root == old_root or candidate["sha256"] == old["sha256"]:
        raise ValueError("candidate is not a new immutable web output")

    manifest_raw = _read(manifest, MAX_MANIFEST, "candidate manifest")
    expected = {"kind": "web-document-stream", "version": 1, "path": "document.html",
                "source_extension": candidate["extension"], "source_sha256": source_sha,
                "bytes": candidate["bytes"], "sha256": candidate["sha256"]}
    if not _json_equal(_json(manifest_raw, "candidate manifest"), expected):
        raise ValueError("candidate manifest does not match producer schema and row")
    doc = _read(html, MAX_HTML, "candidate HTML")
    if len(doc) != candidate["bytes"] or sha256(doc).hexdigest() != candidate["sha256"]:
        raise ValueError("candidate HTML size or SHA-256 mismatch")
    try:
        doc.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("candidate HTML is not strict UTF-8") from None

    # A layout-only repair does not advance the source watermark or reuse old
    # processing records for the new root. Never emit the synthetic payload.
    entry = {**candidate, "source_revision": old["source_revision"]}
    if "checkpointed_at" in old:
        entry["checkpointed_at"] = old["checkpointed_at"]
    try:
        observed = deepcopy(parent)
        preview = merge_observed(observed, observed, [entry.copy()], [], kind=KIND,
                                 operation_id="0" * 32, preserve_canonical_rows=True)
    except (ValueError, RecursionError):
        raise ValueError("canonical merge rejected the exact repair") from None
    after = _rows(preview, "files")
    if (after.keys() != before.keys() or not _json_equal(after[key], entry)
            or [k for k in before if not _json_equal(before[k], after[k])] != [key]
            or not _json_equal(preview.get("failures"), parent["failures"])):
        raise ValueError("canonical preview must change only the exact selected row, with no failure changes")

    return {
        "version": 1, "kind": "reader-web-exact-preflight", "canonical_parent_sha256": canonical_sha256,
        "bucket": BUCKET, "canonical_index": f"documents/web/{old['extension']}/index.json",
        "repo": old["repo"], "path": old["path"], "same_source_sha256": source_sha,
        "changed_rows": 1, "other_rows_preserved": len(before) - 1, "failures_preserved": len(parent["failures"]),
        "retained_roots_plan": [{"bucket": BUCKET, "root": old_root, "objects": [old["object"], old["manifest"]],
                                 "html_sha256": old["sha256"], "html_bytes": old["bytes"],
                                 "processing_roots": old.get("processing_roots", [])}],
        "candidate": {field: candidate[field] for field in ("object", "manifest", "sha256", "bytes")}
        | {"manifest_sha256": sha256(manifest_raw).hexdigest()},
        "retention_verified": False, "publication_ready": False, "remote_writes": 0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-sha256", required=True, help="independently pinned raw canonical SHA-256")
    for option in ("canonical", "old-row", "candidate-row", "manifest", "html", "original"):
        parser.add_argument(f"--{option}", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = plan(**vars(args))
    except ValueError as error:
        print(f"web exact preflight rejected: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
