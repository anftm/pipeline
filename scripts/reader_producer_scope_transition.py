"""Versioned producer-scope transition, prepared offline and inactive.

The live v1 inventory remains authoritative. Deletion or a 404 is not proof
that external writers are gone. This module defines a strict transition record
and immutable journal binding, but has no production activation path: it must
raise until the outstanding writer and repository identities are independently
verified and a separately reviewed activation protocol is implemented.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re

try:
    from . import reader_producer_scope as legacy
except ImportError:
    import reader_producer_scope as legacy


ROOT = Path(__file__).resolve().parents[1]
TRANSITION = ROOT / 'config/producer-isolation-scope-v2.json'
KIND = 'reader-producer-isolation-scope-transition'
JOURNAL_FIELD = 'producer_scope'
EXPECTED = {
    'alicetran68/pipeline': 1412089542,
    'ambrossee768/pdf-pipeline': 1412104354,
    'brodievsalas/pipeline': 1412089517,
    'dellamcastillo/pipeline': 1412089483,
    'devondunn7/pipeline': 1412088148,
    'rioholland79/pipeline': 1412088129,
    'vomebook/pdf-pipeline': 1412104331,
}
ADDITIONAL = {'ambrossee768/pipeline': 1401656391, 'vomebook/pipeline': 1390570183}
FIELDS = {'version', 'kind', 'status', 'publisher_repository', 'publisher_repository_id',
          'legacy_scope_sha256', 'legacy_repositories', 'retired_repository_ids',
          'additional_repository_ids', 'future_active_repositories', 'activation_receipt'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate producer scope field')
        result[key] = value
    return result


def prepare(path=TRANSITION, legacy_path=legacy.SCOPE):
    """Load the entire fixed transition and bind it to exact v1 bytes."""
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError) as error:
        raise ValueError('invalid producer scope transition') from error
    legacy_raw = Path(legacy_path).read_bytes()
    v1 = legacy.load_config(legacy_path)
    if (not isinstance(value, dict) or set(value) != FIELDS or value.get('version') != 2
            or value.get('kind') != KIND or value.get('status') != 'prepared-not-active'
            or value.get('publisher_repository') != 'anftm/pipeline'
            or type(value.get('publisher_repository_id')) is not int
            or value.get('publisher_repository_id') != 1260985830
            or value.get('legacy_scope_sha256') != sha(legacy_raw)
            or value.get('legacy_repositories') != v1['repositories']
            or value.get('retired_repository_ids') != EXPECTED
            or any(type(item) is not int for item in value.get('retired_repository_ids', {}).values())
            or value.get('additional_repository_ids') != ADDITIONAL
            or any(type(item) is not int for item in value.get('additional_repository_ids', {}).values())
            or value.get('future_active_repositories') != ['anftm/pipeline']
            or value.get('activation_receipt') is not None):
        raise ValueError('producer scope transition identity or activation differs')
    return {'version': 2, 'sha256': sha(raw), 'legacy_sha256': sha(legacy_raw),
            'status': value['status'], 'value': copy.deepcopy(value)}


def journal_binding(path=legacy.SCOPE, *, config=None):
    """Bind new v1 windows to the exact configuration they actually used."""
    # Parse and hash the same read. Two reads could bind old bytes to a newer
    # inventory if a checkout changes between them.
    raw = Path(path).read_bytes()
    try:
        parsed = json.loads(raw, object_pairs_hook=unique_object)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid producer scope binding configuration') from error
    # Reuse the v1 validator without using its separate read as the hash input;
    # reject even an otherwise valid file if it changed during validation.
    checked = legacy.load_config(path)
    if parsed != checked or Path(path).read_bytes() != raw:
        raise RuntimeError('producer scope changed during maintenance journal binding')
    if config is not None and parsed != config:
        raise RuntimeError('producer scope changed before maintenance journal binding')
    return {'version': 1, 'sha256': sha(raw), 'repositories': sorted(parsed['repositories'])}


def validate_journal_binding(state, *, expected=None, require_complete=True):
    """Validate bound windows; legacy journals without this field are unchanged.

    This does not infer old journal scope from current config and cannot convert
    a 404 into restoration. Every v1 recovery still uses the recorded repository
    names, workflow IDs, and operations in the existing maintenance journal.
    """
    if not isinstance(state, dict):
        raise ValueError('invalid producer maintenance journal')
    if JOURNAL_FIELD not in state:
        return None
    binding = state[JOURNAL_FIELD]
    if (not isinstance(binding, dict) or set(binding) != {'version', 'sha256', 'repositories'}
            or type(binding.get('version')) is not int or binding.get('version') != 1
            or not re.fullmatch(r'[0-9a-f]{64}', str(binding.get('sha256', '')))
            or not isinstance(binding.get('repositories'), list) or not binding['repositories']
            or any(not isinstance(repo, str) or not legacy.REPOSITORY.fullmatch(repo)
                   for repo in binding['repositories'])
            or binding['repositories'] != sorted(set(binding['repositories']))
            or len({repo.lower() for repo in binding['repositories']}) != len(binding['repositories'])
            or not isinstance(state.get('repositories'), dict)):
        raise ValueError('invalid producer scope journal binding')
    recorded = set(state['repositories'])
    bound = set(binding['repositories'])
    inventory_validated = state.get('producer_scope_inventory_validated', False)
    if type(inventory_validated) is not bool:
        raise ValueError('invalid producer scope inventory state')
    # A failed preflight or interruption during isolation may restore only the
    # records actually saved. The subset must never expand during recovery.
    if not recorded <= bound or require_complete and recorded != bound:
        raise ValueError('producer scope journal repository set differs')
    if expected is not None and binding != expected:
        raise ValueError('producer scope journal binding changed')
    return copy.deepcopy(binding)


def require_activated(_candidate):
    """A prepared file or individual deletion receipt can never unlock GC."""
    raise RuntimeError('producer scope v2 is not activated; retain the legacy fence')
