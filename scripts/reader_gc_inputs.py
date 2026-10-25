#!/usr/bin/env python3
"""Delete lossless OCR-input PNGs of books already released by the v3 journal.

This is a narrow collection tier, not the full graph collector. It only deletes
`page-NNNNNN.png` objects directly under `.../ocr-input` roots that are listed in
`reader-index/v3/retirement.json` `retired_input_roots`. Immediately before
deleting a root it re-checks that:

- the OCR registry still has a ready entry for the same source whose OCR
  manifest digest is the one recorded at release, and
- no current CCRD OCR export retains any resource under that root.

The collector requires the shared maintenance fence. The publication lock alone
cannot prevent a separate CCRD publisher from adding an input pin between the
last read and deletion. Scheduled collection uses the common retired-assets tier.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

try:
    from . import shared, ccrd_lifecycle
    from .reader_gc_fence import assert_collector_context, GitHubProducerFence
except ImportError:
    import shared
    import ccrd_lifecycle
    from reader_gc_fence import assert_collector_context, GitHubProducerFence

ASSETS = shared.READER_ASSETS_BUCKET
INPUT = shared.PDF_OCR_INPUT_BUCKET
CCRD = "vomebook/ccrd-index"
JOURNAL = "reader-index/v3/retirement.json"
REGISTRY = "reader-index/pdf_ocr_manifest.json"
ROOT = re.compile(r"objects/[0-9a-f]{2}/([0-9a-f]{64})/[0-9a-f]{16}/ocr-input")
PAGE = re.compile(r"page-[0-9]{6}\.png")
# Inputs are kept for a grace period after release. A later render-profile or
# recognition change cannot reuse an OCR result whose page images are gone, so
# deleting immediately forces a full re-render for any book we still need to
# touch. The journal records released_at; older entries without it are treated
# as long released.
KEEP_DAYS = 30


def ccrd_pinned_roots(store):
    """Keep inputs pinned by current, rollback, uploading and correction roots."""
    roots = store.list_files(CCRD, ('control/',))
    pending = {p: None for p in roots if ccrd_lifecycle.is_root(p)}
    seen, pinned, controls = set(), set(), {}
    while pending:
        path, expected = pending.popitem()
        if path in seen:
            continue
        raw = store.read_bytes(CCRD, path)
        if expected and (len(raw) != expected['bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']):
            raise ValueError('CCRD input pin checksum mismatch')
        if path in roots:
            controls[path] = raw
        value = json.loads(raw)
        for ref in ccrd_lifecycle.references(value, path):
            if ref['bucket'] == INPUT:
                pinned.add(ref['path'].rsplit('/', 1)[0])
            elif ref['bucket'] == CCRD and ccrd_lifecycle.is_document(ref['path']) and ref['path'] not in seen:
                pending[ref['path']] = ref
        seen.add(path)
    if store.list_files(CCRD, ('control/',)) != roots or any(
            store.read_bytes(CCRD, path) != raw for path, raw in controls.items()):
        raise RuntimeError('CCRD input pins changed during verification')
    return pinned


def eligible_identity(store, root, expected):
    journal = json.loads(store.read_bytes(ASSETS, JOURNAL))
    if journal.get('kind') != 'reader-v3-retirement' or journal.get('version') != 1:
        raise ValueError('invalid retirement journal')
    if journal.get('retired_input_roots', {}).get(root) != expected:
        raise RuntimeError('input retirement changed before deletion')
    registry = json.loads(store.read_bytes(ASSETS, REGISTRY)).get('files', {})
    if not any(e.get('status') == 'ready' and e.get('source_sha256') == expected['source_sha256']
               and e.get('ocr_manifest_sha256') == expected['ocr_manifest_sha256'] for e in registry.values()):
        raise RuntimeError('OCR registry changed before input deletion')
    if root in ccrd_pinned_roots(store):
        raise RuntimeError('CCRD retained input before deletion')


def plan(store, *, limit=0):
    try:
        journal = json.loads(store.read_bytes(ASSETS, JOURNAL))
    except FileNotFoundError:
        return {'journal_roots': 0, 'ccrd_pinned_roots': 0, 'skipped': {}, 'roots': [],
                'planned_objects': 0, 'planned_bytes': 0}
    if journal.get("kind") != "reader-v3-retirement" or journal.get("version") != 1:
        raise ValueError("invalid retirement journal")
    roots = journal.get("retired_input_roots") or {}
    registry = json.loads(store.read_bytes(ASSETS, REGISTRY)).get("files", {})
    ready = {(entry.get("source_sha256"), entry.get("ocr_manifest_sha256"))
             for entry in registry.values() if entry.get("status") == "ready"}
    pinned = ccrd_pinned_roots(store)
    report = {"journal_roots": len(roots), "ccrd_pinned_roots": len(pinned), "skipped": {}, "roots": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)

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
        released_at = item.get("released_at")
        if released_at:
            try:
                released = datetime.fromisoformat(str(released_at).replace("Z", "+00:00"))
            except ValueError:
                skip('invalid-release-time')
                continue
            if released.tzinfo is None:
                skip('invalid-release-time')
                continue
            if released > cutoff:
                skip("recently-released")
                continue
        files = store.list_files(INPUT, (root + "/",))
        pages = sorted(path for path in files if path.rsplit("/", 1)[0] == root and PAGE.fullmatch(path.rsplit("/", 1)[1]))
        if not pages:
            skip("already-empty")
            continue
        sizes = getattr(store, "object_sizes", {})
        report["roots"].append({"root": root, "pages": pages, 'retirement': item,
                                'retained': sorted(files - set(pages)),
                                "bytes": sum(sizes.get((INPUT, path), 0) for path in pages)})
        if limit and len(report["roots"]) >= limit:
            break
    report["planned_objects"] = sum(len(item["pages"]) for item in report["roots"])
    report["planned_bytes"] = sum(item["bytes"] for item in report["roots"])
    return report


def collect(store, report, *, checkpoint=None, fence=None):
    deleted = {"roots": 0, "objects": 0, "bytes": 0}
    report.update(complete=False, deleted=deleted)
    for item in report["roots"]:
        pages = item["pages"]
        item['batches'] = []
        for start in range(0, len(pages), 1000):
            if fence:
                fence.check()
            eligible_identity(store, item['root'], item['retirement'])
            batch = pages[start:start + 1000]
            item['status'] = 'submitted'
            receipt = {'paths': batch, 'status': 'submitted'}
            item['batches'].append(receipt)
            if checkpoint:
                checkpoint(report)
            store.delete(INPUT, batch)
            receipt['status'] = 'accepted'
            if checkpoint:
                checkpoint(report)
        if fence:
            fence.check()
        eligible_identity(store, item['root'], item['retirement'])
        remaining = store.list_files(INPUT, (item["root"] + "/",))
        if set(pages) & remaining:
            raise RuntimeError(f"{len(set(pages) & remaining)} OCR inputs remain after deletion under {item['root']}")
        if not set(item.get('retained', [])) <= remaining:
            raise RuntimeError('retained OCR input sibling disappeared during collection')
        for receipt in item['batches']:
            receipt['status'] = 'verified-deleted'
        item["status"] = "verified-deleted"
        deleted["roots"] += 1
        deleted["objects"] += len(pages)
        deleted["bytes"] += item["bytes"]
        report["deleted"] = deleted
        if checkpoint:
            checkpoint(report)
    report["deleted"] = deleted
    report['complete'] = True
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
        GitHubProducerFence().check()
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
        if args.apply:
            run = os.environ.get('GITHUB_RUN_ID', 'local')
            store.put_json(ASSETS, f'reader-index/gc/{run}/inputs.json', report)

    report = plan(store, limit=args.limit)
    report.update(applied=args.apply, run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    save(report)
    print(json.dumps({k: v for k, v in report.items() if k != "roots"}, sort_keys=True), flush=True)
    if args.apply:
        try:
            collect(store, report, checkpoint=save, fence=GitHubProducerFence())
        except Exception as error:
            report['error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            save(report)
        print(json.dumps(report["deleted"], sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
