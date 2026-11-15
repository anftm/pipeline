"""Read-only, exact-copy replacements for historical derived-PDF catalogs.

prepare returns immutable candidate bytes, not publication or retirement
permission. Each history uses its own pinned base. ACKs, retention, promotion
records, the current pointer and retirement journals are never written.
Discovery covers promotion records only, not retirement aliases or a complete
history inventory. Every returned plan is incomplete and disallows apply.
"""
import copy
import gzip
import json

try:
    from . import publish_reader_v3 as publication, reader_page_audit as audit
    from . import reader_page_migrate as migration, reader_page_reconcile as reconcile
    from . import reader_page_redirects as redirects, reader_page_redirect_retirement as finalization
except ImportError:
    import publish_reader_v3 as publication
    import reader_page_audit as audit
    import reader_page_migrate as migration
    import reader_page_reconcile as reconcile
    import reader_page_redirects as redirects
    import reader_page_redirect_retirement as finalization


PREFIX = 'reader-index/v3/history-replacements/derived-pdf/'
PROMOTIONS = 'reader-index/v3/promotions/'


def _available(store, ref, raw):
    """An already staged immutable candidate may match, but never conflict."""
    try:
        prior = store.read_bytes(ref['bucket'], ref['path'])
    except FileNotFoundError:
        return
    if prior != raw:
        raise ValueError('immutable history replacement conflict: ' + ref['path'])


def prepare(store, mapping, audited_proof=None, *, api=None, promotion_paths=None):
    """Plan histories referenced by non-current promotion records only.

    mapping is the exact audited old->new PDF scope, with no hard-coded count.
    api defaults to store.api. A supplied audit or current-finalization plan is
    checked against a fresh exact-copy audit; it cannot bypass copy checks.
    promotion_paths optionally selects historical promotion paths explicitly.
    Candidates contain catalog_raw/packed bytes and their immutable references.
    Retirement aliases are not inventoried, even with default discovery. These
    plans never establish complete history coverage or allow apply. Future
    integration must resolve that inventory, revalidate evidence and gate ACKs.
    """
    mapping = copy.deepcopy(redirects.validated({redirects.FIELD: mapping}))
    if not mapping:
        raise ValueError('an audited PDF mapping is required')
    if any(part in {'.', '..'} for pair in mapping.items() for path in pair for part in path.split('/')):
        raise ValueError('dot segments are unsupported in PDF mapping paths')
    api = api if api is not None else getattr(store, 'api', None)
    if api is None:
        raise ValueError('a bucket API is required for the exact-copy audit')
    pointer, _ = publication.current(store)
    snapshot = audit.audit(store, api, active_redirects=mapping)
    rows = reconcile.validate_snapshot(snapshot, mapping)
    if set(rows) != set(mapping) or any(row['index_location'] != 'objects' for row in rows.values()):
        raise ValueError('mapping must exactly cover the completed audited PDF copies')
    if audited_proof is not None:
        proof = audited_proof.get('audit', audited_proof)
        reconcile.validate_snapshot(proof, mapping)
        for field in ('bucket', 'index_sha256', 'canonical_sidecar_sha256', 'planned'):
            if proof.get(field) != snapshot.get(field):
                raise ValueError('supplied PDF audit differs from fresh exact-copy evidence')
    migration.verify_copies(store, api, snapshot['planned'])
    roots = sorted({old.rsplit('/', 1)[0] for old in mapping})
    paths = (store.list_files(audit.ASSETS, (PROMOTIONS,))
             if promotion_paths is None else promotion_paths)
    replacements, evidence = [], []
    for path in sorted(set(paths)):
        generation = path.removeprefix(PROMOTIONS).removesuffix('.json')
        if not audit.SHA.fullmatch(generation) or path != PROMOTIONS + generation + '.json':
            raise ValueError('unknown historical promotion path')
        if pointer and generation == pointer['generation']:
            if promotion_paths is not None:
                raise ValueError('current generation is not a historical replacement input')
            continue
        promotion_raw = store.read_bytes(audit.ASSETS, path)
        promotion = json.loads(promotion_raw)
        if (promotion.get('version') != 1 or promotion.get('kind') != 'reader-reading-promotion'
                or promotion.get('generation') != generation):
            raise ValueError('invalid historical promotion identity')
        finalization._no_selected_bundle(promotion, 'historical promotion outside catalog reference', roots)
        refs = promotion.get('resources')
        catalog_path = f'reader-index/v3/generations/{generation}/catalog.json'
        if (not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict)
                or refs[0].get('bucket') != audit.ASSETS or refs[0].get('path') != catalog_path):
            raise ValueError('unknown historical promotion resource reference')
        old_ref = refs[0]
        original = publication.validate_catalog(publication.read_index(store, old_ref))
        if 'rollback_target' in original:
            raise ValueError('historical catalog with an existing rollback_target is unsupported')
        if original['generation'] != generation or not original.get('base_sidecar'):
            raise ValueError('historical catalog requires its own generation and pinned base')
        base = publication.pinned_base(store, original, {})
        rewritten, changed_routes = redirects.rewrite(base, mapping)
        finalization._no_selected_bundle(rewritten, 'historical pinned base', roots)
        candidate = copy.deepcopy(original)
        prior_redirects = redirects.validated(original)
        selected = set(prior_redirects) & set(mapping)
        if any(prior_redirects[old] != mapping[old] for old in selected):
            raise ValueError('historical redirect differs from audited exact copy')
        if selected:
            remaining = {old: new for old, new in prior_redirects.items() if old not in selected}
            if remaining:
                candidate[redirects.FIELD] = remaining
            else:
                del candidate[redirects.FIELD]
        finalization._no_selected_bundle(candidate, 'historical catalog outside selected redirects', roots)
        promotion_ref = publication.metadata(audit.ASSETS, path, promotion_raw)
        evidence.extend([promotion_ref, copy.deepcopy(old_ref), copy.deepcopy(original['base_sidecar'])])
        if not changed_routes and not selected:
            continue
        # Use the existing content-addressed snapshot contract. Its path cannot
        # contain the new catalog generation, which itself hashes this ref.
        packed = gzip.compress(publication.encode(rewritten), mtime=0)
        snapshot_ref = publication.metadata(audit.ASSETS,
            f'reader-index/snapshots/{finalization._sha(packed)}/reader_assets.json.gz', packed)
        candidate['base_sidecar'] = snapshot_ref
        candidate['rollback_target'] = generation
        del candidate['generation']
        candidate['generation'] = publication.text.digest(candidate)
        publication.validate_catalog(candidate)
        expected, _ = redirects.rewrite(finalization._projection(base, original), mapping)
        if finalization._projection(rewritten, candidate) != expected:
            raise ValueError('full Reader projection differs from exact historical substitution')
        raw = publication.encode(candidate)
        catalog_ref = publication.metadata(audit.ASSETS,
            f"{PREFIX}{generation}/{candidate['generation']}/catalog.json", raw)
        _available(store, snapshot_ref, packed)
        _available(store, catalog_ref, raw)
        replacements.append({
            'old_generation': generation, 'generation': candidate['generation'],
            'rollback_target': generation, 'old_catalog': copy.deepcopy(old_ref),
            'old_promotion': promotion_ref, 'catalog': candidate, 'catalog_raw': raw,
            'packed': packed, 'snapshot_ref': snapshot_ref, 'catalog_ref': catalog_ref,
            'replacement_refs': [catalog_ref, snapshot_ref], 'changed_routes': changed_routes,
            'redirects_removed': len(selected)})
    # Bind every input to the exact bytes observed, including unmodified histories.
    for ref in evidence:
        raw = store.read_bytes(ref['bucket'], ref['path'])
        if len(raw) != ref['bytes'] or finalization._sha(raw) != ref['sha256']:
            raise ValueError('historical replacement evidence changed: ' + ref['path'])
    migration.verify_copies(store, api, snapshot['planned'])
    if (publication.current(store)[0] != pointer
            or finalization._sha(store.read_bytes(audit.BUCKET, audit.INDEX)) != snapshot['index_sha256']
            or finalization._sha(store.read_bytes(audit.ASSETS, audit.SIDECAR)) != snapshot['canonical_sidecar_sha256']):
        raise ValueError('PDF audit roots changed during history replacement planning')
    # Observational scan time is deliberately excluded from deterministic plans.
    snapshot.pop('scanned_at', None)
    return {'version': 1, 'kind': 'reader-derived-pdf-history-replacement-plan',
            'mapping': mapping, 'audit': snapshot, 'old_pointer': pointer,
            'selected_old_bundles': roots, 'evidence_resources': evidence,
            'discovery_scope': 'promotion-records-only',
            'discovery_mode': 'listed' if promotion_paths is None else 'explicit',
            'history_inventory_complete': False,
            'unsupported_alias_inventory': {'retirement_aliases': 'not-inspected'},
            'apply_allowed': False,
            'replacements': replacements, 'applied': False, 'consumer_acks_created': False,
            'deletion_authorized': False, 'objects_deleted': False}
