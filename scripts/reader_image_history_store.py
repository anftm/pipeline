"""Conditional S3 ClaimStore; no dispatcher or checkpoint/asset access.

Wire v2: one canonical UTF-8 JSON object (sorted keys, 2-space indent, final LF):
{version: 2, kind: "reader-image-history-claim", intent, record}. Intent and
record have exactly their dataclass fields. Preflight includes the historical
source, current_source and search_sha256. Parent and candidate are padded
standard base64 of the exact index bytes. Record.intent_sha256 binds the intent.

The path stays reader-index/gc/image-history-claims/v1/<claim_key>.json in
vomebook/reader-assets-v2 so a schema change cannot reopen a previous claim.
Wire v1 is rejected, not treated as absent. Advance preserves the intent bytes.

GC retains this control namespace without traversing it: this journal is not
rollback protection. The endpoint must honor conditional PUT and fresh GET;
no ETag here fences independent writers, deletion, or other objects.
"""
from __future__ import annotations

import base64
import json
from dataclasses import asdict

from . import reader_image_history as history
from .reader_bucket_store import S3BucketStore, s3_error_code
from .reader_stream_checkpoint_cas import MAX_INDEX, strong_etag

PREFIX = 'reader-index/gc/image-history-claims/v1/'
KIND = 'reader-image-history-claim'
LOCATION = ('vomebook', 'reader-assets-v2')
# Two MAX_INDEX documents in base64, plus the envelope and identity fields.
MAX_BYTES = 2 * (4 * ((MAX_INDEX + 2) // 3)) + 1024 * 1024


class WriteUnconfirmed(RuntimeError):
    """Inspect persisted evidence; never replay the unknown PUT."""


def _path(key):
    history._hex(key)
    return PREFIX + key + '.json'


def _record(record):
    if type(record) is not history.Record:
        raise ValueError('expected an image history record')
    history._hex(record.intent_sha256)
    if record.state in ('observed', 'finalized'):
        strong_etag(record.target_etag)
    elif record.state != 'claimed' or record.target_etag is not None:
        raise ValueError('invalid image history record state or target ETag')


def _bind(key, intent, record):
    _record(record)
    if type(intent) is not history.Intent:
        raise ValueError('expected a sealed image history intent')
    history._validate(intent)
    if intent.claim_key != key or intent.sha256 != record.intent_sha256:
        raise ValueError('image history claim key or intent digest differs')


def _encode(intent, record):
    fields = asdict(intent)
    for name in ('parent', 'candidate'):
        fields[name] = base64.b64encode(fields[name]).decode('ascii')
    return history._encode({'version': 2, 'kind': KIND, 'intent': fields, 'record': asdict(record)})


def _decode(key, raw):
    try:
        value = json.loads(raw)
        fields = value['intent']
        preflight = fields['preflight']
        intent = history.Intent(**{
            **fields, 'preflight': history.Preflight(**{
                **preflight, 'source': history.Source(**preflight['source']),
                'current_source': history.Source(**preflight['current_source']),
                'old': history.Asset(**preflight['old']), 'new': history.Asset(**preflight['new'])}),
            'parent': base64.b64decode(fields['parent'], validate=True),
            'candidate': base64.b64decode(fields['candidate'], validate=True)})
        record = history.Record(**value['record'])
        if _encode(intent, record) != raw:
            raise ValueError('noncanonical envelope')
        _bind(key, intent, record)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError('invalid image history claim envelope') from error
    return intent, record


class S3ClaimStore:
    """Use a dedicated single-attempt S3 client (or an in-memory test client)."""

    def __init__(self, client=None):
        if client is None:
            from botocore.config import Config
            store = S3BucketStore()
            if store._namespace != LOCATION[0] or store._location(history.BUCKET) != LOCATION:
                raise ValueError('unexpected image history bucket mapping')
            store._config = store._config.merge(Config(
                retries={'mode': 'standard', 'total_max_attempts': 1},
                request_checksum_calculation='when_required', response_checksum_validation='when_required'))
            client = store._client(LOCATION[0])
        if (client.meta.config.retries or {}).get('total_max_attempts') != 1:
            raise ValueError('image history requires a single-attempt S3 client')
        self._client = client

    def _get(self, path):
        try:
            response = self._client.get_object(Bucket=LOCATION[1], Key=path)
        except Exception as error:
            code, status = s3_error_code(error)
            if status in {None, 404} and code in {'404', 'NoSuchKey', 'NotFound'}:
                return None, None
            raise
        length, body = response.get('ContentLength'), response['Body']
        try:
            if type(length) is not int or not 0 < length <= MAX_BYTES:
                raise ValueError('invalid image history claim size')
            raw = body.read(length + 1)
        finally:
            body.close()
        if not isinstance(raw, bytes) or len(raw) != length:
            raise ValueError('image history claim length differs')
        return raw, strong_etag(response.get('ETag'))

    def _put(self, path, raw, **condition):
        if not 0 < len(raw) <= MAX_BYTES:
            raise ValueError('invalid image history claim size')
        message = 'image history journal PUT unconfirmed; inspect persisted evidence, do not retry'
        try:
            response = self._client.put_object(Bucket=LOCATION[1], Key=path, Body=raw,
                                               ContentType='application/json', **condition)
        except Exception as error:
            _, status = s3_error_code(error)
            if status in {409, 412} and error.response['ResponseMetadata'].get('RetryAttempts', 0) == 0:
                return False
            raise WriteUnconfirmed(message) from error
        try:
            meta = response['ResponseMetadata']
            if meta['HTTPStatusCode'] not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
                raise ValueError('ambiguous S3 PUT acknowledgement')
            etag = strong_etag(response.get('ETag'))
            if self._get(path) != (raw, etag):
                raise ValueError('image history journal readback bytes or ETag differ')
        except Exception as error:
            raise WriteUnconfirmed(message) from error
        return True

    def load(self, key: str) -> tuple[history.Intent, history.Record] | None:
        """Freshly load the sealed intent and state together for recovery/finalize."""
        raw, _ = self._get(_path(key))
        return None if raw is None else _decode(key, raw)

    def read(self, key: str) -> history.Record | None:
        loaded = self.load(key)
        return None if loaded is None else loaded[1]

    def claim(self, key: str, intent: history.Intent, record: history.Record) -> bool:
        path = _path(key)
        _bind(key, intent, record)
        if record.state != 'claimed':
            raise ValueError('initial image history record must be claimed')
        return self._put(path, _encode(intent, record), IfNoneMatch='*')

    def advance(self, key: str, expected: history.Record, record: history.Record) -> bool:
        path = _path(key)
        _record(expected)
        _record(record)
        if (record.intent_sha256 != expected.intent_sha256
                or (expected.state, record.state) not in {('claimed', 'observed'), ('observed', 'finalized')}
                or (expected.state == 'observed' and record.target_etag != expected.target_etag)):
            raise ValueError('invalid image history claim transition')
        raw, etag = self._get(path)
        if raw is None:
            return False
        intent, current = _decode(key, raw)
        if current != expected:
            return False
        return self._put(path, _encode(intent, record), IfMatch=etag)
