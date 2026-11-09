"""Conditional immutable-object primitive for protected EPUB staging.

Not a publisher or workflow entrypoint. It cannot write indexes, Reader roots,
processing records, or delete anything. A lost write response is reconciled by
streaming and hashing the exact key; it is never blindly replayed. S3
conditions are single-object operations, not a bucket lock or writer fence.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
import re

try:
    from .reader_bucket_store import S3BucketStore
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    from reader_bucket_store import S3BucketStore
    from shared import READER_ASSETS_BUCKET


PREFIX = re.compile(r'chapters/ebook/epub/([0-9a-f]{64})/([0-9a-f]{16})\Z')
MAX_OBJECT_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def _root(value):
    if not isinstance(value, str) or not PREFIX.fullmatch(value):
        raise ValueError('EPUB object root is outside the immutable allowlist')
    return value


def _key(root, relative):
    _root(root)
    if (not isinstance(relative, str) or not relative or len(relative.encode('utf8')) > 2048
            or relative.startswith('/') or '\\' in relative or any(ord(char) < 32 or ord(char) == 127 for char in relative)
            or any(part in {'', '.', '..'} for part in relative.split('/'))
            or relative.lower().endswith('.epub')):
        raise ValueError('invalid EPUB immutable member path')
    return root + '/' + relative


def _read_fd(stream, before) -> dict:
    digest, count = hashlib.sha256(), 0
    while chunk := stream.read(CHUNK_BYTES):
        count += len(chunk)
        if count > MAX_OBJECT_BYTES:
            raise ValueError('EPUB staging input exceeds size limit')
        digest.update(chunk)
    after = os.fstat(stream.fileno())
    if (count != before.st_size or before.st_size != after.st_size or before.st_ino != after.st_ino
            or before.st_mtime_ns != after.st_mtime_ns):
        raise ValueError('EPUB staging input changed while hashing')
    return {'sha256': digest.hexdigest(), 'bytes': count}


def _open_local(path: Path):
    if not isinstance(path, Path):
        raise ValueError('EPUB staging input must be an ordinary file')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise ValueError('EPUB staging input must be an ordinary file') from error
    stream = os.fdopen(descriptor, 'rb')
    metadata = os.fstat(stream.fileno())
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_OBJECT_BYTES:
        stream.close()
        raise ValueError('EPUB staging input size or type is out of bounds')
    return stream, metadata


def local_identity(path: Path) -> dict:
    stream, metadata = _open_local(path)
    with stream:
        return _read_fd(stream, metadata)


def _expected(value):
    if (not isinstance(value, dict) or set(value) != {'sha256', 'bytes'}
            or not isinstance(value['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])
            or type(value['bytes']) is not int or not 0 < value['bytes'] <= MAX_OBJECT_BYTES):
        raise ValueError('invalid EPUB immutable object identity')
    return value


class S3EpubObjects:
    def __init__(self, store=None):
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            if store._namespace != 'vomebook':
                raise ValueError('EPUB staging requires the vomebook S3 namespace')
            store._config = store._config.merge(Config(
                retries={'total_max_attempts': 1}, request_checksum_calculation='when_required',
                response_checksum_validation='when_required'))
        self.store = store

    def _location(self, root, relative):
        key = _key(root, relative)
        namespace, name = self.store._location(READER_ASSETS_BUCKET)
        if (namespace, name) != ('vomebook', 'reader-assets-v2'):
            raise ValueError('unexpected EPUB staging bucket mapping')
        return self.store._client(namespace), name, key

    def inspect(self, root, relative):
        client, bucket, key = self._location(root, relative)
        try:
            response = client.get_object(Bucket=bucket, Key=key)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error, ClientError):
                code = error.response.get('Error', {}).get('Code')
                if code in {'404', 'NoSuchKey', 'NotFound'}:
                    return None
            raise
        length = response.get('ContentLength')
        body = response.get('Body')
        if body is None:
            raise ValueError('EPUB object response has no body')
        digest, count = hashlib.sha256(), 0
        try:
            if type(length) is not int or not 0 < length <= MAX_OBJECT_BYTES:
                raise ValueError('EPUB object has invalid declared size')
            while chunk := body.read(CHUNK_BYTES):
                count += len(chunk)
                if count > length:
                    raise ValueError('EPUB object exceeds its declared size')
                digest.update(chunk)
        finally:
            body.close()
        if count != length:
            raise ValueError('EPUB object readback is truncated')
        return {'sha256': digest.hexdigest(), 'bytes': count}

    def create_once(self, root, relative, path: Path, expected):
        _expected(expected)
        client, bucket, key = self._location(root, relative)
        stream, metadata = _open_local(path)
        with stream:
            if _read_fd(stream, metadata) != expected:
                raise ValueError('EPUB staging input differs from approved identity')
            stream.seek(0)
            try:
                client.put_object(Bucket=bucket, Key=key, Body=stream, ContentLength=expected['bytes'],
                                  ContentType='application/octet-stream', IfNoneMatch='*')
            except Exception as error:
                actual = self.inspect(root, relative)
                if actual == expected:
                    return 'written-response-unknown'
                raise RuntimeError('EPUB immutable write unconfirmed or conflicted; inspect before recovery') from error
            stream.seek(0)
            if _read_fd(stream, metadata) != expected:
                raise RuntimeError('EPUB local input changed during the write; inspect before recovery')
        if self.inspect(root, relative) != expected:
            raise RuntimeError('EPUB immutable object readback differs')
        return 'written'
