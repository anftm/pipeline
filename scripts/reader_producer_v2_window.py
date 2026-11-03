"""Provisional v2 maintenance-window binding; does not activate a GC scope.

This module tests how a future collector would bind to its exact central-only
maintenance window and durable v2 transition. It intentionally provides no
production opt-in until source credential risks are separately addressed.
"""
from __future__ import annotations

import hashlib
import json
import re

try:
    from . import reader_producer_scope_transition as scope
except ImportError:
    import reader_producer_scope_transition as scope


CENTRAL = 'anftm/pipeline'
HEX32 = re.compile(r'[0-9a-f]{32}')
HEX64 = re.compile(r'[0-9a-f]{64}')
FIELDS = {'version', 'scope_sha256', 'audit_candidate_sha256', 'repositories', 'central_repository_id'}


def binding(scope_sha256, candidate_sha256):
    if not HEX64.fullmatch(str(scope_sha256)) or not HEX64.fullmatch(str(candidate_sha256)):
        raise ValueError('invalid v2 maintenance scope digest')
    return {'version': 2, 'scope_sha256': scope_sha256, 'audit_candidate_sha256': candidate_sha256,
            'repositories': [CENTRAL], 'central_repository_id': 1260985830}


def require_window(raw, *, expected_maintenance_id, expected_binding):
    """Prove the exact window was journaled, isolated and not restored yet.

    Current GitHub workflow/run checks and four-bucket closure are separate.
    This function does not inspect token access and grants no deletion rights.
    """
    if not isinstance(raw, bytes) or len(raw) > 4 * 1024 * 1024:
        raise ValueError('invalid v2 maintenance window bytes')
    try:
        state = json.loads(raw, object_pairs_hook=scope.unique_object)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid v2 maintenance window') from error
    if (not isinstance(expected_binding, dict) or set(expected_binding) != FIELDS
            or expected_binding != binding(expected_binding.get('scope_sha256'), expected_binding.get('audit_candidate_sha256'))
            or not isinstance(expected_maintenance_id, str) or not HEX32.fullmatch(expected_maintenance_id)
            or expected_maintenance_id == '0' * 32 or not isinstance(state, dict)
            or state.get('kind') != 'reader-maintenance-journal' or state.get('version') != 1
            or state.get('maintenance_id') != expected_maintenance_id
            or state.get('producer_scope_v2') != expected_binding
            or state.get('producer_scope_inventory_validated') is not True
            or state.get('producers_isolated') is not True
            or state.get('restored') is True or state.get('restore_errors')
            or state.get('callback_recovery_required') is True
            or state.get('callback_mutation_started') is True and state.get('callback_committed') is not True
            or not isinstance(state.get('repositories'), dict) or set(state['repositories']) != {CENTRAL}):
        raise RuntimeError('v2 maintenance window is not isolated and bound')
    record = state['repositories'][CENTRAL]
    if (not isinstance(record, dict) or record.get('restored') is True
            or record.get('permission_changed') is True
            or not isinstance(record.get('workflows'), list) or not record['workflows']
            or not isinstance(record.get('cancelled'), list) or record['cancelled']):
        raise RuntimeError('v2 central maintenance record differs')
    return {'maintenance_id': expected_maintenance_id, 'window_sha256': hashlib.sha256(raw).hexdigest(),
            'scope_sha256': expected_binding['scope_sha256'], 'external_writers_fenced': False,
            'gc_authorized': False}
