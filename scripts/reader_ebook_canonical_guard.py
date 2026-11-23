"""Exact same-source EPUB canonical replacement; no transport or CLI.

Callers own review, immutable bundle/readback verification, and isolation of
other writers, GC, and restores. This is one index CAS, not an ABA fence or a
transaction with sidecars, pointers, processing roots, or Search.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from math import isfinite
import re

from .reader_chapter_profiles import EPUB_PROFILE, ebook_profile_upgrade
from .reader_page_cas import strong_etag
from .reader_route_plan import source_key
from .reader_stream_checkpoint_cas import MAX_INDEX
from .shared import READER_ASSETS_BUCKET as BUCKET

ROOT = 'chapters/ebook/epub'
INDEX = ROOT + '/index.json'
KIND = 'ebook-chapter-stream-index'
PERMIT_PREFIX = 'reader-index/gc/ebook-rebuild-canonical/v1/'
MANIFEST = 'chapter-manifest.json'
SEARCH = 'epub-search-index.json.gz'


@dataclass(frozen=True)
class Replacement:
    """One reviewed index key and immutable routes; manifest hashes are evidence pins."""

    key: str
    source_sha256: str
    old_root: str
    old_manifest: str
    old_search_index: str | None  # None if the old row has no search path.
    old_manifest_sha256: str
    new_root: str
    new_manifest: str
    new_search_index: str
    new_manifest_sha256: str
    chapter_count: int


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch(rf'[0-9a-f]{{{size}}}', value) is not None


def _encode(value):
    # Same encoding as ConditionalCheckpointStore.write_once.
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _unique(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError('duplicate JSON field')
    return value


def _exact_float(token):
    value = float(token)
    # Reject numbers the checkpoint serializer would round in untouched rows.
    if not isfinite(value) or Decimal(token) != Decimal(repr(value)):
        raise ValueError('non-finite or inexact JSON number')
    return value


def _rows(value, field):
    if not isinstance(value, list):
        raise ValueError(f'invalid EPUB canonical {field} list')
    rows = {}
    for row in value:
        key = row.get('key') if isinstance(row, dict) else None
        if not isinstance(key, str) or not key or key in rows:
            raise ValueError(f'invalid or duplicate EPUB canonical {field} key')
        rows[key] = row
    return rows


def _replacement(row, spec):
    repo, path = row.get('repo'), row.get('path')
    formats = [row[field] for field in ('extension', 'source_extension') if field in row]
    if (not isinstance(repo, str) or not isinstance(path, str) or not path.lower().endswith('.epub')
            or source_key(repo, path) != spec.key or not formats or any(ext != 'epub' for ext in formats)
            or row.get('bucket') != BUCKET or row.get('mode') != 'chapter-stream'
            or ebook_profile_upgrade('epub', row.get('profile')) != EPUB_PROFILE
            or row.get('source_sha256') != spec.source_sha256):
        raise ValueError('reviewed row is not a same-source legacy EPUB chapter stream')
    if not all(_hex(digest, 64) for digest in (spec.source_sha256, spec.old_manifest_sha256, spec.new_manifest_sha256)):
        raise ValueError('invalid immutable EPUB SHA-256 pin')
    prefix = f'{ROOT}/{spec.source_sha256}/'
    for root, manifest, search in ((spec.old_root, spec.old_manifest, spec.old_search_index),
                                   (spec.new_root, spec.new_manifest, spec.new_search_index)):
        if (not isinstance(root, str) or not root.startswith(prefix) or not _hex(root[len(prefix):], 16)
                or manifest != f'{root}/{MANIFEST}' or (search is not None and search != f'{root}/{SEARCH}')):
            raise ValueError('reviewed paths are outside the exact immutable EPUB roots')
    if (row.get('root'), row.get('manifest'), row.get('search_index')) != (
            spec.old_root, spec.old_manifest, spec.old_search_index):
        raise ValueError('canonical EPUB paths differ from the reviewed parent')
    if (spec.old_root == spec.new_root or spec.old_manifest_sha256 == spec.new_manifest_sha256
            or spec.new_search_index is None or type(spec.chapter_count) is not int
            or not 1 <= spec.chapter_count <= 10000):
        raise ValueError('invalid EPUB successor root, manifest hash, search path, or chapter count')
    return {**row, 'profile': EPUB_PROFILE, 'root': spec.new_root, 'manifest': spec.new_manifest,
            'search_index': spec.new_search_index, 'chapter_count': spec.chapter_count}


def build_successor(parent_raw, *, reviewed, operation_id):
    """Return exact CAS-encoded bytes for only the explicitly reviewed replacements."""
    if not isinstance(parent_raw, bytes) or not 0 < len(parent_raw) <= MAX_INDEX:
        raise ValueError('invalid EPUB canonical parent size')
    if not _hex(operation_id, 32):
        raise ValueError('invalid EPUB checkpoint operation ID')
    try:
        parent = json.loads(parent_raw.decode('utf-8'), object_pairs_hook=_unique,
                            parse_constant=_exact_float, parse_float=_exact_float)
    except (ValueError, RecursionError, InvalidOperation) as error:
        raise ValueError('invalid EPUB canonical parent JSON') from error
    if (not isinstance(parent, dict) or type(parent.get('version')) is not int
            or parent['version'] != 1 or parent.get('kind') != KIND):
        raise ValueError('invalid EPUB canonical parent schema')
    if parent.get('checkpoint_operation') == operation_id:
        raise ValueError('checkpoint operation must differ from the parent')
    files = _rows(parent.get('files'), 'files')
    failures = _rows(parent.get('failures', []), 'failures')
    updates = {}
    for spec in reviewed:
        if (type(spec) is not Replacement or not isinstance(spec.key, str)
                or spec.key not in files or spec.key in failures or spec.key in updates):
            raise ValueError('missing, failed, duplicate, or invalid reviewed EPUB key')
        updates[spec.key] = _replacement(files[spec.key], spec)
    if not updates:
        raise ValueError('explicit reviewed EPUB replacements are required')
    target = _encode({**parent, 'files': [updates.get(key, row) for key, row in files.items()],
                      'checkpoint_operation': operation_id})
    if len(target) > MAX_INDEX:
        raise ValueError('EPUB canonical successor exceeds size limit')
    return target


def validate_successor(parent_raw, target_raw, *, reviewed, operation_id):
    """Reject any target other than the exact narrowly constructed successor."""
    expected = build_successor(parent_raw, reviewed=reviewed, operation_id=operation_id)
    if not isinstance(target_raw, bytes) or target_raw != expected:
        raise ValueError('target differs from the exact reviewed EPUB successor')


def apply(checkpoints, journal, *, parent_raw, parent_etag, target_raw, reviewed, operation_id,
          prerequisites, guard, resume=False):
    """Spend one permit and attempt one CAS, or explicitly observe an exact spent permit.

    Inject checkpoints.read(bucket, path) -> (raw, strong_etag), and
    checkpoints.compare_and_swap(bucket, path, raw, if_match=...) -> True only
    for a confirmed single conditional write. Inject journal.read(bucket, path)
    -> bytes or None (only if absent), and journal.claim(bucket, path, seal) ->
    True only for this invocation's confirmed atomic create-if-absent. Adapters
    must not retry mutations; retain permits, never overwrite or delete them.

    prerequisites pins at least the 'review' and 'stage' evidence SHA-256s.
    guard(phase) must freshly verify the caller's pinned review, staged member
    bytes (including both manifests), and isolation, and return literal True.
    If a claim or CAS is uncertain, no write is replayed. resume=True checks
    the same seal and exact live target without claiming or writing anything.
    """
    if type(resume) is not bool or not callable(guard):
        raise ValueError('explicit boolean resume and a fresh guard are required')
    strong_etag(parent_etag)
    reviewed = tuple(reviewed)
    validate_successor(parent_raw, target_raw, reviewed=reviewed, operation_id=operation_id)
    pins = dict(prerequisites) if isinstance(prerequisites, Mapping) else {}
    if (not {'review', 'stage'} <= pins.keys() or len(pins) > 32
            or any(not isinstance(k, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', k)
                   or not _hex(v, 64) for k, v in pins.items())):
        raise ValueError('exact review, stage, and prerequisite SHA-256 pins are required')
    parent_sha = sha256(parent_raw).hexdigest()
    permit_path = f'{PERMIT_PREFIX}{parent_sha}.json'
    seal = _encode({'version': 1, 'kind': 'reader-ebook-canonical-permit', 'state': 'spent',
                    'bucket': BUCKET, 'index': INDEX, 'parent_sha256': parent_sha, 'parent_etag': parent_etag,
                    'target_sha256': sha256(target_raw).hexdigest(), 'operation': operation_id,
                    'replacements_sha256': sha256(_encode([asdict(spec) for spec in sorted(
                        reviewed, key=lambda spec: spec.key)])).hexdigest(), 'prerequisites': pins})

    def check(phase):
        if guard(phase) is not True:
            raise RuntimeError(f'EPUB canonical window not verified: {phase}')

    def read(phase):
        check(phase)
        raw, etag = checkpoints.read(BUCKET, INDEX)
        return raw, strong_etag(etag)

    check('permit-read')
    persisted = journal.read(BUCKET, permit_path)
    if resume:
        if persisted != seal:
            raise RuntimeError('no matching spent EPUB canonical permit')
        if read('resume-readback')[0] != target_raw:
            raise RuntimeError('spent EPUB canonical permit but exact target not observed; do not retry')
        return 'observed'
    if persisted is not None:
        raise RuntimeError('EPUB canonical parent permit already spent; only observation is allowed')
    if read('canonical-read') != (parent_raw, parent_etag):
        raise RuntimeError('EPUB canonical parent bytes or ETag drifted; no claim made')
    check('claim')
    if journal.claim(BUCKET, permit_path, seal) is not True:
        raise RuntimeError('EPUB canonical permit not acquired; do not write')
    check('claim-readback')
    if journal.read(BUCKET, permit_path) != seal:
        raise RuntimeError('EPUB canonical permit readback differs; do not write')
    if read('canonical-recheck') != (parent_raw, parent_etag):
        raise RuntimeError('EPUB canonical parent drifted after permit was spent; do not retry')
    check('put')
    if checkpoints.compare_and_swap(BUCKET, INDEX, target_raw, if_match=parent_etag) is not True:
        raise RuntimeError('EPUB canonical CAS unconfirmed; observe only')
    if read('readback')[0] != target_raw:
        raise RuntimeError('EPUB canonical readback differs; observe only')
    return 'written'
