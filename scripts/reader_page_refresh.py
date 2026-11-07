"""Audit live PDF redirects before rebuilding or changing a pinned v3 base.

This keeps the single-pointer publication model: canonical roots are read, not
rewritten. A changed or missing legacy PDF, target copy, manifest, sidecar or
pointer stops publication rather than silently reusing a stale redirect.
"""
import copy
import hashlib
import json

try:
    from . import reader_page_atomic as atomic, reader_page_audit as audit
    from . import reader_page_migrate as migration, reader_page_redirects as redirects, reader_page_reconcile as reconcile
    from . import reader_route_control as control, reader_route_plan as routes
    from . import publish_reader_v3 as publication
except ImportError:
    import reader_page_atomic as atomic
    import reader_page_audit as audit
    import reader_page_migrate as migration
    import reader_page_redirects as redirects
    import reader_page_reconcile as reconcile
    import reader_route_control as control
    import reader_route_plan as routes
    import publish_reader_v3 as publication


def prepare(store, api, *, expected_parent=None, action='unify', options=None, workers=4):
    pointer, catalog = publication.current(store)
    if not pointer or expected_parent is not None and pointer['generation'] != expected_parent:
        raise ValueError('v3 generation changed before audited base refresh')
    mapping = redirects.validated(catalog)
    if not mapping or not catalog.get('base_sidecar'):
        raise ValueError('audited base refresh requires active redirects and a pinned base')
    snapshot = audit.audit(store, api, workers=workers, active_redirects=mapping)
    if (snapshot['v3_generation'] != pointer['generation'] or snapshot['unresolved']
            or publication.current(store)[0] != pointer):
        raise ValueError('redirect audit is stale or has unresolved PDF copies')
    reconcile.validate_snapshot(snapshot, mapping)
    canonical = store.read_bytes(audit.ASSETS, audit.SIDECAR)
    if hashlib.sha256(canonical).hexdigest() != snapshot['canonical_sidecar_sha256']:
        raise ValueError('canonical sidecar changed during redirect audit')
    base = audit.decode_sidecar(canonical)
    legacy_raw = store.read_bytes(audit.ASSETS, migration.CATALOG)
    legacy = json.loads(legacy_raw)
    current_id = (legacy.get('current') or {}).get('generation')
    current_generation = legacy.get('generations', {}).get(current_id, {})
    if (legacy.get('version') != 1 or legacy.get('kind') != 'reader-catalog'
            or current_generation.get('sidecar_sha256') != hashlib.sha256(audit.encode(base)).hexdigest()):
        raise ValueError('legacy catalog does not bind the canonical sidecar')
    snapshot['legacy_catalog_sha256'] = hashlib.sha256(legacy_raw).hexdigest()
    pinned = publication.pinned_base(store, catalog, {})
    keys = control.validated_sources(catalog)
    files = copy.deepcopy(catalog['files'])
    if any(row['key'] in files for row in snapshot['planned'] if row['old_path'] in mapping):
        raise ValueError('a redirect source has an active v3 override')
    details = {}
    if action != 'unify':
        if (action not in {'remove', 'restore'} or not isinstance(options, dict)
                or set(options) != {'repo', 'path'} or expected_parent is None):
            raise ValueError('provide one exact route, expected generation and valid action')
        key = routes.source_key(options['repo'], options['path'])
        if action == 'remove':
            if key not in keys and key not in base['f'] and key not in pinned['f'] and key not in files:
                raise ValueError('source has no Reader route; nothing was changed')
            removed_override = files.pop(key, None)
            keys = sorted(set(keys) | {key})
        else:
            if key not in keys:
                if key not in pinned['f'] or key in files:
                    raise ValueError('source is not suppressed')
                return {'unchanged': True, 'old_pointer': pointer, 'audit': snapshot,
                        'action': action, 'source_key': key, 'redirect_audited': True}
            if key not in base['f']:
                raise ValueError('canonical source route is unavailable; cannot restore')
            removed_override = None
            keys = [item for item in keys if item != key]
        details = {'action': action, 'source_key': key, 'removed_override': removed_override is not None,
                   'search_record_removed': False}
    revised = {**catalog, 'files': files, 'suppressed_sources': keys}
    packed, ref = control.prepare_base(revised, canonical)
    same = (catalog.get('base_sidecar') == ref and catalog.get('files') == files
            and control.validated_sources(catalog) == keys)
    if same:
        return {'unchanged': True, 'old_pointer': pointer, 'audit': snapshot, 'redirect_audited': True, **details}
    candidate = {'version': 3, 'kind': 'reader-reading-catalog', 'files': files,
                 **({'suppressed_sources': keys} if 'suppressed_sources' in catalog or keys else {}),
                 redirects.FIELD: copy.deepcopy(mapping), 'base_sidecar': ref,
                 'parent_generation': pointer['generation']}
    redirects.preserve(catalog, candidate)
    generation = publication.text.digest(candidate)
    candidate['generation'] = generation
    publication.validate_catalog(candidate)
    raw = publication.encode(candidate)
    catalog_ref = publication.metadata(audit.ASSETS, f'reader-index/v3/generations/{generation}/catalog.json', raw)
    new_pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': generation,
                   'catalog': catalog_ref, 'resources': [catalog_ref]}
    return {'unchanged': False, 'audit': snapshot, 'old_pointer': pointer, 'pointer': new_pointer,
            'redirect_audited': True, 'catalog': candidate, 'catalog_raw': raw, 'packed': packed, 'snapshot_ref': ref,
            'changed_routes': 0, **details}


def run(store, api, *, apply=False, expected_parent=None, action='unify', options=None):
    if apply:
        publication.assert_writer(store)
    plan = prepare(store, api, expected_parent=expected_parent, action=action, options=options)
    if plan['unchanged']:
        if publication.current(store)[0] != plan['old_pointer']:
            raise ValueError('v3 pointer changed during audited base refresh')
        atomic.verify_plan(store, api, plan)
        return {'applied': apply, 'unchanged': True, 'generation': plan['old_pointer']['generation'],
                'redirect_audited': True, 'objects_deleted': False, **{k: plan[k] for k in ('action', 'source_key') if k in plan}}
    if apply:
        atomic.apply(store, api, plan)
    return {'applied': apply, 'unchanged': False, 'generation': plan['pointer']['generation'],
            'previous_generation': plan['old_pointer']['generation'], 'objects_deleted': False,
            'redirect_audited': True, 'canonical_indexes_changed': False, 'deletion_authorized': False,
            **{k: plan[k] for k in ('action', 'source_key', 'removed_override', 'search_record_removed') if k in plan}}
