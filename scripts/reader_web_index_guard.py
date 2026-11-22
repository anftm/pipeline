"""Single-use CAS for one exact web canonical repair; no staging or CLI.

The caller verifies immutable staging and supplies a read-only guard that
freshly checks the window and all pinned prerequisites. Keep independent
writers, GC, and out-of-band restores stopped throughout. This is not an ABA
fence; it relies on fresh GETs, working conditionals, and a retained journal.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import re

from . import reader_stream_checkpoint_cas as cas
from . import reader_web_exact_plan as web
from .reader_bucket_store import S3NotFound
from .reader_image_history_store import LOCATION, S3ClaimStore

PREFIX = 'reader-index/gc/web-exact-index/v1/'
MAX_PERMIT = 8192
ROOT = re.compile(r'documents/web/(html|htm|mht|mhtml)/index\.json')


def _extension(path):
    match = ROOT.fullmatch(path) if isinstance(path, str) else None
    if match is None:
        raise ValueError('outside the exact web canonical index allowlist')
    return match[1]


def _claim_path(parent_sha256):
    if not web._is_sha(parent_sha256):
        raise ValueError('exact raw parent SHA-256 is required')
    return f'{PREFIX}{parent_sha256}.json'


def _encode(value):
    # Same serializer as ConditionalCheckpointStore.write_once.
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _permit(path, parent_sha256, target_sha256, prerequisites):
    _claim_path(parent_sha256)
    _extension(path)
    if not web._is_sha(target_sha256) or target_sha256 == parent_sha256:
        raise ValueError('exact target SHA-256 must differ from the parent')
    if not isinstance(prerequisites, Mapping) or not 0 < len(prerequisites) <= 32:
        raise ValueError('required prerequisite SHA-256 pins must be supplied by the caller')
    pins = dict(prerequisites)
    if any(not isinstance(k, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', k)
           or not web._is_sha(v) for k, v in pins.items()):
        raise ValueError('invalid named prerequisite SHA-256 pin')
    return _encode({'version': 1, 'kind': 'reader-web-exact-index-permit', 'state': 'spent',
                    'bucket': web.BUCKET, 'path': path, 'parent_sha256': parent_sha256,
                    'target_sha256': target_sha256, 'prerequisites': pins})


def _check_window(guard, phase):
    if guard(phase) is not True:
        raise RuntimeError(f'exact web index window not verified: {phase}')


class _ScopedIO(S3ClaimStore):
    """Bind the reused single-attempt transport to one path and guarded GETs."""

    def __init__(self, client, path, maximum, guard, read_phase):
        if client is None:
            raise ValueError('an explicit single-attempt Reader assets S3 client is required')
        super().__init__(client)
        self.path, self.maximum, self.guard, self.read_phase = path, maximum, guard, read_phase

    def _get(self, path, *, maximum):
        if path != self.path or maximum != self.maximum:
            raise ValueError('exact web index read outside bound object')
        _check_window(self.guard, self.read_phase)
        return super()._get(path, maximum=maximum)

    def read_raw(self):
        return self._get(self.path, maximum=self.maximum)

    def _put(self, path, raw, *, maximum, IfNoneMatch):
        if (path != self.path or not path.startswith(PREFIX)
                or maximum != self.maximum or maximum != MAX_PERMIT or IfNoneMatch != '*'):
            raise ValueError('exact web permit create outside journal namespace')
        _check_window(self.guard, 'claim')
        return super()._put(path, raw, maximum=maximum, IfNoneMatch='*')


class S3Journal:
    """An immutable spent permit per raw parent SHA, using an explicit S3 client.

    The client must be dedicated to the Reader assets endpoint with one attempt.
    No overwrite, delete, or retry is provided. Unknown claim outcomes do not
    authorize a checkpoint write, even if the permit later appears.
    """

    def __init__(self, client):
        self._client = client

    def read(self, parent_sha256, *, guard):
        io = _ScopedIO(self._client, _claim_path(parent_sha256), MAX_PERMIT, guard, 'permit-read')
        return io.read_raw()[0]

    def claim(self, parent_sha256, seal, *, guard):
        path = _claim_path(parent_sha256)
        if not isinstance(seal, bytes) or not 0 < len(seal) <= MAX_PERMIT:
            raise ValueError('invalid exact web index permit size')
        record = web._json(seal, 'spent permit')
        if not isinstance(record, dict) or seal != _permit(
                record.get('path'), parent_sha256, record.get('target_sha256'), record.get('prerequisites')):
            raise ValueError('invalid exact web index spent permit')
        io = _ScopedIO(self._client, path, MAX_PERMIT, guard, 'claim-readback')
        return io._put(path, seal, maximum=MAX_PERMIT, IfNoneMatch='*')


class _CheckpointBucket:
    """Bind the existing CAS helper to one root and guard its internal GETs."""

    def __init__(self, store, path, guard):
        bucket = store.store
        if tuple(bucket._location(web.BUCKET)) != LOCATION:
            raise ValueError('unexpected exact web index bucket mapping')
        self.io = _ScopedIO(bucket._client(LOCATION[0]), path, web.MAX_CANONICAL, guard, 'canonical-read')
        self.read_etags = {}

    def _location(self, bucket):
        if bucket != web.BUCKET:
            raise ValueError('unexpected exact web index bucket')
        return LOCATION

    def _client(self, namespace):
        if namespace != LOCATION[0]:
            raise ValueError('unexpected exact web index namespace')
        return self.io._client

    def read_bytes(self, bucket, path):
        self._location(bucket)
        raw, etag = self.io._get(path, maximum=web.MAX_CANONICAL)
        if raw is None:
            raise S3NotFound(path)
        self.read_etags[bucket, path] = etag
        return raw


def _index(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= web.MAX_CANONICAL:
        raise ValueError('invalid exact web canonical index size')
    value = web._json(raw, 'web canonical index')
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1 or value.get('kind') != web.KIND
            or value.keys() - {'version', 'kind', 'files', 'failures', 'checkpoint_operation'}):
        raise ValueError('invalid exact web canonical index schema')
    web._rows(value, 'failures')
    return value, web._rows(value, 'files')


def _check_change(parent_raw, target, after, ext, expected_keys=None):
    parent, before = _index(parent_raw)
    if before.keys() != after.keys() or not web._json_equal(parent['failures'], target['failures']):
        raise ValueError('exact web change must preserve all file keys and failures')
    changed = [key for key in before if not web._json_equal(before[key], after[key])]
    if expected_keys is None:
        if len(changed) != 1:
            raise ValueError('exact web change must replace exactly one file row')
    elif (not isinstance(expected_keys, frozenset) or not 1 <= len(expected_keys) <= 7
          or set(changed) != expected_keys):
        raise ValueError('exact web change does not match sealed keys')
    for key in changed:
        old, new = before[key], after[key]
        candidate = {k: v for k, v in new.items() if k != 'checkpointed_at'}
        old_root, new_root = web._web_row(old, old=True), web._web_row(candidate, old=False)
        if (old['key'] != candidate['key'] or old['extension'] != ext or new['extension'] != ext or old_root == new_root
                or any(old[k] != new[k] for k in ('source_sha256', 'source_revision'))
                or old['sha256'] == new['sha256']
                or ('checkpointed_at' in old) != ('checkpointed_at' in new)
                or old.get('checkpointed_at') != new.get('checkpointed_at')):
            raise ValueError('exact web change must replace the derivative, not the source or watermark')


def apply(store, journal, *, path, parent_sha256, target_raw, prerequisites, guard, resume=False,
          expected_keys=None, parent_etag=None):
    """Use a ConditionalCheckpointStore and S3Journal (or equivalent journal).

    Supply exact CAS-encoded target bytes with a unique checkpoint_operation and
    named SHA-256 pins for every caller-required prerequisite. guard(phase) must
    freshly verify those prerequisites as well as the window and return True.
    Returns the CAS result ('written' or 'written-response-unknown'), or
    'observed' for a read-only resume that finds the exact sealed target.
    """
    if type(resume) is not bool:
        raise ValueError('resume must be explicitly boolean')
    ext = _extension(path)
    target, after = _index(target_raw)
    operation = target.get('checkpoint_operation')
    if (not isinstance(operation, str) or not re.fullmatch(r'[0-9a-f]{32}', operation)
            or _encode(target) != target_raw):
        raise ValueError('target must use exact CAS encoding and a unique checkpoint operation')
    seal = _permit(path, parent_sha256, sha256(target_raw).hexdigest(), prerequisites)
    persisted = journal.read(parent_sha256, guard=guard)
    if resume:
        if persisted != seal:
            raise RuntimeError('no matching spent exact web index permit')
    elif persisted is not None:
        raise RuntimeError('exact web index parent permit already spent; only observation is allowed')

    bucket = _CheckpointBucket(store, path, guard)
    checkpoints = cas.ConditionalCheckpointStore(bucket)
    if resume:
        bucket.io.read_phase = 'resume-readback'
        if checkpoints.read(web.BUCKET, path).raw != target_raw:
            raise RuntimeError('spent exact web index permit but target bytes not observed; do not retry')
        return 'observed'

    parent = checkpoints.read(web.BUCKET, path)
    if parent.raw_sha256 != parent_sha256:
        raise RuntimeError('exact web canonical parent SHA-256 differs; no claim made')
    if parent_etag is not None and parent.etag != parent_etag:
        raise RuntimeError('exact web canonical parent ETag differs; no claim made')
    if parent.value.get('checkpoint_operation') == operation:
        raise ValueError('checkpoint operation must differ from the exact parent')
    _check_change(parent.raw, target, after, ext, expected_keys)
    if journal.claim(parent_sha256, seal, guard=guard) is not True:
        raise RuntimeError('exact web index parent permit not acquired; do not write')
    observed = checkpoints.read(web.BUCKET, path)
    if (observed.raw_sha256, observed.etag) != (parent_sha256, parent.etag):
        raise RuntimeError('exact web parent drifted after the permit was spent; do not retry')
    _check_window(guard, 'put')
    bucket.io.read_phase = 'readback'
    return checkpoints.write_once(web.BUCKET, path, target, parent.etag, operation_id=operation)
