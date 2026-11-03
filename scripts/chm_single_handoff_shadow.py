#!/usr/bin/env python3
"""Pure offline model of part of one interrupted CHM handoff.

This has no storage adapter, command-line entry point or production writer. Its
states describe observations, not permission to resume. In particular, a
single-key ETag is not an external writer fence, a multi-object transaction, or
proof against an ABA change. v3 publication, consumers and recovery of real
partial writes are deliberately outside this incomplete model.
"""

import copy
import hashlib
import json
import re
from datetime import datetime

try:
    from . import chm_single_interrupted_preflight as fixed, reader_lifecycle as lifecycle
except ImportError:
    import chm_single_interrupted_preflight as fixed
    import reader_lifecycle as lifecycle


KIND = 'chm-single-handoff-shadow-plan'
SCOPE = {'sidecar', 'catalog', 'v3_pointer', 'processing', 'legacy_pins', 'etags', 'guards'}
ETAG_KEYS = {'sidecar', 'catalog', 'v3_pointer', 'processing'}
GUARD_KEYS = {'chm_index_sha256', 'maintenance_sha256', 'retirement_hints_sha256'}
HEX = re.compile(r'[0-9a-f]{64}\Z')
PIN_PATH = re.compile(r'reader-index/processing/stream-upload-[0-9a-f]{32}\.json\Z')
# Historical read-only audit: sorted path/owner/ETag rows, not proof that the
# same pins still exist now. Live data must be independently obtained afresh.
LEGACY_PIN_IDENTITY_SHA256 = 'd49b4ea246ee03c88d5419f257305f322e98344a537b6567cb5ec692bf3adf1f'


def digest(value):
    """Hash JSON values only; raw processing bytes are bound separately."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def strong_etag(value):
    return isinstance(value, str) and re.fullmatch(r'"[\x21\x23-\x7e]+"', value) is not None


def legacy_pin_identity(pins):
    rows = [{'path': path, 'etag': pin['etag'], 'key': pin['owner_key']}
            for path, pin in sorted(pins.items())]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _references(items):
    if not isinstance(items, list):
        raise ValueError('invalid catalog references')
    result = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'bucket', 'path'}
                or not isinstance(item['bucket'], str) or not item['bucket']
                or not isinstance(item['path'], str) or not item['path']):
            raise ValueError('invalid catalog reference')
        result.add((item['bucket'], item['path']))
    return result


def legacy_catalog(before, sidecar, now):
    """Propose a generation, retaining all old entries and parent references."""
    if (not isinstance(before, dict) or before.get('version') != 1
            or before.get('kind') != 'reader-catalog' or not isinstance(before.get('generations'), dict)
            or not isinstance(before.get('current'), dict) or not isinstance(now, str) or not now):
        raise ValueError('invalid catalog parent')
    try:
        timestamp = datetime.fromisoformat(now.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('invalid catalog timestamp') from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError('catalog timestamp must have a timezone')
    current = before['current'].get('generation')
    generations = before['generations']
    if (not isinstance(current, str) or current not in generations
            or not isinstance(generations[current], dict)
            or generations[current].get('generation') != current
            or generations[current].get('status') != 'current'
            or sum(isinstance(entry, dict) and entry.get('status') == 'current'
                   for entry in generations.values()) != 1):
        raise ValueError('catalog parent generation differs')
    # Collector streaming may elide reference arrays or install gc_stub
    # markers. Those reduced views must never stand in for complete history.
    if any(not isinstance(entry, dict) or entry.get('generation') != name
           or 'gc_stub' in entry or not isinstance(entry.get('references'), list)
           for name, entry in generations.items()):
        raise ValueError('catalog history is not complete')
    for entry in generations.values():
        _references(entry['references'])
    retired = before.get('retired_generations', {})
    if not isinstance(retired, dict):
        raise ValueError('invalid retired catalog history')
    if not isinstance(sidecar, dict) or sidecar.get('v') != 1 or not isinstance(sidecar.get('f'), dict):
        raise ValueError('invalid proposed sidecar')
    # A sidecar alone is not necessarily the complete reference graph. Do not
    # silently remove any parent references while exploring this proposal.
    refs = sorted(_references(generations[current].get('references'))
                  | _references(lifecycle.sidecar_references(sidecar)))
    sidecar_hash = digest(sidecar)
    signature = lifecycle._generation_id({'sidecar_sha256': sidecar_hash, 'references': refs}, None)
    generation = lifecycle._generation_id({'signature': signature}, current)
    if generation in generations or generation in retired:
        raise ValueError('new catalog generation already exists')
    result = copy.deepcopy(before)
    old = result['generations'][current]
    old['status'] = 'superseded'
    old['superseded_at'] = now
    result['generations'][generation] = {
        'generation': generation, 'status': 'current', 'published_at': now,
        'signature': signature, 'sidecar_sha256': sidecar_hash, 'parent_generation': current,
        'references': [{'bucket': bucket, 'path': path} for bucket, path in refs],
        'acks': {'hf': False, 'pages': False},
    }
    result['current'] = {'generation': generation, 'published_at': now}
    return result, generation


def _normalize(state):
    if not isinstance(state, dict) or set(state) != SCOPE:
        raise ValueError('shadow input scope differs')
    raw = state['processing']
    if type(raw) is not bytes:
        raise ValueError('processing protection must be original bytes')
    fixed.validate_processing(raw)
    etags = state['etags']
    if (not isinstance(etags, dict) or set(etags) != ETAG_KEYS
            or not all(strong_etag(value) for value in etags.values())):
        raise ValueError('control ETags must be strong and complete')
    guards = state['guards']
    if (not isinstance(guards, dict) or set(guards) != GUARD_KEYS
            or not all(isinstance(value, str) and HEX.fullmatch(value) for value in guards.values())):
        raise ValueError('guard inventory differs')
    if (guards['chm_index_sha256'] != fixed.INDEX_SHA256
            or guards['maintenance_sha256'] != fixed.MAINTENANCE_SHA256):
        raise ValueError('pinned CHM index or maintenance journal differs')
    pins = state['legacy_pins']
    if (not isinstance(pins, dict) or len(pins) != 51 or fixed.PROCESSING in pins
            or any(not isinstance(path, str) or not PIN_PATH.fullmatch(path)
                   or not isinstance(value, dict) or set(value) != {'raw', 'etag', 'owner'}
                   or type(value['raw']) is not bytes
                   or not strong_etag(value['etag'])
                   or not isinstance(value['owner'], dict)
                   or not isinstance(value['owner'].get('key'), str) or not value['owner']['key']
                   or value['owner'].get('processing_roots') != [path]
                   for path, value in pins.items())):
        raise ValueError('legacy processing pin inventory differs')
    normalized_pins = {}
    for path, value in pins.items():
        try:
            record = json.loads(value['raw'])
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError('invalid legacy processing record') from error
        if (not isinstance(record, dict) or record.get('version') != 1
                or record.get('kind') != 'reader-processing-roots' or record.get('status') != 'processing'
                or record.get('operation') != 'reader-stream-upload'
                or record.get('release_policy') != 'explicit-publisher-handoff'
                or record.get('run') != {'repository': 'anftm/pipeline', 'run_id': '41172648658', 'attempt': '1'}
                or not isinstance(record.get('resources'), list) or len(record['resources']) != 1):
            raise ValueError('legacy processing record identity differs')
        lifecycle.validate_processing_handoff(record, value['owner'])
        normalized_pins[path] = {'sha256': hashlib.sha256(value['raw']).hexdigest(),
                                 'bytes': len(value['raw']), 'etag': value['etag'],
                                 'owner_key': value['owner']['key']}
    if legacy_pin_identity(normalized_pins) != LEGACY_PIN_IDENTITY_SHA256:
        raise ValueError('legacy processing pin identity differs')
    pointer = state['v3_pointer']
    if not isinstance(pointer, dict) or pointer.get('generation') != fixed.PARENT_V3:
        raise ValueError('v3 parent differs')
    result = copy.deepcopy({key: value for key, value in state.items() if key != 'legacy_pins'})
    result['processing'] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    result['legacy_pins'] = normalized_pins
    return result


def make_plan(before, new_sidecar, now):
    normalized = _normalize(before)
    fixed.validate_route_change(normalized['sidecar'], new_sidecar)
    if ({key: value for key, value in normalized['sidecar'].items() if key != 'f'}
            != {key: value for key, value in new_sidecar.items() if key != 'f'}):
        raise ValueError('sidecar metadata changed')
    after_catalog, generation = legacy_catalog(normalized['catalog'], new_sidecar, now)
    after = {**copy.deepcopy(normalized), 'sidecar': copy.deepcopy(new_sidecar), 'catalog': after_catalog}
    identity = {'kind': KIND, 'version': 1, 'before': normalized, 'after': after,
                'base_generation': generation, 'timestamp': now}
    return {**identity, 'plan_sha256': digest(identity)}


def inspect(plan, current):
    """Classify exact observations only; every result forbids real recovery.

    ETag equality is necessary only for unchanged modeled controls. A changed
    control's ETag has no expected value in this model: changed content with the
    old ETag is suspicious, but a different ETag does not prove exact serialized
    bytes, authorship, isolation, or a successful conditional write.
    """
    if (not isinstance(plan, dict) or set(plan) != {'kind', 'version', 'before', 'after',
                                                   'base_generation', 'timestamp', 'plan_sha256'}
            or plan.get('kind') != KIND or plan.get('version') != 1):
        raise ValueError('invalid shadow plan')
    body = {key: value for key, value in plan.items() if key != 'plan_sha256'}
    if digest(body) != plan['plan_sha256']:
        raise ValueError('shadow plan checksum differs')
    observed = _normalize(current)
    # A checksum is not authority: reconstruct the proposal and reject even a
    # self-consistently rehashed plan if its semantics or scope were changed.
    try:
        proposed_before = copy.deepcopy(plan['before'])
        proposed_before['processing'] = current['processing']
        proposed_before['legacy_pins'] = current['legacy_pins']
        expected = make_plan(proposed_before, plan['after']['sidecar'], plan['timestamp'])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('shadow plan semantics differ') from error
    if expected != plan:
        raise ValueError('shadow plan semantics differ')
    before, after = plan['before'], plan['after']
    if any(observed[key] != before[key] for key in ('processing', 'legacy_pins', 'v3_pointer', 'guards')):
        raise ValueError('processing protections, guards or v3 pointer changed')
    for key in ('processing', 'v3_pointer'):
        if observed['etags'][key] != before['etags'][key]:
            raise ValueError('unchanged control ETag changed')
    matches = (observed['sidecar'] == before['sidecar'], observed['catalog'] == before['catalog'],
               observed['sidecar'] == after['sidecar'], observed['catalog'] == after['catalog'])
    if matches[:2] == (True, True):
        name, changed = 'planned', set()
    elif matches[2] and matches[1]:
        name, changed = 'sidecar-observed', {'sidecar'}
    elif matches[2] and matches[3]:
        name, changed = 'catalog-observed', {'sidecar', 'catalog'}
    else:
        raise ValueError('mixed or foreign shadow state')
    for key in ('sidecar', 'catalog'):
        equal = observed['etags'][key] == before['etags'][key]
        if equal == (key in changed):
            raise ValueError('control ETag inconsistent with observed state')
    return {'observation': name, 'recovery_authorized': False, 'external_writers_fenced': False,
            'consumer_acceptance_proved': False, 'gc_authorized': False}


def require_real_activation(_plan):
    raise RuntimeError('shadow model does not authorize a production handoff')
