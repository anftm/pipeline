"""Validate exact deletion receipts without treating 404 as isolation.

This module is a read-only building block for a future versioned fence. An
authenticated DELETE receipt and post-delete observations can prove the named
repository was removed at that time; they do not prove external tokens were
revoked or authorize GC. The v1 fence does not use this module yet.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

try:
    from . import reader_producer_scope_transition as scope
except ImportError:
    import reader_producer_scope_transition as scope


FIELDS = {'version', 'kind', 'repository', 'repository_id', 'owner', 'git_bundle_sha256',
          'actions_backup_sha256', 'intent_sha256', 'deleted_at', 'delete_http',
          'owner_http', 'owner_matches', 'name_get_http', 'id_get_http'}
HEX = re.compile(r'[0-9a-f]{64}')
MAX_RECEIPT_BYTES = 32 * 1024


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError('invalid repository deletion time')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('invalid repository deletion time') from error
    if result.tzinfo is None:
        raise ValueError('repository deletion time requires timezone')
    return result.astimezone(timezone.utc)


def verify(raw, expected_repository, expected_id, *, now=None, expected_sha256=None):
    """Authenticate exact receipt structure and optionally its content address.

    The caller must obtain raw bytes from a trusted append-only provenance; a
    SHA, JSON, 204, or pair of 404s alone can be fabricated. This function has
    no network access, does not verify source credentials and grants no rights.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError('invalid repository deletion receipt bytes')
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and (not HEX.fullmatch(str(expected_sha256)) or expected_sha256 != digest):
        raise ValueError('repository deletion receipt hash differs')
    try:
        value = json.loads(raw, object_pairs_hook=scope.unique_object)
    except (ValueError, TypeError) as error:
        raise ValueError('invalid repository deletion receipt') from error
    expected_identities = {**scope.EXPECTED, **scope.ADDITIONAL}
    if (not isinstance(expected_repository, str) or type(expected_id) is not int
            or expected_identities.get(expected_repository) != expected_id
            or not isinstance(value, dict) or set(value) != FIELDS
            or type(value.get('version')) is not int or value.get('version') != 1
            or value.get('kind') != 'retired-pipeline-repository-deletion'
            or value.get('repository') != expected_repository or type(value.get('repository_id')) is not int
            or value.get('repository_id') != expected_id
            or value.get('owner') != expected_repository.split('/')[0]
            or type(value.get('delete_http')) is not int or value.get('delete_http') != 204
            or type(value.get('owner_http')) is not int or value.get('owner_http') != 200
            or value.get('owner_matches') is not True
            or type(value.get('name_get_http')) is not int or value.get('name_get_http') != 404
            or type(value.get('id_get_http')) is not int or value.get('id_get_http') != 404
            or any(not isinstance(value.get(key), str) or not HEX.fullmatch(value[key]) for key in
                   ('git_bundle_sha256', 'actions_backup_sha256', 'intent_sha256'))):
        raise ValueError('repository deletion receipt identity or evidence differs')
    deleted = parse_time(value['deleted_at'])
    clock = now or datetime.now(timezone.utc)
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        raise ValueError('repository deletion receipt clock needs timezone')
    if deleted > clock.astimezone(timezone.utc) + timedelta(minutes=5):
        raise ValueError('repository deletion receipt is dated in the future')
    return {'sha256': digest, 'repository': expected_repository, 'repository_id': expected_id,
            'deleted_at': deleted.isoformat(), 'deletion_only': True,
            'writer_isolation_verified': False, 'gc_authorized': False}


def require_isolated(receipts, external_writer_proof):
    """No implementation may equate historical deletion with writer fencing."""
    raise RuntimeError('retired producer external-writer isolation is not verified')
