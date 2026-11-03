"""Pure byte construction/checking for a hypothetical CHM quarantine root.

Nothing here writes a bucket, locks a workflow, or authorizes a deletion.  The
existing root is part of an immutable original CHM plan, so a future change
requires a separately reviewed transaction and must not be applied by itself.
"""
import copy
import hashlib
import json
import re

try:
    from . import chm_subset_retirement as subset, publish_reader_v3 as publication
except ImportError:
    import chm_subset_retirement as subset
    import publish_reader_v3 as publication


def prepare(before_raw, withheld):
    """Append only the exact withheld identities to the supplied retention root."""
    if not isinstance(before_raw, bytes) or not before_raw or not isinstance(withheld, list) or not withheld:
        raise ValueError('missing retention bytes or withheld inventory')
    try:
        before = json.loads(before_raw)
    except (TypeError, ValueError) as error:
        raise ValueError('invalid retention JSON') from error
    if (not isinstance(before, dict) or set(before) != {'inventory_sha256', 'kind', 'resources', 'version'}
            or before.get('kind') != 'reader-chm-epub-withdrawal-retention'
            or type(before.get('version')) is not int or before['version'] != 1
            or before.get('inventory_sha256') != subset.chm.preview.INVENTORY_SHA256
            or not isinstance(before.get('resources'), list) or not before['resources']):
        raise ValueError('invalid original retention root identity')
    existing = set()
    for ref in before['resources']:
        if (not isinstance(ref, dict) or set(ref) != {'bucket', 'path', 'bytes', 'role', 'sha256'}
                or ref['bucket'] != subset.ASSETS or not isinstance(ref['path'], str)
                or not subset.SOURCE.fullmatch(ref['path']) or type(ref['bytes']) is not int or ref['bytes'] < 1
                or ref['role'] != 'runtime' or not isinstance(ref['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', ref['sha256']) or ref['path'] in existing):
            raise ValueError('original chapter retention references are invalid or duplicate')
        existing.add(ref['path'])
    keep = {}
    for row in withheld:
        if (not isinstance(row, dict) or set(row) != {'bucket', 'path', 'bytes', 'sha256'}
                or row['bucket'] != subset.ASSETS or not isinstance(row['path'], str)
                or not subset.TARGET.fullmatch(row['path']) or row['path'] in keep or row['path'] in existing
                or type(row['bytes']) is not int or row['bytes'] < 1 or not isinstance(row['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])):
            raise ValueError('withheld target identity is invalid or duplicate')
        keep[row['path']] = row
    result = copy.deepcopy(before)
    result['resources'].extend({'bucket': row['bucket'], 'path': row['path'], 'bytes': row['bytes'],
                                'role': 'runtime', 'sha256': row['sha256']}
                               for _, row in sorted(keep.items()))
    after_raw = publication.encode(result)
    return {'before_sha256': hashlib.sha256(before_raw).hexdigest(), 'after_sha256': hashlib.sha256(after_raw).hexdigest(),
            'old_references': len(existing), 'appended_references': len(keep), 'after_raw': after_raw,
            'remote_mutations': 0, 'deletion_authorized': False}


def verify_transition(before_raw, after_raw, withheld):
    expected = prepare(before_raw, withheld)
    if not isinstance(after_raw, bytes) or expected['after_raw'] != after_raw:
        raise ValueError('retention transition is not the exact append-only bytes')
    return {key: value for key, value in expected.items() if key != 'after_raw'}
