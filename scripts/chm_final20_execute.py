#!/usr/bin/env python3
"""Delete exactly the final 20 listed historical CHM EPUBs under three locks.

The user explicitly requested removal even if referenced. Never delete source
CHM, chapters, OCR, Reader assets, or a key outside these 20 and the temporary
operator-created protection root. Journal every intent before mutation.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

import boto3
from botocore.config import Config

try:
    from . import chm_final20_journal as model, chm_subset_journal as common
    from . import chm_subset_journal_backend as old_chain, chm_subset_locked_graph as locked
    from . import chm_subset_locked_preflight as preflight, chm_subset_s3_journal as prior
    from . import chm_subset_isolated_root as isolated
    from .reader_gc_fence import GitHubProducerFence
except ImportError:
    import chm_final20_journal as model
    import chm_subset_journal as common
    import chm_subset_journal_backend as old_chain
    import chm_subset_locked_graph as locked
    import chm_subset_locked_preflight as preflight
    import chm_subset_s3_journal as prior
    import chm_subset_isolated_root as isolated
    from reader_gc_fence import GitHubProducerFence

WORKFLOW_REF = 'anftm/pipeline/.github/workflows/chm-final20-execute.yml@refs/heads/main'
PREFIX = 'reader-index/gc/chm-final20/' + locked.IDENTITY['transaction_id'] + '/'
NAME = re.compile(r'(?:plan|event-[0-9]{8})\.json')
OUTPUT = Path('output/chm-final20-execute/result.json')


class Uncertain(RuntimeError):
    """Mutation result unknown; reconcile exact keys before another attempt."""


class Store:
    def __init__(self, reader, bound, root):
        if (model.fixed_plan(bound['targets'], prior.PLAN_SHA, locked.ROOT_SHA) != bound
                or root['sha256'] != locked.ROOT_SHA or root['path'] != isolated.root_path(locked.IDENTITY['transaction_id'])):
            raise ValueError('final CHM fixed scope differs')
        access, secret = os.environ.get('HF_S3_ACCESS_KEY_ID'), os.environ.get('HF_S3_SECRET_ACCESS_KEY')
        if not access or not secret:
            raise RuntimeError('final CHM storage credentials unavailable')
        self.reader, self.bound, self.root = reader, json.loads(common.encode(bound)), root
        self.allowed = frozenset(row['path'] for row in self.bound['targets'])
        self.client = boto3.client('s3', endpoint_url='https://s3.hf.co/vomebook',
                                   aws_access_key_id=access, aws_secret_access_key=secret,
                                   config=Config(region_name='us-east-1', s3={'addressing_style': 'path'},
                                                 retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=30,
                                                 request_checksum_calculation='when_required',
                                                 response_checksum_validation='when_required'))

    def names(self):
        paths = self.reader.list_files(preflight.chm.ASSETS, (PREFIX,))
        names = {path[len(PREFIX):] for path in paths if path.startswith(PREFIX)}
        if len(paths) != len(names) or any(not NAME.fullmatch(name) for name in names):
            raise ValueError('foreign key in final CHM journal')
        return names

    @staticmethod
    def key(name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('invalid final CHM journal key')
        return PREFIX + name

    def read(self, name):
        try:
            raw = self.reader.read_bytes(preflight.chm.ASSETS, self.key(name))
        except FileNotFoundError:
            return None
        if not isinstance(raw, bytes) or len(raw) > 100000:
            raise ValueError('invalid final CHM journal bytes')
        return raw

    def create(self, name, raw):
        path = self.key(name)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 100000:
            raise ValueError('invalid final CHM journal payload')
        if name == 'plan.json':
            if raw != common.encode(self.bound):
                raise ValueError('final CHM journal plan differs')
        else:
            event = json.loads(raw)
            if not isinstance(event, dict) or common.encode(event) != raw or event.get('plan_sha256') != common.digest(self.bound):
                raise ValueError('final CHM journal event differs')
        try:
            response = self.client.put_object(Bucket='reader-assets-v2', Key=path, Body=raw,
                                              ContentType='application/json', IfNoneMatch='*')
        except Exception as error:
            raise Uncertain('journal PUT outcome unknown; inspect before retry') from error
        meta = response.get('ResponseMetadata', {})
        if meta.get('HTTPStatusCode') not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
            raise Uncertain('journal PUT response ambiguous')
        return True

    def delete_targets(self, paths):
        if (not isinstance(paths, list) or not paths or len(paths) != len(set(paths))
                or any(not isinstance(p, str) for p in paths) or not set(paths) <= self.allowed):
            raise ValueError('final CHM deletion exceeds exact 20 paths')
        try:
            response = self.client.delete_objects(Bucket='reader-assets-v2',
                Delete={'Objects': [{'Key': path} for path in paths], 'Quiet': False})
        except Exception as error:
            raise Uncertain('target delete response unknown; reconcile pending paths') from error
        meta, deleted = response.get('ResponseMetadata', {}), response.get('Deleted')
        if (meta.get('HTTPStatusCode') != 200 or meta.get('RetryAttempts', 0) != 0 or response.get('Errors')
                or not isinstance(deleted, list) or len(deleted) != len(paths)
                or any(not isinstance(item, dict) for item in deleted)
                or {item.get('Key') for item in deleted} != set(paths)):
            raise Uncertain('target delete response partial or ambiguous; reconcile')

    def delete_root(self):
        if self.reader.read_bytes(preflight.chm.ASSETS, self.root['path']) != self.root['raw']:
            raise ValueError('operator root changed')
        try:
            response = self.client.delete_object(Bucket='reader-assets-v2', Key=self.root['path'])
        except Exception as error:
            raise Uncertain('operator root removal unknown; inspect before retry') from error
        meta = response.get('ResponseMetadata', {})
        if meta.get('HTTPStatusCode') not in (200, 204) or meta.get('RetryAttempts', 0) != 0:
            raise Uncertain('operator root removal response ambiguous')


def controls(store, fence):
    preflight._identity(store)
    fence.check()


def prior_cursor(store, plan):
    class ReadOnly:
        def names(self):
            paths = store.list_files(preflight.chm.ASSETS, (prior.PREFIX,))
            names = {path[len(prior.PREFIX):] for path in paths if path.startswith(prior.PREFIX)}
            if len(names) != len(paths) or any(not prior.NAME.fullmatch(name) for name in names):
                raise ValueError('prior journal contains foreign keys')
            return names
        def read(self, name):
            try:
                return store.read_bytes(preflight.chm.ASSETS, prior.PREFIX + name)
            except FileNotFoundError:
                return None
        def create(self, *_):
            raise AssertionError('prior journal is read only')
    snapshot = ReadOnly()
    names = snapshot.names()
    if not names:
        return None
    if names == {'plan.json'}:
        if snapshot.read('plan.json') != common.encode(plan):
            raise ValueError('prior plan differs')
        return None
    cursor = old_chain.Cursor(snapshot, plan)
    first = json.loads(snapshot.read('event-00000000.json'))
    if common.digest(first) != prior.INITIAL_SHA:
        raise ValueError('prior initial journal event differs')
    return cursor.head()


def observe(store, targets, state):
    def check(row):
        try:
            raw = store.read_bytes(row['bucket'], row['path'])
        except FileNotFoundError:
            return 'absent'
        if len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('exact final CHM EPUB bytes changed: ' + row['path'])
        return 'present'
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(check, targets))
    if state['pending'] is None and state['receipt'] is None and 'absent' in statuses:
        raise ValueError('final CHM EPUB absent before durable intent')
    if state['receipt'] is not None and 'present' in statuses:
        raise ValueError('receipted final CHM EPUB reappeared')
    return {row['path']: status for row, status in zip(targets, statuses)}


def next_event(bound, head, state):
    return model.envelope(bound, state, sequence=head['sequence'] + 1,
                          parent=common.digest(head), nonce=secrets.token_hex(16))


def execute(reader, backend, fence, targets, root):
    controls(reader, fence)
    try:
        actual_root = reader.read_bytes(preflight.chm.ASSETS, root['path'])
    except FileNotFoundError:
        actual_root = None
    cursor = model.Cursor(backend, backend.bound)
    if actual_root is not None and actual_root != root['raw']:
        raise ValueError('operator protection root differs')
    if actual_root is None and not backend.names():
        raise ValueError('operator root missing before final transaction')
    head = cursor.initialize()
    paths = [row['path'] for row in targets]
    state = head['state']
    if actual_root is None and not (state['root_pending'] or state['complete']):
        raise ValueError('operator root missing before recorded cleanup intent')
    controls(reader, fence)
    status = observe(reader, targets, state)
    if state['pending'] is None and state['receipt'] is None:
        after = next_event(backend.bound, head, {**state, 'pending': paths})
        cursor.append(head, after)
        head, state = after, after['state']
        print(json.dumps({'stage': '20-target-intent-recorded', 'targets': len(paths)}), flush=True)
        controls(reader, fence)
        status = observe(reader, targets, state)
    if state['pending'] is not None:
        present = [path for path in paths if status[path] == 'present']
        if present:
            backend.delete_targets(present)
            print(json.dumps({'stage': '20-target-delete-response', 'objects': len(present)}), flush=True)
            controls(reader, fence)
            status = observe(reader, targets, state)
        if any(value != 'absent' for value in status.values()):
            raise ValueError('not all final CHM EPUBs are absent')
        after = next_event(backend.bound, head, {**state, 'pending': None, 'receipt': paths})
        cursor.append(head, after)
        head, state = after, after['state']
    controls(reader, fence)
    observe(reader, targets, state)
    if not state['root_pending'] and not state['complete']:
        after = next_event(backend.bound, head, {**state, 'root_pending': True})
        cursor.append(head, after)
        head, state = after, after['state']
    if state['root_pending']:
        try:
            actual_root = reader.read_bytes(preflight.chm.ASSETS, root['path'])
        except FileNotFoundError:
            actual_root = None
        if actual_root is not None:
            if actual_root != root['raw']:
                raise ValueError('operator protection root changed before cleanup')
            backend.delete_root()
            try:
                reader.read_bytes(preflight.chm.ASSETS, root['path'])
            except FileNotFoundError:
                pass
            else:
                raise ValueError('operator protection root still exists after removal')
        controls(reader, fence)
        observe(reader, targets, state)
        after = next_event(backend.bound, head, {**state, 'root_pending': False, 'complete': True})
        cursor.append(head, after)
        head, state = after, after['state']
    # A completed cursor is also independently rechecked on a later run.
    controls(reader, fence)
    observe(reader, targets, state)
    try:
        reader.read_bytes(preflight.chm.ASSETS, root['path'])
    except FileNotFoundError:
        pass
    else:
        raise ValueError('removed operator protection root reappeared')
    return {'kind': 'chm-final20-execution', 'complete': state['complete'],
            'verified_absent': len(state['receipt'] or []), 'operator_root_removed': state['complete'],
            'journal_head_sha256': common.digest(head), 'journal_sequence': head['sequence'],
            'original_complete': False, 'producer_restore_authorized': False,
            'caveats': ['target references were not a deletion barrier, per user direction',
                        'other historical references require separate identification and cleanup',
                        'Actions locks do not fence external S3/Hub clients']}


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF or not os.environ.get('GITHUB_RUN_ID', '').isdigit()):
        raise RuntimeError('final CHM deletion requires its central workflow')
    preflight.ensure_read_credentials()
    if OUTPUT.exists():
        raise FileExistsError('final CHM output already exists')
    reader = preflight.ReadOnlyStore()
    rows = preflight.chm.preview.load_inventory(preflight.INVENTORY)['targets']
    prior_plan, _ = prior.fixed_plan(reader, rows, preflight.CLASSIFICATION.read_bytes())
    head = prior_cursor(reader, prior_plan)
    root = isolated.build(prior_plan['withheld'], locked.IDENTITY)
    bound = model.fixed_plan(prior_plan['withheld'], prior.PLAN_SHA, root['sha256'])
    result = execute(reader, Store(reader, bound, root), GitHubProducerFence(api=preflight.github_get),
                     bound['targets'], root)
    result['prior_352_receipted'] = sum(len(batch['paths']) for batch in head['state']['receipts']) if head else 0
    result['prior_352_complete'] = bool(head and head['state']['subset_complete'])
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open('x', encoding='utf-8') as file:
        json.dump(result, file, ensure_ascii=False, sort_keys=True, indent=2)
        file.write('\n')
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
