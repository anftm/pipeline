"""Fixed-transaction S3 journal and exact 352-target delete-once adapter.

Only the predetermined independent journal keys and selected EPUB keys can be
written/deleted. All mutation requests disable SDK retries. The surrounding
workflow must hold the three reviewed Actions concurrency groups throughout.
Conditional PUT is not a distributed lock and does not fence external clients.
"""
import json
import os
import re

import boto3
from botocore.config import Config

try:
    from . import chm_subset_journal as model, chm_subset_journal_backend as chain
    from . import chm_subset_locked_graph as locked, chm_subset_locked_preflight as preflight
except ImportError:
    import chm_subset_journal as model
    import chm_subset_journal_backend as chain
    import chm_subset_locked_graph as locked
    import chm_subset_locked_preflight as preflight

PLAN_SHA = '54cf3a8abdf8795d6d50b32094a2fde97cc82cf58c40cb131c92169e1140d1dc'
INITIAL_SHA = '290c5468b378362d05caafa91b620ddf41b7b51c5dbfd8f64192be6689733634'
INITIAL_NONCE = 'f817575f5110fcf11d3cee92c4636b62'
PREFIX = 'reader-index/gc/chm-subset/' + locked.IDENTITY['transaction_id'] + '/'
NAME = re.compile(r'(?:plan|event-[0-9]{8})\.json')
BUCKET = 'reader-assets-v2'
MAX_JOURNAL_BYTES = 200_000


class DeleteUnknown(RuntimeError):
    """Delete may have happened; reconcile every pending key before a retry."""


class JournalPartialInitialization(chain.OutcomeUnknown):
    """Only the exact plan was persisted before a previous interruption."""


def fixed_plan(store, rows, classification_raw):
    if preflight.digest(classification_raw) != preflight.CLASSIFICATION_SHA:
        raise ValueError('subset classification bytes differ')
    classification = json.loads(classification_raw)
    indexed = {row['path']: row for row in rows}
    selected = sorted((indexed[item['path']] for item in classification['targets']
                       if item['category'] == 'mapped-with-saved-evidence'), key=lambda row: row['path'])
    withheld = sorted((indexed[item['path']] for item in classification['targets']
                       if item['category'] in {'needs-independent-review', 'withheld-oversize'}),
                      key=lambda row: row['path'])
    progress_raw, _, _, progress = preflight._identity(store)
    bound = model.bind_plan(rows, progress['batches'], selected, withheld, locked.IDENTITY,
                            old_progress_raw=progress_raw)
    plan = bound['plan']
    if (bound['plan_sha256'] != PLAN_SHA or bound['partition']['old_receipts'] != 600
            or bound['partition']['selected'] != 352 or bound['partition']['withheld'] != 20):
        raise ValueError('fixed CHM subset journal plan differs')
    initial = {'receipts': [], 'pending': None, 'subset_complete': False,
               'original_complete': False, 'producer_restore_authorized': False}
    first = model.make_envelope(plan, initial, sequence=0, parent_sha256=None, nonce=INITIAL_NONCE)
    if model.digest(first) != INITIAL_SHA:
        raise ValueError('fixed CHM initial journal event differs')
    return plan, progress


class FixedS3:
    def __init__(self, reader, plan):
        if (not isinstance(plan, dict) or model.digest(plan) != PLAN_SHA
                or plan.get('identity') != locked.IDENTITY or len(plan.get('selected', [])) != 352
                or len(plan.get('withheld', [])) != 20):
            raise ValueError('production journal requires the fixed 352/20 plan')
        expected = model._plan(plan['selected'], plan['withheld'], plan['identity'])
        if plan != expected:
            raise ValueError('production journal plan schema differs')
        access, secret = os.environ.get('HF_S3_ACCESS_KEY_ID'), os.environ.get('HF_S3_SECRET_ACCESS_KEY')
        if not access or not secret:
            raise RuntimeError('CHM journal credentials are unavailable')
        # The mutable caller's dictionaries are not the authority after init.
        self.reader, self.plan = reader, json.loads(model.encode(plan))
        self.allowed = frozenset(row['path'] for row in self.plan['selected'])
        self.client = boto3.client('s3', endpoint_url='https://s3.hf.co/vomebook',
                                   aws_access_key_id=access, aws_secret_access_key=secret,
                                   config=Config(region_name='us-east-1', s3={'addressing_style': 'path'},
                                                 retries={'total_max_attempts': 1},
                                                 connect_timeout=10, read_timeout=30,
                                                 request_checksum_calculation='when_required',
                                                 response_checksum_validation='when_required'))

    @staticmethod
    def key(name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError('journal key outside fixed transaction')
        return PREFIX + name

    def names(self):
        paths = self.reader.list_files(preflight.chm.ASSETS, (PREFIX,))
        names = {path[len(PREFIX):] for path in paths if path.startswith(PREFIX)}
        if len(names) != len(paths) or any(not NAME.fullmatch(name) for name in names):
            raise ValueError('journal prefix contains foreign keys')
        return names

    def read(self, name):
        try:
            raw = self.reader.read_bytes(preflight.chm.ASSETS, self.key(name))
        except FileNotFoundError:
            return None
        if not isinstance(raw, bytes) or len(raw) > MAX_JOURNAL_BYTES:
            raise ValueError('journal object size differs')
        return raw

    def assert_chm_bucket_available(self):
        return self.reader.assert_chm_bucket_available()

    def read_bytes(self, bucket, path):
        return self.reader.read_bytes(bucket, path)

    def delete(self, bucket, paths):
        if bucket != preflight.chm.ASSETS:
            raise ValueError('CHM deletion bucket differs')
        self.delete_once(paths)

    def create(self, name, raw):
        path = self.key(name)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_JOURNAL_BYTES:
            raise ValueError('invalid fixed journal bytes')
        if name == 'plan.json':
            if raw != model.encode(self.plan):
                raise ValueError('foreign subset journal plan')
        else:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError) as error:
                raise ValueError('invalid subset journal event') from error
            if (not isinstance(event, dict) or model.encode(event) != raw
                    or event.get('plan_sha256') != PLAN_SHA):
                raise ValueError('foreign or noncanonical subset journal event')
        try:
            response = self.client.put_object(Bucket=BUCKET, Key=path, Body=raw,
                                              ContentType='application/json', IfNoneMatch='*')
        except Exception as error:
            raise chain.OutcomeUnknown('journal PUT may have succeeded; inspect before proceeding') from error
        meta = response.get('ResponseMetadata', {})
        if meta.get('HTTPStatusCode') not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
            raise chain.OutcomeUnknown('journal PUT response ambiguous; do not retry')
        return True

    def delete_once(self, paths):
        if (not isinstance(paths, list) or not paths or len(paths) > 100
                or any(not isinstance(path, str) for path in paths)
                or len(paths) != len(set(paths)) or not set(paths) <= self.allowed):
            raise ValueError('delete exceeds fixed 352-target scope')
        try:
            result = self.client.delete_objects(Bucket=BUCKET, Delete={
                'Objects': [{'Key': path} for path in paths], 'Quiet': False})
        except Exception as error:
            raise DeleteUnknown('delete response unknown; reconcile pending paths, never blindly retry') from error
        meta = result.get('ResponseMetadata', {})
        deleted = result.get('Deleted')
        if (meta.get('HTTPStatusCode') != 200 or meta.get('RetryAttempts', 0) != 0
                or result.get('Errors') or not isinstance(deleted, list)
                or len(deleted) != len(paths) or any(not isinstance(row, dict) for row in deleted)
                or {row.get('Key') for row in deleted} != set(paths)):
            raise DeleteUnknown('delete response partial or ambiguous; reconcile pending paths')
