#!/usr/bin/env python3
"""Switch indexed PDF derivatives to verified existing objects; never delete bytes.

The caller owns the central Reader writer lock. Mutable roots use conditional
single-object S3 writes; these are not a multi-object transaction or a bucket
lock, so external writers must also be coordinated. Old files and old catalog
generations are retained.
"""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import os
import re

try:
    from . import reader_lifecycle as lifecycle, reader_page_audit as audit, publish_reader_v3 as publication
    from .reader_gc_fence import GitHubProducerFence
except ImportError:
    import reader_lifecycle as lifecycle
    import reader_page_audit as audit
    import publish_reader_v3 as publication
    from reader_gc_fence import GitHubProducerFence


CATALOG = 'reader-index/reader_catalog.json'
TRANSACTION_PREFIX = 'reader-index/gc/page-migration/'
WORKFLOW = '.github/workflows/reader-v3-publish.yml'


def require_writer_window(store):
    """Recheck GitHub producers at write boundaries, without claiming bucket isolation."""
    publication.assert_writer(store)
    if os.environ.get('READER_PAGE_EXTERNAL_WRITERS_STOPPED') != 'true':
        raise RuntimeError('independent bucket writers must be stopped throughout the migration')
    GitHubProducerFence().check(allow_current_workflow=WORKFLOW, all_workflows=True)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def prepare(store, api, *, expected_index, expected_sidecar, expected_v3, workers=4):
    """Recompute candidates using fresh metadata and produce a pure local update."""
    if any(not isinstance(value, str) or not audit.SHA.fullmatch(value)
           for value in (expected_index, expected_sidecar, expected_v3)):
        raise ValueError('exact SHA-256 index, sidecar and v3 identities are required')
    snapshot = audit.audit(store, api, workers=workers)
    if (snapshot['index_sha256'] != expected_index or snapshot['canonical_sidecar_sha256'] != expected_sidecar
            or snapshot['v3_generation'] != expected_v3):
        raise ValueError('plan is stale: inspect index, sidecar and v3 generation again')
    if snapshot['unresolved']:
        raise ValueError('legacy PDFs with unresolved or ambiguous copies block migration')
    return prepare_from_audit(store, snapshot, expected_index=expected_index,
                              expected_sidecar=expected_sidecar, expected_v3=expected_v3)


def prepare_from_audit(store, snapshot, *, expected_index, expected_sidecar, expected_v3):
    if (snapshot.get('version') != 1 or snapshot.get('kind') != 'pdf-page-bucket-audit'
            or snapshot.get('bucket') != audit.BUCKET
            or snapshot.get('index_sha256') != expected_index or snapshot.get('canonical_sidecar_sha256') != expected_sidecar
            or snapshot.get('v3_generation') != expected_v3 or snapshot.get('unresolved')):
        raise ValueError('audit does not match requested roots or has unresolved entries')
    before_index = store.read_bytes(audit.BUCKET, audit.INDEX)
    before_sidecar = store.read_bytes(audit.ASSETS, audit.SIDECAR)
    if digest(before_index) != expected_index or digest(before_sidecar) != expected_sidecar:
        raise ValueError('migration roots changed after the inventory')
    index, base = json.loads(before_index), audit.decode_sidecar(before_sidecar)
    pointer, current = publication.current(store)
    if not pointer or pointer['generation'] != expected_v3:
        raise ValueError('v3 generation moved during migration planning')
    pinned = publication.pinned_base(store, current, {})
    if current.get('base_sidecar', {}).get('sha256') != expected_sidecar:
        raise ValueError('v3 pinned base differs from canonical sidecar')
    if current.get('suppressed_sources'):
        raise ValueError('migrate derived PDFs before applying route suppressions')
    legacy_raw = store.read_bytes(audit.ASSETS, CATALOG)
    legacy = json.loads(legacy_raw)
    current_gen = (legacy.get('current') or {}).get('generation')
    if (legacy.get('version') != 1 or legacy.get('kind') != 'reader-catalog'
            or current_gen not in legacy.get('generations', {})
            or legacy['generations'][current_gen].get('sidecar_sha256') != digest(audit.encode(base))):
        raise ValueError('legacy catalog does not bind canonical sidecar')
    mapping = {row['key']: row for row in snapshot['planned']}
    if len(mapping) != len(snapshot['planned']):
        raise ValueError('duplicate migration source')
    if not mapping:
        raise ValueError('no indexed derived PDFs remain to migrate')
    # Never infer a generation from caller-supplied path strings alone. The
    # known old index constrains every planned row and target source identity.
    by_key = {entry.get('key'): entry for entry in index['files']}
    for key, row in mapping.items():
        entry = by_key.get(key)
        if not entry or any(row.get(field) != entry.get(field)
                            for field in ('source_sha256', 'derived_sha256')):
            raise ValueError('planned source identity differs from canonical index')
        match = audit.NEW.fullmatch(str(row.get('new_path', '')))
        if (not match or match.group(1) != match.group(2)[:2] or match.group(2) != entry['source_sha256']
                or row.get('bytes') != entry.get('derived_bytes')
                or row.get('new_manifest') != row['new_path'].rsplit('/', 1)[0] + '/derived-manifest.json'):
            raise ValueError('planned object identity differs from canonical index')
    if any(key in current.get('files', {}) for key in mapping):
        raise ValueError('a migration source has an active v3 override')
    updated_index, updated_base = copy.deepcopy(index), copy.deepcopy(base)
    replacements = {}
    for entry in updated_index['files']:
        row = mapping.get(entry['key'])
        if row is None:
            continue
        if entry.get('new_path') != row['old_path'] or entry.get('manifest_path') != row['old_manifest']:
            raise ValueError('canonical derived entry changed')
        old, new = row['old_path'], row['new_path']
        if old in replacements and replacements[old] != new:
            raise ValueError('one old PDF maps to multiple objects')
        replacements[old] = new
        entry['new_path'], entry['manifest_path'] = new, row['new_manifest']
        if entry.get('path') == old:
            entry['path'] = new
    if len(replacements) != len(mapping):
        raise ValueError('multiple sources share a legacy path')
    # Rewrite exact bucket-qualified fields, including aliases; unknown
    # references block the migration instead of being silently dropped.
    changed_routes = 0
    bucket_fields = (('p', 'b'), ('pd', 'pdb'), ('o', 'ob'), ('c', 'cb'), ('f', 'fb'))
    for key in mapping:
        if base['f'].get(key) != pinned['f'].get(key):
            raise ValueError('pinned route differs from canonical route')
    for entry in updated_base['f'].values():
        if not isinstance(entry, dict):
            raise ValueError('invalid canonical sidecar entry')
        default_bucket = entry.get('b') or audit.ASSETS
        changed = False
        for field, qualifier in bucket_fields:
            old = entry.get(field)
            if isinstance(old, str) and old in replacements and (entry.get(qualifier) or default_bucket) == audit.BUCKET:
                entry[field] = replacements[old]
                changed = True
        changed_routes += changed
    encoded_base = audit.encode(updated_base)
    if any(row['old_path'].encode() in encoded_base for row in snapshot['planned']):
        raise ValueError('an old path remains in an unsupported sidecar field')
    packed = gzip.compress(encoded_base, mtime=0)
    # Build a new generation without calling the legacy compactor. Migration
    # preserves the complete pre-existing generations and acknowledgments.
    old_refs = legacy['generations'][current_gen].get('references')
    if not isinstance(old_refs, list) or any(not isinstance(ref, dict)
            or not isinstance(ref.get('bucket'), str) or not isinstance(ref.get('path'), str)
            for ref in old_refs):
        raise ValueError('current legacy references are invalid')
    carried = []
    for ref in old_refs:
        item = dict(ref)
        if item['bucket'] == audit.BUCKET and item['path'] in replacements:
            item['path'] = replacements[item['path']]
        carried.append(item)
    normalized = sorted({(ref['bucket'], ref['path'])
                         for ref in [*carried, *lifecycle.sidecar_references(updated_base)]})
    sidecar_digest = digest(encoded_base)
    signature = lifecycle._generation_id({'sidecar_sha256': sidecar_digest, 'references': normalized}, None)
    generation = lifecycle._generation_id({'signature': signature}, current_gen)
    if generation in legacy['generations']:
        raise ValueError('legacy generation already exists under different current pointer')
    updated_legacy = copy.deepcopy(legacy)
    now = lifecycle.now_iso()
    old_current = updated_legacy['generations'][current_gen]
    old_current.update(status='superseded', superseded_at=now)
    updated_legacy['generations'][generation] = {
        'generation': generation, 'status': 'current', 'published_at': now, 'signature': signature,
        'sidecar_sha256': sidecar_digest, 'parent_generation': current_gen,
        'references': [{'bucket': b, 'path': p} for b, p in normalized], 'acks': {'hf': False, 'pages': False}}
    updated_legacy['current'] = {'generation': generation, 'published_at': now}
    ref_path = f'reader-index/generations/{generation}/reader_assets.json.gz'
    updated_legacy['generations'][generation]['snapshot'] = {
        'bucket': audit.ASSETS, 'path': ref_path, 'sha256': digest(packed), 'bytes': len(packed)}
    return {'audit': snapshot, 'before_index': before_index, 'before_sidecar': before_sidecar,
            'before_catalog': legacy_raw, 'before_index_value': index, 'before_sidecar_value': base,
            'before_catalog_value': legacy, 'pointer': pointer, 'pinned': pinned,
            'index': updated_index, 'sidecar': updated_base, 'packed': packed,
            'catalog': updated_legacy, 'generation': generation, 'snapshot_path': ref_path,
            'changed_routes': changed_routes}


def transaction(plan):
    """Make a content-addressed transaction containing exact before/after bytes."""
    def item(bucket, path, before, before_value, after):
        return {'bucket': bucket, 'path': path, 'before_sha256': digest(before),
                'before_value_sha256': digest(audit.encode(before_value)), 'after_sha256': digest(after),
                'after_base64': base64.b64encode(after).decode('ascii')}
    result = {
        'version': 1, 'kind': 'pdf-derived-index-migration', 'bucket': audit.BUCKET,
        'pointer': plan['pointer'], 'rows': plan['audit']['planned'],
        'writes': [item(audit.BUCKET, audit.INDEX, plan['before_index'], plan['before_index_value'], audit.encode(plan['index'])),
                   item(audit.ASSETS, audit.SIDECAR, plan['before_sidecar'], plan['before_sidecar_value'], plan['packed']),
                   item(audit.ASSETS, CATALOG, plan['before_catalog'], plan['before_catalog_value'], audit.encode(plan['catalog']))],
        'snapshot_path': plan['snapshot_path'], 'base_generation': plan['generation'],
        'changed_routes': plan['changed_routes'], 'objects_deleted': False,
    }
    validate_transaction(result)
    return result


def validate_transaction(value):
    if (not isinstance(value, dict) or value.get('version') != 1
            or value.get('kind') != 'pdf-derived-index-migration' or value.get('bucket') != audit.BUCKET
            or value.get('objects_deleted') is not False or not isinstance(value.get('rows'), list)
            or not value['rows'] or not isinstance(value.get('writes'), list) or len(value['writes']) != 3
            or not isinstance(value.get('pointer'), dict) or not audit.SHA.fullmatch(str(value['pointer'].get('generation', '')))
            or not re.fullmatch(r'[0-9a-f]{32}', str(value.get('base_generation', '')))):
        raise ValueError('invalid migration transaction')
    expected = [(audit.BUCKET, audit.INDEX), (audit.ASSETS, audit.SIDECAR), (audit.ASSETS, CATALOG)]
    if [(w.get('bucket'), w.get('path')) for w in value['writes']] != expected:
        raise ValueError('migration writes outside exact index roots')
    if value.get('snapshot_path') != f"reader-index/generations/{value['base_generation']}/reader_assets.json.gz":
        raise ValueError('invalid migration snapshot identity')
    for row in value['rows']:
        if (not isinstance(row, dict) or not audit.OLD.fullmatch(str(row.get('old_path', '')))
                or not audit.NEW.fullmatch(str(row.get('new_path', '')))
                or row.get('new_manifest') != row['new_path'].rsplit('/', 1)[0] + '/derived-manifest.json'
                or not audit.SHA.fullmatch(str(row.get('new_manifest_sha256', '')))
                or not isinstance(row.get('old_xet_hash'), str) or not row['old_xet_hash']
                or type(row.get('bytes')) is not int or row['bytes'] < 1):
            raise ValueError('invalid migration target identity')
    for item in value['writes']:
        if any(not audit.SHA.fullmatch(str(item.get(field, '')))
               for field in ('before_sha256', 'before_value_sha256', 'after_sha256')):
            raise ValueError('invalid migration root digest')
        raw = base64.b64decode(item.get('after_base64', ''), validate=True)
        if digest(raw) != item['after_sha256']:
            raise ValueError('migration transaction bytes differ')
    return value


def verify_copies(store, api, rows):
    observed = {item.path: item for item in api.get_bucket_paths_info(audit.BUCKET,
        [path for row in rows for path in (row['old_path'], row['new_path'])])}
    for row in rows:
        for path in (row['old_path'], row['new_path']):
            item = observed.get(path)
            if item is None or item.xet_hash != row['old_xet_hash'] or item.size != row['bytes']:
                raise ValueError('PDF object changed before migration')
        if digest(store.read_bytes(audit.BUCKET, row['new_manifest'])) != row['new_manifest_sha256']:
            raise ValueError('object manifest changed before migration')


def transaction_id(value):
    return digest(audit.encode(validate_transaction(value)))


def persist_transaction(store, value):
    identity = transaction_id(value)
    raw = gzip.compress(audit.encode(value), mtime=0)
    path = TRANSACTION_PREFIX + identity + '.json.gz'
    publication.immutable_put(store, audit.ASSETS, path, raw)
    return identity


def root_state(item, raw):
    if digest(raw) == item['after_sha256']:
        return 'after'
    if digest(raw) == item['before_sha256']:
        return 'before'
    try:
        payload = audit.decode_sidecar(raw) if item['path'] == audit.SIDECAR else json.loads(raw)
    except (ValueError, TypeError, OSError):
        raise ValueError('migration root changed or contains invalid data: ' + item['path']) from None
    if digest(audit.encode(payload)) == item['before_value_sha256']:
        return 'before'
    raise ValueError('migration root changed before write: ' + item['path'])


def run_transaction(store, api, value):
    """Idempotent exact-state resume; never blindly retry a divergent root."""
    require_writer_window(store)
    value = validate_transaction(value)
    verify_copies(store, api, value['rows'])
    current_pointer, _ = publication.current(store)
    sidecar_after = base64.b64decode(value['writes'][1]['after_base64'], validate=True)
    already = current_pointer != value['pointer']
    if already:
        _, catalog = publication.current(store)
        if (not current_pointer or catalog.get('base_sidecar', {}).get('sha256') != digest(sidecar_after)
                or catalog.get('parent_generation') != value['pointer']['generation']):
            raise ValueError('v3 pointer moved outside migration')
    # Reject divergence for all mutable roots before writing any one of them.
    states = []
    for item in value['writes']:
        current = store.read_bytes(item['bucket'], item['path'])
        states.append(root_state(item, current))
    # A later stage cannot precede its prerequisite; mixed states remain resumable.
    after_flags = [state == 'after' for state in states]
    if any(after_flags[i] and not all(after_flags[:i]) for i in range(len(after_flags))):
        raise ValueError('migration roots are in an impossible order')
    if already and not all(after_flags):
        raise ValueError('v3 advanced before all migration roots')
    require_writer_window(store)
    publication.immutable_put(store, audit.ASSETS, value['snapshot_path'], sidecar_after)
    for item, state in zip(value['writes'], states):
        raw = base64.b64decode(item['after_base64'], validate=True)
        require_writer_window(store)
        # Bind the bytes checked here to the same S3 GET's ETag. A normal
        # compare-then-Hub-write can overwrite a competing change in between.
        observed, etag = store.read_for_update(item['bucket'], item['path'])
        if root_state(item, observed) != state:
            raise ValueError('migration root changed during resume: ' + item['path'])
        if state != 'after':
            store.put_if_match(item['bucket'], item['path'], raw, etag)
        actual, _ = store.read_for_update(item['bucket'], item['path'])
        if digest(actual) != item['after_sha256']:
            raise ValueError('migration root readback failed: ' + item['path'])
    # Later root writes can overlap an earlier root's subsequent mutation.
    # Recheck all three before promotion; external isolation is still required
    # because no recheck can atomically cover multiple objects.
    require_writer_window(store)
    for item in value['writes']:
        observed, _ = store.read_for_update(item['bucket'], item['path'])
        if digest(observed) != item['after_sha256']:
            raise ValueError('migration root changed before promotion: ' + item['path'])
    if already:
        if publication.current(store)[0] != current_pointer:
            raise ValueError('v3 pointer changed while checking completed migration')
        generation = current_pointer['generation']
    else:
        require_writer_window(store)
        generation = publication.unify(store, apply=True, conditional_pointer=True,
                                       expected_canonical=value['writes'][1]['after_sha256'],
                                       expected_pointer=value['pointer'])['generation']
    return {'applied': True, 'migrated_entries': len(value['rows']),
            'changed_routes': value['changed_routes'], 'base_generation': value['base_generation'],
            'generation': generation, 'v3_generation': generation, 'objects_deleted': False}


def apply(store, api, plan):
    """Persist the immutable plan before the first mutable index write."""
    require_writer_window(store)
    value = transaction(plan)
    verify_copies(store, api, value['rows'])
    for item in value['writes']:
        if root_state(item, store.read_bytes(item['bucket'], item['path'])) != 'before':
            raise ValueError('migration root changed before write')
    if publication.current(store)[0] != value['pointer']:
        raise ValueError('v3 pointer changed before write')
    require_writer_window(store)
    identity = persist_transaction(store, value)
    print(f'migration transaction persisted: {identity}', flush=True)
    result = run_transaction(store, api, value)
    return {**result, 'transaction_id': identity}


def resume(store, api, identity):
    if not isinstance(identity, str) or not audit.SHA.fullmatch(identity):
        raise ValueError('resume requires an exact transaction SHA-256')
    raw = store.read_bytes(audit.ASSETS, TRANSACTION_PREFIX + identity + '.json.gz')
    value = json.loads(gzip.decompress(raw))
    if transaction_id(value) != identity:
        raise ValueError('migration transaction checksum mismatch')
    return {**run_transaction(store, api, value), 'transaction_id': identity}
