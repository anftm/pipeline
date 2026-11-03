#!/usr/bin/env python3
"""Publish a GC release marker only after the fixed CHM handoff is restored.

This does not run GC or delete an object. A fresh four-bucket graph and
consumer check are required before the marker is written; the normal collector
must still repeat its own graph and producer isolation before any deletion.
"""
import json
import os
from pathlib import Path

import boto3
import httpx
from botocore.config import Config

try:
    from . import chm_gc_terminal_guard as guard, chm_subset_journal as model
    from . import reader_gc_collect as collector, reader_gc_graph as graph, reader_gc_maintenance as maintenance
    from .reader_gc_fence import GitHubProducerFence
except ImportError:
    import chm_gc_terminal_guard as guard
    import chm_subset_journal as model
    import reader_gc_collect as collector
    import reader_gc_graph as graph
    import reader_gc_maintenance as maintenance
    from reader_gc_fence import GitHubProducerFence

WORKFLOW_REF = 'anftm/pipeline/.github/workflows/publish-chm-gc-release.yml@refs/heads/main'
OUTPUT = Path('output/chm-gc-release/result.json')


class Unknown(RuntimeError):
    pass


class Writer:
    def __init__(self):
        access, secret = os.environ.get('HF_S3_ACCESS_KEY_ID'), os.environ.get('HF_S3_SECRET_ACCESS_KEY')
        if not access or not secret:
            raise RuntimeError('CHM GC release credentials unavailable')
        self.client = boto3.client('s3', endpoint_url='https://s3.hf.co/vomebook',
                                   aws_access_key_id=access, aws_secret_access_key=secret,
                                   config=Config(region_name='us-east-1', s3={'addressing_style': 'path'},
                                                 retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=30,
                                                 request_checksum_calculation='when_required',
                                                 response_checksum_validation='when_required'))
    def create(self, raw):
        try:
            response = self.client.put_object(Bucket='reader-assets-v2', Key=guard.PREFIX + 'gc-release.json',
                                              Body=raw, ContentType='application/json', IfNoneMatch='*')
        except Exception as error:
            raise Unknown('GC release marker PUT outcome unknown; inspect before retry') from error
        meta = response.get('ResponseMetadata', {})
        if meta.get('HTTPStatusCode') not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
            raise Unknown('GC release marker response ambiguous')


def verify_original_plan(store):
    source = getattr(store, 'reader', store)
    digest = getattr(source, 'digest', None)
    actual = digest(guard.ASSETS, guard.original.PLAN) if callable(digest) else \
             guard.sha(store.read_bytes(guard.ASSETS, guard.original.PLAN))
    if actual != guard.preflight.PLAN_SHA:
        raise ValueError('original CHM plan bytes differ')


def require_complete_graph_stability(store, traversal):
    """Recheck nested document versions and every root location before release.

    A partial inventory is not a snapshot. These strong validators narrow, but
    do not eliminate, external-writer/ABA races.
    """
    versions = guard.preflight.safety.capture_graph_versions(store, traversal)
    guard.preflight.safety.recheck_full_root_inventory(store, traversal)
    guard.preflight.safety.recheck_graph_versions(store, versions)
    collector.verify_roots(store, traversal)
    return True


def select_history(store, current_raw, existing):
    current = guard.current_state(current_raw)
    if existing is not None:
        marker = guard.parse(existing, 'CHM GC release marker')
        digest = marker.get('maintenance_sha256')
        import re
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('existing GC release marker history is invalid')
        raw = store.read_bytes(guard.ASSETS, maintenance.DURABLE_HISTORY_PREFIX + digest + '.json')
        if guard.sha(raw) != digest:
            raise ValueError('existing GC release history differs')
        return raw, guard.restored_state(raw)
    if current['maintenance_id'] == guard.preflight.OWNER:
        return current_raw, guard.restored_state(current_raw)
    history_paths = store.list_files(guard.ASSETS, (maintenance.DURABLE_HISTORY_PREFIX,))
    matches = []
    for candidate in sorted(history_paths):
        data = store.read_bytes(guard.ASSETS, candidate)
        value = guard.parse(data, 'maintenance history')
        if value.get('maintenance_id') != guard.preflight.OWNER or value.get('restored') is not True:
            continue
        value = guard.restored_state(data)
        if candidate != maintenance.DURABLE_HISTORY_PREFIX + guard.sha(data) + '.json':
            raise ValueError('restored CHM history is not content-addressed')
        matches.append((data, value))
    if not matches or len({item[1]['terminal_handoff']['sha256'] for item in matches}) != 1:
        raise ValueError('restored CHM maintenance archive is missing or ambiguous')
    # Restoring can archive multiple byte-distinct terminal snapshots, such as
    # a final dispatch annotation. Choose reproducibly; retries reuse marker.
    return min(matches, key=lambda item: guard.sha(item[0]))


def validate_graph(traversal, proof, rows):
    if (set(traversal.files) != set(graph.BUCKETS) or proof.get('graph_complete') is not True
            or proof.get('blockers') != [] or proof.get('ambiguous_paths') != [] or proof.get('missing') != []
            or set(proof.get('buckets', {})) != set(graph.BUCKETS)):
        raise ValueError('fresh four-bucket GC reference graph is not complete')
    targets = {(row['bucket'], row['path']) for row in rows}
    if len(rows) != 972 or len(targets) != 972:
        raise ValueError('CHM GC graph target scope differs')
    if any(path in traversal.files[bucket] or (bucket, path) in traversal.references
           or any(bucket == owner and (path == root or path.startswith(root + '/'))
                  for owner, root in traversal.trees) for bucket, path in targets):
        raise ValueError('CHM target exists or remains protected by a live reference')


def require_gc_window(raw, expected_id):
    state = guard.current_state(raw)
    if (not isinstance(expected_id, str) or state['maintenance_id'] != expected_id
            or expected_id == guard.preflight.OWNER or state.get('restored') is True):
        raise ValueError('GC release does not own the isolated maintenance window')
    return state


def verify_retention_graph(store, traversal):
    """The exact CHM chapter protection must actually be a live graph root."""
    ref = guard.ASSETS, guard.preflight.RETENTION
    raw = store.read_bytes(*ref)
    value = guard.parse(raw, 'CHM chapter retention')
    resources = value.get('resources')
    if (guard.sha(raw) != guard.preflight.RETENTION_SHA or not isinstance(resources, list) or not resources
            or ref in traversal.retired_roots or ref not in traversal.references
            or traversal.root_hashes.get(ref) != guard.preflight.RETENTION_SHA
            or traversal.loaded_digests.get(ref) != guard.preflight.RETENTION_SHA):
        raise ValueError('original CHM chapter retention is not an active graph root')
    direct = traversal.direct.get(ref, set())
    for item in resources:
        if (not isinstance(item, dict) or not isinstance(item.get('bucket'), str)
                or not isinstance(item.get('path'), str) or (item['bucket'], item['path']) not in direct
                or (item['bucket'], item['path']) not in traversal.references):
            raise ValueError('CHM retained chapter manifest is not protected by the graph')


def publish(store, writer, fence, traversal, proof, catalog, consumer, rows, *, window_id, client):
    current_raw = store.read_bytes(guard.ASSETS, maintenance.DURABLE_JOURNAL)
    require_gc_window(current_raw, window_id)
    path = guard.PREFIX + 'gc-release.json'
    try:
        existing = store.read_bytes(guard.ASSETS, path)
    except FileNotFoundError:
        existing = None
    raw, state = select_history(store, current_raw, existing)
    digest = guard.sha(raw)
    history_path = maintenance.DURABLE_HISTORY_PREFIX + digest + '.json'
    if store.read_bytes(guard.ASSETS, history_path) != raw:
        raise ValueError('restored maintenance archive is missing or changed')
    validate_graph(traversal, proof, rows)
    verify_retention_graph(store, traversal)
    marker = {'kind': 'chm-gc-release', 'version': 1, 'maintenance_sha256': digest,
              'handoff_sha256': state['terminal_handoff']['sha256']}
    marker_raw = model.encode(marker)
    if existing is not None and existing != marker_raw:
        raise ValueError('CHM GC release marker conflicts')
    fence.check()
    # Full chain verification must happen BEFORE the marker becomes visible to
    # any ordinary collector. A failed post-write check is not a safety gate.
    verify_original_plan(store)
    guard.verify(store, candidate_marker=marker_raw)
    collector.verify_roots(store.reader, traversal)
    latest_catalog, latest_consumer = collector.observe_projections(store.reader, client)
    def stable_receipts(value):
        return {name: {key: item for key, item in receipt.items() if key != 'checked_at'}
                for name, receipt in value.get('receipts', {}).items()}
    if (latest_catalog != catalog or latest_consumer.get('resources') != consumer.get('resources')
            or stable_receipts(latest_consumer) != stable_receipts(consumer)):
        raise ValueError('consumer projections changed before GC release')
    # Root hashes alone do not cover an in-place change to a nested manifest.
    # Bind every parsed document to its GET-time and current validator; detect
    # roots created in previously unseen directories via a full root listing.
    # ETags are validators, not locks; uncooperative external writers and ABA
    # remain outside the scope of these checks.
    require_complete_graph_stability(store.reader, traversal)
    fence.check()
    if store.read_bytes(guard.ASSETS, maintenance.DURABLE_JOURNAL) != current_raw:
        raise ValueError('maintenance changed before GC release')
    if existing is None:
        writer.create(marker_raw)
    if store.read_bytes(guard.ASSETS, path) != marker_raw:
        raise Unknown('CHM GC release marker readback differs')
    guard.verify(store)
    return {'kind': 'chm-gc-release-publication', 'marker_path': path, 'marker_sha256': guard.sha(marker_raw),
            'maintenance_sha256': digest, 'handoff_sha256': marker['handoff_sha256'],
            'graph_complete': True, 'catalog': catalog, 'consumer': consumer,
            'deleted_objects': 0, 'gc_executed': False,
            'caveats': ['marker alone never authorizes a GC deletion',
                        'external S3/Hub writers are outside Actions concurrency groups']}


def main(window):
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF or not os.environ.get('GITHUB_RUN_ID', '').isdigit()):
        raise RuntimeError('CHM GC release requires its central workflow')
    guard.preflight.ensure_read_credentials()
    if OUTPUT.exists():
        raise FileExistsError('CHM GC release output exists')
    store = guard.preflight.ReadOnlyStore()
    window_id = window.get('maintenance_id') if isinstance(window, dict) else None
    require_gc_window(store.read_bytes(guard.ASSETS, maintenance.DURABLE_JOURNAL), window_id)
    rows = guard.preflight.chm.preview.load_inventory(guard.preflight.INVENTORY)['targets']
    fence = GitHubProducerFence(api=guard.preflight.github_get)
    # Caller runs this inside maintenance.run's separately drained producer
    # window after the original terminal handoff has been restored.
    fence.check()
    import time
    traversal = graph.ReferenceGraph(store.reader, workers=8, max_rounds=40, deadline=time.monotonic() + 320 * 60)
    proof = traversal.build()
    collector.verify_roots(store.reader, traversal)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        catalog, consumer = collector.observe_projections(store.reader, client)
        fence.check()
        require_gc_window(store.read_bytes(guard.ASSETS, maintenance.DURABLE_JOURNAL), window_id)
        result = publish(store, Writer(), fence, traversal, proof, catalog, consumer, rows,
                         window_id=window_id, client=client)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open('x', encoding='utf-8') as file:
        json.dump(result, file, ensure_ascii=False, sort_keys=True, indent=2)
        file.write('\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


def isolated_main():
    """Start a new, recoverable maintenance window around the publication."""
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF or not os.environ.get('GITHUB_RUN_ID', '').isdigit()
            or os.environ.get('READER_GC_DURABLE_MAINTENANCE') != 'true'
            or not os.environ.get('GC_PRODUCER_TOKENS_JSON')):
        raise RuntimeError('CHM GC publication requires durable central maintenance and write credentials')
    # Never let a new window's automatic recovery restore the interrupted CHM
    # callback. Check its archived terminal bridge before maintenance.run()
    # can change any workflow state or replace the durable cursor.
    store = guard.preflight.ReadOnlyStore()
    guard.assert_bucket_available(store)
    current_raw = store.read_bytes(guard.ASSETS, maintenance.DURABLE_JOURNAL)
    try:
        existing = store.read_bytes(guard.ASSETS, guard.PREFIX + 'gc-release.json')
    except FileNotFoundError:
        existing = None
    raw, state = select_history(store, current_raw, existing)
    candidate = model.encode({'kind': 'chm-gc-release', 'version': 1,
                              'maintenance_sha256': guard.sha(raw),
                              'handoff_sha256': state['terminal_handoff']['sha256']})
    guard.verify(store, candidate_marker=candidate)
    guard.verify_retention(store)
    # Read-only credential/scope preflight before recovery or producer changes.
    # No writer in a formerly active terminal snapshot may be silently restored.
    inventory = maintenance.preflight()
    expected = guard.filter_restore_scope(guard.parse(
        store.read_bytes(guard.ASSETS, guard.PREFIX + 'original-maintenance.json'), 'original CHM maintenance'))
    for repo, before in expected['repositories'].items():
        current = inventory.get(repo)
        if not isinstance(current, dict) or not isinstance(current.get('workflows'), list):
            raise RuntimeError('GC release producer inventory is incomplete: ' + repo)
        by_path = {entry.get('path'): entry for entry in current['workflows']}
        if len(by_path) != len(current['workflows']):
            raise RuntimeError('GC release producer workflow inventory is ambiguous: ' + repo)
        retired = [item['path'] for item in before['workflows'] if item.get('retired_from_restore')]
        if any(path not in by_path or by_path[path].get('state') == 'active' for path in retired):
            raise RuntimeError('retired CHM-window producer is active or unverified: ' + repo)
        if (before.get('retired_execution_remains_disabled')
                and current.get('permission', {}).get('enabled') is not False):
            raise RuntimeError('retired CHM-window Actions policy is active or unverified: ' + repo)
    if store.read_bytes(guard.ASSETS, maintenance.DURABLE_JOURNAL) != current_raw:
        raise RuntimeError('maintenance changed before isolated GC release')
    maintenance.run(Path('output/chm-gc-release/maintenance.json'),
                    isolated_callback=main, suppress_resume_dispatch=True,
                    preserve_queued_runs=True)


if __name__ == '__main__':
    isolated_main()
