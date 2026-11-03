#!/usr/bin/env python3
"""Locked, exact, independently journaled continuation of 352 CHM EPUBs.

No production resource other than the fixed selected EPUB keys and this
transaction's non-root journal keys can be deleted or written. The 20 withheld
EPUBs stay protected. This never changes the original CHM progress, releases
its maintenance callback, enables producers, or runs generic GC.

Storage conditions are not treated as locks. All cooperating workflows are
serialized by the three Actions concurrency groups. The checks below cannot
fence other Hub/S3 clients or eliminate a reference change after the last read.
"""
import json
import os
from pathlib import Path
import secrets
import time

import httpx

try:
    from . import chm_epub_journal as original, chm_subset_actual_graph as actual
    from . import chm_subset_isolated_root as isolated
    from . import chm_subset_journal as model, chm_subset_journal_backend as chain
    from . import chm_subset_locked_graph as locked, chm_subset_locked_preflight as preflight
    from . import chm_subset_reconcile as reconcile, chm_subset_s3_journal as storage
    from . import chm_subset_delete_engine as engine, reader_gc_collect as collector, reader_gc_graph as graph
    from .reader_gc_fence import GitHubProducerFence
except ImportError:
    import chm_epub_journal as original
    import chm_subset_actual_graph as actual
    import chm_subset_isolated_root as isolated
    import chm_subset_journal as model
    import chm_subset_journal_backend as chain
    import chm_subset_locked_graph as locked
    import chm_subset_locked_preflight as preflight
    import chm_subset_reconcile as reconcile
    import chm_subset_s3_journal as storage
    import chm_subset_delete_engine as engine
    import reader_gc_collect as collector
    import reader_gc_graph as graph
    from reader_gc_fence import GitHubProducerFence

WORKFLOW_REF = 'anftm/pipeline/.github/workflows/chm-subset-execute.yml@refs/heads/main'
OUTPUT = Path('output/chm-subset-execute/result.json')
BATCH_SIZE = 100


def observe(store, rows, old, plan, state):
    def read(bucket, path):
        try:
            return store.read_bytes(bucket, path)
        except FileNotFoundError:
            return None
    return reconcile.reconcile(rows, old, plan['selected'], plan['withheld'], state, read, workers=4)


def live_controls(store, fence, client, plan, updates, root):
    preflight._identity(store)
    if store.read_bytes(preflight.chm.ASSETS, root['path']) != root['raw']:
        raise ValueError('20-item protection root changed')
    fence.check()
    preflight.safety.verify_live_chm_controls(store, plan)
    preflight.safety.verify_live_processing_controls(store, updates)
    preflight.safety.verify_retired_rollback_aliases(store, plan)
    preflight.safety.observe_planned_projection(store, client, plan)


def build_graph(store, fence, client, rows, old, subset_plan, original_plan, updates, progress, state, root):
    """Build a real graph; pending-absent paths are observed, not receipts."""
    live_controls(store, fence, client, original_plan, updates, root)
    original.verify_initial_chapter_evidence(original_plan, updates, progress)
    preflight.verify_selected_manifests(store, rows, subset_plan['selected'], original_plan, progress, updates)
    traversal = graph.ReferenceGraph(store.reader, workers=8, max_rounds=40,
                                     deadline=time.monotonic() + 320 * 60)
    report = traversal.build()
    if (report.get('graph_complete') is not True or report.get('blockers') != []
            or report.get('missing') != [] or report.get('ambiguous_paths') != []
            or set(traversal.files) != set(graph.BUCKETS)):
        raise ValueError('actual four-bucket graph is incomplete')
    expected_absent = {path for receipt in old + state['receipts'] for path in receipt['paths']}
    pending = set((state.get('pending') or {}).get('paths', []))
    missing = {row['path'] for row in rows if row['path'] not in traversal.files[row['bucket']]}
    if not expected_absent <= missing or not missing <= expected_absent | pending:
        raise ValueError('actual graph target presence differs from receipts and pending intent')
    observed = observe(store, rows, old, subset_plan, state)
    absent_now = {item['path'] for item in observed['results'] if item['status'].endswith('-absent')}
    if missing != absent_now:
        raise ValueError('graph target inventory and GET observations differ')
    all_refs = {(row['bucket'], row['path']) for row in rows}
    retained = all_refs & traversal.references
    if retained != {(row['bucket'], row['path']) for row in subset_plan['withheld']}:
        raise ValueError('actual graph does not protect exactly the 20 withheld EPUBs')
    root_ref = (preflight.chm.ASSETS, root['path'])
    if (traversal.root_hashes.get(root_ref) != root['sha256']
            or traversal.loaded_digests.get(root_ref) != root['sha256']):
        raise ValueError('actual graph did not traverse the fixed protection root')
    collector.verify_roots(store.reader, traversal)
    versions = preflight.safety.capture_graph_versions(store.reader, traversal)
    live_controls(store, fence, client, original_plan, updates, root)
    collector.verify_roots(store.reader, traversal)
    preflight.safety.recheck_graph_versions(store.reader, versions)
    if not pending:
        proof = actual.capture(traversal, report, rows, old, subset_plan['selected'], subset_plan['withheld'],
                               root_raw=root['raw'], identity=locked.IDENTITY,
                               subset_receipts=state['receipts'], root_rechecked=True, consumers_checked=True)
        actual.compare_original(progress['actual_graph'], proof, subset_plan['withheld'], locked.IDENTITY, root['raw'])
    else:
        # For recovery compare against the original closure without inventing a
        # receipt for an absent pending object.
        members = {(bucket, path) for bucket, files in traversal.files.items()
                   for path in files if graph.is_root(path, bucket)}
        proof = {'mode': 'hypothetical-overlay', 'graph_complete': True, 'blockers': [], 'ambiguous_paths': [],
                 'retained_target_paths': [{'bucket': bucket, 'path': path} for bucket, path in sorted(retained)],
                 'root_members': [{'bucket': bucket, 'path': path} for bucket, path in sorted(members)],
                 'root_sha256': [{'bucket': b, 'path': p, 'sha256': sha}
                                 for (b, p), sha in sorted(traversal.root_hashes.items())],
                 'closure_sha256': [{'bucket': b, 'path': p, 'sha256': sha}
                                    for (b, p), sha in sorted(traversal.loaded_digests.items())],
                 'retired_roots': [{'bucket': b, 'path': p} for b, p in sorted(traversal.retired_roots)],
                 'root_recheck_complete': True, 'reader_acceptance_complete': True,
                 'remote_mutations': 0, 'deletion_authorized': False}
        isolated.compare(progress['actual_graph'], proof, isolated_path=root['path'], isolated_raw=root['raw'],
                         withheld=subset_plan['withheld'], identity=locked.IDENTITY)
    return traversal, versions


def barrier(store, fence, client, traversal, versions, original_plan, updates, root):
    """Revalidate roots and fresh validators of every read graph document.

    All document bytes were hashed by the fresh graph build; validators are
    checked against fresh listings here. A validator can undergo ABA, so this
    is not a claim that uncooperative external writers are fenced.
    """
    live_controls(store, fence, client, original_plan, updates, root)
    collector.verify_roots(store.reader, traversal)
    preflight.safety.recheck_graph_versions(store.reader, versions)
    # Detect root directories that did not exist at traversal time, not only
    # those in list_roots' optimized parent list.
    preflight.safety.recheck_full_root_inventory(store.reader, traversal)
    live_controls(store, fence, client, original_plan, updates, root)
    collector.verify_roots(store.reader, traversal)
    preflight.safety.recheck_graph_versions(store.reader, versions)


def open_cursor(journal, plan):
    cursor = chain.Cursor(journal, plan)
    names = journal.names()
    if not names:
        cursor.initialize(nonce=storage.INITIAL_NONCE)
    elif names == {'plan.json'}:
        if cursor.reconcile_initialization(nonce=storage.INITIAL_NONCE)['status'] != 'plan-only-unresolved':
            raise chain.Conflict('partial journal initialization differs')
        raise storage.JournalPartialInitialization('journal initialization is plan-only; stop for recovery')
    return cursor


def execute(store, journal, fence, client, rows, subset_plan, progress, original_plan, updates):
    root = isolated.build(subset_plan['withheld'], locked.IDENTITY)
    if root['sha256'] != locked.ROOT_SHA or root['objects'] != 20:
        raise ValueError('fixed protection identity differs')
    # Never initialize a production journal if the real protection is absent.
    live_controls(store, fence, client, original_plan, updates, root)
    old = progress['batches']
    prepared_graph = None
    try:
        cursor = open_cursor(journal, subset_plan)
    except storage.JournalPartialInitialization:
        # The known plan is already present, but no deletion intent exists.
        # Revalidate every target and build a fresh real graph before writing
        # precisely the predetermined first event. Never overwrite either key.
        empty = {'receipts': [], 'pending': None, 'subset_complete': False,
                 'original_complete': False, 'producer_restore_authorized': False}
        prepared_graph = build_graph(store, fence, client, rows, old, subset_plan,
                                     original_plan, updates, progress, empty, root)
        if journal.names() != {'plan.json'} or journal.read('plan.json') != model.encode(subset_plan):
            raise chain.Conflict('partial journal changed while validating recovery')
        first = model.make_envelope(subset_plan, empty, sequence=0, parent_sha256=None,
                                    nonce=storage.INITIAL_NONCE)
        if model.digest(first) != storage.INITIAL_SHA or not journal.create('event-00000000.json', model.encode(first)):
            raise chain.Conflict('cannot recover fixed initial journal event')
        cursor = chain.Cursor(journal, subset_plan)
        if cursor.head() != first:
            raise chain.Conflict('recovered initial journal event differs')
        print(json.dumps({'stage': 'initialization-recovered', 'subset_verified_absent': 0}), flush=True)
    head = cursor.head()
    traversal, versions = prepared_graph or build_graph(store, fence, client, rows, old, subset_plan,
                                                        original_plan, updates, progress, head['state'], root)
    def before_batch():
        barrier(store, fence, client, traversal, versions, original_plan, updates, root)
        observe(store, rows, old, subset_plan, cursor.head()['state'])
    head = engine.run(journal, cursor, rows, old, subset_plan, before_batch,
                      nonce=lambda: secrets.token_hex(16), batch_size=BATCH_SIZE)
    return {'kind': 'chm-subset-execution', 'subset_complete': head['state']['subset_complete'],
            'subset_verified_absent': sum(len(item['paths']) for item in head['state']['receipts']),
            'withheld': len(subset_plan['withheld']), 'journal_head_sha256': model.digest(head),
            'journal_sequence': head['sequence'], 'original_confirmed_deleted': 600,
            'original_complete': False, 'producer_restore_authorized': False,
            'caveats': ['only this 352-object subset is complete',
                        'Actions locks do not fence external Hub/S3 writers',
                        'original maintenance callback and ordinary GC remain unresolved']}


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF
            or not os.environ.get('GITHUB_RUN_ID', '').isdigit()):
        raise RuntimeError('CHM subset execution requires its central main workflow')
    preflight.ensure_read_credentials()
    if OUTPUT.exists():
        raise FileExistsError('subset execution output exists')
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    rows = preflight.chm.preview.load_inventory(preflight.INVENTORY)['targets']
    store, fence = preflight.ReadOnlyStore(), GitHubProducerFence(api=preflight.github_get)
    subset_plan, progress = storage.fixed_plan(store, rows, preflight.CLASSIFICATION.read_bytes())
    original_plan, _, updates, plan_sha = original.load_plan(store, rows)
    if plan_sha != preflight.PLAN_SHA:
        raise ValueError('original plan changed')
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        result = execute(store, storage.FixedS3(store, subset_plan), fence, client, rows, subset_plan,
                         progress, original_plan, updates)
    with OUTPUT.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
