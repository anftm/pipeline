"""Recoverable canonical sidecar/catalog alignment; never authorizes deletion.

The two mutable roots cannot be committed atomically. A content-addressed
journal and legacy snapshot are persisted first; exact root states make a
stopped transaction inspectable and resumable. Actual writes require the
existing full writer fence and verified independent-writer isolation.
"""
import base64
import copy
import gzip
import hashlib
import json
import re
import io

try:
    from . import reader_page_audit as audit, reader_page_migrate as migration
    from . import reader_page_reconcile as reconcile, reader_page_redirects as redirects
    from . import reader_lifecycle as lifecycle, publish_reader_v3 as publication
except ImportError:
    import reader_page_audit as audit
    import reader_page_migrate as migration
    import reader_page_reconcile as reconcile
    import reader_page_redirects as redirects
    import reader_lifecycle as lifecycle
    import publish_reader_v3 as publication

PREFIX = 'reader-index/gc/sidecar-reconcile/'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def prepare(store, api, *, expected_index, expected_sidecar, expected_catalog, expected_generation, workers=4):
    values = (expected_index, expected_sidecar, expected_catalog, expected_generation)
    if any(not isinstance(value, str) or not audit.SHA.fullmatch(value) for value in values):
        raise ValueError('exact PDF index, sidecar, catalog and v3 SHA-256 identities are required')
    pointer, catalog = publication.current(store)
    if not pointer or pointer['generation'] != expected_generation:
        raise ValueError('v3 generation changed before sidecar alignment')
    mapping = redirects.validated(catalog)
    if not mapping or not catalog.get('base_sidecar'):
        raise ValueError('active redirects and a pinned base are required')
    snapshot = audit.audit(store, api, workers=workers, active_redirects=mapping)
    if (snapshot['index_sha256'] != expected_index or snapshot['canonical_sidecar_sha256'] != expected_sidecar
            or snapshot['v3_generation'] != expected_generation):
        raise ValueError('sidecar alignment roots changed during audit')
    by_old = reconcile.validate_snapshot(snapshot, mapping)
    if any(row.get('index_location') != 'objects' for old, row in by_old.items() if old in mapping):
        raise ValueError('PDF canonical index must already point to every audited objects copy')
    sidecar_raw = store.read_bytes(audit.ASSETS, audit.SIDECAR)
    catalog_size = store.stat(audit.ASSETS, migration.CATALOG)
    if type(catalog_size) is not int or not 0 < catalog_size <= 48 * 1024 * 1024:
        raise ValueError('legacy catalog exceeds bounded sidecar-reconciliation size')
    catalog_raw = store.read_bytes(audit.ASSETS, migration.CATALOG)
    if len(catalog_raw) != catalog_size:
        raise ValueError('legacy catalog size changed during sidecar alignment')
    if sha(sidecar_raw) != expected_sidecar or sha(catalog_raw) != expected_catalog:
        raise ValueError('sidecar or legacy catalog changed after audit')
    base = audit.decode_sidecar(sidecar_raw)
    if any(row['key'] in catalog.get('files', {}) for old, row in by_old.items() if old in mapping):
        raise ValueError('a redirected PDF has an active v3 override')
    pinned = publication.pinned_base(store, catalog, {})
    revised, changed = redirects.rewrite(base, mapping)
    if any(revised['f'].get(row['key']) != pinned['f'].get(row['key'])
           for old, row in by_old.items() if old in mapping and row['key'] not in catalog.get('suppressed_sources', [])):
        raise ValueError('reconciled canonical routes differ from the active pinned projection')
    before = json.loads(catalog_raw)
    if 'redirect_sidecar_pending' in before:
        raise ValueError('legacy catalog already has an unfinished sidecar reconciliation')
    current = (before.get('current') or {}).get('generation')
    entry = (before.get('generations') or {}).get(current)
    if (before.get('version') != 1 or before.get('kind') != 'reader-catalog' or not isinstance(entry, dict)
            or entry.get('sidecar_sha256') != sha(audit.encode(base))):
        raise ValueError('legacy catalog does not bind canonical sidecar')
    if not changed:
        raise ValueError('canonical sidecar has no legacy redirect paths to reconcile')
    old_refs = entry.get('references')
    if not isinstance(old_refs, list) or any(not isinstance(ref, dict)
            or not isinstance(ref.get('bucket'), str) or not isinstance(ref.get('path'), str) for ref in old_refs):
        raise ValueError('legacy catalog references are invalid')
    carried = [dict(ref, path=mapping[ref['path']]) if ref['bucket'] == audit.BUCKET and ref['path'] in mapping
               else dict(ref) for ref in old_refs]
    normalized = sorted({(ref['bucket'], ref['path']) for ref in [*carried, *lifecycle.sidecar_references(revised)]})
    sidecar_sha = sha(audit.encode(revised))
    signature = lifecycle._generation_id({'sidecar_sha256': sidecar_sha, 'references': normalized}, None)
    generation = lifecycle._generation_id({'signature': signature}, current)
    if generation in before['generations']:
        raise ValueError('legacy catalog already contains proposed generation')
    updated = copy.deepcopy(before)
    now = lifecycle.now_iso()
    updated['generations'][current].update(status='superseded', superseded_at=now)
    packed = gzip.compress(audit.encode(revised), mtime=0)
    snapshot_path = f'reader-index/generations/{generation}/reader_assets.json.gz'
    updated['generations'][generation] = {
        'generation': generation, 'status': 'current', 'published_at': now, 'signature': signature,
        'sidecar_sha256': sidecar_sha, 'parent_generation': current,
        'references': [{'bucket': b, 'path': p} for b, p in normalized],
        'acks': {'hf': False, 'pages': False},
        'snapshot': {'bucket': audit.ASSETS, 'path': snapshot_path, 'sha256': sha(packed), 'bytes': len(packed)}}
    updated['current'] = {'generation': generation, 'published_at': now}
    updated['redirect_sidecar_pending'] = {'version': 1, 'kind': 'reader-sidecar-reconciliation-pending',
                                           'pointer_generation': pointer['generation'],
                                           'sidecar_sha256': sha(packed), 'base_generation': generation}
    # Conservatively protect the prospective references before either mutable
    # root moves. In particular, old paths remain roots in every old generation.
    if not {(ref['bucket'], ref['path']) for ref in lifecycle.sidecar_references(revised)} <= set(normalized):
        raise ValueError('new legacy generation does not cover canonical sidecar references')
    if (publication.current(store)[0] != pointer or sha(store.read_bytes(audit.BUCKET, audit.INDEX)) != expected_index
            or store.read_bytes(audit.ASSETS, audit.SIDECAR) != sidecar_raw
            or store.read_bytes(audit.ASSETS, migration.CATALOG) != catalog_raw):
        raise ValueError('Reader roots changed during sidecar alignment planning')
    after_catalog = audit.encode(updated)
    result = {'version': 1, 'kind': 'reader-sidecar-reconciliation', 'pointer': pointer,
              'index_sha256': expected_index, 'sidecar_before_sha256': expected_sidecar,
              'catalog_before_sha256': expected_catalog, 'sidecar_after_sha256': sha(packed),
              'catalog_after_sha256': sha(after_catalog), 'sidecar_after_base64': base64.b64encode(packed).decode('ascii'),
              'catalog_after_base64': base64.b64encode(after_catalog).decode('ascii'), 'snapshot_path': snapshot_path,
              'base_generation': generation, 'rows': snapshot['planned'], 'changed_routes': changed,
              'objects_deleted': False, 'deletion_authorized': False}
    validate(result)
    return result


def validate(value):
    if (not isinstance(value, dict) or value.get('version') != 1 or value.get('kind') != 'reader-sidecar-reconciliation'
            or value.get('objects_deleted') is not False or value.get('deletion_authorized') is not False
            or not isinstance(value.get('pointer'), dict) or not audit.SHA.fullmatch(str(value['pointer'].get('generation', '')))
            or not isinstance(value.get('rows'), list) or not value['rows']
            or not re.fullmatch(r'[0-9a-f]{32}', str(value.get('base_generation', '')))
            or value.get('snapshot_path') != f"reader-index/generations/{value['base_generation']}/reader_assets.json.gz"):
        raise ValueError('invalid sidecar reconciliation journal')
    for field in ('index_sha256', 'sidecar_before_sha256', 'catalog_before_sha256',
                  'sidecar_after_sha256', 'catalog_after_sha256'):
        if not audit.SHA.fullmatch(str(value.get(field, ''))):
            raise ValueError('invalid sidecar reconciliation digest')
    for name in ('sidecar', 'catalog'):
        try:
            raw = base64.b64decode(value[name + '_after_base64'], validate=True)
        except (KeyError, ValueError, TypeError):
            raise ValueError('invalid sidecar reconciliation bytes') from None
        if sha(raw) != value[name + '_after_sha256']:
            raise ValueError('sidecar reconciliation bytes differ')
    try:
        packed = base64.b64decode(value['sidecar_after_base64'], validate=True)
        sidecar = audit.decode_sidecar(packed)
        catalog = json.loads(base64.b64decode(value['catalog_after_base64'], validate=True))
        generation = value['base_generation']
        entry = catalog['generations'][generation]
        snapshot = entry['snapshot']
        if (catalog.get('version') != 1 or catalog.get('kind') != 'reader-catalog'
                or catalog.get('current', {}).get('generation') != generation
                or entry.get('status') != 'current' or entry.get('generation') != generation
                or entry.get('sidecar_sha256') != sha(audit.encode(sidecar))
                or snapshot != {'bucket': audit.ASSETS, 'path': value['snapshot_path'],
                                'sha256': sha(packed), 'bytes': len(packed)}):
            raise ValueError('invalid sidecar reconciliation generation')
        pending = catalog.get('redirect_sidecar_pending')
        if pending != {'version': 1, 'kind': 'reader-sidecar-reconciliation-pending',
                       'pointer_generation': value['pointer']['generation'],
                       'sidecar_sha256': value['sidecar_after_sha256'], 'base_generation': generation}:
            raise ValueError('sidecar reconciliation pending marker differs')
        refs = entry.get('references')
        if (not isinstance(refs, list) or any(not isinstance(ref, dict) or not isinstance(ref.get('bucket'), str)
                or not isinstance(ref.get('path'), str) for ref in refs)
                or not {(ref['bucket'], ref['path']) for ref in lifecycle.sidecar_references(sidecar)}
                       <= {(ref['bucket'], ref['path']) for ref in refs}):
            raise ValueError('sidecar reconciliation generation does not protect its sidecar')
        parent = entry.get('parent_generation')
        prior = catalog.get('generations', {}).get(parent)
        if (not isinstance(prior, dict) or prior.get('generation') != parent
                or prior.get('status') != 'superseded' or not isinstance(prior.get('references'), list)):
            raise ValueError('sidecar reconciliation lost its prior generation')
        keys = set()
        for row in value['rows']:
            old = row.get('old_path') if isinstance(row, dict) else None
            new = row.get('new_path') if isinstance(row, dict) else None
            if (not isinstance(old, str) or not audit.OLD.fullmatch(old)
                    or not isinstance(new, str) or not audit.NEW.fullmatch(new)
                    or row.get('new_manifest') != new.rsplit('/', 1)[0] + '/derived-manifest.json'
                    or not audit.SHA.fullmatch(str(row.get('new_manifest_sha256', '')))
                    or not isinstance(row.get('old_xet_hash'), str) or not row['old_xet_hash']
                    or type(row.get('bytes')) is not int or row['bytes'] < 1 or old in keys):
                raise ValueError('invalid sidecar reconciliation row')
            keys.add(old)
        if type(value.get('changed_routes')) is not int or value['changed_routes'] < 1:
            raise ValueError('invalid sidecar reconciliation route count')
    except (KeyError, TypeError, AttributeError, OSError) as error:
        raise ValueError('invalid sidecar reconciliation contents') from error
    return value


def identity(value):
    return sha(audit.encode(validate(value)))


def preview(value):
    return {'version': 1, 'kind': value['kind'], 'applied': False, 'candidate_sha256': identity(value),
            'generation': value['pointer']['generation'], 'base_generation': value['base_generation'],
            'changed_routes': value['changed_routes'], 'sidecar_before_sha256': value['sidecar_before_sha256'],
            'sidecar_after_sha256': value['sidecar_after_sha256'], 'catalog_before_sha256': value['catalog_before_sha256'],
            'catalog_after_sha256': value['catalog_after_sha256'], 'objects_deleted': False, 'deletion_authorized': False,
            'external_credentials_verified': False}


def persist(store, value):
    tx = identity(value)
    raw = audit.encode(value)
    if len(raw) > 100 * 1024 * 1024:
        raise ValueError('sidecar reconciliation journal expands beyond limit')
    store.immutable_put(audit.ASSETS, PREFIX + tx + '.json.gz', gzip.compress(raw, mtime=0))
    print('sidecar reconciliation transaction persisted: ' + tx, flush=True)
    return tx


def run(store, api, value):
    migration.require_writer_window(store)
    validate(value)
    pointer, current_catalog = publication.current(store)
    if pointer != value['pointer']:
        raise ValueError('v3 pointer changed during sidecar reconciliation')
    mapping = redirects.validated(current_catalog)
    by_old = {row['old_path']: row for row in value['rows']}
    if (not mapping or not set(mapping) <= set(by_old)
            or any(by_old[old]['new_path'] != target for old, target in mapping.items())):
        raise ValueError('journal redirects differ from current v3 catalog')
    after_sidecar = audit.decode_sidecar(base64.b64decode(value['sidecar_after_base64'], validate=True))
    pinned = publication.pinned_base(store, current_catalog, {})
    if (any(by_old[old]['key'] in current_catalog.get('files', {}) for old in mapping)
            or any(after_sidecar['f'].get(by_old[old]['key']) != pinned['f'].get(by_old[old]['key'])
                   for old in mapping if by_old[old]['key'] not in current_catalog.get('suppressed_sources', []))):
        raise ValueError('journal routes differ from the active pinned projection')
    journal_path = PREFIX + identity(value) + '.json.gz'
    stored = store.read_bytes(audit.ASSETS, journal_path)
    with gzip.GzipFile(fileobj=io.BytesIO(stored)) as stream:
        expanded = stream.read(100 * 1024 * 1024 + 1)
    if len(expanded) > 100 * 1024 * 1024 or expanded != audit.encode(value):
        raise ValueError('sidecar reconciliation journal was not durably persisted')
    migration.verify_copies(store, api, value['rows'])
    if sha(store.read_bytes(audit.BUCKET, audit.INDEX)) != value['index_sha256'] or publication.current(store)[0] != value['pointer']:
        raise ValueError('PDF index or v3 pointer changed during sidecar reconciliation')
    final_catalog = copy.deepcopy(json.loads(base64.b64decode(value['catalog_after_base64'], validate=True)))
    final_catalog.pop('redirect_sidecar_pending')
    final_raw = audit.encode(final_catalog)
    items = [(migration.CATALOG, 'catalog'), (audit.SIDECAR, 'sidecar')]
    states, observed_roots = [], {}
    for path, name in items:
        raw, _ = store.read_for_update(audit.ASSETS, path)
        observed_roots[name] = raw
        tag = sha(raw)
        if name == 'catalog' and tag == sha(final_raw):
            states.append('final')
        elif tag == value[name + '_after_sha256']:
            states.append('after')
        elif tag == value[name + '_before_sha256']:
            states.append('before')
        else:
            raise ValueError('sidecar reconciliation root diverged: ' + path)
    if states in (['before', 'after'], ['final', 'before']):
        raise ValueError('sidecar reconciliation roots are in an impossible order')
    if states == ['final', 'after']:
        if (publication.current(store)[0] != value['pointer']
                or sha(store.read_bytes(audit.BUCKET, audit.INDEX)) != value['index_sha256']
                or store.read_bytes(audit.ASSETS, migration.CATALOG) != final_raw
                or sha(store.read_bytes(audit.ASSETS, audit.SIDECAR)) != value['sidecar_after_sha256']):
            raise ValueError('Reader roots changed after sidecar finalization')
        migration.verify_copies(store, api, value['rows'])
        return {**preview(value), 'catalog_final_sha256': sha(final_raw),
                'transaction_id': identity(value), 'applied': True}
    after_catalog = json.loads(base64.b64decode(value['catalog_after_base64'], validate=True))
    if states[0] == 'before':
        before_catalog = json.loads(observed_roots['catalog'])
        old_id = before_catalog.get('current', {}).get('generation')
        for generation, entry in before_catalog.get('generations', {}).items():
            carried = after_catalog.get('generations', {}).get(generation)
            if not isinstance(carried, dict) or any(carried.get(k) != v for k, v in entry.items()
                    if generation != old_id or k not in {'status', 'superseded_at'}):
                raise ValueError('sidecar reconciliation would drop historical catalog data')
        if after_catalog.get('retired_generations') != before_catalog.get('retired_generations'):
            raise ValueError('sidecar reconciliation would drop retired catalog data')
    if states[1] == 'before':
        expected_sidecar, _ = redirects.rewrite(audit.decode_sidecar(observed_roots['sidecar']), mapping)
        if expected_sidecar != after_sidecar:
            raise ValueError('sidecar reconciliation would change unrelated canonical routes')
    if states != ['after', 'after']:
        import httpx
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            publication.verify_generation(store, client, value['pointer']['generation'])
        sidecar = base64.b64decode(value['sidecar_after_base64'], validate=True)
        store.immutable_put(audit.ASSETS, value['snapshot_path'], sidecar)
    for (path, name), state in zip(items, states):
        migration.require_writer_window(store)
        current, etag = store.read_for_update(audit.ASSETS, path)
        expected = value[name + ('_before_sha256' if state == 'before' else '_after_sha256')]
        if sha(current) != expected:
            raise ValueError('sidecar reconciliation root changed: ' + path)
        after = base64.b64decode(value[name + '_after_base64'], validate=True)
        if state == 'before':
            store.put_if_match(audit.ASSETS, path, after, etag)
        actual, _ = store.read_for_update(audit.ASSETS, path)
        if actual != after or store.read_bytes(audit.ASSETS, path) != after:
            raise ValueError('sidecar reconciliation readback differs: ' + path)
    if (publication.current(store)[0] != value['pointer']
            or sha(store.read_bytes(audit.BUCKET, audit.INDEX)) != value['index_sha256']
            or sha(store.read_bytes(audit.ASSETS, audit.SIDECAR)) != value['sidecar_after_sha256']
            or sha(store.read_bytes(audit.ASSETS, migration.CATALOG)) != value['catalog_after_sha256']):
        raise ValueError('Reader roots changed after sidecar reconciliation')
    migration.verify_copies(store, api, value['rows'])
    migration.require_writer_window(store)
    current, etag = store.read_for_update(audit.ASSETS, migration.CATALOG)
    if sha(current) != value['catalog_after_sha256']:
        raise ValueError('pending legacy catalog changed before finalization')
    store.put_if_match(audit.ASSETS, migration.CATALOG, final_raw, etag)
    actual, _ = store.read_for_update(audit.ASSETS, migration.CATALOG)
    if actual != final_raw or store.read_bytes(audit.ASSETS, migration.CATALOG) != final_raw:
        raise ValueError('final legacy catalog readback differs')
    if (publication.current(store)[0] != value['pointer']
            or sha(store.read_bytes(audit.BUCKET, audit.INDEX)) != value['index_sha256']
            or sha(store.read_bytes(audit.ASSETS, audit.SIDECAR)) != value['sidecar_after_sha256']):
        raise ValueError('Reader roots changed after sidecar finalization')
    return {**preview(value), 'catalog_final_sha256': sha(final_raw),
            'transaction_id': identity(value), 'applied': True}


def apply(store, api, value):
    migration.require_writer_window(store)
    validate(value)
    for path, name in ((audit.SIDECAR, 'sidecar'), (migration.CATALOG, 'catalog')):
        if sha(store.read_bytes(audit.ASSETS, path)) != value[name + '_before_sha256']:
            raise ValueError('sidecar reconciliation root changed before journal')
    if sha(store.read_bytes(audit.BUCKET, audit.INDEX)) != value['index_sha256'] or publication.current(store)[0] != value['pointer']:
        raise ValueError('PDF index or pointer changed before journal')
    migration.verify_copies(store, api, value['rows'])
    import httpx
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        publication.verify_generation(store, client, value['pointer']['generation'])
    persist(store, value)
    return run(store, api, value)


def resume(store, api, transaction_id):
    if not isinstance(transaction_id, str) or not audit.SHA.fullmatch(transaction_id):
        raise ValueError('exact sidecar transaction identity required')
    raw = store.read_bytes(audit.ASSETS, PREFIX + transaction_id + '.json.gz')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        expanded = stream.read(100 * 1024 * 1024 + 1)
    if len(expanded) > 100 * 1024 * 1024:
        raise ValueError('sidecar reconciliation journal expands beyond limit')
    value = json.loads(expanded)
    if identity(value) != transaction_id:
        raise ValueError('sidecar reconciliation journal checksum mismatch')
    return run(store, api, value)
