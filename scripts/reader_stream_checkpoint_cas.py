"""Single-object CAS for narrowly scoped Reader stream indexes.

This adapter never deletes an object, changes a Reader catalog, or coordinates
other roots. Unknown PUT outcomes are read once and never blindly replayed.
An S3 ETag is not a bucket lock, an ABA fence, or proof all writers cooperate.
"""
from __future__ import annotations

import json
import re
import hashlib
from dataclasses import dataclass

try:
    from .reader_bucket_store import S3BucketStore, S3NotFound
    from .reader_page_cas import strong_etag
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    from reader_bucket_store import S3BucketStore, S3NotFound
    from reader_page_cas import strong_etag
    from shared import READER_ASSETS_BUCKET


ROOT = re.compile(r'(?:'
    r'pages/image/index-v2(?:-0[0-7])?\.json|'
    r'chapters/ebook/(?:epub|mobi|azw3|fb2|chm)/index(?:-0[0-7])?\.json|'
    r'media/(?:audio|video|swf)/index(?:-0[0-7])?\.json|'
    r'documents/web/(?:html|htm|mht|mhtml)/index\.json|'
    r'documents/spreadsheet/(?:xls|xlsx|csv|ods)/index\.json|'
    r'documents/office/(?:doc|docx|odt|rtf)/index\.json|'
    r'documents/pdf/(?:ppt|pptx|pps|wps|ps)/index\.json)\Z')
MAX_INDEX = 64 * 1024 * 1024


def _check(bucket, path):
    if bucket != READER_ASSETS_BUCKET or not isinstance(path, str) or not ROOT.fullmatch(path):
        raise ValueError('Reader stream CAS root is outside the exact allowlist')


def _index(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_INDEX:
        raise ValueError('invalid Reader stream index bytes')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid Reader stream index JSON') from error
    if (not isinstance(value, dict) or value.get('version') != 1
            or not isinstance(value.get('kind'), str) or not value['kind'] or not isinstance(value.get('files'), list)
            or not isinstance(value.get('failures', []), list)):
        raise ValueError('invalid Reader stream index schema')
    for field in ('files', 'failures'):
        rows = value.get(field, [])
        if any(not isinstance(row, dict) or not isinstance(row.get('key'), str) or not row['key'] for row in rows):
            raise ValueError('invalid Reader stream index row')
        keys = [row['key'] for row in rows]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate Reader stream index key')
    return value


def merge_observed(canonical: dict, baseline: dict, entries: list[dict], failures: list[dict], *, kind: str,
                   operation_id: str, preserve_canonical_rows: bool = False) -> dict:
    """Merge only against the verified CAS parent; stale source rows fail closed.

    Sharded callers may derive their operational baseline from a canonical
    index plus the shard. Every row already in the CAS parent must survive in
    that baseline. A newer baseline row from a separately read canonical
    index remains a cross-object race; callers must coordinate other writers.
    """
    try:
        from .reader_stream_index import merge_indexes, validate_index
    except ImportError:
        from reader_stream_index import merge_indexes, validate_index
    validate_index(canonical, 'observed stream index')
    validate_index(baseline, 'stream merge baseline')
    live_fields, supplied_fields = {}, {}
    for field in ('files', 'failures'):
        live = {row['key']: row for row in canonical.get(field, [])}
        supplied = {row['key']: row for row in baseline.get(field, [])}
        if len(live) != len(canonical.get(field, [])) or len(supplied) != len(baseline.get(field, [])):
            raise ValueError('duplicate stream merge baseline')
        if set(live) - set(supplied):
            raise ValueError('stream merge baseline lost an observed row')
        if preserve_canonical_rows and any(supplied[key] != row for key, row in live.items()):
            raise ValueError('observed stream index changed before merge')
        live_fields[field], supplied_fields[field] = live, supplied
    updates = [row.get('key') for row in [*entries, *failures]]
    if any(not isinstance(key, str) or not key for key in updates) or len(updates) != len(set(updates)):
        raise ValueError('invalid or duplicate stream checkpoint update')
    for key in updates:
        if any(live_fields[field].get(key) != supplied_fields[field].get(key) for field in ('files', 'failures')):
            raise ValueError('stream source changed since the baseline')
    if not isinstance(operation_id, str) or not re.fullmatch(r'[0-9a-f]{32}', operation_id):
        raise ValueError('invalid stream checkpoint operation ID')
    payload = merge_indexes(baseline, entries, failures, kind)
    payload['checkpoint_operation'] = operation_id
    _index((json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf8'))
    return payload


@dataclass(frozen=True)
class Snapshot:
    value: dict | None
    etag: str | None
    raw_sha256: str | None


class ConditionalCheckpointStore:
    def __init__(self, store=None):
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            if store._namespace != 'vomebook':
                raise ValueError('Reader stream CAS requires the vomebook S3 namespace')
            store._config = store._config.merge(Config(
                retries={'total_max_attempts': 1}, request_checksum_calculation='when_required',
                response_checksum_validation='when_required'))
        self.store = store

    def read(self, bucket, path):
        _check(bucket, path)
        try:
            raw = self.store.read_bytes(bucket, path)
        except S3NotFound:
            return Snapshot(None, None, None)
        value = _index(raw)
        etag = strong_etag(self.store.read_etags.get((bucket, path)))
        return Snapshot(value, etag, hashlib.sha256(raw).hexdigest())

    def write_once(self, bucket, path, value, etag, *, operation_id):
        _check(bucket, path)
        if (not isinstance(operation_id, str) or not re.fullmatch(r'[0-9a-f]{32}', operation_id)
                or not isinstance(value, dict) or value.get('checkpoint_operation') != operation_id):
            raise ValueError('checkpoint requires its exact unique operation ID')
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf8')
        _index(raw)
        namespace, name = self.store._location(bucket)
        if (namespace, name) != ('vomebook', 'reader-assets-v2'):
            raise ValueError('unexpected Reader stream bucket mapping')
        condition = {'IfNoneMatch': '*'} if etag is None else {'IfMatch': strong_etag(etag)}
        try:
            self.store._client(namespace).put_object(Bucket=name, Key=path, Body=raw,
                                                      ContentType='application/json', **condition)
        except Exception as error:
            # The exact requested payload includes caller-owned operation
            # metadata. Equal bytes resolve an uncertain response; unchanged or
            # foreign bytes remain unknown/conflict and never cause a retry.
            observed = self.read(bucket, path)
            if observed.raw_sha256 == hashlib.sha256(raw).hexdigest():
                return 'written-response-unknown'
            raise RuntimeError('Reader stream checkpoint write unconfirmed or conflicted; inspect before recovery') from error
        observed = self.read(bucket, path)
        if observed.raw_sha256 != hashlib.sha256(raw).hexdigest():
            raise RuntimeError('Reader stream checkpoint readback differs')
        return 'written'
