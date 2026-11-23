"""Retain the exact raw EPUB parents needed to resume an approved release.

The initial release must call retain before staging, under the writer fence.
Recovery calls only load: missing or unconfirmed evidence is not a reason to
recreate it from live roots. These objects are inputs, not write permits or
asset protection records.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re

from .reader_image_history_store import S3ClaimStore
from .reader_image_history_v3 import LIMIT as MAX_POINTER
from .reader_stream_checkpoint_cas import MAX_INDEX
from .shared import READER_ASSETS_BUCKET as BUCKET

PREFIX = 'reader-index/gc/ebook-recovery-inputs/v1/'
REVIEW_PREFIX = 'reader-index/gc/ebook-exact-reviews/v1/'
MAX_REVIEW = 8 * 1024**2  # Same ceiling as reader_ebook_rebuild_publish.
MAX_SIDECAR = 50 * 1024**2  # Packed limit in reader_image_history_catalog.
_SHA256 = re.compile(r'[0-9a-f]{64}')
_INPUTS = (
    ('index', 'index.json', 'canonical_parent_sha256', MAX_INDEX),
    ('sidecar', 'sidecar.gz', 'sidecar_parent_sha256', MAX_SIDECAR),
    ('pointer', 'pointer.json', 'pointer_parent_sha256', MAX_POINTER),
)


def _pin(value):
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ValueError('expected a lowercase EPUB SHA-256 pin')
    return value


def _check_bytes(raw, maximum, digest, path):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ValueError(f'invalid EPUB recovery byte type or size: {path}')
    if sha256(raw).hexdigest() != digest:
        raise ValueError(f'EPUB recovery SHA-256 differs: {path}')


def _unique(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('duplicate EPUB approval field')
    return value


def _inputs(review, review_sha256):
    _pin(review_sha256)
    if (type(review) is not dict or review.get('kind') != 'ebook-rebuild-review'
            or type(review.get('version')) is not int or review['version'] != 1
            or type(review.get('remote_writes')) is not int or review['remote_writes'] != 0):
        raise ValueError('expected a read-only EPUB release review')
    return [(name, f'{PREFIX}{review_sha256}/{filename}', _pin(review.get(field)), maximum)
            for name, filename, field, maximum in _INPUTS]


def _verify_approval(store, reader, review, review_sha256):
    if not isinstance(store, S3ClaimStore) or not callable(getattr(reader, 'read_bytes', None)):
        raise ValueError('expected an S3ClaimStore and a Hub reader')
    # Bind to the stored approval's raw hash, not a new serialization of it.
    path = f'{REVIEW_PREFIX}{review_sha256}.json'
    raw, _ = store._get(path, maximum=MAX_REVIEW)
    if raw is None:
        raise FileNotFoundError(path)
    _check_bytes(raw, MAX_REVIEW, review_sha256, path)
    approved = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    if json.dumps(approved, sort_keys=True, allow_nan=False) != json.dumps(review, sort_keys=True, allow_nan=False):
        raise ValueError('supplied EPUB review differs from the exact stored approval')


def _observe(reader, path, raw, maximum, digest):
    if raw is None:
        raise FileNotFoundError(path)
    _check_bytes(raw, maximum, digest, path)
    seen = reader.read_bytes(BUCKET, path)
    # The Hub API returns a whole body; apply the same ceiling before comparing.
    if type(seen) is not bytes or len(seen) > maximum or seen != raw:
        raise ValueError(f'Hub EPUB recovery bytes differ from S3: {path}')
    return raw


def retain(store: S3ClaimStore, reader, review, review_sha256, *, index, sidecar, pointer, guard) -> None:
    """Create missing inputs once; accept only byte-identical existing objects.

    Call guard('stage:recovery-input-put:<name>') immediately before each PUT.
    Definitive conditional losses can be observed; unknown PUT outcomes escape
    unchanged and never trigger a retry or permit staging to proceed.
    """
    if not callable(guard):
        raise ValueError('an EPUB recovery writer guard is required')
    inputs = _inputs(review, review_sha256)
    supplied = {'index': index, 'sidecar': sidecar, 'pointer': pointer}
    for name, path, digest, maximum in inputs:
        _check_bytes(supplied[name], maximum, digest, path)
    _verify_approval(store, reader, review, review_sha256)
    for name, path, digest, maximum in inputs:
        raw = supplied[name]
        found, _ = store._get(path, maximum=maximum)
        if found is None:
            phase = f'stage:recovery-input-put:{name}'
            if guard(phase) is not True:
                raise RuntimeError(f'EPUB release guard refused {phase}')
            store._put(path, raw, maximum=maximum, IfNoneMatch='*')
            # Also verify the winner if _put reported a definitive conflict.
            found, _ = store._get(path, maximum=maximum)
        if found is not None and found != raw:
            raise ValueError(f'EPUB recovery bytes differ from supplied parent: {path}')
        _observe(reader, path, found, maximum, digest)


def load(store: S3ClaimStore, reader, review, review_sha256) -> dict[str, bytes]:
    """Load all three exact archived parents, without any write or live fallback."""
    inputs = _inputs(review, review_sha256)
    _verify_approval(store, reader, review, review_sha256)
    parents = {}
    for name, path, digest, maximum in inputs:
        raw, _ = store._get(path, maximum=maximum)
        parents[name] = _observe(reader, path, raw, maximum, digest)
    return parents
