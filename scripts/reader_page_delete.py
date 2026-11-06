#!/usr/bin/env python3
"""Execute a reviewed exact page-bucket deletion plan in a fenced maintenance window.

This CLI has two phases. `plan` reads a freshly converged four-bucket graph and
current deployed projections under the existing collector fence, then writes an
exact immutable plan and digest without deleting. `apply` requires that digest,
a plan at most 24 hours old, no producers, a freshly built complete graph,
matching deployed projections, unchanged object versions and an additional
external-writer confirmation. Plan age is never treated as fresh reference proof.

A Hub batch is non-atomic and outcomes may be unknown. Never blindly retry: the
operator must re-plan after an interruption. Historical references and objects
outside the exact supplied paths are retained.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

try:
    from . import reader_gc_graph as graph, reader_gc_collect as collector, reader_gc_chm_window, shared
    from .reader_bucket_store import HubBucketStore
    from .reader_gc_fence import GitHubProducerFence, assert_workflow_context
except ImportError:
    import reader_gc_graph as graph
    import reader_gc_collect as collector
    import reader_gc_chm_window
    import shared
    from reader_bucket_store import HubBucketStore
    from reader_gc_fence import GitHubProducerFence, assert_workflow_context


BUCKET = shared.PDF_PAGES_BUCKET
WORKFLOW = '.github/workflows/reader-gc-collect.yml'
MAX_TARGETS = 1000
# Review identity may remain valid while a full graph is rebuilt. A fresh graph,
# consumer checks, exact versions and writer fence remain mandatory at apply.
MAX_PLAN_AGE_SECONDS = 24 * 60 * 60


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def plan_id(plan):
    return hashlib.sha256(encode(plan)).hexdigest()


def validate_paths(paths):
    if (not isinstance(paths, list) or not 1 <= len(paths) <= MAX_TARGETS
            or any(not isinstance(path, str) or not path.startswith(('derived/', 'objects/'))
                   or '\\' in path or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
                   or any(part in {'', '.', '..'} for part in path.split('/')) for path in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError('supply 1..1000 unique exact derived/ or objects/ paths')
    return sorted(paths)


def candidate_plan(reference, graph_report, api, paths, *, clock=None):
    selected = validate_paths(paths)
    if (not isinstance(graph_report, dict) or graph_report.get('graph_complete') is not True
            or graph_report.get('blockers') or set(reference.files) != set(graph.BUCKETS)):
        raise RuntimeError('a complete current four-bucket graph is required')
    page_data = graph_report.get('buckets', {}).get(BUCKET, {})
    allowed = set(page_data.get('candidates', []))
    for path in selected:
        if (path not in allowed or path not in reference.files.get(BUCKET, set())
                or (BUCKET, path) in reference.references):
            raise ValueError('requested path is not an unreferenced exact graph candidate: ' + path)
    info = {item.path: item for item in api.get_bucket_paths_info(BUCKET, selected)}
    if set(info) != set(selected):
        raise RuntimeError('a candidate disappeared during planning')
    targets = []
    for path in selected:
        item = info[path]
        if type(item.size) is not int or item.size < 0 or not isinstance(item.xet_hash, str) or not item.xet_hash:
            raise ValueError('candidate has no exact object identity')
        targets.append({'path': path, 'bytes': item.size, 'xet_hash': item.xet_hash})
    now = clock or datetime.now(timezone.utc)
    return {'version': 1, 'kind': 'pdf-page-exact-delete-plan', 'bucket': BUCKET,
            'created_at': now.isoformat(), 'targets': targets,
            'total_bytes': sum(item['bytes'] for item in targets), 'deletion_authorized': False}


def check_plan(plan, expected_hash, *, now=None, max_age_seconds=MAX_PLAN_AGE_SECONDS):
    if (not isinstance(expected_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_hash)
            or plan_id(plan) != expected_hash or not isinstance(plan, dict)
            or plan.get('version') != 1 or plan.get('kind') != 'pdf-page-exact-delete-plan'
            or plan.get('bucket') != BUCKET or plan.get('deletion_authorized') is not False
            or not isinstance(plan.get('targets'), list)):
        raise ValueError('plan digest or schema mismatch')
    if (type(plan.get('total_bytes')) is not int or plan['total_bytes'] < 0
            or plan.get('total_bytes') != sum(item.get('bytes', 0) for item in plan['targets']
                                               if isinstance(item, dict) and type(item.get('bytes')) is int)):
        raise ValueError('plan total bytes mismatch')
    try:
        created = datetime.fromisoformat(plan['created_at'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid plan timestamp') from None
    current = now or datetime.now(timezone.utc)
    if created.tzinfo is None or created > current or current - created > timedelta(seconds=max_age_seconds):
        raise ValueError('delete plan expired; create a fresh one')
    paths = validate_paths([item.get('path') if isinstance(item, dict) else None for item in plan['targets']])
    if paths != [item['path'] for item in plan['targets']]:
        raise ValueError('plan targets are not canonical')
    if any(type(item.get('bytes')) is not int or item['bytes'] < 0
           or not isinstance(item.get('xet_hash'), str) or not item['xet_hash'] for item in plan['targets']):
        raise ValueError('plan target identity is invalid')
    return paths


def apply_exact(store, api, reference, fresh_report, plan, expected_hash, *, fence, check_consumers,
                check_chm_window, persist_intent, confirm_external_writers=False):
    """Issue one batch; if response is lost, report unknown and do not retry."""
    paths = check_plan(plan, expected_hash)
    if confirm_external_writers is not True:
        raise ValueError('external writers must be independently stopped')
    fence.check()
    check_chm_window()
    if (fresh_report.get('graph_complete') is not True or fresh_report.get('blockers')
            or set(reference.files) != set(graph.BUCKETS)):
        raise RuntimeError('fresh four-bucket graph is incomplete')
    candidates = set(fresh_report.get('buckets', {}).get(BUCKET, {}).get('candidates', []))
    for path in paths:
        if path not in candidates or path not in reference.files[BUCKET] or (BUCKET, path) in reference.references:
            raise RuntimeError('candidate is newly referenced or absent: ' + path)
    # A page object may be a nested dependency, a review/provenance input, or
    # part of a bundle shared by a referenced sibling. Refuse those cases even
    # if a stale or unexpectedly permissive graph classified the leaf as free.
    for path in paths:
        parent = path.rsplit('/', 1)[0]
        if any(bucket == BUCKET and (ref == parent or ref.startswith(parent + '/'))
               for bucket, ref in reference.references):
            raise RuntimeError('candidate bundle has a referenced sibling: ' + path)
    observed = {item.path: item for item in api.get_bucket_paths_info(BUCKET, paths, token=store.token(BUCKET))}
    for item in plan['targets']:
        current = observed.get(item['path'])
        if current is None or current.xet_hash != item['xet_hash'] or current.size != item['bytes']:
            raise RuntimeError('candidate version changed or disappeared: ' + item['path'])
    check_consumers()
    collector.verify_roots(store, reference)
    collector.verify_graph_documents(store, reference)
    check_chm_window()
    fence.check()
    check_plan(plan, expected_hash)
    # Write an immutable intent before the remote request. Its presence after a
    # failed run means the result is uncertain; always re-scan before retrying.
    persist_intent({'version': 1, 'kind': 'pdf-page-delete-intent', 'bucket': BUCKET,
                    'plan_sha256': expected_hash, 'targets': plan['targets'],
                    'submitted_at': datetime.now(timezone.utc).isoformat()})
    collector.verify_roots(store, reference)
    collector.verify_graph_documents(store, reference)
    check_chm_window()
    fence.check()
    check_plan(plan, expected_hash)
    latest = {item.path: item for item in api.get_bucket_paths_info(BUCKET, paths, token=store.token(BUCKET))}
    for item in plan['targets']:
        observed = latest.get(item['path'])
        if observed is None or observed.xet_hash != item['xet_hash'] or observed.size != item['bytes']:
            raise RuntimeError('candidate version changed before deletion: ' + item['path'])
    # The Hub client retries batch operations on network errors. A blind retry
    # can remove a newly recreated key; send one explicit POST instead.
    from huggingface_hub.constants import ENDPOINT
    from huggingface_hub.utils import build_hf_headers, get_session
    token = os.environ.get('HF_TOKEN')
    if not token:
        raise RuntimeError('HF_TOKEN is required for page deletion')
    body = b''.join(encode({'type': 'deleteFile', 'path': path}) + b'\n' for path in paths)
    response = get_session().post(f'{ENDPOINT}/api/buckets/{BUCKET}/batch',
        headers=build_hf_headers(token=token, headers={'Content-Type': 'application/x-ndjson'}),
        content=body, timeout=60, follow_redirects=False)
    try:
        response.raise_for_status()
        payload = response.json()
        # Hub omits `failed` on a fully successful batch in some versions.
        # Exact post-delete reads below remain the success criterion.
        if not isinstance(payload, dict) or not isinstance(payload.get('failed', []), list) or payload.get('failed'):
            raise RuntimeError('delete outcome may be partial; re-plan before retry')
    finally:
        response.close()
    remaining = list(api.get_bucket_paths_info(BUCKET, paths, token=store.token(BUCKET)))
    if remaining:
        raise RuntimeError('some paths remain or were recreated; re-plan before retry')
    return {'kind': 'pdf-page-exact-delete-result', 'plan_sha256': expected_hash,
            'objects': len(paths), 'bytes': plan['total_bytes'], 'verified_absent': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'apply'))
    parser.add_argument('--paths', type=Path, help='JSON array of exact paths (plan only)')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--expected-sha256', help='reviewed plan digest (apply only)')
    parser.add_argument('--confirm-external-writers-stopped', action='store_true')
    parser.add_argument('--deadline-minutes', type=int, default=0,
                        help='zero disables the scan deadline')
    parser.add_argument('--storage', choices=('s3', 'hub'), default='s3')
    args = parser.parse_args(argv)
    mutation_started = False
    try:
        import httpx
        if args.deadline_minutes < 0:
            raise ValueError('deadline must not be negative')
        if not os.environ.get('HF_TOKEN'):
            raise RuntimeError('HF_TOKEN is required for exact page-bucket metadata and deletion')
        if args.command == 'plan':
            if not args.paths or args.expected_sha256 or args.confirm_external_writers_stopped:
                raise ValueError('plan requires --paths and no apply-only options')
            paths = validate_paths(json.loads(args.paths.read_text(encoding='utf-8')))
            value = None
        else:
            if args.paths or not args.expected_sha256:
                raise ValueError('apply requires --expected-sha256 and no --paths')
            value = json.loads(args.plan.read_text(encoding='utf-8'))
            check_plan(value, args.expected_sha256)
            if not args.confirm_external_writers_stopped:
                raise ValueError('external writers must be independently stopped')
        # Its workflow identity alone does not create a safe window.
        assert_workflow_context(WORKFLOW)
        fence = GitHubProducerFence()
        fence.check()
        if args.storage == 's3':
            try:
                from .reader_gc_store import CollectionStore
            except ImportError:
                from reader_gc_store import CollectionStore
            store = CollectionStore()
            api = store.writer.api
        else:
            store = HubBucketStore()
            api = store.api
        deadline = time.monotonic() + args.deadline_minutes * 60 if args.deadline_minutes else None
        reference = graph.ReferenceGraph(store, workers=8, deadline=deadline)
        report = reference.build()
        if not report['graph_complete']:
            raise RuntimeError('fresh reference graph is incomplete: ' + '; '.join(report['blockers'][:8]))
        reader_gc_chm_window.require_clear(store)
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            def consumers():
                collector.observe_projections(store, client)
            consumers()
            fence.check()
            reader_gc_chm_window.require_clear(store)
            collector.verify_roots(store, reference)
            if args.command == 'plan':
                result = candidate_plan(reference, report, api, paths)
                args.plan.parent.mkdir(parents=True, exist_ok=True)
                args.plan.write_bytes(encode(result) + b'\n')
                print(json.dumps({'plan_sha256': plan_id(result), 'objects': len(result['targets']),
                                  'bytes': result['total_bytes'], 'deletion_authorized': False}))
            else:
                def persist_intent(intent):
                    nonlocal mutation_started
                    path = f"reader-index/gc/page-delete/{args.expected_sha256}/intent.json"
                    raw = encode(intent)
                    try:
                        store.read_bytes(shared.READER_ASSETS_BUCKET, path)
                    except FileNotFoundError:
                        mutation_started = True
                        store.put_bytes(shared.READER_ASSETS_BUCKET, path, raw)
                    else:
                        raise RuntimeError('this deletion intent already exists; re-plan after checking outcomes')
                    if store.read_bytes(shared.READER_ASSETS_BUCKET, path) != raw:
                        raise RuntimeError('delete intent readback failed')
                    mutation_started = True
                result = apply_exact(store, api, reference, report, value, args.expected_sha256,
                    fence=fence, check_consumers=consumers, check_chm_window=lambda: reader_gc_chm_window.require_clear(store),
                    persist_intent=persist_intent, confirm_external_writers=args.confirm_external_writers_stopped)
                result['completed_at'] = datetime.now(timezone.utc).isoformat()
                result_path = f"reader-index/gc/page-delete/{args.expected_sha256}/result.json"
                store.put_bytes(shared.READER_ASSETS_BUCKET, result_path, encode(result))
                if store.read_bytes(shared.READER_ASSETS_BUCKET, result_path) != encode(result):
                    raise RuntimeError('delete result readback failed; inspect targets before retry')
                print(json.dumps(result))
        return 0
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        if mutation_started:
            print('Deletion outcome may be unknown or partial. Inspect exact paths and re-plan; do not blind retry.', file=sys.stderr)
            return 2
        return 1


if __name__ == '__main__':
    sys.exit(main())
