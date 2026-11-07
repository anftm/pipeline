"""Publish verified PDF path redirects with a single conditional v3 pointer.

Only new content-addressed snapshot/catalog objects and the v3 pointer are
written. Canonical indexes, old generations, source PDFs and old copies remain
unchanged. There is no physical deletion or GC authorization here.
"""
import gzip
import hashlib
import json

try:
    from . import reader_page_audit as audit, reader_page_migrate as migration
    from . import reader_page_redirects as redirects, reader_route_control as control
    from . import publish_reader_v3 as publication
except ImportError:
    import reader_page_audit as audit
    import reader_page_migrate as migration
    import reader_page_redirects as redirects
    import reader_route_control as control
    import publish_reader_v3 as publication


def prepare(store, api, *, expected_index, expected_sidecar, expected_v3, workers=4):
    plan = migration.prepare(store, api, expected_index=expected_index,
                             expected_sidecar=expected_sidecar, expected_v3=expected_v3, workers=workers)
    snapshot = plan['audit']
    if plan['changed_routes'] < len(snapshot['planned']) or any(
            not row.get('currently_direct_route') for row in snapshot['planned']):
        raise ValueError('each migrated PDF must be an active direct route')
    mapping = {row['old_path']: row['new_path'] for row in snapshot['planned']}
    if len(mapping) != len(snapshot['planned']):
        raise ValueError('duplicate redirect source')
    redirects.validated({redirects.FIELD: mapping})
    pointer, current = publication.current(store)
    if pointer != plan['pointer']:
        raise ValueError('v3 pointer moved during redirect planning')
    existing = redirects.validated(current)
    if existing:
        raise ValueError('an existing redirect publication requires a new audited plan')
    revised = {**current, redirects.FIELD: mapping}
    packed, ref = control.prepare_base(revised, plan['before_sidecar'])
    projected = audit.decode_sidecar(packed)
    if projected != plan['sidecar'] or packed != plan['packed']:
        raise ValueError('redirect projection differs from exact migration plan')
    catalog = {'version': 3, 'kind': 'reader-reading-catalog', 'files': current['files'],
               **({'suppressed_sources': current['suppressed_sources']} if 'suppressed_sources' in current else {}),
               redirects.FIELD: mapping, 'base_sidecar': ref, 'parent_generation': pointer['generation']}
    generation = publication.text.digest(catalog)
    catalog['generation'] = generation
    publication.validate_catalog(catalog)
    raw = publication.encode(catalog)
    catalog_ref = publication.metadata(audit.ASSETS, f'reader-index/v3/generations/{generation}/catalog.json', raw)
    new_pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': generation,
                   'catalog': catalog_ref, 'resources': [catalog_ref]}
    return {'audit': snapshot, 'old_pointer': pointer, 'pointer': new_pointer, 'catalog': catalog,
            'catalog_raw': raw, 'packed': packed, 'snapshot_ref': ref, 'changed_routes': plan['changed_routes']}


def verify_plan(store, api, plan):
    snapshot = plan['audit']
    migration.verify_copies(store, api, snapshot['planned'])
    if hashlib.sha256(store.read_bytes(audit.BUCKET, audit.INDEX)).hexdigest() != snapshot['index_sha256']:
        raise ValueError('derived index changed before redirect promotion')
    if hashlib.sha256(store.read_bytes(audit.ASSETS, audit.SIDECAR)).hexdigest() != snapshot['canonical_sidecar_sha256']:
        raise ValueError('canonical sidecar changed before redirect promotion')
    if publication.current(store)[0] != plan['old_pointer']:
        raise ValueError('v3 pointer changed before redirect promotion')


def report(plan, *, applied):
    return {'version': 1, 'kind': 'reader-derived-pdf-redirect-publication', 'applied': applied,
            'previous_v3_generation': plan['old_pointer']['generation'],
            'generation': plan['pointer']['generation'], 'migrated_entries': len(plan['audit']['planned']),
            'changed_routes': plan['changed_routes'], 'canonical_indexes_changed': False,
            'objects_deleted': False, 'deletion_authorized': False,
            'external_credentials_verified': False,
            'unindexed_legacy_pdfs': len(plan['audit']['unindexed_legacy_pdfs']),
            'unclassified_object_pdfs': len(plan['audit']['unclassified_object_pdfs'])}


def apply(store, api, plan):
    """Compare all inputs, then advance exactly one S3 pointer conditionally."""
    publication.assert_writer(store)
    verify_plan(store, api, plan)
    store.immutable_put(audit.ASSETS, plan['snapshot_ref']['path'], plan['packed'])
    store.immutable_put(audit.ASSETS, plan['pointer']['catalog']['path'], plan['catalog_raw'])
    history_path = f"reader-index/v3/promotions/{plan['pointer']['generation']}.json"
    history = {'version': 1, 'kind': 'reader-reading-promotion', 'generation': plan['pointer']['generation'],
               'parent_generation': plan['old_pointer']['generation'], 'resources': [plan['pointer']['catalog']],
               'acks': {'hf': False, 'pages': False}, 'retention_days': 30,
               'created_at': publication.reader_lifecycle.now_iso()}
    # A prior attempt may have staged this history before its pointer write.
    # It can only be reused while every target object still has this identity.
    try:
        existing_history = json.loads(store.read_bytes(audit.ASSETS, history_path))
    except FileNotFoundError:
        store.immutable_put(audit.ASSETS, history_path, publication.encode(history))
    else:
        if (not isinstance(existing_history, dict)
                or any(existing_history.get(key) != history[key] for key in
                       ('version', 'kind', 'generation', 'parent_generation', 'resources', 'acks', 'retention_days'))
                or not isinstance(existing_history.get('created_at'), str)):
            raise ValueError('existing redirect promotion record differs')
    # Recheck input object identities and all mutable roots after staging.
    verify_plan(store, api, plan)
    raw, etag = store.read_for_update(audit.ASSETS, publication.POINTER)
    if json.loads(raw) != plan['old_pointer']:
        raise ValueError('v3 pointer changed before conditional redirect promotion')
    print('redirect generation staged: ' + plan['pointer']['generation'], flush=True)
    store.put_if_match(audit.ASSETS, publication.POINTER, publication.encode(plan['pointer']), etag)
    if publication.current(store)[0] != plan['pointer']:
        raise ValueError('redirect pointer readback differs')
    # Conditional pointer publication is atomic, but target object writes are
    # not coupled to it. Detect changed or missing copies before reporting any
    # successful publication; a failure still retains every old object.
    migration.verify_copies(store, api, plan['audit']['planned'])
    if publication.current(store)[0] != plan['pointer']:
        raise ValueError('redirect pointer changed during post-publication verification')
    return report(plan, applied=True)
