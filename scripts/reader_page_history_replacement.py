"""Read-only, exact-copy replacements for historical derived-PDF catalogs.

prepare returns immutable candidate bytes, not publication or retirement
permission. Each history uses its own pinned base. ACKs, retention, promotion
records, the current pointer and retirement journals are never written.
Default discovery covers the scoped history inventory, including retirement
aliases. Explicit promotion selection remains partial. No plan proves a full GC
graph or allows apply.
"""
import copy
import gzip
import json

try:
    from . import reader_page_history_inventory as history_inventory
    from . import publish_reader_v3 as publication, reader_page_audit as audit
    from . import reader_page_migrate as migration, reader_page_reconcile as reconcile
    from . import reader_page_redirects as redirects, reader_page_redirect_retirement as finalization
except ImportError:
    import reader_page_history_inventory as history_inventory
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


def _plan_aliases(replacements, retirement):
    """Compose aliases through their exact existing catalog, never by loop order."""
    existing = retirement.get('rollback_replacements', {})
    aliases = copy.deepcopy(existing)
    retired = set(retirement.get('retired_paths', []))
    decisions = []
    by_ref = {(item['old_catalog']['bucket'], item['old_catalog']['path']): item
              for item in replacements}
    for generation, ref in sorted(existing.items()):
        item = by_ref.get((ref['bucket'], ref['path']))
        if item is not None:
            if item['old_catalog'] != ref or item['rollback_target'] != generation:
                raise ValueError('rollback alias conflict: ' + generation)
            aliases[generation] = copy.deepcopy(item['catalog_ref'])
        decisions.append({'generation': generation, 'previous_reference': copy.deepcopy(ref),
                          'reference': copy.deepcopy(aliases[generation]),
                          'action': 'replace-existing-target' if item else 'preserve-existing-target'})
    for item in replacements:
        generation, target = item['old_generation'], item['rollback_target']
        item['candidate_required'] = True
        if generation in existing:
            if item['old_catalog']['path'] not in retired:
                raise ValueError('rollback alias conflict: original is not retired: ' + generation)
            item['candidate_required'] = False
            item['alias_action'] = 'unnecessary-retired-original'
            decisions.append({'generation': generation, 'candidate_generation': item['generation'],
                              'reference': copy.deepcopy(aliases[generation]),
                              'action': item['alias_action']})
            continue
        if not isinstance(target, str) or not audit.SHA.fullmatch(target):
            raise ValueError('rollback alias requires a valid original generation')
        if target in aliases and aliases[target] != item['catalog_ref']:
            raise ValueError('rollback alias conflict: ' + target)
        item['alias_action'] = 'replace-existing-target' if target in existing else 'add-target'
        aliases[target] = copy.deepcopy(item['catalog_ref'])
        if target not in existing:
            decisions.append({'generation': target, 'previous_reference': None,
                              'reference': copy.deepcopy(item['catalog_ref']), 'action': 'add-target'})
    return aliases, decisions


def prepare(store, mapping, audited_proof=None, *, api=None, promotion_paths=None):
    """Plan non-current catalogs from a fresh, scope-complete inventory.

    mapping is the exact audited old->new PDF scope, with no hard-coded count.
    api defaults to store.api. A supplied audit or current-finalization plan is
    checked against a fresh exact-copy audit; it cannot bypass copy checks.
    promotion_paths optionally selects historical promotion paths explicitly.
    Candidates contain catalog_raw/packed bytes and their immutable references.
    Explicit promotion_paths retains partial promotion-only discovery. Default
    discovery binds inventory evidence and preserves existing rollback aliases.
    Neither mode authorizes publication, retirement, ACKs or deletion.
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
    report = None
    replacements, evidence, inputs = [], [], []
    retirement = {}
    if promotion_paths is None:
        report = history_inventory.inventory(store, mapping)
        if not report['scope_complete'] or report['blockers']:
            raise ValueError('history inventory is not scope complete: ' + str(report['blockers']))
        evidence = copy.deepcopy(report['evidence_resources'])
        refs_by_path = {ref['path']: ref for ref in evidence if ref['bucket'] == audit.ASSETS}
        if history_inventory.RETIREMENT in refs_by_path:
            retirement = publication.read_index(store, refs_by_path[history_inventory.RETIREMENT])
        for item in report['histories']:
            if item['is_current']:
                continue
            promotion_refs = [refs_by_path[source['source']] for source in item['sources']
                              if source['source'].startswith(PROMOTIONS)]
            inputs.append((item['generation'], item['catalog_ref'],
                           promotion_refs[0] if promotion_refs else None))
    for path in sorted(set(promotion_paths or [])):
        generation = path.removeprefix(PROMOTIONS).removesuffix('.json')
        if not audit.SHA.fullmatch(generation) or path != PROMOTIONS + generation + '.json':
            raise ValueError('unknown historical promotion path')
        if pointer and generation == pointer['generation']:
            raise ValueError('current generation is not a historical replacement input')
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
        inputs.append((generation, refs[0], publication.metadata(audit.ASSETS, path, promotion_raw)))
    for generation, old_ref, promotion_ref in inputs:
        original = publication.validate_catalog(publication.read_index(store, old_ref))
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
        if report is None:
            evidence.extend([copy.deepcopy(promotion_ref), copy.deepcopy(old_ref),
                             copy.deepcopy(original['base_sidecar'])])
        if not changed_routes and not selected:
            continue
        # Use the existing content-addressed snapshot contract. Its path cannot
        # contain the new catalog generation, which itself hashes this ref.
        packed = gzip.compress(publication.encode(rewritten), mtime=0)
        snapshot_ref = publication.metadata(audit.ASSETS,
            f'reader-index/snapshots/{finalization._sha(packed)}/reader_assets.json.gz', packed)
        candidate['base_sidecar'] = snapshot_ref
        candidate.setdefault('rollback_target', generation)
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
            'rollback_target': candidate['rollback_target'], 'old_catalog': copy.deepcopy(old_ref),
            'old_promotion': promotion_ref, 'catalog': candidate, 'catalog_raw': raw,
            'packed': packed, 'snapshot_ref': snapshot_ref, 'catalog_ref': catalog_ref,
            'replacement_refs': [catalog_ref, snapshot_ref], 'changed_routes': changed_routes,
            'redirects_removed': len(selected)})
    alias_map, alias_decisions = _plan_aliases(replacements, retirement) if report else (None, [])
    # Bind every input to the exact bytes observed, including unmodified histories.
    for ref in evidence:
        raw = store.read_bytes(ref['bucket'], ref['path'])
        if len(raw) != ref['bytes'] or finalization._sha(raw) != ref['sha256']:
            raise ValueError('historical replacement evidence changed: ' + ref['path'])
    if report:
        for path, state in report['root_observations'].items():
            if state == 'absent':
                try:
                    store.read_bytes(audit.ASSETS, path)
                except FileNotFoundError:
                    continue
                raise ValueError('historical replacement evidence changed: ' + path)
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
            'discovery_scope': report['discovery_scope'] if report else 'promotion-records-only',
            'discovery_mode': 'inventory' if report else 'explicit',
            'history_inventory_complete': report is not None,
            'history_inventory': report,
            'unsupported_alias_inventory': {} if report else {'retirement_aliases': 'not-inspected'},
            'rollback_replacements': alias_map, 'alias_decisions': alias_decisions,
            'graph_complete': False, 'fully_unreferenced': False,
            'apply_allowed': False,
            'replacements': replacements, 'applied': False, 'consumer_acks_created': False,
            'deletion_authorized': False, 'objects_deleted': False}
