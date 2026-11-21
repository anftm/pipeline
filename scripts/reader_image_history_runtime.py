"""Run one reviewed historical image through the existing index and legacy CASes.

No CLI, credentials, writer fence acquisition, ACKs, releases, or deletions.
Retain the original review files, small parents (including the sidecar ETag),
archived catalog, and both journals for recovery. A spent permit is observed,
never reset. The caller owns the continuous all-writer/GC fence and prevents ABA.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import publish_reader_v3 as v3, reader_image_history as image
from . import reader_image_history_catalog as catalog, reader_image_history_publish as legacy
from . import reader_image_history_shard as shard, reader_image_history_transition as indexes
from . import reader_lifecycle as lifecycle, reader_route_control as control
from .reader_image_history_publish_s3 import _digest
from .reader_stream_checkpoint_cas import MAX_INDEX, Snapshot, strong_etag

REPORTS = ('report.json', 'owning-shard-preview.json', 'legacy-catalog-preview.json', 'v3-handoff-preview.json')


@dataclass(frozen=True)
class Reviewed:
    sha256: str
    transition: indexes.Transition
    report_raw: bytes
    before_sidecar: bytes
    sidecar_etag: str
    after_sidecar: bytes
    pinned_after: bytes
    avif: bytes
    pointer_parent: bytes
    pointer_target: bytes
    catalog_parent: legacy.Version
    catalog_target_sha256: str
    catalog_target_bytes: int
    catalog_timestamp: str
    legacy_generation: str

    @property
    def report(self):
        return json.loads(self.report_raw)

    @property
    def legacy_snapshot(self):
        return f'reader-index/generations/{self.legacy_generation}/reader_assets.json.gz'

    @property
    def processing_path(self):
        return f'reader-index/processing/stream-upload-{self.transition.canonical.operation}.json'


def _read(path, limit):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError(f'preview artifact outside its size limit: {path.name}')
    return raw


def _check_raw(raw, sha, label, *, limit, size=None):
    image._hex(sha)
    if (not isinstance(raw, bytes) or not 0 < len(raw) <= limit
            or (size is not None and (type(size) is not int or len(raw) != size)) or image._sha(raw) != sha):
        raise ValueError(f'exact reviewed {label} bytes differ')


def _asset(row):
    return image.Asset(row['path'].rsplit('/', 1)[0], row['path'], row['sha256'], row['bytes'])


def review(preview_dir, *, reviewed_sha256, canonical_parent, shard_parent, before_sidecar,
           sidecar_etag, pointer_parent) -> Reviewed:
    """Bind the raw preview reports and artifact/parent bytes without remote I/O.

    reviewed_sha256 is externally approved: SHA256(v3.encode([(name, SHA256(raw))
    for name in REPORTS])). The two index parents, sidecar and pointer are raw
    bytes. sidecar_etag is the original S3 ETag from a full GET of before_sidecar;
    retain it separately (it was not recorded by the preview).
    """
    directory = Path(preview_dir)
    reports = [_read(directory / name, 256 * 1024) for name in REPORTS]
    if image._sha(v3.encode(list(zip(REPORTS, map(image._sha, reports))))) != reviewed_sha256:
        raise ValueError('preview reports differ from the independently reviewed digest')
    p, s, l, u = [json.loads(raw, object_pairs_hook=catalog._unique) for raw in reports]
    key, operation = p['key'], p['operation']
    if (p['kind'] != 'reader-image-history-preview' or type(p['version']) is not int or p['version'] != 1
            or p['changed_route_keys'] != [key] or any(q['deletion_authorized'] is not False for q in (p, l, u))
            or any(q['operation'] != operation for q in (s, l, u))
            or s['applied'] is not False or u['applied'] is not False or u['key'] != key
            or p['producer_revision'] != shard.PRODUCER or s['actual_producer'] != shard.PRODUCER):
        raise ValueError('inconsistent one-image review reports')
    cp, sp = p['canonical'], s['parent']
    if (cp['path'] != image.CANONICAL or cp['target_sha256'] != s['canonical_shadow_sha256']
            or sp != {k: p['shard'][k] for k in sp}):
        raise ValueError('index review pins disagree')
    _check_raw(canonical_parent, cp['sha256'], 'canonical parent', limit=MAX_INDEX, size=cp['bytes'])
    _check_raw(shard_parent, sp['sha256'], 'shard parent', limit=MAX_INDEX, size=sp['bytes'])
    candidate = _read(directory / 'candidate.json', MAX_INDEX)
    splice = _read(directory / 'owning-shard-splice.json', MAX_INDEX)
    _check_raw(candidate, cp['target_sha256'], 'canonical target', limit=MAX_INDEX)
    _check_raw(splice, s['target_sha256'], 'shard target', limit=MAX_INDEX, size=s['target_bytes'])
    source, current = (image.Source(**p[name]) for name in ('source', 'current_source'))
    old, new = (_asset(p[name]) for name in ('old_row', 'target_row'))
    avif = _read(directory / 'document.avif', 64 * 1024 * 1024)
    _check_raw(avif, new.sha256, 'AVIF', limit=64 * 1024 * 1024, size=new.size)
    after = _read(directory / 'sidecar.json.gz', catalog.MAX_SIDECAR_BYTES)
    packed = _read(directory / 'pinned-base.json.gz', 8 * 1024 * 1024)
    _check_raw(before_sidecar, p['parent_sidecar_sha256'], 'before sidecar', limit=catalog.MAX_SIDECAR_BYTES)
    _check_raw(after, p['sidecar_sha256'], 'after sidecar', limit=catalog.MAX_SIDECAR_BYTES)
    ref = v3.base_sidecar_ref(p['proposed_base'])
    _check_raw(packed, ref['sha256'], 'v3 pinned base', limit=8 * 1024 * 1024, size=ref['bytes'])
    _check_raw(pointer_parent, p['reader_pointer_sha256'], 'v3 parent pointer', limit=1024 * 1024)
    pointer = json.loads(pointer_parent, object_pairs_hook=catalog._unique)
    pair = p['before_projection_sha256'], p['after_projection_sha256']
    for digest in (*pair, l['parent_sha256'], l['target_sha256']):
        image._hex(digest)
    for count in (l['parent_bytes'], l['target_bytes']):
        if type(count) is not int or not 0 < count <= catalog.MAX_CATALOG_BYTES:
            raise ValueError('legacy catalog length is outside bounds')
    image._hex(l['generation'], 32)
    if (pointer['generation'] != p['reader_generation'] or pointer['catalog']['sha256'] != p['reader_catalog_sha256']
            or u['parent_generation'] != pointer['generation'] or u['pointer']['generation'] != u['generation']
            or u['expected_base'] != ref or u['expected_canonical_sidecar'] != p['sidecar_sha256']
            or (u['before_projection_sha256'], u['after_projection_sha256']) != pair
            or (p['before_route'], p['after_route']) != (image._route(old), image._route(new))
            or p['search_sha256'] != image._sha(image._encode(p['search_proof']))
            or l['snapshot'] != {'bucket': v3.ASSETS, 'path': f"reader-index/generations/{l['generation']}/reader_assets.json.gz",
                                 'bytes': len(after), 'sha256': p['sidecar_sha256']}):
        raise ValueError('legacy, Reader v3 or Search preview pins disagree')
    if catalog._route_delta(before_sidecar, after, key, old.path, new.path)[:2] != (
            l['before_sidecar_sha256'], l['after_sidecar_sha256']):
        raise ValueError('legacy sidecar delta differs from review')
    strong_etag(sidecar_etag)
    pins = image._sha(image._encode([reviewed_sha256, sidecar_etag]))
    proof = image.Preflight(source, current, p['search_sha256'], old, new, l['parent_sha256'], pins)
    prior = image._parent_index(canonical_parent)
    owned = image._parent_index(shard_parent)
    if (image._encode(image._selected(prior, key)[1]) != image._encode(p['old_row'])
            or image._encode(image._selected(owned, key)[1]) != image._encode(p['shard']['selected_row'])
            or any(r['key'] == key for r in prior.get('failures', []))
            or p['target_row'].get('profile') != image.AVIF_PROFILE
            or 'processing_roots' in p['target_row'] or 'checkpointed_at' in p['target_row']):
        raise ValueError('index review does not describe the physical parent and exact preview row')
    canonical = image.Intent(operation, key, proof, p['old_row_sha256'], image.row_digest(p['target_row']),
                             cp['sha256'], strong_etag(cp['etag']), canonical_parent, candidate, *pair)
    pin = shard.ShardPin(**sp)
    intent = indexes.seal(canonical, shard=Snapshot(owned, pin.etag, pin.sha256, shard_parent), pin=pin)
    if intent.shard_target != splice:
        raise ValueError('physical splice does not match the reviewed target')
    parent = legacy.Version(l['parent_sha256'], l['parent_bytes'], strong_etag(l['parent_etag']))
    return Reviewed(reviewed_sha256, intent, reports[0], before_sidecar, sidecar_etag, after, packed, avif,
                    pointer_parent, v3.encode(u['pointer']), parent, l['target_sha256'], l['target_bytes'],
                    l['timestamp'], l['generation'])


def _fence(guard, phase):
    if guard(phase) is not True:
        raise ValueError(f'actual image history writer/GC fence not confirmed: {phase}')
    return True


class _AfterSidecar:
    def __init__(self, store, packed):
        self.store, self.packed = store, packed

    def read_bytes(self, bucket, path):
        if (bucket, path) == (v3.ASSETS, legacy.SIDECAR):
            return self.packed
        return self.store.read_bytes(bucket, path)


def _v3_preview(reviewed, store):
    report = reviewed.report
    canonical = reviewed.transition.canonical
    if store.read_bytes(v3.ASSETS, v3.POINTER) != reviewed.pointer_parent:
        raise ValueError('exact v3 parent pointer differs')
    pointer, reading = v3.current(store)
    if (pointer != json.loads(reviewed.pointer_parent) or reading.get('derived_pdf_redirects')
            or canonical.key in reading['files'] or canonical.key in reading.get('suppressed_sources', [])
            or reading.get('base_sidecar', {}).get('sha256') != report['pinned_base_sha256']):
        raise ValueError('v3 parent does not admit the reviewed image')
    packed, ref = control.prepare_base(reading, reviewed.after_sidecar)
    before = v3.project_sidecar(v3.pinned_base(store, reading, {}), reading)
    after = v3.project_sidecar(catalog._sidecar(packed), reading)
    if (packed != reviewed.pinned_after or ref != report['proposed_base'] or image._projection_delta(
            canonical.key, canonical.preflight.old, canonical.preflight.new, before, after) != (
            canonical.before_projection_sha256, canonical.after_projection_sha256)):
        raise ValueError('v3 base or whole projected route delta differs from review')
    result = v3.unify(_AfterSidecar(store, reviewed.after_sidecar), expected_pointer=pointer,
                      expected_canonical=report['sidecar_sha256'], conditional_pointer=True)
    if result.get('unchanged') or v3.encode(result['pointer']) != reviewed.pointer_target:
        raise ValueError('v3 conditional unify dry run differs from reviewed successor')


def publish_legacy(reviewed: Reviewed, *, open_catalog_parent, checkpoints, index_journal, legacy_journal,
                   reader_store, observe_legacy, reader_catalog_digest, legacy_cas, guard, revalidate,
                   create_immutable, observe_avif_mime, resume_indexes=False, resume_legacy=False, legacy_etag=None):
    """Apply/observe exactly one index transition and legacy publication.

    open_catalog_parent() opens the *archived*, seekable binary parent from byte
    zero. create_immutable(bucket, path, raw) is single-attempt create-if-absent;
    reader_store provides fresh independent reads (including FileNotFoundError).
    observe_legacy(path) is a full streaming S3 digest/length/ETag observation;
    optional legacy_etag(path) is fresh S3 HEAD, never a cached/client ETag;
    reuse a digest only for a matching strong ETag under the no-ABA fence.
    reader_catalog_digest() independently streams the public resolver catalog.
    observe_avif_mime(bucket, path) freshly verifies the serving view's media
    type; it must return the observed MIME type, not an upload header or guess.

    guard(phase) must interrogate the real continuous fence, including producer
    writers, sidecar/Reader writers and GC, not an approval flag or receipt.
    revalidate(reviewed, phase) returns fresh image.Checks: independently verify
    both immutable source pins, live Search proof, pinned Reader builder inputs,
    WebP rollback protection and both whole shadow projections. In Preflight,
    rollback_sha256 binds the reviewed archival catalog identity and pins_sha256
    is SHA256(image._encode([reviewed.sha256, reviewed.sidecar_etag])); use
    those identities only after verification, never by echoing the Preflight.
    Phase labels distinguish indexes:parents/shard-target/both-targets from
    legacy:parents/catalog-target/both-targets; use sealed parents as shadows
    for roots that have intentionally advanced. The archive does not by itself
    keep WebP reachable: revalidate must verify the actual rollback root.

    The active processing record is visible before AVIF upload, remains active
    throughout, and is NOT released. It is separate from the exact reviewed index
    bytes. Recovery requires it and the immutable objects still to exist.
    Returns legacy evidence and conditional-unify admission, not a v3 promotion
    or consumer receipt. Keep the fence for any use of that admission.
    """
    if resume_legacy and not resume_indexes:
        raise ValueError('legacy recovery requires index recovery')
    intent = reviewed.transition
    canonical = intent.canonical
    proof = canonical.preflight
    bucket = v3.ASSETS
    fence = lambda phase: _fence(guard, phase)
    fence('prepare')

    observed = {}

    def observe(path):
        prior = observed.get(path)
        if prior is None or legacy_etag is None or strong_etag(legacy_etag(path)) != prior.etag:
            version = observe_legacy(path)
            prior = observed[path] = legacy.Version(version.sha256, version.size, strong_etag(version.etag))
        return prior

    def ensure(path, raw):
        try:
            actual = reader_store.read_bytes(bucket, path)
        except FileNotFoundError:
            if resume_indexes:
                raise ValueError(f'missing immutable recovery input: {path}') from None
            fence('prepare:create')
            create_immutable(bucket, path, raw)
            actual = reader_store.read_bytes(bucket, path)
        if actual != raw:
            raise ValueError(f'exact immutable readback differs: {path}')

    record = lifecycle.processing_record([{'bucket': bucket, 'root': proof.new.root}], 'reader-image-history',
                                         {'operation': canonical.operation, 'reviewed_sha256': reviewed.sha256})
    processing_identity = {k: record[k] for k in ('version', 'kind', 'status', 'operation', 'run', 'resources', 'release_policy')}

    def protection():
        actual = json.loads(reader_store.read_bytes(bucket, reviewed.processing_path), object_pairs_hook=catalog._unique)
        if v3.encode({k: actual.get(k) for k in processing_identity}) != v3.encode(processing_identity):
            raise ValueError('active AVIF processing root does not match the reviewed operation')

    def fresh(phase):
        protection()
        for asset in (proof.old, proof.new):
            _check_raw(reader_store.read_bytes(bucket, asset.path), asset.sha256, asset.path,
                       limit=64 * 1024 * 1024, size=asset.size)
        return revalidate(reviewed, phase)

    with open_catalog_parent() as parent:
        parent.seek(0)
        if _digest(parent, reviewed.catalog_parent.size) != reviewed.catalog_parent.sha256:
            raise ValueError('archived legacy catalog parent differs from review')
        parent.seek(0)
        _v3_preview(reviewed, reader_store)
        if not resume_legacy:
            prior = observe(legacy.SIDECAR)
            if (observe(legacy.CATALOG) != reviewed.catalog_parent
                    or prior != legacy.Version(image._sha(reviewed.before_sidecar), len(reviewed.before_sidecar),
                                               reviewed.sidecar_etag)
                    or reader_store.read_bytes(bucket, legacy.SIDECAR) != reviewed.before_sidecar):
                raise ValueError('legacy roots no longer match the reviewed parents')
        if not resume_indexes:
            try:
                protection()
            except FileNotFoundError:
                fence('prepare:protect')
                create_immutable(bucket, reviewed.processing_path, v3.encode(record) + b'\n')
        protection()
        ensure(proof.new.path, reviewed.avif)
        if observe_avif_mime(bucket, proof.new.path) != 'image/avif':
            raise ValueError('served AVIF MIME differs from image/avif')
        ensure(reviewed.legacy_snapshot, reviewed.after_sidecar)
        index_args = dict(checkpoints=checkpoints, revalidate=lambda _, phase: fresh('indexes:' + phase),
                          require_fence=lambda _, phase: fence('indexes:' + phase))
        if not resume_indexes:
            indexes.start(index_journal, intent, **index_args)
        applied = indexes.reconcile(index_journal, intent, **index_args)

        def legacy_guard(phase):
            label = 'legacy:' + phase
            fence(label)
            if index_journal.load(intent.claim_key) != (intent, applied):
                raise ValueError('applied index journal no longer matches the reviewed transition')
            image._fresh(canonical, lambda *_: fresh(label), 'after')
            for path, raw, etag in ((image.CANONICAL, canonical.candidate, applied.canonical_etag),
                                    (intent.shard_pin.path, intent.shard_target, applied.shard_etag)):
                actual = checkpoints.read(bucket, path)
                if actual.raw != raw or actual.etag != etag:
                    raise ValueError(f'exact applied index target differs: {path}')
            return fence(label)

        def cas(path, stream, **kwargs):
            fence('legacy:put-catalog' if path == legacy.CATALOG else 'legacy:put-sidecar')
            return legacy_cas(path, stream, **kwargs)

        result = legacy.publish(parent, parent=reviewed.catalog_parent, before_sidecar=reviewed.before_sidecar,
                                sidecar_etag=reviewed.sidecar_etag, after_sidecar=reviewed.after_sidecar,
                                key=canonical.key, webp_path=proof.old.path, avif_path=proof.new.path,
                                now=reviewed.catalog_timestamp, expected_catalog_sha256=reviewed.catalog_target_sha256,
                                expected_catalog_bytes=reviewed.catalog_target_bytes,
                                expected_pointer_raw=reviewed.pointer_parent, journal=legacy_journal, observe=observe,
                                reader_catalog_digest=reader_catalog_digest, cas=cas, store=reader_store,
                                guard=legacy_guard, resume=resume_legacy)
    if result['generation'] != reviewed.legacy_generation:
        raise ValueError('observed legacy generation differs from review')
    return {**result, 'indexes': applied, 'reviewed_sha256': reviewed.sha256,
            'expected_v3_pointer_raw': reviewed.pointer_target, 'reader_consumers_verified': False}


def observe_v3(reviewed: Reviewed, *, reader_store, observe_receipt, guard):
    """Read-only verification after promotion, including after a lost response.

    observe_receipt(surface) must independently fetch status and verify the
    full deployed projection (e.g. publish_reader_v3.observe_consumer). There
    are no acknowledgments, handoffs or retries of any publication here.
    """
    _fence(guard, 'v3:observe')

    def current():
        if (reader_store.read_bytes(v3.ASSETS, v3.POINTER) != reviewed.pointer_target
                or reader_store.read_bytes(v3.ASSETS, legacy.SIDECAR) != reviewed.after_sidecar):
            raise ValueError('exact reviewed v3 successor or canonical sidecar is not visible')
        pointer, reading = v3.current(reader_store)
        if v3.encode(pointer) != reviewed.pointer_target or reading['base_sidecar'] != reviewed.report['proposed_base']:
            raise ValueError('observed v3 catalog differs from the review')
        projected = v3.project_sidecar(v3.pinned_base(reader_store, reading, {}), reading)
        intent = reviewed.transition.canonical
        if projected['f'].get(intent.key) != image._route(intent.preflight.new):
            raise ValueError('observed v3 successor does not route the reviewed AVIF')
        return pointer, reading

    pointer, reading = current()
    receipts = {}
    for surface in ('hf', 'pages'):
        receipt = observe_receipt(surface)
        if (receipt.get('version') != 1 or receipt.get('surface') != surface or receipt.get('active') is not True
                or receipt.get('projection_verified') is not True or not receipt.get('checked_url')
                or receipt.get('catalog_sha256') != pointer['catalog']['sha256']
                or receipt.get('generation') != pointer['generation'] or receipt.get('files') != len(reading['files'])):
            raise ValueError(f'{surface} did not verify the reviewed v3 successor and full projection')
        receipts[surface] = receipt
    current()
    _fence(guard, 'v3:observe')
    return {'generation': pointer['generation'], 'consumers': receipts, 'reader_consumers_verified': True,
            'deletion_authorized': False}
