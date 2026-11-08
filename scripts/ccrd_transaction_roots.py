"""Fail-closed retention roots for an in-flight CCRD CW:0 transaction.

The durable intent contains both full manifests.  A graph must retain the
union of their qualified resources; malformed or incomplete journal objects
must block collection rather than silently discard provenance.
"""
from __future__ import annotations

import json
import re

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_transaction as transaction
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_transaction as transaction


PATH = re.compile(r'control/cw0-publications/([0-9a-f]{32})/(records|evidence|intents)/([0-9a-f]{64})\.json\Z')


def _json_bytes(value):
    return lifecycle.canonical(value)


def _intent_references(value: dict, path: str) -> list[dict]:
    match = PATH.fullmatch(path)
    if not match or match.group(2) != 'intents':
        raise ValueError('invalid CCRD durable intent path')
    raw = _json_bytes(value)
    import hashlib
    if hashlib.sha256(raw).hexdigest() != match.group(3):
        raise ValueError('CCRD durable intent content address differs')
    try:
        parent_raw = bytes.fromhex(value['parent_pointer_hex'])
        candidate_raw = bytes.fromhex(value['candidate_manifest_hex'])
        plan = value['activation_plan']
        parent_sha = hashlib.sha256(parent_raw).hexdigest()
        candidate_sha = hashlib.sha256(candidate_raw).hexdigest()
        candidate = json.loads(candidate_raw)
        identity = {'parent_sha256': parent_sha, 'candidate_generation': candidate['generation'],
                    'candidate_manifest_sha256': candidate_sha, 'activation_plan_sha256': plan['plan_sha256'],
                    'parent_space_revision': plan['parent_revision']}
        transaction._intent(raw, identity)
        refs = [*lifecycle.references(json.loads(parent_raw), 'current.json'),
                *lifecycle.references(candidate, 'current.json')]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid CCRD durable intent retention roots') from error
    # The immutable generation manifest itself is a rollback/activation root.
    refs.append({'bucket': lifecycle.BUCKET,
                 'path': f"generations/{candidate['generation']}/manifest.json",
                 'bytes': len(candidate_raw), 'sha256': candidate_sha, 'role': 'runtime'})
    unique = {}
    for ref in refs:
        key = (ref['bucket'], ref['path'])
        old = unique.get(key)
        if old is not None and (old['sha256'], old['bytes']) != (ref['sha256'], ref['bytes']):
            raise ValueError('conflicting CCRD transaction retained object identities')
        unique[key] = ref
    return list(unique.values())


def references(value: dict, path: str) -> list[dict] | None:
    if path == transaction.HEAD:
        transaction._head(_json_bytes(value))
        return []  # Every immutable intent is also a control root.
    match = PATH.fullmatch(path)
    if not match:
        return None
    transaction_id, kind, digest = match.groups()
    import hashlib
    raw = _json_bytes(value)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('CCRD journal content address differs')
    if kind == 'intents':
        return _intent_references(value, path)
    if kind == 'records':
        record = transaction._record(raw)
        if record['transaction'] != transaction_id:
            raise ValueError('CCRD journal record transaction differs from path')
        return []
    if (not isinstance(value, dict) or value.get('kind') not in {'ccrd-activation-evidence', 'ccrd-rollback-evidence'}):
        raise ValueError('unknown CCRD journal evidence')
    return []
