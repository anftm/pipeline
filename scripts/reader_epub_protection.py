"""Exact conditional processing protection for future EPUB staged objects.

This module can create a uniquely named protection or read it back; it cannot
write content, checkpoint, sidecar or delete/release a record. A lost response
is reconciled against exact bytes, with no blind retry. Objects under the root
must not be written until an independently verified protection is present.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

try:
    from . import reader_epub_immutable, search_snapshot
    from .reader_bucket_store import S3BucketStore
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    import reader_epub_immutable
    import search_snapshot
    from reader_bucket_store import S3BucketStore
    from shared import READER_ASSETS_BUCKET


IDENTITY = re.compile(r'[0-9a-f]{32}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
PROTECTION = re.compile(r'reader-index/processing/epub-exact-([0-9a-f]{32})-([0-9]{2})\.json\Z')
MAX_RECORD = 16 * 1024


def _time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        raise ValueError('EPUB protection requires an exact UTC time')
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError('invalid EPUB protection time') from error
    if result.utcoffset() != timedelta(0):
        raise ValueError('EPUB protection time must be UTC')
    return value


def make_record(operation: str, plan_sha256: str, root: str, created_at: str) -> dict:
    if not isinstance(operation, str) or not IDENTITY.fullmatch(operation):
        raise ValueError('invalid EPUB protection operation')
    if not isinstance(plan_sha256, str) or not SHA.fullmatch(plan_sha256):
        raise ValueError('invalid EPUB protection plan digest')
    reader_epub_immutable._root(root)
    _time(created_at)
    return {'version': 1, 'kind': 'reader-processing-roots', 'status': 'processing',
            'operation': 'epub-exact-staging', 'run': {'operation': operation},
            'resources': [{'bucket': READER_ASSETS_BUCKET, 'root': root}],
            'created_at': created_at, 'updated_at': created_at, 'heartbeat_at': created_at,
            'release_policy': 'explicit-publisher-handoff', 'plan_sha256': plan_sha256}


def validate(path: str, raw: bytes) -> dict:
    match = PROTECTION.fullmatch(path) if isinstance(path, str) else None
    if not match or int(match.group(2)) > 2:
        raise ValueError('EPUB protection key is outside the exact allowlist')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RECORD:
        raise ValueError('invalid EPUB protection bytes')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid EPUB protection JSON') from error
    if not isinstance(value, dict) or search_snapshot.canonical(value) != raw:
        raise ValueError('EPUB protection must use canonical JSON')
    resources = value.get('resources')
    if (not isinstance(resources, list) or len(resources) != 1 or not isinstance(resources[0], dict)
            or set(resources[0]) != {'bucket', 'root'} or resources[0]['bucket'] != READER_ASSETS_BUCKET):
        raise ValueError('EPUB protection resource is invalid')
    expected = make_record(match.group(1), value.get('plan_sha256'), resources[0]['root'], value.get('created_at'))
    if value != expected:
        raise ValueError('EPUB protection identity or status differs')
    return value


class S3EpubProtections:
    def __init__(self, store=None):
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            if store._namespace != 'vomebook':
                raise ValueError('EPUB protection requires the vomebook S3 namespace')
            store._config = store._config.merge(Config(retries={'total_max_attempts': 1},
                request_checksum_calculation='when_required', response_checksum_validation='when_required'))
        self.store = store

    def _location(self, path):
        match = PROTECTION.fullmatch(path) if isinstance(path, str) else None
        if not match or int(match.group(2)) > 2:
            raise ValueError('EPUB protection key is outside the exact allowlist')
        namespace, bucket = self.store._location(READER_ASSETS_BUCKET)
        if (namespace, bucket) != ('vomebook', 'reader-assets-v2'):
            raise ValueError('unexpected EPUB protection bucket mapping')
        return self.store._client(namespace), bucket

    def inspect(self, path):
        client, bucket = self._location(path)
        try:
            response = client.get_object(Bucket=bucket, Key=path)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                return None
            raise
        body, length = response.get('Body'), response.get('ContentLength')
        if body is None:
            raise ValueError('EPUB protection readback has no body')
        try:
            if type(length) is not int or not 0 < length <= MAX_RECORD:
                raise ValueError('EPUB protection readback size is invalid')
            raw = body.read(length + 1)
        finally:
            body.close()
        if not isinstance(raw, bytes) or len(raw) != length:
            raise ValueError('EPUB protection readback is truncated')
        validate(path, raw)
        return raw

    def create_once(self, path, raw):
        validate(path, raw)
        client, bucket = self._location(path)
        try:
            client.put_object(Bucket=bucket, Key=path, Body=raw, ContentType='application/json', IfNoneMatch='*')
        except Exception as error:
            after = self.inspect(path)
            if after == raw:
                return 'written-response-unknown'
            raise RuntimeError('EPUB protection write unconfirmed or conflicted; inspect before recovery') from error
        if self.inspect(path) != raw:
            raise RuntimeError('EPUB protection readback differs')
        return 'written'
