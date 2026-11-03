#!/usr/bin/env python3
"""Resume the exact 352 after the user's final-20 cleanup if necessary.

The original historical journal remains unchanged. This reuses the exact
352-only writer and its durable intents. It does not reinstate the deleted
20-item root or use referencedness as a barrier: the user explicitly requested
removal even for referenced CHM EPUBs. It never touches any other data object.
"""
import json
import os
from pathlib import Path
import secrets

try:
    from . import chm_final20_execute as final, chm_final20_journal as last
    from . import chm_subset_journal as model
    from . import chm_subset_journal_backend as chain, chm_subset_locked_graph as locked
    from . import chm_subset_locked_preflight as preflight, chm_subset_s3_journal as storage
    from .reader_gc_fence import GitHubProducerFence
except ImportError:
    import chm_final20_execute as final
    import chm_final20_journal as last
    import chm_subset_journal as model
    import chm_subset_journal_backend as chain
    import chm_subset_locked_graph as locked
    import chm_subset_locked_preflight as preflight
    import chm_subset_s3_journal as storage
    from reader_gc_fence import GitHubProducerFence

WORKFLOW_REF = 'anftm/pipeline/.github/workflows/chm-remaining352-after-final20.yml@refs/heads/main'
OUTPUT = Path('output/chm-remaining352-after-final20/result.json')


class FinalReadOnly:
    def __init__(self, store):
        self.store = store
    def names(self):
        paths = self.store.list_files(preflight.chm.ASSETS, (final.PREFIX,))
        names = {path[len(final.PREFIX):] for path in paths if path.startswith(final.PREFIX)}
        if len(paths) != len(names) or any(not final.NAME.fullmatch(name) for name in names):
            raise ValueError('final-20 journal contains a foreign key')
        return names
    def read(self, name):
        try:
            return self.store.read_bytes(preflight.chm.ASSETS, final.PREFIX + name)
        except FileNotFoundError:
            return None
    def create(self, *_):
        raise AssertionError('final-20 journal is read-only')


def final_head(store, prior_plan):
    bound = last.fixed_plan(prior_plan['withheld'], storage.PLAN_SHA, locked.ROOT_SHA)
    head = last.Cursor(FinalReadOnly(store), bound).head()
    state = head['state']
    if (state['complete'] is not True or state['pending'] is not None or state['root_pending']
            or state['receipt'] != [row['path'] for row in bound['targets']]):
        raise ValueError('final-20 transaction has not completed')
    if any(value != 'absent' for value in final.observe(store, bound['targets'], state).values()):
        raise ValueError('final-20 objects are not absent')
    root = final.isolated.build(prior_plan['withheld'], locked.IDENTITY)
    try:
        store.read_bytes(preflight.chm.ASSETS, root['path'])
    except FileNotFoundError:
        pass
    else:
        raise ValueError('final-20 operator root is still present')
    return head


def observe(store, rows, old, plan, state):
    from concurrent.futures import ThreadPoolExecutor
    import hashlib
    preflight.subset.verify_exact_partition(rows, old, plan['selected'], plan['withheld'])
    preflight.subset.verify_subset_cursor(plan['selected'], plan['withheld'], state)
    old_paths = {path for item in old for path in item['paths']}
    receipted = {path for item in state['receipts'] for path in item['paths']}
    pending = set((state.get('pending') or {}).get('paths', []))
    kept = {row['path'] for row in plan['withheld']}
    def read(row):
        try:
            raw = store.read_bytes(row['bucket'], row['path'])
        except FileNotFoundError:
            raw = None
        if raw is not None and (len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']):
            raise ValueError('352 target bytes differ: ' + row['path'])
        path = row['path']
        if path in old_paths or path in receipted or path in kept:
            if raw is not None:
                raise ValueError('previously absent CHM target reappeared: ' + path)
            return 'absent'
        if path in pending:
            return 'pending-absent' if raw is None else 'pending-present'
        if raw is None:
            raise ValueError('unsubmitted 352 target is absent')
        return 'unsubmitted-present'
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(read, rows))
    return {row['path']: status for row, status in zip(rows, statuses)}


def run(store, writer, fence, rows, old, plan):
    fence.check()
    preflight._identity(store)
    final_before = final_head(store, plan)
    cursor = chain.Cursor(writer, plan)
    names = writer.names()
    if not names:
        cursor.initialize(nonce=storage.INITIAL_NONCE)
    elif names == {'plan.json'}:
        if cursor.reconcile_initialization(nonce=storage.INITIAL_NONCE)['status'] != 'plan-only-unresolved':
            raise ValueError('prior journal initialization differs')
        initial = model.make_envelope(plan, {'receipts': [], 'pending': None, 'subset_complete': False,
                                            'original_complete': False, 'producer_restore_authorized': False},
                                      sequence=0, parent_sha256=None, nonce=storage.INITIAL_NONCE)
        if model.digest(initial) != storage.INITIAL_SHA or writer.read('plan.json') != model.encode(plan):
            raise ValueError('prior fixed initial event differs')
        writer.create('event-00000000.json', model.encode(initial))
    head = cursor.head()
    def check():
        preflight._identity(store)
        fence.check()
        if final_head(store, plan) != final_before:
            raise ValueError('final-20 journal changed')
        return observe(store, rows, old, plan, head['state'])
    while not head['state']['subset_complete']:
        statuses = check()
        state = head['state']
        if state['pending'] is None:
            verified = sum(len(item['paths']) for item in state['receipts'])
            if verified == len(plan['selected']):
                event = model.make_envelope(plan, {**state, 'subset_complete': True}, sequence=head['sequence'] + 1,
                                            parent_sha256=model.digest(head), nonce=secrets.token_hex(16))
                cursor.append(model.digest(head), event)
                head = cursor.head()
                break
            paths = [row['path'] for row in plan['selected'][verified:verified + 100]]
            if not paths or any(statuses[path] != 'unsubmitted-present' for path in paths):
                raise ValueError('remaining 352 target observation differs')
            event = model.make_envelope(plan, {**state, 'pending': {'bucket': model.subset.ASSETS,
                                        'status': 'submitted', 'paths': paths}}, sequence=head['sequence'] + 1,
                                        parent_sha256=model.digest(head), nonce=secrets.token_hex(16))
            cursor.append(model.digest(head), event)
            head = cursor.head()
            statuses = check()
            state = head['state']
        pending = set(state['pending']['paths'])
        present = [row['path'] for row in plan['selected']
                   if row['path'] in pending and statuses[row['path']] == 'pending-present']
        if present:
            writer.delete_once(present)
            statuses = check()
        if any(statuses[path] != 'pending-absent' for path in pending):
            raise ValueError('pending 352 paths are not all absent')
        receipt = {**state['pending'], 'status': 'verified-absent'}
        event = model.make_envelope(plan, {**state, 'pending': None, 'receipts': [*state['receipts'], receipt]},
                                    sequence=head['sequence'] + 1, parent_sha256=model.digest(head),
                                    nonce=secrets.token_hex(16))
        cursor.append(model.digest(head), event)
        head = cursor.head()
        print(json.dumps({'stage': '352-receipted',
                          'objects': sum(len(item['paths']) for item in head['state']['receipts'])}), flush=True)
    check()
    return {'kind': 'chm-remaining352-after-final20', 'subset_complete': head['state']['subset_complete'],
            'subset_verified_absent': sum(len(item['paths']) for item in head['state']['receipts']),
            'subset_head_sha256': model.digest(head), 'final20_head_sha256': model.digest(final_before),
            'final20_verified_absent': 20, 'original_complete': False, 'producer_restore_authorized': False,
            'caveats': ['user explicitly requested removal even if referenced',
                        'historical callback and dangling references require separate cleanup']}


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF or not os.environ.get('GITHUB_RUN_ID', '').isdigit()):
        raise RuntimeError('remaining CHM deletion requires its central workflow')
    preflight.ensure_read_credentials()
    if OUTPUT.exists():
        raise FileExistsError('remaining CHM output already exists')
    rows = preflight.chm.preview.load_inventory(preflight.INVENTORY)['targets']
    store = preflight.ReadOnlyStore()
    plan, progress = storage.fixed_plan(store, rows, preflight.CLASSIFICATION.read_bytes())
    result = run(store, storage.FixedS3(store, plan), GitHubProducerFence(api=preflight.github_get),
                 rows, progress['batches'], plan)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open('x', encoding='utf-8') as file:
        json.dump(result, file, ensure_ascii=False, sort_keys=True, indent=2)
        file.write('\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
