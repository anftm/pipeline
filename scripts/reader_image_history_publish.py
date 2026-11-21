"""Publish one legacy image route under a caller-owned writer/GC fence.

Precreate the immutable snapshot outside this coordinator. Move the catalog
first: its successor protects the new route *and* retains the old generations
while the canonical sidecar still serves WebP. Legacy acknowledgments require
the catalog and sidecar to agree. The caller must exclude other writers and GC
(including during recovery) and prevent ABA; the two CASes are not atomic.

There is no transport, snapshot creation, v3 promotion, ACK, or deletion here.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from tempfile import TemporaryFile
from typing import BinaryIO, Callable, Literal, Protocol

from . import publish_reader_v3 as v3, reader_image_history as image
from . import reader_image_history_catalog as catalog, reader_route_control as control
from .reader_page_cas import strong_etag

ASSETS = v3.ASSETS
CATALOG = 'reader-index/reader_catalog.json'
SIDECAR = 'reader-index/reader_assets.json.gz'


@dataclass(frozen=True)
class Version:
    sha256: str
    size: int
    etag: str


@dataclass(frozen=True)
class Record:
    seal: str
    state: Literal['catalog-permitted', 'sidecar-permitted', 'legacy-observed']
    catalog_etag: str | None = None
    sidecar_etag: str | None = None


class PutOnce(Protocol):
    def __call__(self, path: str, stream: BinaryIO, *, size: int, digest: str, parent_etag: str) -> str: ...


Phase = Literal['parents', 'catalog-target', 'both-targets']


class Journal(Protocol):
    """Dedicated durable namespace; linearizable reads and CAS, no reset/delete.

    Claim is create-if-absent. True grants permission only to the confirmed
    winner; a lost response must raise, never infer a win from readback.
    """

    def read(self, key: str) -> Record | None: ...
    def claim(self, key: str, record: Record) -> bool: ...
    def advance(self, key: str, expected: Record, record: Record) -> bool: ...


def publish(parent_catalog: BinaryIO, *, parent: Version, before_sidecar: bytes, sidecar_etag: str,
            after_sidecar: bytes, key: str, webp_path: str, avif_path: str, now: str,
            expected_pointer_raw: bytes, journal: Journal, observe: Callable[[str], Version],
            cas: PutOnce, store, guard: Callable[[Phase], bool], resume: bool = False) -> dict:
    """Return legacy evidence and kwargs for a separate v3 unify.

    Retain the original reviewed inputs (including the parent catalog bytes)
    for resume. observe(path) returns the digest, size and ETag from one fresh
    full read; the streaming adapter's equivalent Observed also works.
    cas is a synchronous single-attempt If-Match PUT with the sealed size and
    digest. It returns the response ETag, or raises on failure or ambiguity.
    store.read_bytes is the fresh reader view for small objects only.
    guard(phase) must freshly confirm the fence and the reviewed prerequisites
    (applied image indexes, source/Search pins, full projected route delta,
    retained WebP and protected AVIF) and return True. It must be read-only.
    Keep the fence through any use of the returned v3 admission arguments.
    A spent permit with parent/foreign bytes stops for operator inspection.
    """
    strong_etag(parent.etag)
    strong_etag(sidecar_etag)
    with TemporaryFile() as target:
        plan = catalog.plan(parent_catalog, target, key=key, webp_path=webp_path, avif_path=avif_path,
                            before_sidecar=before_sidecar, after_sidecar=after_sidecar, now=now,
                            expected_parent_sha256=parent.sha256)
        if type(parent.size) is not int or plan['parent_bytes'] != parent.size:
            raise ValueError('catalog parent size differs')
        before = Version(image._sha(before_sidecar), len(before_sidecar), sidecar_etag)
        snapshot = plan['snapshot']
        catalog_target = plan['target_sha256'], plan['target_bytes']
        sidecar_target = snapshot['sha256'], snapshot['bytes']
        seal = image._sha(image._encode(['reader-image-legacy-publish-v1',
                                        (parent.sha256, parent.size, parent.etag),
                                        (before.sha256, before.size, before.etag),
                                        catalog_target, sidecar_target, image._sha(expected_pointer_raw)]))
        # One claim for the entire catalog parent, not one per route, ETag, or proposal.
        claim = parent.sha256

        def persist(previous, record):
            won = (journal.claim(claim, record) if previous is None else
                   journal.advance(claim, previous, record))
            if won is not True or journal.read(claim) != record:
                raise RuntimeError('legacy journal claim/advance not confirmed; no write permit')
            return record

        def root(path, prior, wanted, etag):
            actual = observe(path)
            tag = strong_etag(actual.etag)
            if ((actual.sha256, actual.size) != wanted or (etag is not None and tag != etag)
                    or (wanted != (prior.sha256, prior.size) and tag == prior.etag)):
                raise ValueError(f'exact legacy bytes or ETag differ: {path}; do not replay')
            return tag

        def check(phase, *, catalog_etag=None, sidecar_etag=None):
            parents = phase == 'parents'
            both = phase == 'both-targets'
            c = root(CATALOG, parent, (parent.sha256, parent.size) if parents else catalog_target,
                     parent.etag if parents else catalog_etag)
            s = root(SIDECAR, before, sidecar_target if both else (before.sha256, before.size),
                     sidecar_etag if both else before.etag)
            # Check the continuous fence after potentially long catalog reads.
            if guard(phase) is not True:
                raise ValueError('legacy publication guard not confirmed')
            if store.read_bytes(ASSETS, snapshot['path']) != after_sidecar:
                raise ValueError('precreated legacy snapshot differs')
            if store.read_bytes(ASSETS, SIDECAR) != (after_sidecar if both else before_sidecar):
                raise ValueError('legacy sidecar reader view differs')
            if store.read_bytes(ASSETS, v3.POINTER) != expected_pointer_raw:
                raise ValueError('exact v3 pointer changed')
            return c, s

        pointer, reading = v3.current(store)
        if pointer is None or v3.encode(pointer) != v3.encode(json.loads(expected_pointer_raw)):
            raise ValueError('existing exact v3 pointer required')
        if (reading.get('derived_pdf_redirects') or key in reading.get('suppressed_sources', [])
                or key in reading['files']):
            raise ValueError('ordinary v3 unify cannot expose the selected image')
        control.prepare_base(reading, after_sidecar)  # Same base limits and filtering as unify.

        current = journal.read(claim)
        if resume:
            _bind(current, seal)
            c, s = current.catalog_etag, current.sidecar_etag
        else:
            if current is not None:
                raise ValueError('legacy catalog parent already claimed; resume instead')
            check('parents')
            current = persist(None, Record(seal, 'catalog-permitted'))
            check('parents')
            target.seek(0)
            c = strong_etag(cas(CATALOG, target, size=catalog_target[1], digest=catalog_target[0],
                                parent_etag=parent.etag))
            s = None

        if current.state == 'catalog-permitted':
            c, _ = check('catalog-target', catalog_etag=c)
            current = persist(current, Record(seal, 'sidecar-permitted', c))
            check('catalog-target', catalog_etag=c)
            s = strong_etag(cas(SIDECAR, io.BytesIO(after_sidecar), size=sidecar_target[1],
                                digest=sidecar_target[0], parent_etag=sidecar_etag))

        c, s = check('both-targets', catalog_etag=c, sidecar_etag=s)
        if current.state != 'legacy-observed':
            persist(current, Record(seal, 'legacy-observed', c, s))
        elif journal.read(claim) != current:
            raise RuntimeError('legacy journal changed before v3 admission')
        return {'claim_id': claim, 'seal': seal, 'generation': plan['generation'], 'snapshot': snapshot,
                'retirement_hint': plan['retirement_hint'], 'deletion_authorized': False,
                'catalog': Version(*catalog_target, c), 'sidecar': Version(*sidecar_target, s),
                'unify': {'expected_pointer': pointer, 'expected_canonical': snapshot['sha256'],
                          'conditional_pointer': True}}


def _bind(record, seal):
    if (type(record) is not Record or record.seal != seal or record.state not in
            ('catalog-permitted', 'sidecar-permitted', 'legacy-observed')
            or (record.catalog_etag is None) != (record.state == 'catalog-permitted')
            or (record.sidecar_etag is not None) != (record.state == 'legacy-observed')):
        raise ValueError('no matching legacy publication record')
