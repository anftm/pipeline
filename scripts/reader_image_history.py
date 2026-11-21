"""Single already-mapped static image WebP -> AVIF checkpoint; not a publisher.

The only non-journal write is an exact-parent CAS on pages/image/index-v2.json.
Finalization records evidence in the journal only. There is no conversion,
rollback write, deletion, shard update, sidecar write, unify, or site handoff.

Still needed before live use: a durable conditional journal and fresh-read
adapter for the retained WebP and its rollback protection, the immutable AVIF,
and pinned source/Reader inputs; coordination with all index/producer writers;
and separate sidecar/unify integration that checks the whole actual projection,
keeps rollback resources protected, and verifies both consumers. CAS and hook
readbacks are neither a multi-root fence nor ABA protection.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Callable, Literal, Protocol

from .reader_stream_checkpoint_cas import ConditionalCheckpointStore, Snapshot, _index, strong_etag
from .shared import READER_ASSETS_BUCKET as BUCKET

CANONICAL = 'pages/image/index-v2.json'
IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'bmp', 'webp', 'gif', 'tif', 'tiff'}
OUTPUT_FIELDS = {'root', 'path', 'bytes', 'sha256', 'processing_roots', 'checkpointed_at'}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _encode(value) -> bytes:
    # Wire encoding of ConditionalCheckpointStore.write_once.
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf8')


def row_digest(row: dict) -> str:
    return _sha(_encode(row))


def _hex(value, digits=64):
    if not isinstance(value, str) or not re.fullmatch(rf'[0-9a-f]{{{digits}}}', value):
        raise ValueError('invalid image history digest or operation ID')


@dataclass(frozen=True)
class Source:
    repo: str
    path: str
    revision: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Asset:
    root: str
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Preflight:
    source: Source
    old: Asset
    new: Asset
    rollback_sha256: str  # Digest of the exact retained rollback protection evidence.
    pins_sha256: str      # Digest of the exact other pinned inputs, including projection inputs.


@dataclass(frozen=True)
class Intent:
    operation: str
    key: str
    preflight: Preflight
    old_row_sha256: str
    new_row_sha256: str
    parent_raw_sha256: str
    parent_etag: str
    parent: bytes         # Deterministic encoding of the parsed parent for re-projection.
    candidate: bytes      # Exact bytes to be written by the checkpoint adapter.
    before_projection_sha256: str
    after_projection_sha256: str

    @property
    def claim_key(self) -> str:
        # A different operation ID cannot reopen the same exact parent for this key.
        return _sha(_encode([BUCKET, CANONICAL, self.key, self.parent_raw_sha256]))

    @property
    def sha256(self) -> str:
        fields = asdict(self)
        for name in ('parent', 'candidate'):
            fields[name] = _sha(fields[name])
        return _sha(_encode(['reader-static-image-history-v1', BUCKET, CANONICAL, fields]))


@dataclass(frozen=True)
class Checks:
    """Fresh exact input identities and complete before/after shadow projections."""

    preflight: Preflight
    before_projection: dict
    after_projection: dict


# Read-only, fail-closed adapter. It must freshly validate the source and both
# static assets (format, digest and length), verify the old root remains rollback-
# protected and the new one immutable/protected, and derive both complete
# projections from the exact index views and fresh pinned inputs. Even after CAS,
# the before projection is a shadow of Intent.parent, not evidence of a live route.
Revalidate = Callable[[Intent, Literal['before', 'after']], Checks]


@dataclass(frozen=True)
class Record:
    intent_sha256: str
    state: Literal['claimed', 'observed', 'finalized']
    target_etag: str | None = None


class ClaimStore(Protocol):
    """Durable, linearizable journal. Never delete or reset claims.

    claim returns True only to the creator (atomic create-if-absent), and only
    once the sealed Intent is also durably retrievable for recovery. An
    ambiguous claim must raise, not confer write permission. advance uses
    exact compare-and-set and preserves the original sealed Intent.
    """

    def read(self, key: str) -> Record | None: ...
    def claim(self, key: str, intent: Intent, record: Record) -> bool: ...
    def advance(self, key: str, expected: Record, record: Record) -> bool: ...


def _selected(index, key):
    if type(index['version']) is not int or index['kind'] != 'image-stream-index':
        raise ValueError('expected the canonical v2 image index')
    for i, row in enumerate(index['files']):
        if row['key'] == key:
            return i, row
    raise ValueError('exact key is not already mapped')


def _static(row, key, source, asset, suffix):
    repo, sep, path = key.partition('\0')
    extension = row.get('extension')
    if (not sep or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*', repo)
            or any(p in {'', '.', '..'} for p in path.split('/'))
            or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or not isinstance(extension, str) or extension not in IMAGE_EXTENSIONS
            or not path.lower().endswith('.' + extension)):
        raise ValueError('invalid exact static-image key')
    _hex(source.revision, 40)
    _hex(source.sha256)
    _hex(asset.sha256)
    if (type(source.size) is not int or source.size < 1 or type(asset.size) is not int or asset.size < 1
            or not isinstance(asset.root, str)
            or not re.fullmatch(rf'pages/image/{extension}/[0-9a-f]{{64}}', asset.root)
            or asset.path != f'{asset.root}/document.{suffix}'):
        raise ValueError('invalid exact static-image asset or source identity')
    expected = {'key': key, 'repo': repo, 'source_path': path, 'source_revision': source.revision,
                'source_sha256': source.sha256, 'source_bytes': source.size, 'mode': 'image',
                'page_count': 1, 'bucket': BUCKET, 'root': asset.root, 'path': asset.path,
                'sha256': asset.sha256, 'bytes': asset.size}
    if (source.repo != repo or source.path != path
            or _encode({k: row.get(k) for k in expected}) != _encode(expected)
            or any(field in row for field in ('manifest', 'object', 'fallback'))):
        raise ValueError('row does not match the exact static-image source and asset')


def _candidate(parent, key, old_row_sha256, new, preflight, operation):
    _hex(operation, 32)
    _hex(preflight.rollback_sha256)
    _hex(preflight.pins_sha256)
    position, old = _selected(parent, key)
    if row_digest(old) != old_row_sha256:
        raise ValueError('old mapped row digest differs')
    _static(old, key, preflight.source, preflight.old, 'webp')
    _static(new, key, preflight.source, preflight.new, 'avif')
    if (preflight.old.root == preflight.new.root or preflight.old.sha256 == preflight.new.sha256
            or parent.get('checkpoint_operation') == operation):
        raise ValueError('replacement needs a distinct asset root, content and operation')
    if _encode({k: v for k, v in old.items() if k not in OUTPUT_FIELDS}) != _encode(
            {k: v for k, v in new.items() if k not in OUTPUT_FIELDS}):
        raise ValueError('replacement changes non-output row fields')
    files = list(parent['files'])
    files[position] = new
    return _encode({**parent, 'files': files, 'checkpoint_operation': operation})


def _projection_delta(key, preflight, before, after):
    def route(asset):
        return {'s': 2, 'm': 'i', 'b': BUCKET, 'p': asset.path}

    if (not isinstance(before, dict) or type(before.get('v')) is not int or before['v'] != 1
            or not isinstance(before.get('f'), dict)
            or _encode(before['f'].get(key)) != _encode(route(preflight.old))):
        raise ValueError('old projected route differs')
    expected = _encode({**before, 'f': {**before['f'], key: route(preflight.new)}})
    if _encode(after) != expected:
        raise ValueError('projected route delta is not exactly the selected path replacement')
    return _sha(_encode(before)), _sha(expected)


def seal(operation: str, *, key: str, parent: Snapshot, old_row_sha256: str,
         new_row: dict, checks: Checks) -> Intent:
    """Seal a reviewed row replacement, without I/O. Apply never trusts cached checks."""
    strong_etag(parent.etag)
    _hex(parent.raw_sha256)
    parent_bytes = _encode(parent.value)
    candidate = _candidate(_index(parent_bytes), key, old_row_sha256, new_row, checks.preflight, operation)
    _index(candidate)
    before, after = _projection_delta(key, checks.preflight, checks.before_projection, checks.after_projection)
    return Intent(operation, key, checks.preflight, old_row_sha256, row_digest(new_row),
                  parent.raw_sha256, parent.etag, parent_bytes, candidate, before, after)


def _validate(intent):
    strong_etag(intent.parent_etag)
    for digest in (intent.parent_raw_sha256, intent.before_projection_sha256, intent.after_projection_sha256):
        _hex(digest)
    candidate = _index(intent.candidate)
    _, new = _selected(candidate, intent.key)
    if (row_digest(new) != intent.new_row_sha256 or intent.candidate != _candidate(
            _index(intent.parent), intent.key, intent.old_row_sha256, new, intent.preflight, intent.operation)):
        raise ValueError('sealed canonical delta or candidate bytes differ')
    return candidate


def _fresh(intent, revalidate, phase):
    checks = revalidate(intent, phase)
    if (not isinstance(checks, Checks) or not isinstance(checks.preflight, Preflight)
            or _encode(asdict(checks.preflight)) != _encode(asdict(intent.preflight))):
        raise ValueError('fresh source, rollback, asset or pin preflight differs')
    if _projection_delta(intent.key, intent.preflight, checks.before_projection, checks.after_projection) != (
            intent.before_projection_sha256, intent.after_projection_sha256):
        raise ValueError('whole fresh projected route delta differs from seal')


def _check_snapshot(checkpoints, raw, digest, etag, error):
    snapshot = checkpoints.read(BUCKET, CANONICAL)
    if (snapshot.raw_sha256 != digest or _encode(snapshot.value) != raw
            or (etag is not None and snapshot.etag != etag)):
        raise ValueError(error)
    return strong_etag(snapshot.etag)


def _guard(checkpoints, intent, revalidate, phase, etag=None):
    if phase == 'before':
        raw, digest, etag = intent.parent, intent.parent_raw_sha256, intent.parent_etag
        error = 'exact canonical parent bytes or ETag differ'
    else:
        raw, digest = intent.candidate, _sha(intent.candidate)
        error = 'exact canonical target bytes or ETag differ; do not replay'
    observed = _check_snapshot(checkpoints, raw, digest, etag, error)
    _fresh(intent, revalidate, phase)
    _check_snapshot(checkpoints, raw, digest, observed, error)
    return observed


def _advance(journal, intent, previous, record):
    if (journal.advance(intent.claim_key, previous, record) is not True
            or journal.read(intent.claim_key) != record):
        raise RuntimeError('image history journal changed; inspect before recovery')
    return record


def apply_once(journal: ClaimStore, intent: Intent, *, checkpoints: ConditionalCheckpointStore,
               revalidate: Revalidate) -> Record:
    """Only a new durable claim authorizes one CAS. Any failure spends the claim."""
    candidate = _validate(intent)
    if journal.read(intent.claim_key) is not None:
        raise ValueError('image history write already claimed; reconcile instead')
    _guard(checkpoints, intent, revalidate, 'before')
    claimed = Record(intent.sha256, 'claimed')
    if journal.claim(intent.claim_key, intent, claimed) is not True:
        raise ValueError('image history write already claimed; reconcile instead')
    _guard(checkpoints, intent, revalidate, 'before')
    if journal.read(intent.claim_key) != claimed:
        raise RuntimeError('image history claim readback differs')
    checkpoints.write_once(BUCKET, CANONICAL, candidate, intent.parent_etag, operation_id=intent.operation)
    return reconcile(journal, intent, checkpoints=checkpoints, revalidate=revalidate)


def reconcile(journal: ClaimStore, intent: Intent, *, checkpoints: ConditionalCheckpointStore,
              revalidate: Revalidate) -> Record:
    """Recover a spent claim by observing the exact target, never by retrying CAS."""
    _validate(intent)
    claimed = Record(intent.sha256, 'claimed')
    if journal.read(intent.claim_key) != claimed:
        raise ValueError('no matching unresolved image history claim')
    etag = _guard(checkpoints, intent, revalidate, 'after')
    return _advance(journal, intent, claimed, Record(intent.sha256, 'observed', etag))


def finalize(journal: ClaimStore, intent: Intent, *, checkpoints: ConditionalCheckpointStore,
             revalidate: Revalidate) -> Record:
    """Verified journal-only finalization (or recovery of a lost finalizer response)."""
    _validate(intent)
    current = journal.read(intent.claim_key)
    if (current is None or current.intent_sha256 != intent.sha256
            or current.state not in ('observed', 'finalized')):
        raise ValueError('no matching observed image history target to finalize')
    etag = _guard(checkpoints, intent, revalidate, 'after', strong_etag(current.target_etag))
    if current.state == 'finalized':
        if journal.read(intent.claim_key) != current:
            raise RuntimeError('image history journal changed during finalization readback')
        return current
    return _advance(journal, intent, current, Record(intent.sha256, 'finalized', etag))
