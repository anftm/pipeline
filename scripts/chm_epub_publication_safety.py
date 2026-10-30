#!/usr/bin/env python3
"""Read-only gates for an eventual two-site CHM EPUB withdrawal."""
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


def validate_delete_batch(rows, batch):
    allowed = {(row['bucket'], row['path']) for row in rows}
    if not batch or len(batch) > 100 or len(batch) != len(set(batch)) or not set(batch) <= allowed:
        raise ValueError('CHM delete batch exceeds the reviewed exact list')
    return True


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
