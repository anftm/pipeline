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
