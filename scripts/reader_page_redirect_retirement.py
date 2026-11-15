"""Finalize only the current generation's completed derived PDF redirects.

The caller owns the serialized writer window. Old catalogs and promotion
records remain protected until separately retired with real consumer ACKs.
Only old bundles selected by the active mapping are finalized. Unrelated
derived references remain unchanged and protected. This operation neither
deletes objects nor supplies deletion authority.
"""
import copy
import hashlib
import json

try:
    from . import reader_page_atomic as atomic, reader_page_audit as audit
    from . import reader_page_migrate as migration, reader_page_reconcile as reconcile
    from . import reader_page_redirects as redirects, reader_route_control as control
    from . import publish_reader_v3 as publication
except ImportError:
    import reader_page_atomic as atomic
    import reader_page_audit as audit
    import reader_page_migrate as migration
    import reader_page_reconcile as reconcile
    import reader_page_redirects as redirects
    import reader_route_control as control
    import publish_reader_v3 as publication


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _no_selected_bundle(value, label, old_bundles):
    # Check unknown fields too, but only for the selected old bundle roots or
    # their descendants (including manifests). Other derived bundles and old
    # historical generations remain independent protection roots.
    if isinstance(value, dict):
        for key, child in value.items():
            _no_selected_bundle(key, label, old_bundles)
            _no_selected_bundle(child, label, old_bundles)
    elif isinstance(value, list):
        for child in value:
            _no_selected_bundle(child, label, old_bundles)
    elif isinstance(value, str) and any(value == root or value.startswith(root + '/') for root in old_bundles):
        raise ValueError(label + ' still references selected old derived bundle')


def _projection(base, catalog):
    projected = publication.project_sidecar(base, catalog)
    # These two fields necessarily change with a new catalog. Compare every
    # other field, including top-level metadata and the complete route table.
    del projected['v3']['generation']
    del projected['v3']['catalog_sha256']
    return projected


def _verify_resources(store, plan):
    for ref in plan['evidence_resources']:
        raw = store.read_bytes(ref['bucket'], ref['path'])
        if len(raw) != ref['bytes'] or _sha(raw) != ref['sha256']:
            raise ValueError('finalize evidence resource changed: ' + ref['path'])


def _verify(store, api, plan):
    atomic.verify_plan(store, api, plan)
    _verify_resources(store, plan)


def prepare(store, api, *, expected_index, expected_sidecar, expected_catalog,
            expected_generation, workers=4):
    """Read-only plan bound to exact canonical roots and the current v3 catalog."""
    expected = dict(expected_index=expected_index, expected_sidecar=expected_sidecar,
                    expected_catalog=expected_catalog, expected_generation=expected_generation)
    if any(not isinstance(value, str) or not audit.SHA.fullmatch(value) for value in expected.values()):
        raise ValueError('exact PDF index, sidecar, legacy catalog and v3 SHA-256 identities are required')
    reconciled = reconcile.prepare(store, api, expected_index=expected_index,
                                   expected_generation=expected_generation, workers=workers)
    if reconciled['changed']:
        raise ValueError('canonical PDF index migration is not complete')
    pointer, current = publication.current(store)
    if pointer != reconciled['pointer']:
        raise ValueError('v3 pointer changed during finalize planning')
    mapping = redirects.validated(current)
    snapshot = reconciled['snapshot']
    if snapshot['canonical_sidecar_sha256'] != expected_sidecar:
        raise ValueError('canonical sidecar changed during finalize audit')
    by_old = reconcile.validate_snapshot(snapshot, mapping)
    if set(by_old) != set(mapping) or any(row['index_location'] != 'objects' for row in by_old.values()):
        raise ValueError('every audited PDF must be a completed active redirect')
    old_bundles = sorted({old.rsplit('/', 1)[0] for old in mapping})
    _no_selected_bundle(json.loads(reconciled['before']), 'canonical PDF index', old_bundles)
    canonical = store.read_bytes(audit.ASSETS, audit.SIDECAR)
    legacy_raw = store.read_bytes(audit.ASSETS, migration.CATALOG)
    if _sha(canonical) != expected_sidecar or _sha(legacy_raw) != expected_catalog:
        raise ValueError('canonical sidecar or legacy catalog changed after audit')
    base = audit.decode_sidecar(canonical)
    legacy = json.loads(legacy_raw)
    if 'redirect_sidecar_pending' in legacy:
        raise ValueError('sidecar reconciliation is pending')
    current_id = (legacy.get('current') or {}).get('generation')
    entry = (legacy.get('generations') or {}).get(current_id)
    if (legacy.get('version') != 1 or legacy.get('kind') != 'reader-catalog'
            or not isinstance(entry, dict) or entry.get('generation') != current_id
            or entry.get('status') != 'current'
            or entry.get('sidecar_sha256') != _sha(audit.encode(base))):
        raise ValueError('current legacy generation does not bind canonical sidecar')
    refs = entry.get('references')
    if not isinstance(refs, list) or any(not isinstance(ref, dict)
            or not isinstance(ref.get('bucket'), str) or not isinstance(ref.get('path'), str) for ref in refs):
        raise ValueError('current legacy references are invalid')
    if not {(ref['bucket'], ref['path']) for ref in publication.reader_lifecycle.sidecar_references(base)} <= {
            (ref['bucket'], ref['path']) for ref in refs}:
        raise ValueError('current legacy references do not cover canonical sidecar')
    _no_selected_bundle(base, 'canonical sidecar', old_bundles)
    _no_selected_bundle(entry, 'current legacy generation', old_bundles)
    pinned = publication.pinned_base(store, current, {})
    _no_selected_bundle(pinned, 'pinned base', old_bundles)
    candidate = copy.deepcopy(current)
    del candidate[redirects.FIELD]
    del candidate['generation']
    candidate['parent_generation'] = pointer['generation']
    _no_selected_bundle(candidate, 'current catalog outside redirect table', old_bundles)
    packed = store.read_bytes(current['base_sidecar']['bucket'], current['base_sidecar']['path'])
    # Keep the exact pinned bytes; canonical routes must already agree after
    # existing suppressions. No route rewrite is part of this operation.
    canonical_packed, _ = control.prepare_base(candidate, canonical)
    if audit.decode_sidecar(canonical_packed) != pinned:
        raise ValueError('canonical sidecar differs from full pinned base')
    generation = publication.text.digest(candidate)
    candidate['generation'] = generation
    publication.validate_catalog(candidate)
    if _projection(pinned, current) != _projection(pinned, candidate):
        raise ValueError('full Reader projection changed during finalization')
    raw = publication.encode(candidate)
    ref = publication.metadata(audit.ASSETS, f'reader-index/v3/generations/{generation}/catalog.json', raw)
    new_pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': generation,
                   'catalog': ref, 'resources': [ref]}
    evidence = [copy.deepcopy(current['base_sidecar'])]
    # A current legacy snapshot, when present, must describe the same base.
    legacy_ref = entry.get('snapshot')
    if legacy_ref is not None:
        if (not isinstance(legacy_ref, dict) or legacy_ref.get('bucket') != audit.ASSETS
                or legacy_ref.get('path') != f'reader-index/generations/{current_id}/reader_assets.json.gz'
                or not audit.SHA.fullmatch(str(legacy_ref.get('sha256', '')))
                or type(legacy_ref.get('bytes')) is not int or legacy_ref['bytes'] < 1):
            raise ValueError('invalid current legacy snapshot identity')
        legacy_packed = store.read_bytes(legacy_ref['bucket'], legacy_ref['path'])
        if audit.decode_sidecar(legacy_packed) != base:
            raise ValueError('current legacy snapshot differs from canonical sidecar')
        evidence.append(copy.deepcopy(legacy_ref))
    snapshot['legacy_catalog_sha256'] = expected_catalog
    plan = {'audit': snapshot, 'old_pointer': pointer, 'pointer': new_pointer,
            'catalog': candidate, 'catalog_raw': raw, 'packed': packed,
            'snapshot_ref': copy.deepcopy(current['base_sidecar']), 'changed_routes': 0,
            'expected': expected, 'evidence_resources': evidence, 'redirects_removed': len(mapping),
            'finalized_redirects': len(mapping), 'finalized_old_bundles': old_bundles,
            'finalization_scope': 'active-redirect-old-bundles',
            'unrelated_derived_references_preserved': True}
    _verify(store, api, plan)
    return plan


def report(plan, *, applied):
    return {'version': 1, 'kind': 'reader-derived-pdf-redirect-finalization',
            'action': 'finalize-derived-redirects', 'applied': applied,
            'previous_generation': plan['old_pointer']['generation'],
            'generation': plan['pointer']['generation'], 'redirects_removed': plan['redirects_removed'],
            'finalized_redirects': plan['finalized_redirects'],
            'finalized_old_bundles': plan['finalized_old_bundles'],
            'finalization_scope': plan['finalization_scope'],
            'unrelated_derived_references_preserved': True,
            'current_generation_redirect_protection_ended': applied,
            'historical_generations_require_retirement': True, 'consumer_acks_created': False,
            'canonical_indexes_changed': False, 'objects_deleted': False,
            'deletion_authorized': False, 'external_credentials_verified': False}


class _VerifiedStore:
    """Add evidence checks at atomic publication boundaries without global patches."""
    def __init__(self, store, api, plan):
        self.store, self.api, self.plan = store, api, plan

    def __getattr__(self, name):
        return getattr(self.store, name)

    def immutable_put(self, bucket, path, raw):
        _verify_resources(self.store, self.plan)
        return self.store.immutable_put(bucket, path, raw)

    def put_if_match(self, bucket, path, raw, etag):
        if (bucket, path, raw) != (audit.ASSETS, publication.POINTER, publication.encode(self.plan['pointer'])):
            raise ValueError('finalize may conditionally write only the expected v3 pointer')
        _verify(self.store, self.api, self.plan)
        return self.store.put_if_match(bucket, path, raw, etag)


def apply(store, api, plan):
    """Rebuild evidence, stage immutable objects and CAS via the atomic publisher."""
    publication.assert_writer(store)
    fresh = prepare(store, api, **plan['expected'])
    # Audit timestamps are observational, not identity. Everything else,
    # including targets, bytes, hashes and the candidate, must match exactly.
    def stable(value):
        value = copy.deepcopy(value)
        value['audit'].pop('scanned_at', None)
        return value
    if stable(fresh) != stable(plan):
        raise ValueError('finalize plan differs from fresh evidence or expected identity')
    atomic.apply(_VerifiedStore(store, api, fresh), api, fresh)
    _verify_resources(store, fresh)
    # atomic.apply has already verified copies and the new pointer. Recheck
    # canonical roots too before reporting success after publication.
    atomic.verify_plan(store, api, {**fresh, 'old_pointer': fresh['pointer']})
    return report(fresh, applied=True)
