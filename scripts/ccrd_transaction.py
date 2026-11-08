"""Content-addressed CCRD activation transaction with one conditional head.

This journal tracks intent and recovery observations; it does not itself deploy
Space, mutate the CCRD pointer, or fence independent writers. A single-object
CAS is not a cross-object lock and cannot prevent an external ABA change.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Protocol

try:
    from . import ccrd_activation_inspection as activation
    from . import ccrd_lifecycle as lifecycle
    from .reader_page_cas import strong_etag
except ImportError:
    import ccrd_activation_inspection as activation
    import ccrd_lifecycle as lifecycle
    from reader_page_cas import strong_etag


HEAD = 'control/cw0-publication-head.json'
ROOT = 'control/cw0-publications/'
ID = re.compile(r'[0-9a-f]{32}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
REV = re.compile(r'[0-9a-f]{40}\Z')
GEN = re.compile(r'ccrd-four-[0-9a-f]{64}\Z')
MAX_BYTES = 256 * 1024
STATES = {'prepared', 'space-intent', 'space-verified', 'pointer-intent', 'pointer-verified', 'complete',
          'rollback-intent', 'rolled-back'}
STATE_SEQUENCE = {'prepared': {0}, 'space-intent': {1}, 'space-verified': {2},
                  'pointer-intent': {3}, 'pointer-verified': {4}, 'complete': {5},
                  'rollback-intent': {2, 3, 4, 5}, 'rolled-back': {3, 4, 5, 6}}


class Store(Protocol):
    def read_head(self) -> tuple[bytes | None, str | None]: ...
    def put_head(self, raw: bytes, expected_etag: str | None) -> None: ...
    def read_record(self, key: str) -> bytes | None: ...
    def put_record(self, key: str, raw: bytes) -> None: ...
    def read_evidence(self, key: str) -> bytes | None: ...
    def put_evidence(self, key: str, raw: bytes) -> None: ...
    def read_intent(self, key: str) -> bytes | None: ...
    def put_intent(self, key: str, raw: bytes) -> None: ...


def _exact(raw: bytes, fields: set[str]) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('invalid CCRD transaction object size')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid CCRD transaction JSON') from error
    if not isinstance(value, dict) or set(value) != fields or lifecycle.canonical(value) != raw:
        raise ValueError('invalid or noncanonical CCRD transaction schema')
    return value


def _match(value: object, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError('invalid CCRD ' + label)
    return value


def _head(raw: bytes) -> dict:
    value = _exact(raw, {'version', 'kind', 'transaction', 'sequence', 'record'})
    if (type(value['version']) is not int or value['version'] != 1 or value['kind'] != 'ccrd-cw0-transaction-head'
            or type(value['sequence']) is not int or not 0 <= value['sequence'] <= 1_000_000):
        raise ValueError('invalid CCRD transaction head')
    _match(value['transaction'], ID, 'transaction ID'); _match(value['record'], SHA, 'record digest')
    return value


def _record(raw: bytes) -> dict:
    keys = {'version', 'kind', 'transaction', 'sequence', 'prior', 'state', 'parent_sha256', 'candidate_generation',
            'candidate_manifest_sha256', 'activation_plan_sha256', 'durable_intent_sha256', 'parent_space_revision', 'space_revision',
            'evidence_sha256', 'rollback_revision', 'rollback_evidence_sha256'}
    value = _exact(raw, keys)
    if (type(value['version']) is not int or value['version'] != 1 or value['kind'] != 'ccrd-cw0-transaction-record'
            or type(value['sequence']) is not int or not 0 <= value['sequence'] <= 1_000_000
            or not isinstance(value['state'], str) or value['state'] not in STATES):
        raise ValueError('invalid CCRD transaction record')
    _match(value['transaction'], ID, 'transaction ID')
    for key in ('parent_sha256', 'candidate_manifest_sha256', 'activation_plan_sha256', 'durable_intent_sha256'):
        _match(value[key], SHA, key)
    _match(value['candidate_generation'], GEN, 'candidate generation')
    _match(value['parent_space_revision'], REV, 'parent Space revision')
    if value['space_revision'] is not None:
        _match(value['space_revision'], REV, 'Space revision')
    if value['evidence_sha256'] is not None:
        _match(value['evidence_sha256'], SHA, 'evidence digest')
    if value['rollback_revision'] is not None:
        _match(value['rollback_revision'], REV, 'rollback revision')
    if value['rollback_evidence_sha256'] is not None:
        _match(value['rollback_evidence_sha256'], SHA, 'rollback evidence digest')
    if value['prior'] is not None:
        _match(value['prior'], SHA, 'prior record')
    if ((value['sequence'] == 0) != (value['prior'] is None)
            or value['sequence'] not in STATE_SEQUENCE[value['state']]):
        raise ValueError('CCRD transaction predecessor or state sequence differs')
    if value['state'] in {'prepared', 'space-intent'}:
        if value['space_revision'] is not None or value['evidence_sha256'] is not None:
            raise ValueError('CCRD transaction reports Space evidence before activation')
    elif value['state'] not in {'rollback-intent', 'rolled-back'} and (
            value['space_revision'] is None or value['evidence_sha256'] is None):
        raise ValueError('CCRD transaction lacks Space activation evidence')
    if (value['space_revision'] is None) != (value['evidence_sha256'] is None):
        raise ValueError('CCRD activation revision and evidence must occur together')
    if value['state'] == 'rolled-back':
        if value['rollback_revision'] is None or value['rollback_evidence_sha256'] is None:
            raise ValueError('CCRD rollback evidence is missing')
    elif value['rollback_revision'] is not None or value['rollback_evidence_sha256'] is not None:
        raise ValueError('CCRD rollback evidence appears before rollback completion')
    return value


def _advance(previous: dict, current: dict, previous_digest: str) -> None:
    immutable = {'version', 'kind', 'transaction', 'parent_sha256', 'candidate_generation',
                 'candidate_manifest_sha256', 'activation_plan_sha256', 'durable_intent_sha256', 'parent_space_revision'}
    if (any(previous[key] != current[key] for key in immutable)
            or current['sequence'] != previous['sequence'] + 1 or current['prior'] != previous_digest):
        raise ValueError('CCRD transaction immutable identity or predecessor changed')
    allowed = {('prepared', 'space-intent'), ('space-intent', 'space-verified'),
               ('space-verified', 'pointer-intent'), ('pointer-intent', 'pointer-verified'),
               ('pointer-verified', 'complete'), ('space-intent', 'rollback-intent'),
               ('space-verified', 'rollback-intent'), ('pointer-intent', 'rollback-intent'),
               ('pointer-verified', 'rollback-intent'), ('rollback-intent', 'rolled-back')}
    if (previous['state'], current['state']) not in allowed:
        raise ValueError('invalid CCRD transaction state transition')
    if (previous['state'], current['state']) != ('space-intent', 'space-verified') and (
            previous['space_revision'] != current['space_revision'] or previous['evidence_sha256'] != current['evidence_sha256']):
        raise ValueError('CCRD transaction activation evidence changed')
    if (previous['state'], current['state']) != ('rollback-intent', 'rolled-back') and (
            previous['rollback_revision'] != current['rollback_revision']
            or previous['rollback_evidence_sha256'] != current['rollback_evidence_sha256']):
        raise ValueError('CCRD transaction rollback evidence changed')


@dataclass(frozen=True)
class Current:
    head: dict
    head_raw: bytes
    etag: str
    record: dict
    record_raw: bytes


def _record_key(transaction: str, digest: str) -> str:
    return f'{ROOT}{transaction}/records/{digest}.json'


def _evidence_key(transaction: str, digest: str) -> str:
    return f'{ROOT}{transaction}/evidence/{digest}.json'


def _intent_key(transaction: str, digest: str) -> str:
    return f'{ROOT}{transaction}/intents/{digest}.json'


def _intent(raw: bytes, record: dict) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('bounded CCRD durable intent is required')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid CCRD durable intent JSON') from error
    if (not isinstance(value, dict) or lifecycle.canonical(value) != raw
            or set(value) != {'version', 'kind', 'parent_pointer_hex', 'candidate_manifest_hex', 'activation_plan'}
            or type(value['version']) is not int or value['version'] != 1 or value['kind'] != 'ccrd-cw0-durable-intent'):
        raise ValueError('invalid CCRD durable intent schema')
    if (not isinstance(value['parent_pointer_hex'], str) or not isinstance(value['candidate_manifest_hex'], str)
            or not re.fullmatch(r'(?:[0-9a-f]{2})+', value['parent_pointer_hex'])
            or not re.fullmatch(r'(?:[0-9a-f]{2})+', value['candidate_manifest_hex'])):
        raise ValueError('invalid CCRD durable intent snapshots')
    try:
        parent = bytes.fromhex(value['parent_pointer_hex'])
        candidate = bytes.fromhex(value['candidate_manifest_hex'])
    except (ValueError, TypeError) as error:
        raise ValueError('invalid CCRD durable intent snapshots') from error
    if hashlib.sha256(parent).hexdigest() != record['parent_sha256']:
        raise ValueError('CCRD durable intent parent bytes differ')
    if hashlib.sha256(candidate).hexdigest() != record['candidate_manifest_sha256']:
        raise ValueError('CCRD durable intent candidate manifest bytes differ')
    try:
        parent_value, candidate_value = json.loads(parent), json.loads(candidate)
        lifecycle.references(parent_value, 'current.json')
        lifecycle.references(candidate_value, 'current.json')
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError('invalid CCRD durable intent manifest') from error
    if (candidate_value.get('generation') != record['candidate_generation']
            or lifecycle.canonical(candidate_value) != candidate
            or candidate_value.get('previous_index_manifest_sha256') != record['parent_sha256']):
        raise ValueError('CCRD durable intent candidate does not name the parent')
    plan = value['activation_plan']
    if (not isinstance(plan, dict) or plan.get('kind') != 'ccrd-space-activation-plan'
            or plan.get('space') != 'vomebook/Search' or plan.get('parent_revision') != record['parent_space_revision']
            or plan.get('parent_generation') != parent_value.get('generation')
            or plan.get('parent_pointer_sha256') != record['parent_sha256']
            or plan.get('candidate_generation') != record['candidate_generation']
            or plan.get('candidate_manifest_sha256') != record['candidate_manifest_sha256']
            or plan.get('plan_sha256') != record['activation_plan_sha256']):
        raise ValueError('CCRD durable activation plan identity differs')
    if hashlib.sha256(lifecycle.canonical({k: v for k, v in plan.items() if k != 'plan_sha256'})).hexdigest() != plan['plan_sha256']:
        raise ValueError('CCRD durable activation plan hash differs')
    old_activation = plan.get('parent_activation_hex')
    if (not isinstance(old_activation, str) or len(old_activation) > 2 * activation.MAX_ACTIVATION
            or not re.fullmatch(r'(?:[0-9a-f]{2})+', old_activation)):
        raise ValueError('CCRD durable intent lacks the parent activation')
    try:
        activation_bytes = bytes.fromhex(old_activation)
    except ValueError as error:
        raise ValueError('invalid CCRD parent activation bytes') from error
    activation.decode(activation_bytes, parent_value['generation'], record['parent_sha256'])
    if hashlib.sha256(activation_bytes).hexdigest() != plan.get('parent_activation_sha256'):
        raise ValueError('CCRD durable parent activation bytes differ')
    return value


def _activation_evidence(raw: bytes, record: dict, revision: str | None) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('bounded CCRD activation evidence is required')
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid CCRD activation evidence JSON') from error
    if (not isinstance(parsed, dict) or lifecycle.canonical(parsed) != raw
            or parsed.get('kind') != 'ccrd-activation-evidence'
            or parsed.get('revision') != revision
            or parsed.get('generation') != record['candidate_generation']
            or parsed.get('manifest_sha256') != record['candidate_manifest_sha256']
            or parsed.get('space_plan_sha256') != record['activation_plan_sha256']
            or parsed.get('consumer_verified') is not True or parsed.get('activation_verified') is not True):
        raise ValueError('CCRD activation evidence identity differs')
    return parsed


def _rollback_evidence(raw: bytes, record: dict, revision: str | None) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('bounded CCRD rollback evidence is required')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid CCRD rollback evidence JSON') from error
    if (not isinstance(value, dict) or lifecycle.canonical(value) != raw
            or value.get('kind') != 'ccrd-rollback-evidence' or value.get('revision') != revision
            or value.get('parent_sha256') != record['parent_sha256']
            or value.get('candidate_generation') != record['candidate_generation']
            or value.get('space_plan_sha256') != record['activation_plan_sha256']
            or value.get('consumer_parent_verified') is not True
            or value.get('activation_parent_verified') is not True
            or value.get('pointer_parent_verified') is not True):
        raise ValueError('CCRD rollback evidence identity differs')
    return value


def load(store: Store) -> Current | None:
    raw, etag = store.read_head()
    if raw is None:
        if etag is not None: raise ValueError('missing CCRD head has an ETag')
        return None
    strong_etag(etag); head = _head(raw)
    record_raw = store.read_record(_record_key(head['transaction'], head['record']))
    if record_raw is None or hashlib.sha256(record_raw).hexdigest() != head['record']:
        raise ValueError('CCRD transaction head record is missing or altered')
    value = _record(record_raw)
    if value['transaction'] != head['transaction'] or value['sequence'] != head['sequence']:
        raise ValueError('CCRD transaction head and record differ')
    intent = store.read_intent(_intent_key(value['transaction'], value['durable_intent_sha256']))
    if intent is None or hashlib.sha256(intent).hexdigest() != value['durable_intent_sha256']:
        raise ValueError('CCRD durable transaction intent is missing or altered')
    _intent(intent, value)
    if value['evidence_sha256'] is not None:
        evidence = store.read_evidence(_evidence_key(value['transaction'], value['evidence_sha256']))
        if evidence is None or hashlib.sha256(evidence).hexdigest() != value['evidence_sha256']:
            raise ValueError('CCRD transaction evidence is missing or altered')
        _activation_evidence(evidence, value, value['space_revision'])
    if value['rollback_evidence_sha256'] is not None:
        evidence = store.read_evidence(_evidence_key(value['transaction'], value['rollback_evidence_sha256']))
        if evidence is None or hashlib.sha256(evidence).hexdigest() != value['rollback_evidence_sha256']:
            raise ValueError('CCRD rollback evidence is missing or altered')
        _rollback_evidence(evidence, value, value['rollback_revision'])
    if value['prior'] is not None:
        prior_raw = store.read_record(_record_key(head['transaction'], value['prior']))
        if prior_raw is None or hashlib.sha256(prior_raw).hexdigest() != value['prior']:
            raise ValueError('CCRD prior transaction record is missing or altered')
        _advance(_record(prior_raw), value, value['prior'])
    return Current(head, raw, etag, value, record_raw)


def audit(store: Store, current: Current) -> int:
    """Verify the complete bounded immutable record chain and activation evidence."""
    value = current.record
    count = 1
    while value['prior'] is not None:
        prior_raw = store.read_record(_record_key(value['transaction'], value['prior']))
        if prior_raw is None or hashlib.sha256(prior_raw).hexdigest() != value['prior']:
            raise ValueError('CCRD transaction history is missing or altered')
        prior = _record(prior_raw)
        _advance(prior, value, value['prior'])
        count += 1
        if count > max(max(values) for values in STATE_SEQUENCE.values()) + 1:
            raise ValueError('CCRD transaction history exceeds state bounds')
        value = prior
    if value['sequence'] != 0 or value['state'] != 'prepared':
        raise ValueError('CCRD transaction does not have an exact prepared root')
    return count


def _commit(store: Store, value: dict, previous: Current | None) -> Current:
    raw = lifecycle.canonical(value); _record(raw)
    if previous is None:
        if value['sequence'] != 0 or value['state'] != 'prepared':
            raise ValueError('first CCRD transaction record must be prepared')
    else:
        _advance(previous.record, value, previous.head['record'])
    digest = hashlib.sha256(raw).hexdigest(); key = _record_key(value['transaction'], digest)
    observed = store.read_record(key)
    if observed is None:
        try:
            store.put_record(key, raw)
        except Exception:
            observed = store.read_record(key)
            if observed != raw: raise
        else:
            observed = store.read_record(key)
    if observed != raw: raise ValueError('CCRD transaction immutable record conflicts')
    head = lifecycle.canonical({'version': 1, 'kind': 'ccrd-cw0-transaction-head',
                                'transaction': value['transaction'], 'sequence': value['sequence'], 'record': digest})
    try:
        store.put_head(head, previous.etag if previous else None)
    except Exception:
        actual = load(store)
        if actual is not None and actual.head_raw == head: return actual
        raise
    actual = load(store)
    if actual is None or actual.head_raw != head:
        raise RuntimeError('CCRD transaction head readback differs')
    return actual


def begin(store: Store, *, parent_sha256: str, candidate_generation: str, candidate_manifest_sha256: str,
          activation_plan_sha256: str, parent_space_revision: str, durable_intent: bytes,
          transaction: str | None = None) -> Current:
    if load(store) is not None:
        raise ValueError('a CCRD transaction head already exists; inspect it before starting')
    transaction = transaction or uuid.uuid4().hex
    _match(transaction, ID, 'transaction ID')
    identity = {'parent_sha256': parent_sha256, 'candidate_generation': candidate_generation,
                'candidate_manifest_sha256': candidate_manifest_sha256, 'activation_plan_sha256': activation_plan_sha256,
                'parent_space_revision': parent_space_revision}
    _intent(durable_intent, identity)
    digest = hashlib.sha256(durable_intent).hexdigest(); key = _intent_key(transaction, digest)
    existing = store.read_intent(key)
    if existing is None:
        try:
            store.put_intent(key, durable_intent)
        except Exception:
            existing = store.read_intent(key)
            if existing != durable_intent: raise
        else:
            existing = store.read_intent(key)
    if existing != durable_intent: raise ValueError('CCRD durable intent immutable conflict')
    value = {'version': 1, 'kind': 'ccrd-cw0-transaction-record', 'transaction': transaction,
             'sequence': 0, 'prior': None, 'state': 'prepared', 'parent_sha256': parent_sha256,
             'candidate_generation': candidate_generation, 'candidate_manifest_sha256': candidate_manifest_sha256,
             'activation_plan_sha256': activation_plan_sha256, 'durable_intent_sha256': digest,
             'parent_space_revision': parent_space_revision,
             'space_revision': None, 'evidence_sha256': None,
             'rollback_revision': None, 'rollback_evidence_sha256': None}
    return _commit(store, value, None)


def _store_evidence(store: Store, transaction: str, evidence: bytes) -> str:
    digest = hashlib.sha256(evidence).hexdigest(); key = _evidence_key(transaction, digest)
    old = store.read_evidence(key)
    if old is None:
        try:
            store.put_evidence(key, evidence)
        except Exception:
            old = store.read_evidence(key)
            if old != evidence: raise
        else:
            old = store.read_evidence(key)
    if old != evidence: raise ValueError('CCRD immutable transaction evidence conflicts')
    return digest


def advance(store: Store, current: Current, state: str, *, space_revision: str | None = None,
            evidence: bytes | None = None) -> Current:
    """Internal state primitive; callers must independently verify observations.

    This function is not a public activation/rollback command. The orchestration
    layer is responsible for fresh remote evidence before invoking it.
    """
    active = load(store)
    if active is None or active.head_raw != current.head_raw or active.etag != current.etag:
        raise ValueError('CCRD transaction head changed before advancing')
    audit(store, active)
    value = {**current.record, 'sequence': current.record['sequence'] + 1,
             'prior': current.head['record'], 'state': state}
    if current.record['state'] == 'space-intent' and state == 'space-verified':
        _activation_evidence(evidence, current.record, space_revision)
        digest = _store_evidence(store, current.record['transaction'], evidence)
        value.update(space_revision=space_revision, evidence_sha256=digest)
    elif current.record['state'] == 'rollback-intent' and state == 'rolled-back':
        _rollback_evidence(evidence, current.record, space_revision)
        digest = _store_evidence(store, current.record['transaction'], evidence)
        value.update(rollback_revision=space_revision, rollback_evidence_sha256=digest)
    elif space_revision is not None or evidence is not None:
        raise ValueError('CCRD evidence can only be recorded after its corresponding intent')
    return _commit(store, value, current)
