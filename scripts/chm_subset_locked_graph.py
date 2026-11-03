#!/usr/bin/env python3
"""Read-only four-bucket subset graph diagnostic under three Actions groups.

The 20-target protection root is an in-memory overlay, never published. This
program has no delete or write API. It compares current roots and graph closure
with the original saved CHM graph, checks both Reader sites and preserves the
old journal. It does not fence non-Actions writers or authorize production.
"""
import json
import os
from pathlib import Path
import time

import httpx

try:
    from . import chm_epub_journal as journal, chm_subset_isolated_root as isolated
    from . import chm_subset_locked_preflight as preflight, chm_subset_protection_overlay as overlay
    from . import reader_gc_collect as collector, reader_gc_graph as graph
    from .reader_gc_fence import GitHubProducerFence
except ImportError:
    import chm_epub_journal as journal
    import chm_subset_isolated_root as isolated
    import chm_subset_locked_preflight as preflight
    import chm_subset_protection_overlay as overlay
    import reader_gc_collect as collector
    import reader_gc_graph as graph
    from reader_gc_fence import GitHubProducerFence

WORKFLOW_REF = 'anftm/pipeline/.github/workflows/chm-subset-locked-graph.yml@refs/heads/main'
OUTPUT = Path('output/chm-subset-locked-graph/result.json')
IDENTITY = {'classification_sha256': preflight.CLASSIFICATION_SHA,
            'inventory_sha256': 'dec3b852cfe46c4c534c105d80b8eba8a57468b2e4550925a714d53d980a6e69',
            'maintenance_id': preflight.OWNER, 'old_plan_sha256': preflight.PLAN_SHA,
            'old_progress_sha256': preflight.PROGRESS_SHA,
            'old_receipts_sha256': '42f176a8eb6a04a5f4e6343f955004557533d4883dc7b31bbde1efc09e6bd6a4',
            'transaction_id': '3c697c95412476f22f53b49bd8a6e966'}
ROOT_SHA = 'd1856b5418e7cf0d241eebf3556c0dfb91eab265bd3058ca963e015f7f6c2ef9'


def build(store, fence, client, rows, classification_raw, *, deadline_minutes=320):
    """Observe a fixed synthetic root in a fresh four-bucket graph, without writes."""
    if (type(deadline_minutes) is not int or not 1 <= deadline_minutes <= 320
            or preflight.digest(classification_raw) != preflight.CLASSIFICATION_SHA):
        raise ValueError('invalid locked graph duration or classification bytes')
    classification = json.loads(classification_raw)
    indexed = {row['path']: row for row in rows}
    selected = sorted((indexed[item['path']] for item in classification['targets']
                       if item['category'] == 'mapped-with-saved-evidence'), key=lambda row: row['path'])
    withheld = sorted((indexed[item['path']] for item in classification['targets']
                       if item['category'] in {'needs-independent-review', 'withheld-oversize'}),
                      key=lambda row: row['path'])
    progress_raw, maintenance_raw, retention_raw, progress = preflight._identity(store)
    partition = preflight.subset.verify_exact_partition(rows, progress['batches'], selected, withheld)
    if (partition['old_receipts'], partition['selected'], partition['withheld']) != (600, 352, 20):
        raise ValueError('locked graph exact partition differs')
    plan, _, updates, digest = journal.load_plan(store, rows)
    if digest != preflight.PLAN_SHA or plan['overrides'][preflight.RETENTION] != retention_raw:
        raise ValueError('locked graph original plan differs')
    candidate = isolated.build(withheld, IDENTITY)
    if candidate['objects'] != 20 or candidate['sha256'] != ROOT_SHA:
        raise ValueError('locked graph fixed synthetic protection differs')
    fence_before = fence.check()
    preflight.safety.verify_live_chm_controls(store, plan)
    preflight.safety.verify_live_processing_controls(store, updates)
    preflight.safety.verify_retired_rollback_aliases(store, plan)
    sites_before = preflight.safety.observe_planned_projection(store, client, plan)
    # Underlying reader is used only by an adapter with read/list/digest methods.
    # No graph operation receives its put or delete methods.
    view, candidate = overlay.view(store.reader, withheld, IDENTITY)
    traversal = graph.ReferenceGraph(view, workers=8, max_rounds=40,
                                     deadline=time.monotonic() + deadline_minutes * 60)
    report = traversal.build()
    expected_absent = {(batch['bucket'], path) for batch in progress['batches'] for path in batch['paths']}
    proof = overlay.capture(traversal, report, rows, selected, withheld,
                            candidate=candidate, expected_absent=expected_absent)
    fence.check()
    collector.verify_roots(view, traversal)
    sites_after = preflight.safety.observe_planned_projection(store, client, plan)
    for site in ('hf', 'pages'):
        for field in ('surface', 'generation', 'catalog_sha256'):
            if sites_before[site].get(field) != sites_after[site].get(field):
                raise ValueError('Reader projection changed during locked graph: ' + site)
    collector.verify_roots(view, traversal)
    fence_after = fence.check()
    proof['root_recheck_complete'] = True
    proof['reader_acceptance_complete'] = True
    comparison = isolated.compare(progress['actual_graph'], proof, isolated_path=candidate['path'],
                                  isolated_raw=candidate['raw'], withheld=withheld, identity=IDENTITY)
    end = preflight._identity(store)
    if end[:3] != (progress_raw, maintenance_raw, retention_raw):
        raise ValueError('original CHM controls changed during locked graph')
    if candidate['path'] in store.list_files(preflight.chm.ASSETS, (candidate['path'],)):
        raise ValueError('hypothetical protection root appeared in production')
    store.assert_buckets_available()
    return {'kind': 'locked-read-only-chm-independent-root-graph', 'partition': partition,
            'candidate_path': candidate['path'], 'candidate_sha256': candidate['sha256'],
            'candidate_objects': candidate['objects'], 'hypothetical_graph_proof': proof,
            'comparison': comparison, 'fence_before': fence_before, 'fence_after': fence_after,
            'sites_before': sites_before, 'sites_after': sites_after,
            'synthetic_root_published': False, 'remote_mutations': 0, 'deletion_authorized': False,
            'producer_restore_authorized': False,
            'caveats': ['synthetic root exists only in memory and does not protect production objects',
                        'Actions groups do not fence other Hub/S3 clients or prove storage ownership',
                        'not an authorization to create a root, delete, or restore producers']}


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF
            or not os.environ.get('GITHUB_RUN_ID', '').isdigit()):
        raise RuntimeError('CHM locked graph requires its central main workflow context')
    preflight.ensure_read_credentials()
    if OUTPUT.exists():
        raise FileExistsError('locked graph output already exists')
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    rows = preflight.chm.preview.load_inventory(preflight.INVENTORY)['targets']
    store, fence = preflight.ReadOnlyStore(), GitHubProducerFence(api=preflight.github_get)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        payload = build(store, fence, client, rows, preflight.CLASSIFICATION.read_bytes())
    with OUTPUT.open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps({'output': str(OUTPUT), 'sha256': preflight.digest(OUTPUT.read_bytes()),
                      'candidate_objects': payload['candidate_objects'], 'remote_mutations': 0,
                      'deletion_authorized': False}))


if __name__ == '__main__':
    main()
