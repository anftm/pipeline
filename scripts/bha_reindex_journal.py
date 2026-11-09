"""Durable, narrowly scoped BHA index-request journal.

The journal stores source identities, not the request credential. A CAS claim
is a one-attempt gate for cooperating callers, not an external writer fence or
an ABA lock. This module never contacts BHA or proves index delivery.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass

try:
    from . import bha_reindex_plan as planning
    from . import search_snapshot
    from .reader_bucket_store import S3BucketStore
    from .reader_page_cas import strong_etag
except ImportError:
    import bha_reindex_plan as planning
    import search_snapshot
    from reader_bucket_store import S3BucketStore
    from reader_page_cas import strong_etag


BUCKET = 'vomebook/search-data-v1'
HEAD = 'bha-reindex/head.json'
ROOT = 'bha-reindex/operations/'
ID = re.compile(r'[0-9a-f]{32}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_JSON = 256 * 1024
STATES = {'prepared', 'claimed', 'accepted', 'verified', 'failed'}


def _parse(raw: bytes, keys: set[str]) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_JSON:
        raise ValueError('invalid BHA journal JSON size')
    try: value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error: raise ValueError('invalid BHA journal JSON') from error
    if not isinstance(value, dict) or set(value) != keys or search_snapshot.canonical(value) != raw:
        raise ValueError('invalid or noncanonical BHA journal object')
    return value


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value): raise ValueError('invalid BHA operation identity')
    return value


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value): raise ValueError('invalid BHA journal digest')
    return value


def _head(raw):
    value = _parse(raw, {'version', 'kind', 'operation', 'record', 'sequence'})
    if (value['version'] != 1 or type(value['version']) is not int or value['kind'] != 'bha-reindex-head'
            or type(value['sequence']) is not int or not 0 <= value['sequence'] <= 4):
        raise ValueError('invalid BHA journal head')
    _id(value['operation']); _sha(value['record'])
    return value


def _record(raw):
    fields = {'version', 'kind', 'operation', 'sequence', 'prior', 'previous_terminal', 'state',
              'mode', 'plan_sha256', 'archive_commits', 'archive_revisions', 'baseline_revisions',
              'baseline_fingerprint', 'baseline_job', 'changed_archives', 'claim', 'job_id', 'result'}
    value = _parse(raw, fields)
    if (type(value['version']) is not int or value['version'] != 1 or value['kind'] != 'bha-reindex-record'
            or type(value['sequence']) is not int or not 0 <= value['sequence'] <= 4
            or not isinstance(value['state'], str) or value['state'] not in STATES
            or not isinstance(value['mode'], str) or value['mode'] not in {'incremental', 'full'}):
        raise ValueError('invalid BHA journal record')
    _id(value['operation']); _sha(value['plan_sha256'])
    for name in ('archive_commits', 'archive_revisions', 'baseline_revisions'):
        planning._trees(value[name], name)
    if value['baseline_fingerprint'] is not None and (not isinstance(value['baseline_fingerprint'], str)
                                                       or len(value['baseline_fingerprint']) > 512):
        raise ValueError('BHA baseline fingerprint is invalid')
    if value['baseline_job'] is not None and not isinstance(value['baseline_job'], dict):
        raise ValueError('BHA baseline job is invalid')
    changed = value['changed_archives']
    expected = [int(key) for key in sorted(planning.KEYS, key=int)
                if value['archive_revisions'][key] != value['baseline_revisions'][key]]
    if (not isinstance(changed, list) or any(type(item) is not int for item in changed) or changed != expected
            or (value['mode'] == 'incremental' and not changed)):
        raise ValueError('BHA journal changed archive set differs')
    if (value['sequence'] == 0) != (value['prior'] is None) or (value['sequence'] == 0) != (value['state'] == 'prepared'):
        raise ValueError('BHA journal predecessor differs')
    if value['prior'] is not None: _sha(value['prior'])
    previous = value['previous_terminal']
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != {'operation', 'record', 'sequence'}:
            raise ValueError('invalid BHA previous operation')
        _id(previous['operation']); _sha(previous['record'])
        if previous['operation'] == value['operation'] or type(previous['sequence']) is not int or not 2 <= previous['sequence'] <= 4:
            raise ValueError('invalid BHA previous operation identity')
    if value['state'] == 'prepared':
        if value['claim'] is not None or value['job_id'] is not None or value['result'] is not None:
            raise ValueError('BHA prepared state cannot claim a result')
    else:
        _id(value['claim'])
        if value['state'] in {'accepted', 'verified'}:
            _id(value['job_id'])
        elif value['state'] == 'claimed' and value['job_id'] is not None:
            raise ValueError('BHA claimed state cannot claim an accepted job')
        if value['state'] in {'verified', 'failed'}:
            if not isinstance(value['result'], dict): raise ValueError('BHA terminal observation is missing')
        elif value['result'] is not None:
            raise ValueError('BHA nonterminal state cannot claim a result')
    return value


def _advance(previous: dict, value: dict, prior_digest: str) -> None:
    fields = {'version', 'kind', 'operation', 'previous_terminal', 'mode', 'plan_sha256',
              'archive_commits', 'archive_revisions', 'baseline_revisions', 'baseline_fingerprint',
              'baseline_job', 'changed_archives'}
    if (any(previous[key] != value[key] for key in fields) or value['prior'] != prior_digest
            or value['sequence'] != previous['sequence'] + 1):
        raise ValueError('BHA journal immutable identity or predecessor changed')
    pair = previous['state'], value['state']
    if pair not in {('prepared', 'claimed'), ('claimed', 'accepted'), ('accepted', 'verified'),
                    ('accepted', 'failed'), ('claimed', 'failed')}:
        raise ValueError('invalid BHA journal transition')
    if pair != ('prepared', 'claimed') and previous['claim'] != value['claim']:
        raise ValueError('BHA write claim changed')
    if pair in {('accepted', 'verified'), ('accepted', 'failed')} and previous['job_id'] != value['job_id']:
        raise ValueError('BHA job identity changed')


def _successor(previous: dict, value: dict, digest: str) -> None:
    expected = {'operation': previous['operation'], 'record': digest, 'sequence': previous['sequence']}
    if (previous['state'] not in {'verified', 'failed'} or value['state'] != 'prepared'
            or value['sequence'] != 0 or value['previous_terminal'] != expected
            or value['operation'] == previous['operation']):
        raise ValueError('BHA previous operation is not an exact terminal predecessor')


@dataclass(frozen=True)
class Current:
    head: dict
    head_raw: bytes
    etag: str
    record: dict


def _record_key(operation: str, digest: str) -> str:
    return f'{ROOT}{_id(operation)}/records/{_sha(digest)}.json'


def load(store) -> Current | None:
    raw, etag = store.read_head()
    if raw is None:
        if etag is not None: raise ValueError('missing BHA journal head has an ETag')
        return None
    strong_etag(etag); head = _head(raw)
    data = store.read_record(_record_key(head['operation'], head['record']))
    if data is None or hashlib.sha256(data).hexdigest() != head['record']:
        raise ValueError('BHA journal head record is missing or changed')
    value = _record(data)
    if value['operation'] != head['operation'] or value['sequence'] != head['sequence']:
        raise ValueError('BHA head does not match record')
    return Current(head, raw, etag, value)


def audit(store, current: Current, *, maximum: int = 10_000) -> int:
    if type(maximum) is not int or not 1 <= maximum <= 10_000: raise ValueError('invalid BHA journal audit limit')
    value, count, visited = current.record, 0, set()
    while True:
        count += 1
        if count > maximum: raise ValueError('BHA journal history exceeds audit limit')
        link = value['previous_terminal'] if value['prior'] is None else None
        if value['prior'] is None and link is None: return count
        operation = link['operation'] if link else value['operation']
        digest = link['record'] if link else value['prior']
        key = _record_key(operation, digest)
        if key in visited: raise ValueError('BHA journal history cycle')
        visited.add(key); raw = store.read_record(key)
        if raw is None or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('BHA journal history is missing or changed')
        previous = _record(raw)
        if link: _successor(previous, value, digest)
        else: _advance(previous, value, digest)
        value = previous


def _commit(store, value: dict, previous: Current | None) -> Current:
    raw = search_snapshot.canonical(value); _record(raw)
    if previous is None:
        if value['state'] != 'prepared' or value['previous_terminal'] is not None: raise ValueError('invalid first BHA operation')
    elif value['operation'] != previous.record['operation']:
        _successor(previous.record, value, previous.head['record'])
    else: _advance(previous.record, value, previous.head['record'])
    digest = hashlib.sha256(raw).hexdigest(); key = _record_key(value['operation'], digest)
    prior = store.read_record(key)
    if prior is None:
        try: store.create_record(key, raw)
        except Exception:
            prior = store.read_record(key)
            if prior != raw: raise
        else: prior = store.read_record(key)
    if prior != raw: raise ValueError('BHA immutable record differs')
    head = search_snapshot.canonical({'version': 1, 'kind': 'bha-reindex-head', 'operation': value['operation'],
                                      'record': digest, 'sequence': value['sequence']})
    try: store.write_head(head, previous.etag if previous else None)
    except Exception:
        actual = load(store)
        if actual is not None and actual.head_raw == head: return actual
        raise
    actual = load(store)
    if actual is None or actual.head_raw != head: raise RuntimeError('BHA journal head readback differs')
    return actual


def begin(store, plan: dict, *, mode: str, operation: str | None = None) -> Current:
    """Persist identities from a plan; caller must re-read actual sources/status."""
    if not isinstance(plan, dict) or plan.get('kind') != 'bha-reindex-readonly-plan':
        raise ValueError('BHA exact read-only plan is required')
    body = {key: value for key, value in plan.items() if key != 'plan_sha256'}
    digest = hashlib.sha256(search_snapshot.canonical(body)).hexdigest()
    if digest != plan.get('plan_sha256') or plan.get('publication_ready') is not False or plan.get('remote_writes') != 0:
        raise ValueError('BHA plan hash or read-only status differs')
    previous = load(store)
    if previous is not None:
        audit(store, previous)
        # A failed request may still have partial external effects. Do not
        # authorize another request merely because it is terminal.
        if previous.record['state'] != 'verified':
            raise ValueError('BHA journal has unfinished or failed work requiring recovery')
    operation = _id(operation or uuid.uuid4().hex)
    if previous is not None and operation == previous.record['operation']:
        raise ValueError('BHA new operation ID must differ')
    value = {'version': 1, 'kind': 'bha-reindex-record', 'operation': operation, 'sequence': 0, 'prior': None,
             'previous_terminal': ({'operation': previous.record['operation'], 'record': previous.head['record'],
                                    'sequence': previous.record['sequence']} if previous else None),
             'state': 'prepared', 'mode': mode, 'plan_sha256': digest, 'archive_commits': plan['archive_commits'],
             'archive_revisions': plan['archive_revisions'], 'baseline_revisions': plan['consumer_revisions'],
             'baseline_fingerprint': plan['consumer_fingerprint'], 'baseline_job': plan['last_job'],
             'changed_archives': plan['changed_archives'], 'claim': None, 'job_id': None, 'result': None}
    return _commit(store, value, previous)


def claim(store, current: Current) -> Current:
    if current.record['state'] != 'prepared': raise ValueError('BHA request already claimed or not prepared')
    active = load(store)
    if active is None or active.head_raw != current.head_raw or active.etag != current.etag:
        raise ValueError('BHA journal changed before request claim')
    audit(store, current)
    value = {**current.record, 'sequence': current.record['sequence'] + 1, 'prior': current.head['record'],
             'state': 'claimed', 'claim': uuid.uuid4().hex}
    return _commit(store, value, current)


def check_claim(store, current: Current) -> None:
    active = load(store)
    if (current.record['state'] != 'claimed' or active is None or active.head_raw != current.head_raw
            or active.etag != current.etag):
        raise ValueError('BHA journal claim changed before request')
    audit(store, current)


def accepted(store, current: Current, *, job_id: str) -> Current:
    if current.record['state'] != 'claimed': raise ValueError('BHA request has no matching claim')
    active = load(store)
    if active is None or active.head_raw != current.head_raw or active.etag != current.etag:
        raise ValueError('BHA journal changed before accepted observation')
    audit(store, current)
    _id(job_id)
    return _commit(store, {**current.record, 'sequence': current.record['sequence'] + 1,
                           'prior': current.head['record'], 'state': 'accepted', 'job_id': job_id}, current)


def terminal(store, current: Current, *, state: str, result: dict) -> Current:
    if state not in {'verified', 'failed'} or not isinstance(result, dict):
        raise ValueError('invalid BHA terminal observation')
    active = load(store)
    if active is None or active.head_raw != current.head_raw or active.etag != current.etag:
        raise ValueError('BHA journal changed before terminal observation')
    audit(store, current)
    return _commit(store, {**current.record, 'sequence': current.record['sequence'] + 1,
                           'prior': current.head['record'], 'state': state, 'result': result}, current)


class S3Store:
    """Conditional head and immutable records, confined to the BHA namespace."""
    def __init__(self, store=None):
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            store._config = store._config.merge(Config(retries={'total_max_attempts': 1},
                request_checksum_calculation='when_required', response_checksum_validation='when_required'))
        self.store = store

    def _client(self):
        namespace, bucket = self.store._location(BUCKET)
        if (namespace, bucket) != ('vomebook', 'search-data-v1'):
            raise ValueError('unexpected BHA journal bucket mapping')
        return self.store._client(namespace), bucket

    def _get(self, key):
        client, bucket = self._client()
        try: response = client.get_object(Bucket=bucket, Key=key)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                return None, None
            raise
        body, length = response['Body'], response.get('ContentLength')
        try:
            if type(length) is not int or not 0 < length <= MAX_JSON: raise ValueError('BHA journal object is oversized')
            raw = body.read(length + 1)
        finally: body.close()
        if len(raw) != length: raise ValueError('BHA journal object is truncated')
        return raw, strong_etag(response.get('ETag'))

    def read_head(self):
        raw, etag = self._get(HEAD)
        if raw is not None: _head(raw)
        return raw, etag

    def write_head(self, raw, etag):
        _head(raw); client, bucket = self._client()
        condition = {'IfNoneMatch': '*'} if etag is None else {'IfMatch': strong_etag(etag)}
        client.put_object(Bucket=bucket, Key=HEAD, Body=raw, ContentType='application/json', **condition)

    def read_record(self, key):
        if not isinstance(key, str) or not re.fullmatch(r'bha-reindex/operations/[0-9a-f]{32}/records/[0-9a-f]{64}\.json', key):
            raise ValueError('BHA journal key outside allowlist')
        raw, _etag = self._get(key)
        return raw

    def create_record(self, key, raw):
        value = _record(raw)
        if key != _record_key(value['operation'], hashlib.sha256(raw).hexdigest()):
            raise ValueError('BHA journal record content address differs')
        client, bucket = self._client()
        client.put_object(Bucket=bucket, Key=key, Body=raw, ContentType='application/json', IfNoneMatch='*')
