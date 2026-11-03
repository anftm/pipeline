"""Verify the exact historical processing-retirement receipt, without mutating it.

An absent upload protection is not itself a handoff.  This verifier is limited
to the one completed, independently identified collector transaction whose
verified deletion batches and original handoff records it checks below.
It never recreates, deletes, or modifies processing records or source assets.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import PurePosixPath
import re

try:
    from . import reader_lifecycle as lifecycle, reader_gc_retirement as retirement
except ImportError:
    import reader_lifecycle as lifecycle
    import reader_gc_retirement as retirement


ASSETS = lifecycle.READER_ASSETS_BUCKET
RECEIPT_PATH = 'reader-index/gc/40218962766/collection.json'
RECEIPT_SHA256 = '05cc94c7c8f47ec452936c609ca73bcda93ec284b85ea7abbbf93501027248da'
PLAN_SHA256 = '294461fe5c48f1f98477a8caa4085f933afea809a54f54c770648c1d3fa130e5'
OLD_BASE_GENERATION = '661546abd7cb3a85f6433cf61bd32e2f'
OLD_V3_GENERATION = '6e4eae290fe7ce84250d85cdfc4c735890e8dba0b7f3c125eea7c022b1215ef6'
OLD_BASE_DIGEST = '599cf9e071706d12d22ccc71a829ee4bb9de25a53fe94a2e1a7899672bfafaf7'
PROCESSING_PATH = re.compile(r'reader-index/processing/(office-chapters|stream-upload|text)-[0-9a-f]{32}\.json')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return bool(re.fullmatch(r'[0-9a-f]{64}', str(value)))


def load_receipt(raw, *, expected_sha256=RECEIPT_SHA256, expected_plan=PLAN_SHA256):
    """Authenticate the complete transaction and exact verified batch set."""
    if _hash(raw) != expected_sha256:
        raise ValueError('historical processing receipt checksum mismatch')
    value = json.loads(raw)
    if (value.get('applied') is not True or value.get('complete') is not True
            or value.get('reader_only') is not True or value.get('scope') != 'unreferenced'
            or value.get('plan_sha256') != expected_plan):
        raise ValueError('historical processing receipt is not a completed transaction')
    targets = value.get('targets')
    batches = value.get('batches')
    records = value.get('processing_retirements')
    if not isinstance(targets, list) or not isinstance(batches, list) or not isinstance(records, list):
        raise ValueError('historical processing receipt is incomplete')
    try:
        if any(not isinstance(batch, dict) or not isinstance(batch.get('paths'), list) for batch in batches):
            raise ValueError('invalid historical deletion batch')
        pairs = [(row['bucket'], row['path']) for row in targets]
        if any(not isinstance(b, str) or not isinstance(p, str) for b, p in pairs):
            raise ValueError('invalid historical deletion target')
        unique = set(pairs)
        plan = sorted(pairs)
        verified = [(batch['bucket'], path) for batch in batches
                    if batch['status'] == 'verified-deleted' for path in batch['paths']]
    except (KeyError, TypeError):
        raise ValueError('malformed historical processing transaction') from None
    if (len(unique) != len(pairs) or len(verified) != len(set(verified))
            or set(verified) != unique or len(batches) != sum(b.get('status') == 'verified-deleted' for b in batches)
            or value.get('planned_objects') != len(unique)
            or value.get('confirmed_deleted_objects') != len(unique)
            or _hash(retirement.encode(plan)) != expected_plan):
        raise ValueError('historical processing deletion batches do not match the exact plan')
    by_path = {}
    for row in records:
        if (not isinstance(row, dict) or row.get('bucket') != ASSETS
                or not re.fullmatch(r'reader-index/processing/[a-z0-9_-]+\.json', str(row.get('path', '')))
                or (ASSETS, row['path']) not in unique or row['path'] in by_path
                or not _digest(row.get('sha256'))):
            raise ValueError('invalid or duplicate historical processing retirement')
        by_path[row['path']] = row
    return by_path


def _route_covers(sidecar, entry, resources):
    """Every former resource root must still be routed for its own source key."""
    route = sidecar.get('f', {}).get(entry.get('key'))
    if not isinstance(route, dict):
        raise ValueError('historical processing owner is absent from the sidecar')
    paths = [(route.get(field), route.get(bucket_field) or route.get('b') or ASSETS)
             for field, bucket_field in (('p', 'b'), ('c', 'cb'), ('f', 'fb'), ('pd', 'pdb'))]
    for item in resources:
        root, bucket = item.get('root'), item.get('bucket')
        if (bucket != ASSETS or not lifecycle.valid_processing_root(bucket, root)
                or not any(isinstance(path, str) and path.startswith(root + '/') and owner == bucket
                           for path, owner in paths)):
            raise ValueError('historical processing resource is not covered by the accepted route')


def stream_identity(entry, archived_resources=None):
    """Reproduce the stream producer's complete content-addressed owner key."""
    if archived_resources is not None:
        if (not isinstance(archived_resources, list) or not archived_resources
                or any(not isinstance(item, dict) or item.get('bucket') != ASSETS for item in archived_resources)):
            raise ValueError('historical stream resources are invalid')
        roots = {item.get('root') for item in archived_resources}
    else:
        roots = {entry['root']} if entry.get('root') else set()
        for field in ('object', 'manifest', 'fallback'):
            if entry.get(field):
                roots.add(str(PurePosixPath(entry[field]).parent))
    if any(not isinstance(root, str) for root in roots):
        raise ValueError('historical stream identity has invalid roots')
    roots = {root for root in roots if not any(root.startswith(other + '/') for other in roots if other != root)}
    if (not roots or not isinstance(entry.get('key'), str)
            or any(not isinstance(root, str) or '\\' in root or '\0' in root
                   or any(part in {'', '.', '..'} for part in root.split('/'))
                   or not lifecycle.valid_processing_root(ASSETS, root) for root in roots)):
        raise ValueError('historical stream identity has no bounded resources')
    identity = _hash((entry['key'] + '\0' + '\0'.join(sorted(roots))).encode())[:32]
    return f'reader-index/processing/stream-upload-{identity}.json', roots


def verify_owner_identity(path, entry, row, archived_resources=None):
    match = PROCESSING_PATH.fullmatch(path)
    if not match or not isinstance(entry.get('key'), str):
        raise ValueError('historical processing identity is invalid')
    family = match.group(1)
    if family == 'office-chapters':
        if not isinstance(entry.get('source_sha256'), str) or not isinstance(entry.get('root'), str):
            raise ValueError('historical chapter identity is incomplete')
        identity = _hash((entry['key'] + '\0' + entry['source_sha256'] + '\0' + entry['root']).encode())[:32]
        if path != f'reader-index/processing/office-chapters-{identity}.json':
            raise ValueError('historical chapter deterministic identity mismatch')
    elif family == 'stream-upload' and stream_identity(entry, archived_resources)[0] != path:
        raise ValueError('historical stream deterministic identity mismatch')
    expected = {'office-chapters': 'office-chapter-upload', 'stream-upload': 'reader-stream-upload',
                'text': 'text-utf8-upload'}
    if row.get('operation') != expected[family]:
        raise ValueError('historical processing operation does not match its producer')


def verify_owner(path, entry, row, accepted_sidecar, new_sidecar):
    """Check one absent path against its old receipt and two exact sidecars."""
    if (row.get('path') != path or row.get('bucket') != ASSETS
            or path not in entry.get('processing_roots', [])):
        raise ValueError('historical processing owner changed')
    supersession = row.get('supersession')
    if supersession is not None:
        try:
            raw = base64.b64decode(supersession['original_record_base64'], validate=True)
            record = json.loads(raw)
        except (KeyError, TypeError, ValueError):
            raise ValueError('historical processing original bytes are invalid') from None
        validate_archived_record(path, row, record, raw)
        verify_owner_identity(path, entry, row, record['resources'])
        lifecycle.validate_processing_handoff(record, entry)
        resources = record['resources']
        evidence = 'archived-original'
    else:
        # Thirty records were retired directly against the then-current catalog,
        # so the collector stored their digest and release metadata, not bytes.
        # Accept only the deterministic stream identity with every resource
        # still present in both exact source routes; never synthesize a record.
        if (row.get('generation') != OLD_BASE_GENERATION or row.get('operation') != 'reader-stream-upload'
                or not isinstance(row.get('run'), dict) or not isinstance(row.get('released_at'), str)):
            raise ValueError('historical processing retirement lacks original evidence')
        verify_owner_identity(path, entry, row)
        _, roots = stream_identity(entry)
        resources = [{'bucket': ASSETS, 'root': root} for root in sorted(roots)]
        lifecycle.validate_processing_handoff({'resources': resources}, entry)
        evidence = 'verified-retirement-digest'
    _route_covers(accepted_sidecar, entry, resources)
    _route_covers(new_sidecar, entry, resources)
    return evidence


def validate_archived_record(path, row, record, raw):
    """Validate immutable retired bytes before using them as a read-only proof."""
    supersession = row.get('supersession')
    if (not isinstance(supersession, dict) or row.get('path') != path or row.get('bucket') != ASSETS
            or supersession.get('version') != 1 or supersession.get('kind') != 'reader-processing-supersession'
            or supersession.get('record_sha256') != row.get('sha256')
            or supersession.get('original_generation') != row.get('generation')
            or supersession.get('replacement_generation') != OLD_BASE_GENERATION
            or supersession.get('sidecar_sha256') != OLD_BASE_DIGEST):
        raise ValueError('historical processing supersession identity mismatch')
    for surface in ('hf', 'pages'):
        receipt = supersession.get('consumer_receipts', {}).get(surface, {})
        if (receipt.get('projection_verified') is not True or receipt.get('surface') != surface
                or receipt.get('generation') != OLD_V3_GENERATION
                or receipt.get('base_sidecar_sha256') != OLD_BASE_DIGEST):
            raise ValueError('historical processing lacks both consumer receipts')
    try:
        original = base64.b64decode(supersession['original_record_base64'], validate=True)
    except (KeyError, TypeError, ValueError):
        raise ValueError('historical processing original bytes are invalid') from None
    if (raw != original or _hash(raw) != row['sha256'] or record.get('version') != 1
            or record.get('kind') != 'reader-processing-roots' or record.get('status') != 'released'
            or record.get('release_policy') != 'explicit-publisher-handoff'
            or record.get('generation') != row.get('generation') or record.get('operation') != row.get('operation')
            or record.get('run') != row.get('run') or record.get('released_at') != row.get('released_at')
            or record.get('resources') != supersession.get('resources')):
        raise ValueError('historical processing original record identity mismatch')
    return record
