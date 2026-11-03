"""Offline-only design checks for an independent CHM protection root.

Creating this root would still be a production mutation and would change the
old graph's root membership.  This module only compares supplied bytes and
observations. It has no storage backend or workflow/deletion entry point.
"""
import hashlib
import re

try:
    from . import chm_subset_retirement as subset, reader_gc_graph as graph
    from . import publish_reader_v3 as publication
except ImportError:
    import chm_subset_retirement as subset
    import reader_gc_graph as graph
    import publish_reader_v3 as publication

SHA = re.compile(r'[0-9a-f]{64}')
TX = re.compile(r'[0-9a-f]{32}')
PREFIX = 'reader-index/chm-subset-retention-'
ROOT = re.compile(r'reader-index/chm-subset-retention-([0-9a-f]{32})\.json')


def root_path(transaction_id):
    if not isinstance(transaction_id, str) or not TX.fullmatch(transaction_id) or transaction_id == '0' * 32:
        raise ValueError('invalid isolated-root transaction id')
    return PREFIX + transaction_id + '.json'


def build(withheld, identity):
    """Construct only a deterministic root payload; do not publish it."""
    if (not isinstance(identity, dict) or set(identity) != {
            'old_plan_sha256', 'inventory_sha256', 'old_progress_sha256', 'old_receipts_sha256',
            'classification_sha256', 'maintenance_id', 'transaction_id'}):
        raise ValueError('isolated-root identity is incomplete')
    for field in ('old_plan_sha256', 'inventory_sha256', 'old_progress_sha256',
                  'old_receipts_sha256', 'classification_sha256'):
        if not isinstance(identity[field], str) or not SHA.fullmatch(identity[field]):
            raise ValueError('invalid isolated-root identity digest')
    if (identity['inventory_sha256'] != subset.chm.preview.INVENTORY_SHA256
            or not isinstance(identity['maintenance_id'], str) or not TX.fullmatch(identity['maintenance_id'])
            or identity['maintenance_id'] == '0' * 32):
        raise ValueError('invalid original inventory or maintenance identity')
    path = root_path(identity['transaction_id'])
    if not isinstance(withheld, list) or not withheld:
        raise ValueError('empty withheld root resources')
    refs = {}
    for row in withheld:
        if (not isinstance(row, dict) or set(row) != {'bucket', 'path', 'bytes', 'sha256'}
                or row['bucket'] != subset.ASSETS or not isinstance(row['path'], str)
                or not subset.TARGET.fullmatch(row['path']) or row['path'] in refs
                or type(row['bytes']) is not int or row['bytes'] < 1
                or not isinstance(row['sha256'], str) or not SHA.fullmatch(row['sha256'])):
            raise ValueError('invalid or duplicate withheld root resource')
        refs[row['path']] = row
    payload = {'version': 1, 'kind': 'reader-chm-subset-retention',
               # Audit identities are intentionally non-path-bearing fields.
               'original_inventory_sha256': identity['inventory_sha256'],
               'original_plan_sha256': identity['old_plan_sha256'],
               'original_progress_sha256': identity['old_progress_sha256'],
               'original_receipts_sha256': identity['old_receipts_sha256'],
               'classification_sha256': identity['classification_sha256'],
               'maintenance_id': identity['maintenance_id'], 'transaction_id': identity['transaction_id'],
               'resources': [{'bucket': row['bucket'], 'path': row['path'], 'bytes': row['bytes'],
                              'sha256': row['sha256'], 'role': 'runtime'} for _, row in sorted(refs.items())]}
    raw = publication.encode(payload)
    return {'path': path, 'raw': raw, 'sha256': hashlib.sha256(raw).hexdigest(),
            'objects': len(refs), 'remote_mutations': 0, 'deletion_authorized': False}


def compare(before, after, *, isolated_path, isolated_raw, withheld, identity):
    """Compare supplied old and simulated root closures without authorization.

    The new root must be the only additional member and graph document; every
    old active root and observed graph-document hash must be identical. Old
    retired roots can remain retired, but none can be invented or unretired.
    Caller-supplied observations must independently establish exact bytes,
    bucket inventory, target refs, both sites and isolation at the relevant time.
    """
    if (not isinstance(isolated_path, str) or not ROOT.fullmatch(isolated_path)
            or not graph.is_root(isolated_path, subset.ASSETS) or not isinstance(isolated_raw, bytes)
            or not isolated_raw):
        raise ValueError('invalid isolated-root path or bytes')
    expected_root = build(withheld, identity)
    if expected_root['path'] != isolated_path or expected_root['raw'] != isolated_raw:
        raise ValueError('isolated-root document identity or exact resources differ')
    if (not isinstance(before, dict) or before.get('mode') != 'actual'
            or before.get('graph_complete') is not True or before.get('target_count') != 972
            or before.get('retained_targets') != 0 or before.get('origins') != []
            or before.get('blockers') != [] or before.get('ambiguous_paths') != []
            or before.get('root_recheck_complete') is not True
            or before.get('reader_acceptance_complete') is not True or before.get('remote_mutations') != 0
            or not isinstance(after, dict) or after.get('mode') != 'hypothetical-overlay'
            or after.get('graph_complete') is not True or after.get('blockers') != []
            or after.get('ambiguous_paths') != [] or after.get('root_recheck_complete') is not True
            or after.get('reader_acceptance_complete') is not True or after.get('remote_mutations') != 0
            or after.get('deletion_authorized') is not False):
        raise ValueError('old or hypothetical graph is incomplete')

    retained = after.get('retained_target_paths')
    if (not isinstance(retained, list) or any(not isinstance(row, dict) or set(row) != {'bucket', 'path'}
                                              or row.get('bucket') != subset.ASSETS
                                              or not isinstance(row.get('path'), str) for row in retained)
            or len({(row['bucket'], row['path']) for row in retained}) != len(retained)
            or {(row['bucket'], row['path']) for row in retained}
                != {(row['bucket'], row['path']) for row in withheld}):
        raise ValueError('isolated-root graph target references differ')

    def refs(value, field, *, hashes=False):
        if not isinstance(value, list):
            raise ValueError('invalid graph ' + field)
        result = {}
        for item in value:
            if (not isinstance(item, dict) or item.get('bucket') not in graph.BUCKETS
                    or not isinstance(item.get('path'), str) or not item['path']
                    or (hashes and (not isinstance(item.get('sha256'), str) or not SHA.fullmatch(item['sha256'])))):
                raise ValueError('invalid graph ' + field + ' entry')
            key = (item['bucket'], item['path'])
            if key in result:
                raise ValueError('duplicate graph ' + field + ' entry')
            result[key] = item['sha256'] if hashes else True
        return result

    old_members = refs(before.get('root_members'), 'old members')
    new_members = refs(after.get('root_members'), 'new members')
    old_roots = refs(before.get('root_sha256'), 'old roots', hashes=True)
    new_roots = refs(after.get('root_sha256'), 'new roots', hashes=True)
    old_closure = refs(before.get('closure_sha256'), 'old closure', hashes=True)
    new_closure = refs(after.get('closure_sha256'), 'new closure', hashes=True)
    old_retired = refs(before.get('retired_roots'), 'old retired roots')
    new_retired = refs(after.get('retired_roots'), 'new retired roots')
    candidate = (subset.ASSETS, isolated_path)
    if (not old_members or before.get('root_count') != len(old_roots) or candidate in old_members
            or candidate in old_roots or candidate in old_closure or candidate in new_retired
            or candidate in old_retired or not set(old_retired) <= set(old_members)
            or set(new_members) != set(old_members) | {candidate}
            or not set(old_retired) <= set(new_retired) <= set(old_members)
            or not (set(old_members) - set(old_retired)) <= set(old_roots)
            or not (set(new_members) - set(new_retired)) <= set(new_roots)
            or not set(old_roots) <= set(old_members) or not set(new_roots) <= set(new_members)):
        raise ValueError('isolated-root graph membership changed unexpectedly')
    candidate_sha = hashlib.sha256(isolated_raw).hexdigest()
    if (new_roots.get(candidate) != candidate_sha or new_closure.get(candidate) != candidate_sha
            or {k: v for k, v in new_roots.items() if k != candidate} != old_roots
            or {k: v for k, v in new_closure.items() if k != candidate} != old_closure
            or any(old_closure.get(ref) != sha for ref, sha in old_roots.items())
            or any(new_closure.get(ref) != sha for ref, sha in new_roots.items())):
        raise ValueError('isolated-root graph closure changed unexpectedly')
    return {'mode': 'hypothetical-isolated-root-only', 'original_roots': len(old_members),
            'original_closure': len(old_closure), 'isolated_root_sha256': candidate_sha,
            'remote_mutations': 0, 'deletion_authorized': False}
