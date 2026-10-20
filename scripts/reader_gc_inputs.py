#!/usr/bin/env python3
"""Delete lossless OCR-input PNGs of books already released by the v3 journal.

This is a narrow collection tier, not the full graph collector. It only deletes
`page-NNNNNN.png` objects directly under `.../ocr-input` roots that are listed in
`reader-index/v3/retirement.json` `retired_input_roots`. Immediately before
deleting a root it re-checks that:

- the OCR registry still has a ready entry for the same source whose OCR
  manifest digest is the one recorded at release, and
- no current CCRD OCR export retains any resource under that root.

Producers never write into such a root: its 16-hex namespace is derived from the
source digest and render profile, and a ready book is not re-rendered (a forced
re-OCR uses its own namespace). The full drained collector is therefore not
required, and OCR/v3 producers keep running.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

try:
    from . import shared
    from .reader_gc_fence import assert_collector_context
except ImportError:
    import shared
    from reader_gc_fence import assert_collector_context

ASSETS = shared.READER_ASSETS_BUCKET
INPUT = shared.PDF_OCR_INPUT_BUCKET
CCRD = "vomebook/ccrd-index"
JOURNAL = "reader-index/v3/retirement.json"
REGISTRY = "reader-index/pdf_ocr_manifest.json"
ROOT = re.compile(r"objects/[0-9a-f]{2}/([0-9a-f]{64})/[0-9a-f]{16}/ocr-input")
PAGE = re.compile(r"page-[0-9]{6}\.png")


def ccrd_pinned_roots(store):
    """Input roots retained by the live CCRD OCR release (normally none)."""
    store = getattr(store, "writer", store)
    try:
        pointer = json.loads(store.read_bytes(CCRD, "control/ocr-current.json"))
    except FileNotFoundError:
        return set()
    release = json.loads(store.read_bytes(CCRD, pointer["release"]["path"]))
    pinned = set()
    for ref in release.get("exports", []):
        export = json.loads(store.read_bytes(CCRD, ref["path"]))
        for item in export.get("retained_reader_resources", []):
            if item.get("bucket") == INPUT:
                pinned.add(item["path"].rsplit("/", 1)[0])
    return pinned


def plan(store, *, limit=0):
    journal = json.loads(store.read_bytes(ASSETS, JOURNAL))
    if journal.get("kind") != "reader-v3-retirement" or journal.get("version") != 1:
        raise ValueError("invalid retirement journal")
    roots = journal.get("retired_input_roots") or {}
    registry = json.loads(store.read_bytes(ASSETS, REGISTRY)).get("files", {})
    ready = {(entry.get("source_sha256"), entry.get("ocr_manifest_sha256"))
             for entry in registry.values() if entry.get("status") == "ready"}
    pinned = ccrd_pinned_roots(store)
    report = {"journal_roots": len(roots), "ccrd_pinned_roots": len(pinned), "skipped": {}, "roots": []}

    def skip(reason):
        report["skipped"][reason] = report["skipped"].get(reason, 0) + 1

    for root in sorted(roots):
        item = roots[root]
        match = ROOT.fullmatch(root)
        if (not match or item.get("root") != root or item.get("bucket") != INPUT
                or item.get("source_sha256") != match[1]):
            skip("invalid-journal-root")
            continue
        if root in pinned:
            skip("ccrd-retained")
            continue
        if (item["source_sha256"], item.get("ocr_manifest_sha256")) not in ready:
            skip("ocr-registry-changed")
            continue
        files = store.list_files(INPUT, (root + "/",))
        pages = sorted(path for path in files if path.rsplit("/", 1)[0] == root and PAGE.fullmatch(path.rsplit("/", 1)[1]))
        if not pages:
            skip("already-empty")
            continue
        sizes = getattr(store, "object_sizes", {})
        report["roots"].append({"root": root, "pages": pages,
                                "bytes": sum(sizes.get((INPUT, path), 0) for path in pages)})
        if limit and len(report["roots"]) >= limit:
            break
    report["planned_objects"] = sum(len(item["pages"]) for item in report["roots"])
    report["planned_bytes"] = sum(item["bytes"] for item in report["roots"])
    return report


def collect(store, report, *, checkpoint=None):
    deleted = {"roots": 0, "objects": 0, "bytes": 0}
    for item in report["roots"]:
        pages = item["pages"]
        for start in range(0, len(pages), 1000):
            store.delete(INPUT, pages[start:start + 1000])
        remaining = set(pages) & store.list_files(INPUT, (item["root"] + "/",))
        if remaining:
            raise RuntimeError(f"{len(remaining)} OCR inputs remain after deletion under {item['root']}")
        item["status"] = "verified-deleted"
        deleted["roots"] += 1
        deleted["objects"] += len(pages)
        deleted["bytes"] += item["bytes"]
        report["deleted"] = deleted
        if checkpoint:
            checkpoint(report)
    report["deleted"] = deleted
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="maximum roots; zero means all")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("limit must be non-negative")
    if args.apply:
        assert_collector_context()
    try:
        from .reader_gc_store import CollectionStore
    except ImportError:
        from reader_gc_store import CollectionStore
    store = CollectionStore()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save(report):
        summary = {key: value for key, value in report.items() if key != "roots"}
        summary["root_samples"] = [{k: v for k, v in item.items() if k != "pages"} | {"pages": len(item["pages"])}
                                   for item in report["roots"][:50]]
        args.output.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    report = plan(store, limit=args.limit)
    report.update(applied=args.apply, run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    save(report)
    print(json.dumps({k: v for k, v in report.items() if k != "roots"}, sort_keys=True), flush=True)
    if args.apply:
        collect(store, report, checkpoint=save)
        save(report)
        print(json.dumps(report["deleted"], sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
