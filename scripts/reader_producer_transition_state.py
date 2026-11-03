"""Pure fail-closed migration state model; does not write or activate a fence.

Each candidate is content-bound and immutable. A mutable current pointer, if
implemented later, must be protected by conditional writes and an independent
lock; this module deliberately supplies neither a storage writer nor an active
GC scope. Valid deletion receipts alone never prove source-token retirement.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

try:
    from . import reader_producer_scope_transition as scope
    from . import reader_retired_repository_receipts as receipts
except ImportError:
    import reader_producer_scope_transition as scope
    import reader_retired_repository_receipts as receipts


FIELDS = {'version', 'kind', 'phase', 'transition_sha256', 'legacy_scope_sha256',
          'deleted_receipts', 'additional_repository_ids', 'source_writer_proofs'}
EXTENDED_FIELDS = FIELDS | {'parent_candidate_sha256', 'additional_deleted_receipts'}
HEX = re.compile(r'[0-9a-f]{64}')


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def create(receipt_raws, *, prepared=None, now=None):
    """Bind exactly seven authenticated-deletion candidates and both extra forks.

    Raw receipt provenance remains the caller's responsibility. No activation
    occurs, and the returned candidate is not a producer-isolation proof.
    """
    prepared = prepared or scope.prepare()
    if (not isinstance(prepared, dict) or prepared.get('status') != 'prepared-not-active'
            or not HEX.fullmatch(str(prepared.get('sha256', '')))
            or not HEX.fullmatch(str(prepared.get('legacy_sha256', '')))
            or prepared.get('value', {}).get('retired_repository_ids') != scope.EXPECTED
            or prepared.get('value', {}).get('additional_repository_ids') != scope.ADDITIONAL):
        raise ValueError('prepared producer transition differs')
    if not isinstance(receipt_raws, dict) or set(receipt_raws) != set(scope.EXPECTED):
        raise ValueError('deletion receipt inventory is incomplete')
    found = {}
    for repository, repository_id in scope.EXPECTED.items():
        verified = receipts.verify(receipt_raws[repository], repository, repository_id, now=now)
        found[repository] = {'repository_id': repository_id, 'sha256': verified['sha256']}
    value = {'version': 1, 'kind': 'reader-producer-scope-migration', 'phase': 'prepared',
             'transition_sha256': prepared['sha256'], 'legacy_scope_sha256': prepared['legacy_sha256'],
             'deleted_receipts': found, 'additional_repository_ids': copy.deepcopy(scope.ADDITIONAL),
             'source_writer_proofs': None}
    validate(value, prepared=prepared)
    return value


def validate(value, *, prepared=None):
    prepared = prepared or scope.prepare()
    if (not isinstance(value, dict) or set(value) != FIELDS or type(value.get('version')) is not int
            or value.get('version') != 1 or value.get('kind') != 'reader-producer-scope-migration'
            or value.get('phase') != 'prepared'
            or value.get('transition_sha256') != prepared.get('sha256')
            or value.get('legacy_scope_sha256') != prepared.get('legacy_sha256')
            or value.get('additional_repository_ids') != scope.ADDITIONAL
            or value.get('source_writer_proofs') is not None
            or not isinstance(value.get('deleted_receipts'), dict)
            or set(value['deleted_receipts']) != set(scope.EXPECTED)):
        raise ValueError('invalid or unauthorized producer scope migration')
    for name, entry in value['deleted_receipts'].items():
        if (not isinstance(entry, dict) or set(entry) != {'repository_id', 'sha256'}
                or type(entry.get('repository_id')) is not int or entry['repository_id'] != scope.EXPECTED[name]
                or not isinstance(entry.get('sha256'), str) or not HEX.fullmatch(entry['sha256'])):
            raise ValueError('producer migration deletion receipt differs: ' + name)
    return copy.deepcopy(value)


def add_additional_deletions(previous, additional_raws, *, prepared=None, now=None):
    """Append two exact deletion receipts; preserve the previous candidate."""
    prepared = prepared or scope.prepare()
    validate(previous, prepared=prepared)
    if not isinstance(additional_raws, dict) or set(additional_raws) != set(scope.ADDITIONAL):
        raise ValueError('additional deletion receipt inventory is incomplete')
    found = {}
    for repository, repository_id in scope.ADDITIONAL.items():
        verified = receipts.verify(additional_raws[repository], repository, repository_id, now=now)
        found[repository] = {'repository_id': repository_id, 'sha256': verified['sha256']}
    result = {**copy.deepcopy(previous), 'parent_candidate_sha256': digest(previous),
              'additional_deleted_receipts': found}
    validate_extended(result, previous, prepared=prepared)
    return result


def validate_extended(value, previous, *, prepared=None):
    prepared = prepared or scope.prepare()
    validate(previous, prepared=prepared)
    if (not isinstance(value, dict) or set(value) != EXTENDED_FIELDS
            or value.get('parent_candidate_sha256') != digest(previous)
            or {key: value[key] for key in FIELDS} != previous
            or not isinstance(value.get('additional_deleted_receipts'), dict)
            or set(value['additional_deleted_receipts']) != set(scope.ADDITIONAL)):
        raise ValueError('additional deletion transition differs')
    for name, entry in value['additional_deleted_receipts'].items():
        if (not isinstance(entry, dict) or set(entry) != {'repository_id', 'sha256'}
                or type(entry.get('repository_id')) is not int or entry['repository_id'] != scope.ADDITIONAL[name]
                or not isinstance(entry.get('sha256'), str) or not HEX.fullmatch(entry['sha256'])):
            raise ValueError('additional deletion receipt differs: ' + name)
    return copy.deepcopy(value)


def verify_candidate_receipts(value, raw_receipts, *, prepared=None, now=None):
    """Re-read every content-addressed receipt; state hashes alone are not proof."""
    prepared = prepared or scope.prepare()
    all_ids = {**scope.EXPECTED, **scope.ADDITIONAL}
    if not isinstance(raw_receipts, dict) or set(raw_receipts) != set(all_ids):
        raise ValueError('complete receipt bytes are required')
    if not isinstance(value, dict) or set(value) != EXTENDED_FIELDS:
        raise ValueError('extended producer migration is required')
    previous = {key: value[key] for key in FIELDS}
    validate_extended(value, previous, prepared=prepared)
    for name, repo_id in all_ids.items():
        entry = (value['deleted_receipts'] if name in scope.EXPECTED else value['additional_deleted_receipts'])[name]
        receipts.verify(raw_receipts[name], name, repo_id, now=now, expected_sha256=entry['sha256'])
    return {'kind': 'reader-producer-migration-receipts-check', 'candidate_sha256': digest(value),
            'receipt_count': len(all_ids), 'writer_isolation_verified': False, 'gc_authorized': False}


def advance(_state, _target, **_proofs):
    """No amount of caller-supplied booleans can commit or authorize v2."""
    raise RuntimeError('producer scope migration cannot advance without reviewed source-writer isolation')
