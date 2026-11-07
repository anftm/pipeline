"""Compact superseded legacy-catalog history under one conditional write; never deletes objects.

A superseded generation lists every object an older deployment might still use.
Once both consumers serve the current v3 generation and the current legacy
generation binds the canonical sidecar, those lists can shrink to tombstones.
This refuses unless everything the dropped generations protected is still
protected by a kept generation or an active v3 redirect. The exact
pre-compaction catalog is saved in a content-addressed journal before one
If-Match write replaces it. Writer isolation is the owner's declaration, not
something proven here, and success never authorizes object deletion.
"""
import base64
import gzip
import hashlib
import io
import json

try:
    from . import reader_page_audit as audit, reader_page_migrate as migration
    from . import reader_page_redirects as redirects, reader_lifecycle as lifecycle
    from . import reader_sidecar_reconcile as window, publish_reader_v3 as publication
    from .reader_gc_stream import compact_catalog_stream
except ImportError:
    import reader_page_audit as audit
    import reader_page_migrate as migration
    import reader_page_redirects as redirects
    import reader_lifecycle as lifecycle
    import reader_sidecar_reconcile as window
    import publish_reader_v3 as publication
    from reader_gc_stream import compact_catalog_stream

PREFIX = 'reader-index/gc/legacy-compact/'
MAX_CATALOG = 64 * 1024 * 1024
MAX_JOURNAL = 100 * 1024 * 1024
KIND = 'reader-legacy-catalog-compaction'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def refs_of(entry):
    refs = entry.get('references') if isinstance(entry, dict) else None
    if not isinstance(refs, list) or any(not isinstance(ref, dict) or not isinstance(ref.get('bucket'), str)
                                         or not isinstance(ref.get('path'), str) for ref in refs):
        raise ValueError('legacy catalog generation has invalid references')
    return {(ref['bucket'], ref['path']) for ref in refs}


def check_transition(before, after, mapping):
    """Prove the compacted catalog only retires history; mapping=None skips the pin check."""
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or any(doc.get('version') != 1 or doc.get('kind') != 'reader-catalog'
                   or 'redirect_sidecar_pending' in doc for doc in (before, after))):
        raise ValueError('invalid or pending legacy catalog')
    history = {'generations', 'retired_generations'}
    if ({k: v for k, v in before.items() if k not in history}
            != {k: v for k, v in after.items() if k not in history}):
        raise ValueError('compaction changed top-level catalog fields')
    current = (before.get('current') or {}).get('generation')
    old, new = before.get('generations'), after.get('generations')
    if (not isinstance(current, str) or not isinstance(old, dict) or not isinstance(new, dict)
            or current not in new or not set(new) <= set(old)):
        raise ValueError('compaction lost the current generation or invented one')
    if any(entry != old[name] for name, entry in new.items()):
        raise ValueError('compaction rewrote a kept generation')
    dropped = sorted(set(old) - set(new))
    old_retired = before.get('retired_generations') or {}
    new_retired = after.get('retired_generations') or {}
    if not set(old_retired) <= set(new_retired) or set(new_retired) != set(old_retired) | set(dropped):
        raise ValueError('compaction lost or invented retirement records')
    for name in dropped:
        tombstone = new_retired[name]
        if (not isinstance(tombstone, dict) or tombstone.get('replacement_generation') != current
                or tombstone.get('replacement_acks') != {'hf': True, 'pages': True}):
            raise ValueError('invalid compaction tombstone')
    kept_refs = set()
    for entry in new.values():
        kept_refs |= refs_of(entry)
    dropped_refs = set()
    for name in dropped:
        dropped_refs |= refs_of(old[name])
    released = dropped_refs - kept_refs
    redirect_pins = set()
    for source, target in (mapping or {}).items():
        redirect_pins |= {(audit.BUCKET, source), (audit.BUCKET, target)}
    unpinned = released - redirect_pins
    if mapping is not None and unpinned:
        raise ValueError('compaction would release %d references that nothing else pins, e.g. %s'
                         % (len(unpinned), sorted(unpinned)[:3]))
    return {'kept': sorted(new), 'dropped': dropped, 'released': len(released),
            'redirect_only': len(released & redirect_pins)}


def decoded(value):
    try:
        return (base64.b64decode(value['catalog_before_base64'], validate=True),
                base64.b64decode(value['catalog_after_base64'], validate=True))
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid legacy compaction bytes') from None


def validate(value):
    if (not isinstance(value, dict) or value.get('version') != 1 or value.get('kind') != KIND
            or value.get('objects_deleted') is not False or value.get('deletion_authorized') is not False
            or not isinstance(value.get('pointer'), dict)
            or not audit.SHA.fullmatch(str(value['pointer'].get('generation', '')))):
        raise ValueError('invalid legacy compaction journal')
    for field in ('index_sha256', 'sidecar_sha256', 'catalog_before_sha256', 'catalog_after_sha256'):
        if not audit.SHA.fullmatch(str(value.get(field, ''))):
            raise ValueError('invalid legacy compaction digest')
    before_raw, after_raw = decoded(value)
    if sha(before_raw) != value['catalog_before_sha256'] or sha(after_raw) != value['catalog_after_sha256']:
        raise ValueError('legacy compaction bytes differ')
    try:
        before, after = json.loads(before_raw), json.loads(after_raw)
    except ValueError:
        raise ValueError('invalid legacy compaction catalog') from None
    plan = check_transition(before, after, None)
    if (value.get('generations_kept') != plan['kept'] or value.get('generations_dropped') != plan['dropped']
            or not plan['dropped'] or value.get('receipt_generation') != before['current']['generation']):
        raise ValueError('legacy compaction journal differs from its catalogs')
    for field in ('released_references', 'redirect_only_references'):
        if type(value.get(field)) is not int or value[field] < 0:
            raise ValueError('invalid legacy compaction counts')
    return value


def identity(value):
    return sha(audit.encode(validate(value)))


def prepare(store, *, expected_generation, expected_catalog):
    for value in (expected_generation, expected_catalog):
        if not isinstance(value, str) or not audit.SHA.fullmatch(value):
            raise ValueError('exact v3 generation and legacy catalog SHA-256 identities are required')
    pointer, v3 = publication.current(store)
    if not pointer or pointer['generation'] != expected_generation:
        raise ValueError('v3 generation changed before legacy compaction')
    mapping = redirects.validated(v3)
    receipt = publication.record_legacy_acknowledgments(store, apply=False)
    if receipt.get('kind') != 'reader-gc-acknowledgments':
        raise ValueError('legacy catalog is not acknowledged for compaction: ' + str(receipt.get('reason')))
    size = store.stat(audit.ASSETS, migration.CATALOG)
    if type(size) is not int or not 0 < size <= MAX_CATALOG:
        raise ValueError('legacy catalog exceeds the bounded compaction size')
    raw = store.read_bytes(audit.ASSETS, migration.CATALOG)
    if len(raw) != size or sha(raw) != expected_catalog or receipt['current']['catalog_sha256'] != expected_catalog:
        raise ValueError('legacy catalog changed after review')
    before = json.loads(raw)
    current = (before.get('current') or {}).get('generation')
    encoded, _digest, _stats = compact_catalog_stream([raw], False, current=current, acknowledged=True,
                                                      grace_days=0, now=lifecycle.now_iso())
    plan = check_transition(before, json.loads(encoded), mapping)
    if not plan['dropped']:
        raise ValueError('legacy catalog has no superseded generation to compact')
    index_raw = store.read_bytes(audit.BUCKET, audit.INDEX)
    sidecar_raw = store.read_bytes(audit.ASSETS, audit.SIDECAR)
    if publication.current(store)[0] != pointer or store.read_bytes(audit.ASSETS, migration.CATALOG) != raw:
        raise ValueError('Reader roots changed during legacy compaction planning')
    value = {'version': 1, 'kind': KIND, 'pointer': pointer, 'index_sha256': sha(index_raw),
             'sidecar_sha256': sha(sidecar_raw), 'catalog_before_sha256': expected_catalog,
             'catalog_after_sha256': sha(encoded),
             'catalog_before_base64': base64.b64encode(raw).decode('ascii'),
             'catalog_after_base64': base64.b64encode(encoded).decode('ascii'),
             'receipt_generation': current, 'generations_kept': plan['kept'],
             'generations_dropped': plan['dropped'], 'released_references': plan['released'],
             'redirect_only_references': plan['redirect_only'],
             'objects_deleted': False, 'deletion_authorized': False}
    validate(value)
    return value


def preview(value):
    return {'version': 1, 'kind': value['kind'], 'applied': False, 'candidate_sha256': identity(value),
            'generation': value['pointer']['generation'], 'generations_kept': value['generations_kept'],
            'generations_dropped': value['generations_dropped'],
            'released_references': value['released_references'],
            'redirect_only_references': value['redirect_only_references'],
            'catalog_before_sha256': value['catalog_before_sha256'],
            'catalog_after_sha256': value['catalog_after_sha256'],
            'objects_deleted': False, 'deletion_authorized': False, 'external_credentials_verified': False}


def persist(store, value):
    tx = identity(value)
    raw = audit.encode(value)
    if len(raw) > MAX_JOURNAL:
        raise ValueError('legacy compaction journal expands beyond limit')
    store.immutable_put(audit.ASSETS, PREFIX + tx + '.json.gz', gzip.compress(raw, mtime=0))
    print('legacy compaction transaction persisted: ' + tx, flush=True)
    return tx


def read_journal(store, tx):
    stored = store.read_bytes(audit.ASSETS, PREFIX + tx + '.json.gz')
    with gzip.GzipFile(fileobj=io.BytesIO(stored)) as stream:
        expanded = stream.read(MAX_JOURNAL + 1)
    if len(expanded) > MAX_JOURNAL:
        raise ValueError('legacy compaction journal expands beyond limit')
    return expanded


def verify_consumers(store, generation):
    import httpx
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        publication.verify_generation(store, client, generation)


def unchanged_roots(store, value):
    if (publication.current(store)[0] != value['pointer']
            or sha(store.read_bytes(audit.BUCKET, audit.INDEX)) != value['index_sha256']
            or sha(store.read_bytes(audit.ASSETS, audit.SIDECAR)) != value['sidecar_sha256']):
        raise ValueError('PDF index, canonical sidecar or v3 pointer changed during legacy compaction')


def run(store, value):
    window.require_writer_window(store)
    validate(value)
    tx = identity(value)
    if read_journal(store, tx) != audit.encode(value):
        raise ValueError('legacy compaction journal was not durably persisted')
    pointer, v3 = publication.current(store)
    if pointer != value['pointer']:
        raise ValueError('v3 pointer changed during legacy compaction')
    unchanged_roots(store, value)
    before_raw, after_raw = decoded(value)
    check_transition(json.loads(before_raw), json.loads(after_raw), redirects.validated(v3))
    raw, etag = store.read_for_update(audit.ASSETS, migration.CATALOG)
    if sha(raw) == value['catalog_after_sha256']:
        written = False
    elif sha(raw) == value['catalog_before_sha256']:
        written = True
        verify_consumers(store, value['pointer']['generation'])
        unchanged_roots(store, value)
        window.require_writer_window(store)
        raw, etag = store.read_for_update(audit.ASSETS, migration.CATALOG)
        if sha(raw) != value['catalog_before_sha256']:
            raise ValueError('legacy catalog changed before conditional compaction')
        store.put_if_match(audit.ASSETS, migration.CATALOG, after_raw, etag)
        actual, _ = store.read_for_update(audit.ASSETS, migration.CATALOG)
        if actual != after_raw or store.read_bytes(audit.ASSETS, migration.CATALOG) != after_raw:
            raise ValueError('compacted legacy catalog readback differs')
    else:
        raise ValueError('legacy catalog diverged from the compaction journal')
    unchanged_roots(store, value)
    window.require_writer_window(store)
    receipt = publication.record_legacy_acknowledgments(store, apply=True)
    if (receipt.get('kind') != 'reader-gc-acknowledgments'
            or receipt['current']['generation'] != value['receipt_generation']):
        raise ValueError('acknowledgment receipt was not refreshed: ' + str(receipt.get('reason')))
    return {**preview(value), 'applied': True, 'transaction_id': tx, 'catalog_written': written,
            'acknowledgment_catalog_sha256': receipt['current']['catalog_sha256']}


def apply(store, value):
    window.require_writer_window(store)
    validate(value)
    if sha(store.read_bytes(audit.ASSETS, migration.CATALOG)) != value['catalog_before_sha256']:
        raise ValueError('legacy catalog changed before the journal')
    unchanged_roots(store, value)
    verify_consumers(store, value['pointer']['generation'])
    persist(store, value)
    return run(store, value)


def resume(store, transaction_id):
    if not isinstance(transaction_id, str) or not audit.SHA.fullmatch(transaction_id):
        raise ValueError('exact legacy compaction transaction identity required')
    value = json.loads(read_journal(store, transaction_id))
    if identity(value) != transaction_id:
        raise ValueError('legacy compaction journal checksum mismatch')
    return run(store, value)
