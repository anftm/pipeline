"""Reconcile released upload ownership after catalog history has been pruned.

The original release and generation stay unchanged. A drained collector may
record that both live consumers now own a later complete catalog; the current
four-bucket graph then decides which former upload objects are still referenced.
"""
from __future__ import annotations

from datetime import datetime
import base64
import hashlib
import json
import re

try:
    from . import reader_lifecycle as lifecycle, reader_gc_retirement as retirement, shared
except ImportError:
    import reader_lifecycle as lifecycle
    import reader_gc_retirement as retirement
    import shared

ASSETS = shared.READER_ASSETS_BUCKET


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('processing timestamp missing')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('processing timestamp is not timezone-qualified')
    return parsed


def certificate(path, raw, catalog, consumers):
    record = json.loads(raw)
    if record.get('status') != 'released':
        return None
    generation = record.get('generation')
    retired = catalog.get('retired_generations', {}).get(generation, {})
    if generation in catalog['generations'] or retired.get('replacement_acks') == {'hf': True, 'pages': True}:
        return None
    current = catalog['current']['generation']
    accepted = catalog['generations'][current]
    if (consumers.get('kind') != 'reader-gc-consumers' or consumers.get('version') != 1
            or any(consumers.get('receipts', {}).get(surface, {}).get('projection_verified') is not True
                   or consumers['receipts'][surface].get('base_sidecar_sha256') != accepted.get('sidecar_sha256')
                   for surface in ('hf', 'pages'))):
        raise ValueError('historical processing reconciliation requires both current consumer projections')
    if (not re.fullmatch(r'reader-index/processing/[a-z0-9_-]+\.json', path)
            or record.get('kind') != 'reader-processing-roots' or record.get('version') != 1
            or record.get('release_policy') != 'explicit-publisher-handoff'
            or not re.fullmatch(r'[0-9a-f]{32}|[0-9a-f]{64}', str(generation))
            or not isinstance(record.get('resources'), list) or not record['resources']
            or not record.get('operation') or not isinstance(record.get('run'), dict)):
        raise ValueError('historical processing release identity is incomplete: ' + path)
    if not (timestamp(record.get('created_at')) <= timestamp(record.get('released_at'))
            <= timestamp(record.get('updated_at')) <= timestamp(accepted.get('published_at'))):
        raise ValueError('processing release is newer than the accepted catalog: ' + path)
    for resource in record['resources']:
        root = resource.get('root') if isinstance(resource, dict) else None
        if (not isinstance(resource, dict) or not lifecycle.valid_processing_root(resource.get('bucket'), root)
                or retirement.bundle_root(root) is None
                or '\\' in root or '\0' in root
                or any(part in {'', '.', '..'} for part in root.split('/'))):
            raise ValueError('historical processing release has an unbounded root: ' + path)
    return {'kind': 'reader-processing-supersession', 'version': 1,
            'record_sha256': hashlib.sha256(raw).hexdigest(), 'original_generation': generation,
            'original_record_base64': base64.b64encode(raw).decode('ascii'),
            'replacement_generation': current, 'sidecar_sha256': accepted['sidecar_sha256'],
            'released_at': record['released_at'], 'resources': record['resources'],
            'consumer_receipts': {surface: consumers['receipts'][surface] for surface in ('hf', 'pages')},
            'reason': 'released ownership precedes both verified current consumer projections'}


def reconcile(store, catalog, consumers):
    paths = sorted(store.list_files(ASSETS, ('reader-index/processing/',)))
    def inspect(path):
        raw = store.read_bytes(ASSETS, path)
        return path, certificate(path, raw, catalog, consumers)
    return {(ASSETS, path): value for path, value in shared.ordered_reads(inspect, paths, workers=4) if value}
