"""Exact-key, retry-free S3 adapter for bounded search GC.

Uses existing HF_S3_ACCESS_KEY_ID/HF_S3_SECRET_ACCESS_KEY via S3BucketStore.
Only immutable gc/ metadata can be written; only manifests/content-addressed
objects can be deleted. No other bucket, URL, path or prefix is accepted.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime

try:
    from .reader_bucket_store import S3BucketStore, s3_error_code
    from . import search_gc as gc
except ImportError:
    from reader_bucket_store import S3BucketStore, s3_error_code
    import search_gc as gc

READ_RE = re.compile(
    r'(?:published/(?:current|transition)\.json|'
    r'published/transitions/[0-9a-f]{32}/(?:receipts/)?[0-9a-f]{64}\.json)\Z')


class S3SearchGCStore:
    def __init__(self, store=None):
        from botocore.config import Config
        self.store = store if store is not None else S3BucketStore()
        namespace, bucket = self.store._location(gc.BUCKET)
        if (namespace, bucket) != ('vomebook', 'search-data-v1'):
            raise ValueError('unexpected search GC bucket mapping')
        # Do not use S3BucketStore.read_bytes/delete: they implement retries.
        # Reject already-cached clients rather than trusting their retry policy.
        if self.store._clients:
            raise ValueError('GC requires a fresh S3BucketStore without cached clients')
        self.store._config = self.store._config.merge(Config(
            retries={'total_max_attempts': 1}, request_checksum_calculation='when_required',
            response_checksum_validation='when_required'))
        self.client = self.store._client(namespace)
        if self.client.meta.config.retries.get('total_max_attempts') != 1:
            raise ValueError('S3 GC client retries must be disabled')
        self.bucket = bucket

    @staticmethod
    def _check(key):
        if isinstance(key, str) and (READ_RE.fullmatch(key) or gc.GC_RE.fullmatch(key)):
            return
        gc.payload_key(key)

    @staticmethod
    def _meta(key, response, *, listing=False):
        modified = response.get('LastModified')
        if not isinstance(modified, datetime) or modified.tzinfo is None:
            raise ValueError('S3 metadata requires timezone-aware LastModified')
        # LIST exposes fractional seconds; HTTP HEAD/GET Last-Modified has
        # whole-second precision. Compare all three at that common precision.
        # GC age thresholds include one extra second to avoid early deletion.
        # Same-second recreation of identical bytes still requires the declared
        # writer coordination; different contents remain guarded by IfMatch.
        modified_seconds = math.floor(gc.timestamp(modified.timestamp()))
        return gc.metadata({'key': key, 'etag': response.get('ETag'),
                            'size': response.get('Size' if listing else 'ContentLength'),
                            'mtime': modified_seconds})

    @staticmethod
    def _mismatch(message, key, expected, observed):
        # Emit only normalized object identity, never raw S3 responses, headers,
        # client configuration or credentials. Preserve strict equality checks.
        def identity(item):
            return None if item is None else {field: item.get(field)
                                              for field in ('key', 'etag', 'size', 'mtime')}
        detail = {'key': key, 'expected': identity(expected), 'observed': identity(observed)}
        return ValueError(message + ': ' + json.dumps(detail, ensure_ascii=True, sort_keys=True))

    @staticmethod
    def _missing(error):
        code, status = s3_error_code(error)
        return status == 404 or str(code) in {'404', 'NoSuchKey', 'NotFound'}

    def inventory(self):
        # No delimiter, prefix filtering, max-items cap or sharded approximation:
        # exhaust the entire bucket, retaining metadata for every manifest.
        token, seen = None, set()
        while True:
            kwargs = {'Bucket': self.bucket, 'MaxKeys': 1000}
            if token is not None:
                kwargs['ContinuationToken'] = token
            response = self.client.list_objects_v2(**kwargs)
            if type(response.get('IsTruncated')) is not bool:
                raise ValueError('unknown S3 listing completeness')
            contents = response.get('Contents', [])
            if not isinstance(contents, list):
                raise ValueError('malformed S3 listing')
            for item in contents:
                yield self._meta(item.get('Key'), item, listing=True)
            if not response['IsTruncated']:
                return
            token = response.get('NextContinuationToken')
            if not isinstance(token, str) or not token or token in seen:
                raise ValueError('missing or repeated S3 pagination token')
            seen.add(token)

    def head(self, key):
        self._check(key)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            if self._missing(error):
                return None
            raise
        return self._meta(key, response)

    def read(self, key, expected=None):
        self._check(key)
        observed = self.head(key)
        if observed is None:
            if expected is not None:
                raise ValueError('listed key disappeared before read')
            return None
        if expected is not None and observed != expected:
            raise self._mismatch('S3 metadata changed before read', key, expected, observed)
        if observed['size'] > gc.MAX_JSON:
            raise ValueError('S3 JSON exceeds read cap')
        # Payload objects are inspected by HEAD only in GC, never downloaded.
        if gc.OBJECT_RE.fullmatch(key):
            raise ValueError('GC reads object metadata only')
        response = self.client.get_object(Bucket=self.bucket, Key=key, IfMatch=observed['etag'])
        body = response['Body']
        try:
            received = self._meta(key, response)
            if received != observed:
                raise self._mismatch('S3 GET metadata differs from HEAD', key, observed, received)
            raw = body.read(gc.MAX_JSON + 1)
        finally:
            body.close()
        if not isinstance(raw, bytes) or len(raw) != observed['size'] or len(raw) > gc.MAX_JSON:
            raise ValueError('S3 JSON read size mismatch')
        return raw

    def create(self, key, raw):
        if not isinstance(key, str) or not gc.GC_RE.fullmatch(key):
            raise ValueError('GC creates only immutable GC metadata')
        gc.decode(raw)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=raw,
                               ContentType='application/json', IfNoneMatch='*')

    def delete(self, item):
        gc.metadata(item)
        gc.payload_key(item['key'])
        observed = self.head(item['key'])
        if observed != item:
            raise self._mismatch('S3 delete identity changed', item['key'], item, observed)
        # Exactly one request. Caller performs readback and stops on ANY exception.
        self.client.delete_object(Bucket=self.bucket, Key=item['key'], IfMatch=item['etag'])
