#!/usr/bin/env python3
"""Narrow CHM EPUB retirement gates and exact, journalled batch helper.

The batch helper is not an execution entry point; callers must establish the
actual four-bucket and two-consumer barriers before invoking it.
"""
import json

try:
    from . import chm_epub_publication_preflight as chm, publish_reader_v3 as publication
except ImportError:
    import chm_epub_publication_preflight as chm
    import publish_reader_v3 as publication


def require_projection_acceptance(plan, reports):
    """Both deployed Readers must acknowledge the planned exact generation."""
    generation = plan['new_v3_generation']
    pointer = json.loads(plan['overrides']['reader-index/v3/current.json'])
    ref = pointer['catalog']
    if pointer.get('generation') != generation or ref.get('sha256') is None:
        raise ValueError('planned CHM Reader pointer is inconsistent')
    if set(reports) != {'hf', 'pages'}:
        raise ValueError('both Reader surfaces must be verified')
    for surface, receipt in reports.items():
        if (receipt.get('version') != 1 or receipt.get('surface') != surface
                or receipt.get('active') is not True or receipt.get('generation') != generation
                or receipt.get('catalog_sha256') != ref['sha256']
                or receipt.get('projection_verified') is not True):
            raise ValueError('Reader surface has not accepted the CHM replacement: ' + surface)
    return {'hf': True, 'pages': True}


def observe_planned_projection(store, client, plan):
    """Read both deployed surfaces and compare their complete planned routes."""
    import gzip
    pointer = json.loads(plan['overrides']['reader-index/v3/current.json'])
    catalog = publication.validate_catalog(json.loads(plan['immutable'][pointer['catalog']['path']]))
    base = json.loads(gzip.decompress(plan['immutable'][catalog['base_sidecar']['path']]))
    expected = publication.project_sidecar(base, catalog)['f']
    urls = {'hf': ('https://voiceofml-search.hf.space/api/reader-v3-status',
                   'https://voiceofml-search.hf.space/api/reader-assets'),
            'pages': ('https://vomebook.github.io/search/data/reader_v3_receipt.json',
                      'https://vomebook.github.io/search/data/reader_assets.json.gz')}
    reports = {}
    for surface, (status_url, projection_url) in urls.items():
        status_response = client.get(status_url, headers={'Cache-Control': 'no-cache'}, timeout=60)
        status_response.raise_for_status()
        receipt = status_response.json()
        response = client.get(projection_url, headers={'Cache-Control': 'no-cache'}, timeout=120)
        response.raise_for_status()
        raw = response.content
        if surface == 'pages':
            if (receipt.get('reader_contract') != 'pdf-reading-v3-v1'
                    or chm.digest(raw) != receipt.get('sidecar_sha256')):
                raise ValueError('Pages sidecar receipt does not match the deployed projection')
            raw = gzip.decompress(raw)
        projection = json.loads(raw)
        if projection.get('v') != 1 or projection.get('f') != expected:
            raise ValueError('Reader surface does not serve the complete planned projection: ' + surface)
        reports[surface] = {**receipt, 'projection_verified': True, 'checked_url': status_url}
    require_projection_acceptance(plan, reports)
    if publication.current(store)[0] != pointer:
        raise ValueError('Reader pointer changed during consumer acceptance')
    return reports


def validate_document_reset_identity(plan, reports):
    """Validate the planned receipt identity without authorizing document GC.

    This intentionally does not replace the existing reset guard's checks of
    registries, current projections, and CCRD controls.
    """
    require_projection_acceptance(plan, reports)
    overrides = plan['overrides']
    immutable = plan['immutable']
    receipt = json.loads(overrides['reader-index/gc/document-reset/withdrawal.json'])
    pointer = json.loads(overrides['reader-index/v3/current.json'])
    catalog = publication.validate_catalog(json.loads(immutable[pointer['catalog']['path']]))

    if (receipt.get('kind') != 'reader-document-reset' or receipt.get('status') != 'withdrawn'
            or pointer.get('generation') != receipt.get('generation') or catalog.get('files')
            or catalog.get('document_reset') != receipt.get('reset_id')
            or catalog.get('base_sidecar') != receipt.get('base_snapshot')):
        raise ValueError('planned document reset receipt and v3 identity disagree')
    return {'document_reset': receipt['reset_id'], 'identity_compatible': True,
            'remote_mutations': 0, 'document_collection_authorized': False}


def verify_document_reset_prerequisites(store, plan):
    """Verify no document/DjVu route is introduced by the CHM replacement.

    This is a read-only prerequisite, not a request to run the broad document
    collector; its separate preflight retains full authority over that scope.
    """
    import gzip
    try:
        from . import reader_gc_documents as documents, reader_document_reset as reset
    except ImportError:
        import reader_gc_documents as documents
        import reader_document_reset as reset
    receipt = json.loads(plan['overrides'][reset.RECEIPT])
    pointer = json.loads(plan['overrides'][publication.POINTER])
    catalog = publication.validate_catalog(json.loads(plan['immutable'][pointer['catalog']['path']]))
    if (receipt.get('reset_id') != catalog.get('document_reset') or receipt.get('status') != 'withdrawn'
            or receipt.get('generation') != pointer['generation'] or catalog['files']
            or catalog.get('base_sidecar') != receipt.get('base_snapshot')):
        raise ValueError('document reset identity cannot be preserved')
    sidecar = json.loads(gzip.decompress(plan['overrides'][reset.SIDECAR]))
    derived_raw = store.read_bytes(reset.PAGES, reset.DERIVED)
    derived = reset.decode(derived_raw)
    if any(reset.is_djvu(entry) for entry in derived['files']):
        raise ValueError('DjVu derived registry was repopulated')
    documents.withdrawn_sidecar(sidecar, derived)
    hashes = {'derived': chm.digest(derived_raw)}
    for name in reset.REGISTRIES:
        raw = store.read_bytes(reset.ASSETS, 'reader-index/' + name)
        if reset.decode(raw).get('files') != {}:
            raise ValueError('document registry was repopulated: ' + name)
        hashes[name] = chm.digest(raw)
    ccrd_path = f"reports/ccrd-ocr-reset/{receipt['reset_id']}/withdrawal.json"
    ccrd_raw = store.read_bytes('vomebook/ccrd-index', ccrd_path)
    if reset.decode(ccrd_raw).get('status') != 'withdrawn':
        raise ValueError('previous OCR withdrawal receipt is not complete')
    try:
        store.read_bytes('vomebook/ccrd-index', 'control/ocr-current.json')
    except FileNotFoundError:
        assert_all_buckets_available(store)
    else:
        raise ValueError('OCR pointer was republished')
    hashes['ccrd_withdrawal'] = chm.digest(ccrd_raw)
    return {'reset_id': receipt['reset_id'], 'registry_sha256': hashes,
            'document_collection_authorized': False, 'remote_mutations': 0}


def verify_prepared_chapter_retention(plan):
    """Every retained current/historical CHM bundle must have an exact root pin."""
    retention = json.loads(plan['overrides']['reader-index/chm-epub-withdrawal-retention.json'])
    if (retention.get('kind') != 'reader-chm-epub-withdrawal-retention'
            or retention.get('inventory_sha256') != plan.get('inventory_sha256')):
        raise ValueError('CHM reading retention root has the wrong identity')
    refs = retention.get('resources')
    if not isinstance(refs, list):
        raise ValueError('CHM reading retention references are missing')
    paths = [(r.get('bucket'), r.get('path')) for r in refs if isinstance(r, dict)]
    if len(paths) != len(refs) or len(paths) != len(set(paths)):
        raise ValueError('CHM reading retention references are invalid or duplicated')
    wanted = {(chm.ASSETS, path) for path in plan['chapter_manifests_to_preserve']}
    if not wanted <= set(paths):
        raise ValueError('CHM reading retention omits a preserved chapter manifest')
    return {'protected_chapter_manifests': len(wanted), 'retention_resources': len(refs)}


def verify_originals_and_chapter_retention(before, after, chapter_roots):
    """Prove a bounded metadata transition leaves all unrelated paths intact."""
    expected = set(before)
    observed = set(after)
    if expected != observed:
        raise ValueError('unrelated object inventory changed during CHM retirement')
    for key in expected:
        if before[key] != after[key]:
            raise ValueError('unrelated object identity changed during CHM retirement')
    for root in chapter_roots:
        if not any(path.startswith(root + '/') for bucket, path in observed if bucket == chm.ASSETS):
            raise ValueError('preserved CHM chapter bundle disappeared')
    return {'unrelated_objects_verified': len(expected), 'chapter_roots_verified': len(chapter_roots),
            'remote_mutations': 0}


def before_values(store, paths):
    """Capture exact current bytes for resumable compare-before-write planning."""
    values = {}
    for path in sorted(paths):
        try:
            raw = store.read_bytes(chm.ASSETS, path)
        except FileNotFoundError:
            raw = None
        values[path] = {'present': raw is not None, 'sha256': chm.digest(raw) if raw is not None else None,
                        'bytes': len(raw) if raw is not None else None}
    return values


def enforce_transition(path, current, expected_before, planned_after):
    """Classify a control as original/already-applied; reject every other value."""
    if current == planned_after:
        return 'already-applied'
    if expected_before is None:
        if current is not None:
            raise ValueError('new CHM control unexpectedly exists: ' + path)
    elif current != expected_before:
        raise ValueError('CHM control changed outside the exact plan: ' + path)
    return 'ready'


def require_exact_epub_inventory(plan, rows):
    """A publication plan cannot widen its supplied, independently hashed set."""
    try:
        from . import chm_epub_withdrawal_preview as preview
    except ImportError:
        import chm_epub_withdrawal_preview as preview
    if (plan.get('inventory_sha256') != preview.INVENTORY_SHA256
            or plan.get('target_count') != len(rows) or len(rows) != 972
            or plan.get('target_bytes') != sum(row['bytes'] for row in rows)
            or plan.get('target_bytes') != preview.DATA_BYTES):
        raise ValueError('CHM retirement plan differs from the exact reviewed inventory')
    if len({(row['bucket'], row['path']) for row in rows}) != len(rows):
        raise ValueError('CHM retirement inventory contains duplicates')


def require_actual_zero_graph(proof, rows):
    """Never use hypothetical CHM graph evidence as deletion authority."""
    if (proof.get('graph_complete') is not True or proof.get('mode') != 'actual'
            or proof.get('target_count') != len(rows) or proof.get('retained_targets') != 0
            or proof.get('origins') or proof.get('ambiguous_paths') or proof.get('blockers')
            or proof.get('root_recheck_complete') is not True
            or proof.get('reader_acceptance_complete') is not True):
        raise ValueError('actual CHM graph and two-site acceptance have not cleared every target')


def actual_graph_evidence(store, fence, client, rows, plan, *, workers=8, deadline_minutes=300):
    """Build current four-bucket reachability and recheck roots from real bytes."""
    import time
    try:
        from . import reader_gc_graph as graph, reader_gc_collect as collector
    except ImportError:
        import reader_gc_graph as graph
        import reader_gc_collect as collector
    reports = observe_planned_projection(store, client, plan)
    assert_all_buckets_available(store)
    fence.check()
    if hasattr(store, 'read_cache'):
        store.read_cache = None
    store.root_predicate = graph.is_root
    traversal = graph.ReferenceGraph(store, workers=workers, max_rounds=40,
                                     deadline=time.monotonic() + deadline_minutes * 60)
    report = traversal.build()
    if not report.get('graph_complete') or report.get('blockers') or report.get('ambiguous_paths'):
        raise ValueError('actual CHM four-bucket graph is incomplete or ambiguous')
    targets = {(row['bucket'], row['path']) for row in rows}
    retained = targets & traversal.references
    origins = [{'bucket': bucket, 'path': path, 'targets': len(refs & targets)}
               for (bucket, path), refs in traversal.direct.items() if refs & targets]
    fence.check()
    collector.verify_roots(store, traversal)
    reports_after = observe_planned_projection(store, client, plan)
    for site in ('hf', 'pages'):
        for field in ('generation', 'catalog_sha256', 'surface'):
            if reports[site].get(field) != reports_after[site].get(field):
                raise ValueError('Reader consumer changed during CHM graph traversal: ' + site)
    expected_roots = {(bucket, path) for bucket, files in traversal.files.items()
                      for path in files if graph.is_root(path, bucket)}
    unrecorded = expected_roots - set(traversal.root_hashes) - traversal.retired_roots
    if unrecorded or set(traversal.files) != set(graph.BUCKETS):
        raise ValueError('actual CHM graph did not read every active four-bucket root')
    proof = {'mode': 'actual', 'graph_complete': True, 'target_count': len(targets),
             'retained_targets': len(retained), 'origins': origins, 'blockers': [], 'ambiguous_paths': [],
             'root_recheck_complete': True, 'reader_acceptance_complete': True,
             'root_count': len(report['roots']), 'remote_mutations': 0,
             'root_members': [{'bucket': bucket, 'path': path} for bucket, path in sorted(expected_roots)],
             'root_sha256': [{'bucket': b, 'path': p, 'sha256': sha}
                             for (b, p), sha in sorted(traversal.root_hashes.items())],
             'retired_roots': [{'bucket': b, 'path': p} for b, p in sorted(traversal.retired_roots)]}
    return proof, traversal


def validate_delete_batch(rows, batch):
    allowed = {(row['bucket'], row['path']) for row in rows}
    if not batch or len(batch) > 100 or len(batch) != len(set(batch)) or not set(batch) <= allowed:
        raise ValueError('CHM delete batch exceeds the reviewed exact list')
    return True


def recheck_actual_graph(store, traversal, proof, rows, fence, client, plan):
    """Repeat root membership and SHA checks immediately before deletion."""
    from concurrent.futures import ThreadPoolExecutor
    try:
        from . import reader_gc_collect as collector
    except ImportError:
        import reader_gc_collect as collector
    require_actual_zero_graph(proof, rows)
    assert_all_buckets_available(store)
    fence.check()
    collector.verify_roots(store, traversal)
    expected = {(row['bucket'], row['path']): row['sha256'] for row in proof['root_sha256']}
    members = {(row['bucket'], row['path']) for row in proof['root_members']}
    retired = {(row['bucket'], row['path']) for row in proof['retired_roots']}
    if not (members - retired) <= set(expected) or len(expected) != len(proof['root_sha256']):
        raise ValueError('CHM root fingerprint evidence is incomplete')
    def check(item):
        ref, digest = item
        if store.digest(*ref) != digest:
            raise ValueError('CHM graph root changed before deletion: ' + ':'.join(ref))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(check, sorted(expected.items())))
    observe_planned_projection(store, client, plan)
    return True


def verify_live_chm_controls(store, plan):
    """Require every staged CHM publication control to match the exact plan."""
    assert_all_buckets_available(store)
    for path, expected in plan['overrides'].items():
        if store.read_bytes(chm.ASSETS, path) != expected:
            raise ValueError('live CHM control differs from the approved plan: ' + path)
    for path, expected in plan['immutable'].items():
        if store.read_bytes(chm.ASSETS, path) != expected:
            raise ValueError('live CHM immutable replacement differs from the approved plan: ' + path)
    return True


def validate_preserved_catalog_roots(plan, old_catalogs, rows):
    """Every withdrawn v3 root must have an independently verified successor."""
    import gzip
    targets = {row['path'] for row in rows}
    aliases = plan.get('rollback_aliases', {})
    if set(aliases) != set(old_catalogs):
        raise ValueError('CHM rollback history does not have exact replacement aliases')
    journal = json.loads(plan['overrides']['reader-index/v3/retirement.json'])
    retained_paths = {(ref['bucket'], ref['path']) for ref in journal['resources']}
    for generation, old_catalog in old_catalogs.items():
        publication.validate_catalog(old_catalog)
        alias = aliases[generation]
        if (alias['bucket'], alias['path']) not in retained_paths:
            raise ValueError('CHM rollback replacement is not protected by the retirement root')
        raw = plan['immutable'].get(alias['path'])
        if raw is None or chm.digest(raw) != alias['sha256'] or len(raw) != alias['bytes']:
            raise ValueError('CHM rollback replacement catalog checksum mismatch')
        replacement = publication.validate_catalog(json.loads(raw))
        if replacement.get('rollback_target') != generation or replacement['files'] != old_catalog['files']:
            raise ValueError('CHM rollback replacement changes unrelated v3 overrides')
        base_ref = replacement['base_sidecar']
        packed = plan['immutable'].get(base_ref['path'])
        if packed is None or chm.digest(packed) != base_ref['sha256'] or len(packed) != base_ref['bytes']:
            raise ValueError('CHM rollback replacement sidecar checksum mismatch')
        base = json.loads(gzip.decompress(packed))
        if any(isinstance(value, str) and value in targets for mapping in base['f'].values()
               if isinstance(mapping, dict) for value in mapping.values()):
            raise ValueError('CHM rollback replacement still contains an approved EPUB')
        expected_retired = {f'reader-index/v3/generations/{generation}/catalog.json',
                            f'reader-index/v3/promotions/{generation}.json'}
        if not expected_retired <= set(journal.get('retired_paths', [])):
            raise ValueError('CHM old history roots were not explicitly retired')
        if journal.get('rollback_replacements', {}).get(generation) != alias:
            raise ValueError('CHM rollback alias is not recorded in the history journal')
    return True


def validate_old_base_routes(store, plan, old_catalogs, rows):
    """Compare sanitized rollback bases with independently verified originals."""
    import gzip
    targets = {row['path'] for row in rows}
    for generation, old_catalog in old_catalogs.items():
        old_base = publication.pinned_base(store, old_catalog, None)
        expected, _ = chm.sanitize_historical_v3_base(old_base, targets)
        alias_ref = plan['rollback_aliases'][generation]
        replacement = publication.validate_catalog(json.loads(plan['immutable'][alias_ref['path']]))
        actual = json.loads(gzip.decompress(plan['immutable'][replacement['base_sidecar']['path']]))
        if actual != expected:
            raise ValueError('sanitized rollback changes preserved historical routes: ' + generation)
    return True


def confirm_initial_snapshot(store, plan):
    """Capture original control hashes without accepting changes since planning."""
    assert_all_buckets_available(store)
    paths = set(plan['overrides']) | set(plan['immutable'])
    values = before_values(store, paths)
    expected = {**plan['index_hashes_before'], chm.SIDECAR: plan['sidecar_sha256_before'],
                chm.CATALOG: plan['catalog_sha256_before']}
    # Source pointer formatting may differ; compare decoded identity separately.
    live_pointer, _ = publication.current(store)
    if live_pointer != plan['v3_pointer_before']:
        raise ValueError('Reader pointer moved after CHM planning')
    for path, digest in expected.items():
        if values[path]['sha256'] != digest:
            raise ValueError('CHM control changed after planning: ' + path)
    for path, raw in plan['immutable'].items():
        if values[path]['present'] and values[path]['sha256'] != chm.digest(raw):
            raise ValueError('CHM immutable replacement conflicts: ' + path)
    catalog_raw = store.read_bytes(chm.ASSETS, chm.CATALOG)
    if chm.digest(catalog_raw) != values[chm.CATALOG]['sha256']:
        raise ValueError('CHM base catalog changed while capturing recovery state')
    values[chm.CATALOG]['raw'] = catalog_raw.decode('utf-8')
    return values


def store_immutable_replacements(store, plan, fence, checkpoint):
    """Upload only content-addressed CHM replacement snapshots/catalogs."""
    for path, raw in sorted(plan['immutable'].items()):
        fence.check()
        publication.immutable_put(store, chm.ASSETS, path, raw)
        checkpoint('immutable-replacement-verified', path)


def apply_exact_controls(store, plan, before, paths, fence, checkpoint):
    """Apply approved controls one by one, accepting idempotent retries only."""
    if len(paths) != len(set(paths)) or any(path not in plan['overrides'] for path in paths):
        raise ValueError('CHM control write exceeds the approved plan')
    for path in paths:
        fence.check()
        raw = plan['overrides'][path]
        if compare_before_write(store, path, before[path]['sha256'], raw):
            store.put_bytes(chm.ASSETS, path, raw)
        if store.read_bytes(chm.ASSETS, path) != raw:
            raise ValueError('CHM control readback differs after publication: ' + path)
        checkpoint('control-verified', path)


def validate_publication_order(plan, receipt_state):
    """Do not release old CHM references until both real surfaces accept."""
    staged = set(receipt_state.get('staged_controls', []))
    post = set(receipt_state.get('post_ack_controls', []))
    allowed_pre_ack = {chm.SIDECAR, chm.CATALOG, 'reader-index/v3/current.json',
                       f"reader-index/v3/promotions/{plan['new_v3_generation']}.json",
                       'reader-index/chm-epub-withdrawal-retention.json'}
    if not staged <= allowed_pre_ack or post & staged:
        raise ValueError('CHM publication checkpoint contains an invalid phase')
    if post and receipt_state.get('both_surfaces_accepted') is not True:
        raise ValueError('CHM history references cannot retire before both surfaces accept')
    if receipt_state.get('delete_started') and not receipt_state.get('actual_graph_verified'):
        raise ValueError('CHM deletion cannot start before the actual graph barrier')
    return True


def apply_pre_ack_publication(store, plan, before, fence, checkpoint):
    """Publish replacement Reader pointer while keeping old history live."""
    pointer_path = 'reader-index/v3/current.json'
    planned_pointer = json.loads(plan['overrides'][pointer_path])
    live_pointer, _ = publication.current(store)
    if live_pointer not in (plan['v3_pointer_before'], planned_pointer):
        raise ValueError('Reader pointer moved before CHM publication')
    store_immutable_replacements(store, plan, fence, checkpoint)
    catalog = json.loads(plan['overrides'][chm.CATALOG])
    # Before both deployments acknowledge, retain the old base generation's
    # original complete reference set and snapshot unchanged. The post-ack
    # plan sanitizes that generation only after the consumer handoff.
    old_catalog = json.loads(before[chm.CATALOG]['raw'].encode('utf-8'))
    old_generation = old_catalog['current']['generation']
    old_entry = old_catalog['generations'][old_generation]
    catalog['generations'][old_generation] = {**old_entry, 'status': 'superseded',
          'superseded_at': catalog['generations'][old_generation]['superseded_at']}
    for generation, entry in catalog['generations'].items():
        if generation == plan['base_generation']:
            entry['acks'] = {'hf': False, 'pages': False}
        elif entry.get('status') == 'superseded':
            entry.pop('replacement_acks', None)
    promotion_path = f"reader-index/v3/promotions/{plan['new_v3_generation']}.json"
    promotion = json.loads(plan['overrides'][promotion_path])
    promotion['acks'] = {'hf': False, 'pages': False}
    staged = {chm.SIDECAR: plan['overrides'][chm.SIDECAR],
              chm.CATALOG: publication.encode(catalog), promotion_path: publication.encode(promotion),
              'reader-index/chm-epub-withdrawal-retention.json':
                  plan['overrides']['reader-index/chm-epub-withdrawal-retention.json'],
              pointer_path: plan['overrides'][pointer_path]}
    for path in (promotion_path, 'reader-index/chm-epub-withdrawal-retention.json',
                 chm.SIDECAR, chm.CATALOG, pointer_path):
        fence.check()
        raw = staged[path]
        current = store.read_bytes(chm.ASSETS, path) if before[path]['present'] else None
        # A prior interrupted run may already have uploaded the exact staged
        # bytes even when the original control did not exist.
        if current is None:
            try:current = store.read_bytes(chm.ASSETS, path)
            except FileNotFoundError:pass
        if current != raw:
            if current is None and before[path]['present']:
                raise ValueError('original CHM control disappeared: ' + path)
            if current is not None and chm.digest(current) != before[path]['sha256']:
                raise ValueError('CHM control moved during publication: ' + path)
            store.put_bytes(chm.ASSETS, path, raw)
        if store.read_bytes(chm.ASSETS, path) != raw:
            raise ValueError('staged CHM control readback differs: ' + path)
        checkpoint('pre-ack-control-verified', path)
    if publication.current(store)[0] != planned_pointer:
        raise ValueError('planned CHM Reader pointer did not publish')
    return staged


def validate_reference_handoff(before, plan, rows):
    """Before acknowledgement no current or rollback route may be dropped."""
    import gzip
    old_sidecar = json.loads(gzip.decompress(before[chm.SIDECAR]))
    new_sidecar = json.loads(gzip.decompress(plan['overrides'][chm.SIDECAR]))
    expected, _ = chm.sanitize_historical_v3_base(old_sidecar, {r['path'] for r in rows})
    if new_sidecar != expected:
        raise ValueError('CHM handoff changes unrelated Reader routes')
    old_catalog = json.loads(before[chm.CATALOG])
    planned = json.loads(plan['overrides'][chm.CATALOG])
    old_id = old_catalog['current']['generation']
    prior = old_catalog['generations'][old_id]
    replacement = planned['generations'][old_id]
    previous_refs = {(r['bucket'], r['path']) for r in prior['references']}
    replacement_refs = {(r['bucket'], r['path']) for r in replacement['references']}
    target_refs = {(r['bucket'], r['path']) for r in rows}
    if previous_refs - replacement_refs != previous_refs & target_refs or replacement_refs - previous_refs:
        raise ValueError('CHM base history loses unrelated resource references')
    if plan['v3_pointer_before']['generation'] != plan['current_v3_generation']:
        raise ValueError('CHM handoff parent generation changed')
    verify_prepared_chapter_retention(plan)
    return True


def validate_chm_index_transition(before_indexes, planned_indexes, rows, sidecar, *, missing_processing,
                                  generation, acknowledged_generations):
    """Recompute every chapter-index update from exact pre-operation bytes."""
    # Catalog generation identity is immaterial to the index comparison; the
    # same canonical sidecar builds a deterministic temporary base catalog.
    catalog, _ = publication.reader_lifecycle.catalog_generation(sidecar)
    original = {path: json.loads(raw) for path, raw in before_indexes.items()}
    result = chm.synchronize_indexes(original, sidecar, catalog, rows, missing_processing,
                    current_generation=generation, receipts=acknowledged_generations)
    if set(planned_indexes) != set(result['indexes']):
        raise ValueError('planned CHM index inventory differs from recomputed scope')
    for path, expected in result['indexes'].items():
        if json.loads(planned_indexes[path]) != expected:
            raise ValueError('planned CHM index changes unapproved fields: ' + path)
    return {'indexes_verified': len(planned_indexes), 'remote_mutations': 0}


def apply_acknowledged_controls(store, plan, before, staged, client, fence, checkpoint):
    """After real consumer acceptance, retire only the planned CHM references."""
    reports = observe_planned_projection(store, client, plan)
    require_projection_acceptance(plan, reports)
    journal = 'reader-index/v3/retirement.json'
    pointer = 'reader-index/v3/current.json'
    promotion = f"reader-index/v3/promotions/{plan['new_v3_generation']}.json"
    # These controls were published in a deliberately unacknowledged form.
    # Verify their exact bytes before applying the acknowledged replacement.
    for path in (chm.CATALOG, promotion):
        fence.check()
        current = store.read_bytes(chm.ASSETS, path)
        desired = plan['overrides'][path]
        if current not in (staged[path], desired):
            raise ValueError('staged CHM acknowledgment changed: ' + path)
        if current != desired:
            store.put_bytes(chm.ASSETS, path, desired)
        if store.read_bytes(chm.ASSETS, path) != desired:
            raise ValueError('CHM acknowledgment readback differs: ' + path)
        checkpoint('acknowledgment-verified', path)
    untouched = {chm.SIDECAR, pointer, chm.CATALOG, promotion,
                 'reader-index/chm-epub-withdrawal-retention.json'}
    ordered = sorted(set(plan['overrides']) - untouched - {journal}) + [journal]
    apply_exact_controls(store, plan, before, ordered, fence, checkpoint)
    verify_live_chm_controls(store, plan)
    return ordered


def verify_exact_epub_targets(store, rows, *, workers=8):
    """Read every reviewed EPUB and verify its exact length and SHA-256."""
    from concurrent.futures import ThreadPoolExecutor
    assert_bucket_available(store)
    def check(row):
        raw = store.read_bytes(row['bucket'], row['path'])
        if len(raw) != row['bytes'] or chm.digest(raw) != row['sha256']:
            raise ValueError('CHM EPUB target changed: ' + row['path'])
        return len(raw)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        total = sum(pool.map(check, rows))
    if total != sum(row['bytes'] for row in rows):
        raise ValueError('CHM EPUB target byte total changed')
    return {'verified_objects': len(rows), 'verified_bytes': total, 'remote_mutations': 0}


def reconcile_epub_deletions(store, rows, batches, *, workers=8):
    """Count direct absence only for targets previously submitted durably."""
    from concurrent.futures import ThreadPoolExecutor
    assert_bucket_available(store)
    approved = {(row['bucket'], row['path']) for row in rows}
    submitted = set()
    for batch in batches:
        refs = [(batch.get('bucket'), path) for path in batch.get('paths', [])]
        validate_delete_batch(rows, refs)
        if batch.get('status') not in {'submitted', 'verified-absent'} or submitted & set(refs):
            raise ValueError('CHM deletion receipt has conflicting batches')
        submitted.update(refs)
    def check(row):
        try:
            raw = store.read_bytes(row['bucket'], row['path'])
        except FileNotFoundError:
            return False
        if len(raw) != row['bytes'] or chm.digest(raw) != row['sha256']:
            raise ValueError('CHM EPUB target changed during recovery: ' + row['path'])
        return True
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        present = list(pool.map(check, rows))
    missing = {(r['bucket'], r['path']) for r, exists in zip(rows, present) if not exists}
    if not missing <= submitted or not submitted <= approved:
        raise ValueError('an unsubmitted CHM EPUB target is missing')
    return {'present_objects': sum(present), 'confirmed_absent_objects': len(missing),
            'confirmed_absent_bytes': sum(row['bytes'] for row, exists in zip(rows, present) if not exists),
            'missing': missing}


def assert_bucket_available(store):
    """A missing/inaccessible bucket is not evidence that EPUBs were deleted."""
    check = getattr(store, 'assert_chm_bucket_available', None)
    if not callable(check) or check() is not True:
        raise RuntimeError('CHM bucket availability was not independently verified')


def assert_all_buckets_available(store):
    check = getattr(store, 'assert_buckets_available', None)
    if not callable(check):
        raise RuntimeError('four-bucket availability was not independently verified')
    check()


def validate_staged_plan(plan, rows):
    """Reject an incomplete or out-of-scope saved CHM plan before publication."""
    import re
    require_exact_epub_inventory(plan, rows)
    if plan.get('kind') != 'chm-epub-withdrawal-plan':
        raise ValueError('unexpected CHM plan kind')
    overrides, immutable = plan.get('overrides'), plan.get('immutable')
    if not isinstance(overrides, dict) or not isinstance(immutable, dict):
        raise ValueError('CHM plan control maps are missing')
    new_generation = plan.get('new_v3_generation')
    if not re.fullmatch(r'[0-9a-f]{64}', str(new_generation)):
        raise ValueError('invalid planned Reader generation')
    fixed = {chm.SIDECAR, chm.CATALOG, 'reader-index/v3/current.json', 'reader-index/v3/retirement.json',
             'reader-index/reader_gc_consumers.json', 'reader-index/gc/document-reset/withdrawal.json',
             'reader-index/chm-epub-withdrawal-retention.json',
             f'reader-index/v3/promotions/{new_generation}.json',
             chm.INDEX, *(f'{chm.INDEX_PREFIX}index-0{i}.json' for i in range(8))}
    if set(overrides) != fixed or any(not isinstance(value, bytes) for value in overrides.values()):
        raise ValueError('CHM plan modifies an unexpected control path')
    for path, raw in immutable.items():
        if (not isinstance(raw, bytes) or not (re.fullmatch(
                r'reader-index/snapshots/[0-9a-f]{64}/reader_assets\.json\.gz', path)
                or re.fullmatch(r'reader-index/generations/[0-9a-f]{32}/reader_assets\.json\.gz', path)
                or re.fullmatch(r'reader-index/v3/generations/[0-9a-f]{64}/catalog\.json', path))):
            raise ValueError('CHM plan contains an unexpected immutable object')
        if path.startswith('reader-index/snapshots/') and path.split('/')[2] != chm.digest(raw):
            raise ValueError('planned CHM immutable sidecar checksum mismatch')
        if path.startswith('reader-index/v3/generations/'):
            generation = path.split('/')[3]
            if publication.validate_catalog(json.loads(raw))['generation'] != generation:
                raise ValueError('planned CHM immutable catalog identity mismatch')
    pointer = json.loads(overrides['reader-index/v3/current.json'])
    if pointer.get('generation') != new_generation:
        raise ValueError('planned CHM pointer generation mismatch')
    ref = pointer.get('catalog', {})
    raw = immutable.get(ref.get('path'))
    if raw is None or len(raw) != ref.get('bytes') or chm.digest(raw) != ref.get('sha256'):
        raise ValueError('planned CHM catalog pointer checksum mismatch')
    publication.validate_catalog(json.loads(raw))
    return True


def compare_before_write(store, path, before_digest, after):
    """Return whether an exact CHM control still needs its planned update."""
    assert_bucket_available(store)
    try:
        current = store.read_bytes(chm.ASSETS, path)
    except FileNotFoundError:
        current = None
    if current == after:
        return False
    if before_digest is None:
        if current is not None:
            raise ValueError('new CHM control appeared after planning: ' + path)
    elif current is None or chm.digest(current) != before_digest:
        raise ValueError('CHM control changed after planning: ' + path)
    return True


def delete_exact_epub_batches(store, rows, fence, checkpoint, *, batches=None, batch_size=100):
    """Delete only submitted exact keys, recording partial outcomes durably."""
    import copy
    if not 1 <= batch_size <= 100:
        raise ValueError('invalid CHM deletion batch size')
    progress = copy.deepcopy(batches or [])
    status = reconcile_epub_deletions(store, rows, progress)
    def serializable(value):
        return {key: item for key, item in value.items() if key != 'missing'}
    checkpoint('reconciled', progress, serializable(status))
    by_key = {(r['bucket'], r['path']): r for r in rows}
    submitted = {(batch['bucket'], p) for batch in progress for p in batch['paths']}
    remaining = [row for row in rows if (row['bucket'], row['path']) not in submitted]
    work = list(progress)
    work.extend({'bucket': chm.ASSETS, 'paths': [r['path'] for r in remaining[i:i + batch_size]],
                 'status': 'new'} for i in range(0, len(remaining), batch_size))
    for batch in work:
        refs = [(batch['bucket'], p) for p in batch['paths']]
        validate_delete_batch(rows, refs)
        fence.check()
        existing = []
        for ref in refs:
            row = by_key[ref]
            try:
                raw = store.read_bytes(*ref)
            except FileNotFoundError:
                if batch['status'] == 'new':
                    raise ValueError('unsubmitted CHM EPUB disappeared before deletion')
                continue
            if len(raw) != row['bytes'] or chm.digest(raw) != row['sha256']:
                raise ValueError('CHM EPUB target changed before deletion: ' + row['path'])
            existing.append(row['path'])
        if batch['status'] == 'new':
            batch['status'] = 'submitted'
            progress.append(batch)
        checkpoint('batch-submitted', progress, serializable(status))
        if existing:
            store.delete(chm.ASSETS, existing)
        assert_bucket_available(store)
        for ref in refs:
            try:
                store.read_bytes(*ref)
            except FileNotFoundError:
                continue
            raise ValueError('CHM EPUB remains readable after deletion: ' + ref[1])
        batch['status'] = 'verified-absent'
        completed = {(entry['bucket'], p) for entry in progress
                     if entry['status'] == 'verified-absent' for p in entry['paths']}
        status = {'confirmed_absent_objects': len(completed),
                  'confirmed_absent_bytes': sum(by_key[key]['bytes'] for key in completed)}
        checkpoint('batch-verified', progress, status)
    status = reconcile_epub_deletions(store, rows, progress)
    if status['present_objects'] or status['confirmed_absent_objects'] != len(rows):
        raise ValueError('not every reviewed CHM EPUB is absent')
    checkpoint('all-direct-reads-absent', progress, serializable(status))
    return progress, serializable(status)


def write_exact_progress(store, path, state):
    """Persist and read back a narrow CHM operation checkpoint."""
    if not path.startswith('reader-index/gc/chm-epub-withdrawal/') or '..' in path.split('/'):
        raise ValueError('CHM checkpoint escaped its audit prefix')
    raw = publication.encode(state)
    store.put_bytes(chm.ASSETS, path, raw)
    if store.read_bytes(chm.ASSETS, path) != raw:
        raise ValueError('CHM checkpoint readback mismatch')


def run_guarded_retirement(store, fence, client, plan, rows, *, preflight, persist, save_plan,
                           publish_pages, acknowledge_timeout=1200):
    """Execute a freshly planned CHM withdrawal with fail-closed phase barriers.

    The caller supplies durable plan/preflight evidence and a serialized
    producer-isolated window. No broad collector or document reset is invoked.
    """
    import time
    import httpx
    validate_staged_plan(plan, rows)
    if (preflight.get('graph_complete') is not True or preflight.get('retained_targets') != 0
            or preflight.get('target_count') != len(rows) or preflight.get('remote_mutations') != 0):
        raise ValueError('fresh simulated CHM graph has unresolved references')
    if preflight.get('deletion_authorized') is not False:
        raise ValueError('CHM preflight does not identify itself as simulation')
    assert_all_buckets_available(store)
    fence.check()
    verify_exact_epub_targets(store, rows)
    # A prior Actions preview is diagnostic only. Rebuild the hypothetical
    # closure from current roots under the same isolation window.
    fresh_preflight = chm.simulate_complete_withdrawal(store, plan['overrides'], rows, fence,
                          workers=8, deadline_minutes=300, immutable=plan['immutable'])
    if (fresh_preflight.get('graph_complete') is not True or fresh_preflight.get('retained_targets') != 0
            or fresh_preflight.get('target_count') != len(rows)):
        raise ValueError('current simulated CHM graph has unresolved references')
    preflight = fresh_preflight
    before = confirm_initial_snapshot(store, plan)
    old_catalogs = {}
    for generation in (plan['current_v3_generation'], plan['old_v3_generation']):
        path = f'reader-index/v3/generations/{generation}/catalog.json'
        value = publication.validate_catalog(json.loads(store.read_bytes(chm.ASSETS, path)))
        if value['generation'] != generation:
            raise ValueError('historical Reader catalog identity changed')
        old_catalogs[generation] = value
    validate_preserved_catalog_roots(plan, old_catalogs, rows)
    validate_old_base_routes(store, plan, old_catalogs, rows)
    old_sidecar = store.read_bytes(chm.ASSETS, chm.SIDECAR)
    old_catalog = store.read_bytes(chm.ASSETS, chm.CATALOG)
    validate_reference_handoff({chm.SIDECAR: old_sidecar, chm.CATALOG: old_catalog}, plan, rows)
    verify_prepared_chapter_retention(plan)
    index_bytes = {path: store.read_bytes(chm.ASSETS, path) for path in plan['index_hashes_before']}
    index = json.loads(index_bytes[chm.INDEX])
    processing = {path for item in index['files'] for path in item.get('processing_roots', [])}
    missing = processing - store.existing_files(chm.ASSETS, processing)
    import gzip
    validate_chm_index_transition(index_bytes, {path: plan['overrides'][path] for path in index_bytes}, rows,
         json.loads(gzip.decompress(old_sidecar)), missing_processing=missing,
         generation=plan['current_v3_generation'],
         acknowledged_generations={'hf': plan['current_v3_generation'], 'pages': plan['current_v3_generation']})
    reset_evidence = verify_document_reset_prerequisites(store, plan)
    save_plan(plan, before)
    state = {'kind': 'chm-epub-retirement', 'complete': False, 'target_count': len(rows),
             'confirmed_deleted_objects': 0, 'confirmed_deleted_bytes': 0, 'batches': [],
             'preflight': preflight, 'document_reset_prerequisites': reset_evidence,
             'staged_controls': [], 'post_ack_controls': [], 'both_surfaces_accepted': False,
             'actual_graph_verified': False, 'delete_started': False}
    persist('plan-and-preconditions-verified', state, before)

    def checkpoint(phase, path):
        if phase == 'pre-ack-control-verified' and path not in state['staged_controls']:
            state['staged_controls'].append(path)
        if phase == 'control-verified' and path not in state['post_ack_controls']:
            state['post_ack_controls'].append(path)
        validate_publication_order(plan, state)
        persist(phase, state, before)

    try:
        staged = apply_pre_ack_publication(store, plan, before, fence, checkpoint)
        publish_pages(plan)
        deadline = time.monotonic() + acknowledge_timeout
        while True:
            try:
                reports = observe_planned_projection(store, client, plan)
                break
            except (ValueError, OSError, httpx.HTTPError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(15)
        state['both_surfaces_accepted'] = True
        state['consumer_receipts'] = reports
        persist('both-readers-accepted', state, before)
        apply_acknowledged_controls(store, plan, before, staged, client, fence, checkpoint)
        validate_document_reset_identity(plan, reports)
        persist('chm-references-retired', state, before)
        actual, traversal = actual_graph_evidence(store, fence, client, rows, plan)
        require_actual_zero_graph(actual, rows)
        state['actual_graph_verified'] = True
        state['actual_graph'] = actual
        persist('actual-graph-verified', state, before)
        recheck_actual_graph(store, traversal, actual, rows, fence, client, plan)
        state['delete_started'] = True
        validate_publication_order(plan, state)
        persist('deletion-started', state, before)

        def deletion_checkpoint(phase, batches, status):
            state['batches'] = batches
            state['confirmed_deleted_objects'] = status.get('confirmed_absent_objects', 0)
            state['confirmed_deleted_bytes'] = status.get('confirmed_absent_bytes', 0)
            persist(phase, state, before)

        state['batches'], final = delete_exact_epub_batches(store, rows, fence, deletion_checkpoint)
        state['confirmed_deleted_objects'] = final['confirmed_absent_objects']
        state['confirmed_deleted_bytes'] = final['confirmed_absent_bytes']
        recheck_actual_graph(store, traversal, actual, rows, fence, client, plan)
        verify_live_chm_controls(store, plan)
        state['complete'] = True
        persist('complete', state, before)
        return state
    except Exception as error:
        state.update(complete=False, error_type=type(error).__name__, error=str(error))
        try:
            counts = reconcile_epub_deletions(store, rows, state['batches'])
            state['confirmed_deleted_objects'] = counts['confirmed_absent_objects']
            state['confirmed_deleted_bytes'] = counts['confirmed_absent_bytes']
        except Exception as reconcile_error:
            state['reconciliation_error'] = type(reconcile_error).__name__
        persist('failed', state, before)
        raise
