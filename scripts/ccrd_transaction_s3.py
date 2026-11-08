"""S3 adapter restricted to the CCRD publication journal namespace."""
from __future__ import annotations

import hashlib
import re

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_transaction as journal
    from .reader_bucket_store import S3BucketStore
    from .reader_page_cas import strong_etag
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_transaction as journal
    from reader_bucket_store import S3BucketStore
    from reader_page_cas import strong_etag


KEY = re.compile(r'control/cw0-publications/[0-9a-f]{32}/(records|evidence|intents)/([0-9a-f]{64})\.json\Z')


class S3Store:
    def __init__(self, store=None):
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            store._config = store._config.merge(Config(retries={'total_max_attempts': 1},
                                                       request_checksum_calculation='when_required',
                                                       response_checksum_validation='when_required'))
        self.store = store

    def _client(self):
        namespace, name = self.store._location(lifecycle.BUCKET)
        if (namespace, name) != ('vomebook', 'ccrd-index'):
            raise ValueError('unexpected CCRD bucket mapping')
        return self.store._client(namespace), name

    def _get(self, key: str) -> tuple[bytes | None, str | None]:
        client, bucket = self._client()
        try:
            response = client.get_object(Bucket=bucket, Key=key)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
                return None, None
            raise
        length, body = response.get('ContentLength'), response['Body']
        try:
            if type(length) is not int or not 0 < length <= journal.MAX_BYTES:
                raise ValueError('invalid CCRD journal object size')
            raw = body.read(length + 1)
        finally:
            body.close()
        if not isinstance(raw, bytes) or len(raw) != length:
            raise ValueError('CCRD journal object is truncated')
        return raw, strong_etag(response.get('ETag'))

    def read_head(self) -> tuple[bytes | None, str | None]:
        raw, etag = self._get(journal.HEAD)
        if raw is not None: journal._head(raw)
        return raw, etag

    def put_head(self, raw: bytes, expected_etag: str | None) -> None:
        journal._head(raw)
        condition = {'IfNoneMatch': '*'} if expected_etag is None else {'IfMatch': strong_etag(expected_etag)}
        client, bucket = self._client()
        client.put_object(Bucket=bucket, Key=journal.HEAD, Body=raw, ContentType='application/json', **condition)

    def _read(self, key: str, kind: str) -> bytes | None:
        match = KEY.fullmatch(key)
        if not match or match.group(1) != kind:
            raise ValueError('CCRD journal object outside allowed namespace')
        raw, _etag = self._get(key)
        if raw is not None and hashlib.sha256(raw).hexdigest() != match.group(2):
            raise ValueError('CCRD journal object hash differs')
        return raw

    def _put(self, key: str, raw: bytes, kind: str) -> None:
        match = KEY.fullmatch(key)
        if (not match or match.group(1) != kind or not isinstance(raw, bytes) or not 0 < len(raw) <= journal.MAX_BYTES
                or hashlib.sha256(raw).hexdigest() != match.group(2)):
            raise ValueError('CCRD journal immutable object identity differs')
        if kind == 'records': journal._record(raw)
        client, bucket = self._client()
        client.put_object(Bucket=bucket, Key=key, Body=raw, ContentType='application/json', IfNoneMatch='*')

    def read_record(self, key: str) -> bytes | None: return self._read(key, 'records')
    def put_record(self, key: str, raw: bytes) -> None: self._put(key, raw, 'records')
    def read_evidence(self, key: str) -> bytes | None: return self._read(key, 'evidence')
    def put_evidence(self, key: str, raw: bytes) -> None: self._put(key, raw, 'evidence')
    def read_intent(self, key: str) -> bytes | None: return self._read(key, 'intents')
    def put_intent(self, key: str, raw: bytes) -> None: self._put(key, raw, 'intents')
