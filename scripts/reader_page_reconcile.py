"""Reconcile an active v3 PDF redirect into the single canonical PDF index.

Only the canonical PDF index may be replaced, using one S3 If-Match write.
The v3 catalog, sidecar, legacy catalog, old PDFs and manifests are retained.
Neither this audit nor a successful update authorizes object deletion.
"""
import copy
import hashlib
import json

try:
    from . import reader_page_audit as audit, reader_page_migrate as migration
    from . import reader_page_redirects as redirects, reader_page_atomic as atomic
    from . import publish_reader_v3 as publication
except ImportError:
    import reader_page_audit as audit
    import reader_page_migrate as migration
    import reader_page_redirects as redirects
    import reader_page_atomic as atomic
    import publish_reader_v3 as publication


def validate_snapshot(snapshot, mapping):
    """Bind every active redirect to the freshly audited old/new identity."""
    rows = snapshot.get('planned')
    if not isinstance(rows, list) or snapshot.get('unresolved'):
        raise ValueError('PDF redirect audit has unresolved candidates')
    by_old = {row['old_path']: row for row in rows}
    if len(by_old) != len(rows):
        raise ValueError('duplicate PDF source in redirect audit')
    for old, new in mapping.items():
        if old not in by_old or by_old[old].get('new_path') != new:
            raise ValueError('active PDF redirect no longer matches canonical source and exact copy')
    return by_old


def prepare(store, api, *, expected_index, expected_generation, workers=4):
    if not audit.SHA.fullmatch(str(expected_index)) or not audit.SHA.fullmatch(str(expected_generation)):
        raise ValueError('exact PDF index and v3 generation SHA-256 identities are required')
    pointer, catalog = publication.current(store)
    if not pointer or pointer['generation'] != expected_generation:
        raise ValueError('v3 generation changed before PDF index reconciliation')
    mapping = redirects.validated(catalog)
    if not mapping or not catalog.get('base_sidecar'):
        raise ValueError('PDF index reconciliation requires active redirects and pinned base')
    snapshot = audit.audit(store, api, workers=workers, active_redirects=mapping)
    if snapshot['index_sha256'] != expected_index or snapshot['v3_generation'] != expected_generation:
        raise ValueError('PDF index or v3 generation changed during audit')
    if publication.current(store)[0] != pointer:
        raise ValueError('v3 pointer changed during PDF index reconciliation')
    by_old = validate_snapshot(snapshot, mapping)
    raw = store.read_bytes(audit.BUCKET, audit.INDEX)
    if hashlib.sha256(raw).hexdigest() != expected_index:
        raise ValueError('PDF index changed after audit')
    index = json.loads(raw)
    updated = copy.deepcopy(index)
    by_key = {entry.get('key'): entry for entry in updated['files']}
    changed = 0
    for old, new in mapping.items():
        row = by_old[old]
        entry = by_key.get(row['key'])
        if not isinstance(entry, dict):
            raise ValueError('PDF index source disappeared')
        current = entry.get('new_path')
        if current == old:
            if (entry.get('manifest_path') != row['old_manifest']
                    or any(row.get(field) != entry.get(field) for field in ('source_sha256', 'derived_sha256'))
                    or row.get('bytes') != entry.get('derived_bytes')):
                raise ValueError('legacy PDF identity changed after audit')
            entry['new_path'], entry['manifest_path'] = new, row['new_manifest']
            if entry.get('path') == old:
                entry['path'] = new
            changed += 1
        elif current == new:
            if (entry.get('manifest_path') != row['new_manifest'] or entry.get('path') == old
                    or any(row.get(field) != entry.get(field) for field in ('source_sha256', 'derived_sha256'))
                    or row.get('bytes') != entry.get('derived_bytes')):
                raise ValueError('reconciled PDF index has inconsistent identity or paths')
        else:
            raise ValueError('PDF index source no longer names either audited copy')
    encoded = audit.encode(updated)
    index_diff = []
    for before, after in zip(index['files'], updated['files']):
        fields = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
        if fields:
            if any(key not in {'path', 'new_path', 'manifest_path'} for key in fields):
                raise ValueError('unexpected PDF index field changed')
            index_diff.append({'key': before['key'], 'changed_fields': fields})
    if len(index_diff) != changed:
        raise ValueError('PDF index change count differs from exact diff')
    return {'pointer': pointer, 'snapshot': snapshot, 'before': raw,
            'after': encoded if changed else raw, 'changed': changed, 'redirects': len(mapping), 'index_diff': index_diff}


def result(plan, *, applied):
    return {'version': 1, 'kind': 'reader-derived-pdf-index-reconciliation', 'applied': applied,
            'generation': plan['pointer']['generation'], 'index_before_sha256': hashlib.sha256(plan['before']).hexdigest(),
            'index_after_sha256': hashlib.sha256(plan['after']).hexdigest(), 'changed_entries': plan['changed'],
            'redirects_verified': plan['redirects'], 'objects_deleted': False, 'deletion_authorized': False,
            'sidecar_changed': False, 'legacy_catalog_changed': False, 'v3_pointer_changed': False,
            'external_credentials_verified': False}


def apply(store, api, plan):
    publication.assert_writer(store)
    atomic.verify_plan(store, api, {'audit': plan['snapshot'], 'old_pointer': plan['pointer']})
    if not plan['changed']:
        return result(plan, applied=True)
    raw, etag = store.read_for_update(audit.BUCKET, audit.INDEX)
    if raw != plan['before']:
        raise ValueError('PDF index changed before conditional reconciliation')
    # Verify both deployed views still use the active redirect before touching
    # the canonical PDF index. This does not establish external isolation.
    import httpx
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        publication.verify_generation(store, client, plan['pointer']['generation'])
    # The two-site check can take time; renew all inputs and the ETag after it.
    atomic.verify_plan(store, api, {'audit': plan['snapshot'], 'old_pointer': plan['pointer']})
    raw, etag = store.read_for_update(audit.BUCKET, audit.INDEX)
    if raw != plan['before']:
        raise ValueError('PDF index changed before conditional reconciliation')
    print('PDF index expected after SHA-256: ' + hashlib.sha256(plan['after']).hexdigest(), flush=True)
    store.put_if_match(audit.BUCKET, audit.INDEX, plan['after'], etag)
    actual, _ = store.read_for_update(audit.BUCKET, audit.INDEX)
    if actual != plan['after'] or store.read_bytes(audit.BUCKET, audit.INDEX) != plan['after']:
        raise ValueError('PDF index S3/Hub readback differs after reconciliation')
    if publication.current(store)[0] != plan['pointer']:
        raise ValueError('v3 pointer changed during PDF index reconciliation')
    migration.verify_copies(store, api, plan['snapshot']['planned'])
    if (store.read_bytes(audit.BUCKET, audit.INDEX) != plan['after']
            or hashlib.sha256(store.read_bytes(audit.ASSETS, audit.SIDECAR)).hexdigest()
               != plan['snapshot']['canonical_sidecar_sha256']
            or publication.current(store)[0] != plan['pointer']):
        raise ValueError('Reader roots changed after PDF index reconciliation')
    return result(plan, applied=True)
