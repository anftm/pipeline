#!/usr/bin/env python3
"""Collect explicitly withdrawn document outputs in independent, resumable scopes.

This collector does not infer orphanhood from a filename. A central reset receipt,
empty canonical document registries, current consumer projections and a drained
document-producer fence establish retirement first. Strict schemas then bound the
exact paths; ordinary image/TIFF assets and original source repositories are out
of scope.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

try:
    from . import reader_document_reset as reset, ccrd_lifecycle
    from . import publish_reader_v3 as publication
    from .reader_bucket_store import S3BucketStore, HubBucketStore, retry_s3
    from .reader_gc_fence import assert_collector_context
except ImportError:
    import reader_document_reset as reset
    import ccrd_lifecycle
    import publish_reader_v3 as publication
    from reader_bucket_store import S3BucketStore, HubBucketStore, retry_s3
    from reader_gc_fence import assert_collector_context


SCOPES = ('ocr-inputs', 'document-pages', 'djvu-pdf', 'ocr-library', 'document-control')
CCRD = ccrd_lifecycle.BUCKET
CONTROL_KEEP = {'reader-index/v3/current.json', 'reader-index/v3/automation.json',
                'reader-index/v3/corrections/state.json', 'reader-index/pdf_ocr_progress/index.json'}


class Store(S3BucketStore):
    """S3 batches remove exact keys; the existing Hub writer owns control JSON."""
    def __init__(self):
        super().__init__()
        self.writer = HubBucketStore()

    def put_bytes(self, bucket, path, raw):
        self.writer.put_bytes(bucket, path, raw)

    def delete(self, bucket, paths):
        if bucket not in {reset.ASSETS, reset.PAGES, reset.INPUTS, CCRD}:
            raise ValueError('unsupported document collection bucket')
        if any(not path or path.startswith('/') or '\\' in path or '\0' in path
               or any(part in {'', '.', '..'} for part in path.split('/')) for path in paths):
            raise ValueError('invalid collection key')
        # S3 reports per-key failures even with HTTP 200. The base implementation
        # checks Errors; retry remains idempotent for these exact retired keys.
        retry_s3(lambda: super(Store, self).delete(bucket, paths))


def preflight(store, reset_id, client):
    receipt_raw = store.read_bytes(reset.ASSETS, reset.RECEIPT)
    receipt = reset.decode(receipt_raw)
    if receipt.get('reset_id') != reset_id or receipt.get('status') != 'withdrawn':
        raise ValueError('document withdrawal is not complete')
    ccrd_path = f'reports/ccrd-ocr-reset/{reset_id}/withdrawal.json'
    ccrd_raw = store.read_bytes(CCRD, ccrd_path)
    if reset.decode(ccrd_raw).get('status') != 'withdrawn':
        raise ValueError('OCR library withdrawal is not complete')
    try:
        store.read_bytes(CCRD, 'control/ocr-current.json')
    except FileNotFoundError:
        pass
    else:
        raise ValueError('OCR library is still published')
    pinned = {(reset.ASSETS, reset.RECEIPT): receipt_raw, (CCRD, ccrd_path): ccrd_raw}
    raw = store.read_bytes(reset.ASSETS, reset.SIDECAR)
    if reset.digest(raw) != receipt['sidecar_sha256']:
        raise ValueError('canonical sidecar changed after withdrawal')
    pinned[reset.ASSETS, reset.SIDECAR] = raw
    sidecar = reset.decode(raw)
    pointer, catalog = publication.current(store)
    if (not pointer or pointer['generation'] != receipt['generation'] or catalog['files']
            or catalog.get('document_reset') != reset_id
            or publication.pinned_base(store, catalog, {}) != sidecar):
        raise ValueError('v3 document withdrawal changed')
    pinned[reset.ASSETS, publication.POINTER] = store.read_bytes(reset.ASSETS, publication.POINTER)
    for name in reset.REGISTRIES:
        path = 'reader-index/' + name
        raw = store.read_bytes(reset.ASSETS, path)
        if reset.decode(raw).get('files') != {}:
            raise ValueError('document registry was repopulated: ' + name)
        pinned[reset.ASSETS, path] = raw
    derived = store.read_bytes(reset.PAGES, reset.DERIVED)
    if any(reset.is_djvu(entry) for entry in reset.decode(derived)['files']):
        raise ValueError('DJVU derived registry was repopulated')
    pinned[reset.PAGES, reset.DERIVED] = derived
    for surface, url in [('hf', 'https://voiceofml-search.hf.space/api/reader-assets'),
                         ('pages', 'https://vomebook.github.io/search/data/reader_assets.json.gz')]:
        response = client.get(url, headers={'Cache-Control': 'no-cache'})
        response.raise_for_status()
        if reset.decode(response.content).get('f') != sidecar['f']:
            raise ValueError(surface + ' has not withdrawn the document routes')
    response = client.get('https://vomebook-search.hf.space/api/index-status',
                          headers={'Cache-Control': 'no-cache'})
    response.raise_for_status()
    status = response.json()
    if (not status.get('ready') or status.get('ocr_release') is not None
            or not {'CCRD', 'CW', 'RMRB', 'PLA'} <= set(status.get('loaded_sources', []))):
        raise ValueError('live OCR library is not withdrawn')
    control_paths = store.list_files(CCRD, ('control/',)) | {'current.json'}
    def read_control(path):
        raw = store.read_bytes(CCRD, path)
        for ref in ccrd_lifecycle.references(reset.decode(raw), path):
            if (ref['bucket'] in {reset.PAGES, reset.INPUTS}
                    or ref['bucket'] == CCRD and ref['path'].startswith(ccrd_lifecycle.MANAGED_PREFIXES)):
                raise ValueError('OCR retirement still has a live control reference: ' + path)
        return path, raw
    with ThreadPoolExecutor(max_workers=16) as pool:
        for path, raw in pool.map(read_control, sorted(control_paths)):
            pinned[CCRD, path] = raw
    return receipt, pinned, pointer, catalog


def djvu_targets(receipt):
    targets = set()
    for entry in receipt['djvu_derivatives']:
        if not reset.is_djvu(entry):
            raise ValueError('non-DJVU source in retirement receipt')
        for field in ('new_path', 'manifest_path'):
            path = entry.get(field)
            if not isinstance(path, str) or not re.fullmatch(
                    r'derived/[A-Za-z0-9._-]+/[0-9a-f]{32}/(?:document\.pdf|derived-manifest\.json)', path):
                raise ValueError('invalid DJVU derivative path')
            targets.add(path)
    return targets


def classify(scope, bucket, path, receipt):
    if scope == 'ocr-inputs':
        return bucket == reset.INPUTS and bool(reset.INPUT.fullmatch(path))
    if scope == 'document-pages':
        return bucket == reset.PAGES and bool(reset.PAGE_OUTPUT.fullmatch(path))
    if scope == 'djvu-pdf':
        return bucket == reset.PAGES and path in djvu_targets(receipt)
    if scope == 'ocr-library':
        return bucket == CCRD and bool(re.fullmatch(
            r'(?:indexes/ocr/[0-9a-f]{64}/OCR\.sqlite3|releases/ocr-[0-9a-f]{64}/manifest\.json|'
            r'text/objects/[0-9a-f]{2}/[0-9a-f]{64}\.txt|'
            r'text/exports/[0-9a-f]{64}/[0-9a-f]{64}/manifest\.json)', path))
    return False


def processing_record_target(value):
    if value.get('kind') != 'reader-processing-roots':
        raise ValueError('unknown processing root')
    resources = value.get('resources')
    if not isinstance(resources, list) or not resources:
        raise ValueError('processing root lacks resources')
    # Do not retire mixed/foreign producer roots based on a generic UUID name.
    return all(ref.get('bucket') in {reset.PAGES, reset.INPUTS}
               and re.fullmatch(r'objects/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{16}', ref.get('root', ''))
               for ref in resources)


def control_targets(store, receipt, pointer, catalog, workspace):
    files = store.list_files(reset.ASSETS, ('reader-index/',))
    keep = CONTROL_KEEP | {pointer['catalog']['path'],
        f"reader-index/v3/promotions/{pointer['generation']}.json",
        catalog['base_sidecar']['path'],
        f"reader-index/generations/{receipt['base_generation']}/reader_assets.json.gz"}
    # Current publication may compact the base catalog, but the current snapshot
    # remains exact and only prior snapshots are selected here.
    prefixes = ('reader-index/v3/', 'reader-index/pdf_ocr_progress/',
                'reader-index/generations/', 'reader-index/snapshots/')
    targets = {path for path in files if path.startswith(prefixes) and path not in keep}
    records = sorted(path for path in files if path.startswith('reader-index/processing/'))
    audit = workspace / 'processing-retirement.jsonl.gz'
    with gzip.open(audit, 'wt', encoding='utf-8') as output, ThreadPoolExecutor(max_workers=64) as pool:
        def read(path):
            raw = store.read_bytes(reset.ASSETS, path)
            value = reset.decode(raw)
            return path, raw, value, processing_record_target(value)
        for path, raw, value, selected in pool.map(read, records):
            if selected:
                targets.add(path)
                output.write(json.dumps({'path': path, 'sha256': reset.digest(raw),
                    'bytes': len(raw), 'operation': value.get('operation'), 'run': value.get('run'),
                    'status': value.get('status'), 'resources': value['resources']}, ensure_ascii=False) + '\n')
    return files, targets, audit


def inventory_scope(store, scope, receipt, pointer, catalog, workspace):
    if scope == 'document-control':
        files, targets, audit = control_targets(store, receipt, pointer, catalog, workspace)
        return reset.ASSETS, ('reader-index/',), files, targets, audit
    bucket, prefixes = {
        'ocr-inputs': (reset.INPUTS, ('objects/',)),
        'document-pages': (reset.PAGES, ('objects/',)),
        'djvu-pdf': (reset.PAGES, ('derived/',)),
        'ocr-library': (CCRD, ('text/objects/', 'text/exports/', 'indexes/ocr/', 'releases/ocr-')),
    }[scope]
    files = store.list_files(bucket, prefixes)
    if scope == 'djvu-pdf':
        targets = files & djvu_targets(receipt)
    else:
        targets = {path for path in files if classify(scope, bucket, path, receipt)}
    if scope in {'ocr-inputs', 'ocr-library'} and files != targets:
        raise ValueError('unknown object in exclusively managed scope: ' + sorted(files - targets)[0])
    return bucket, prefixes, files, targets, None


def collect(store, bucket, prefixes, files, targets, *, workers, journal, barrier, checkpoint):
    """Bounded batched deletion with durable receipts and one exact final listing."""
    paths = sorted(targets)
    chunks = [paths[i:i + 500] for i in range(0, len(paths), 500)]
    accepted = set(journal.get('accepted_batches', []))
    lock = threading.Lock()
    last_barrier = [time.monotonic()]
    def execute(index):
        batch = chunks[index]
        identity = reset.digest(reset.encode([bucket, batch]))
        if identity in accepted:
            return
        with lock:
            if time.monotonic() - last_barrier[0] > 60:
                barrier()
                last_barrier[0] = time.monotonic()
        store.delete(bucket, batch)
        with lock:
            accepted.add(identity)
            journal['accepted_batches'] = sorted(accepted)
            journal['accepted_objects'] = journal.get('accepted_objects', 0) + len(batch)
            checkpoint(journal)
    barrier()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(execute, i) for i in range(len(chunks))]):
            future.result()
    barrier()
    remaining = store.list_files(bucket, prefixes)
    if remaining & targets:
        raise RuntimeError('deletion readback still contains selected objects')
    if not (files - targets) <= remaining:
        raise RuntimeError('an object outside the selected scope disappeared')
    journal.update(complete=True, confirmed_deleted_objects=len(targets),
                   confirmed_deleted_bytes=sum(store.object_sizes.get((bucket, p), 0) for p in targets),
                   retained_objects=len(files - targets), remaining_objects=len(remaining))
    checkpoint(journal)
    return journal


def main():
    import httpx
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=SCOPES, required=True)
    parser.add_argument('--reset-id', required=True)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.workers <= 16 or not re.fullmatch(r'[a-z0-9-]{8,80}', args.reset_id):
        parser.error('invalid reset ID or workers')
    if args.apply:
        assert_collector_context()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    store = Store()
    fence = reset.DocumentProducerFence()
    fence.check()
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        receipt, pins, pointer, catalog = preflight(store, args.reset_id, client)
    bucket, prefixes, files, targets, audit = inventory_scope(
        store, args.scope, receipt, pointer, catalog, args.output.parent)
    report = {'version': 1, 'kind': 'reader-document-reset-collection', 'scope': args.scope,
              'reset_id': args.reset_id, 'bucket': bucket, 'planned_objects': len(targets),
              'planned_bytes': sum(store.object_sizes.get((bucket, p), 0) for p in targets),
              'plan_sha256': reset.digest(reset.encode(sorted(targets))), 'applied': args.apply,
              'complete': False, 'accepted_batches': [], 'accepted_objects': 0}
    plan_path = args.output.with_name(args.scope + '-targets.json.gz')
    plan_path.write_bytes(gzip.compress(reset.encode(sorted(targets)), mtime=0))
    prefix = f'reader-index/gc/document-reset/{args.reset_id}/{args.scope}'
    last_save = [0.0]
    def checkpoint(value):
        temporary = args.output.with_suffix('.pending')
        temporary.write_bytes(reset.encode(value))
        temporary.replace(args.output)
        if args.apply and (time.monotonic() - last_save[0] > 20 or value.get('complete')):
            store.put_bytes(reset.ASSETS, prefix + '/collection.json', reset.encode(value))
            last_save[0] = time.monotonic()
            print(json.dumps({k: v for k, v in value.items() if k != 'accepted_batches'}), flush=True)
    checkpoint(report)
    if not args.apply:
        return
    store.put_bytes(reset.ASSETS, prefix + '/targets.json.gz', plan_path.read_bytes())
    if audit:
        store.put_bytes(reset.ASSETS, prefix + '/processing-retirement.jsonl.gz', audit.read_bytes())
    def barrier():
        fence.check()
        def check(item):
            (b, p), expected = item
            if store.read_bytes(b, p) != expected:
                raise RuntimeError('retirement control changed before deletion: ' + p)
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(check, pins.items()))
        expected_controls = {p for b, p in pins if b == CCRD and p.startswith('control/')}
        if store.list_files(CCRD, ('control/',)) != expected_controls:
            raise RuntimeError('OCR control inventory changed before deletion')
    collect(store, bucket, prefixes, files, targets, workers=args.workers, journal=report,
            barrier=barrier, checkpoint=checkpoint)


if __name__ == '__main__':
    main()
