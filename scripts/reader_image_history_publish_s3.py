"""Exact streaming S3 transport for the two mutable legacy Reader roots.

This is not a writer fence or permission to publish. The caller must hold a
separately checked fence and spend a durable permit before calling put_once.
A transport error is ambiguous; callers must observe, never retry the PUT.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .reader_bucket_store import retry_s3
from .reader_image_history_catalog import MAX_CATALOG_BYTES, MAX_SIDECAR_BYTES
from .reader_stream_checkpoint_cas import strong_etag

BUCKET = 'reader-assets-v2'
CATALOG = 'reader-index/reader_catalog.json'
SIDECAR = 'reader-index/reader_assets.json.gz'
ROOTS = {CATALOG: (MAX_CATALOG_BYTES, 'application/json'), SIDECAR: (MAX_SIDECAR_BYTES, 'application/gzip')}
BLOCK = 1024 * 1024


@dataclass(frozen=True)
class Observed:
    sha256: str
    size: int
    etag: str


def _size(path, size):
    if path not in ROOTS or type(size) is not int or not 0 < size <= ROOTS[path][0]:
        raise ValueError('legacy image publication path or length is outside its reviewed bounds')
    return size


def _digest(body, size):
    h = sha256()
    left = size
    while left:
        block = body.read(min(BLOCK, left))
        if not isinstance(block, bytes) or not 0 < len(block) <= min(BLOCK, left):
            raise ValueError('stream does not contain exactly the sealed number of bytes')
        left -= len(block)
        h.update(block)
    if body.read(1) != b'':
        raise ValueError('stream exceeds the sealed byte length')
    return h.hexdigest()


class LegacyRoots:
    """Bind whole-object readback to the same response's strong ETag."""

    def __init__(self, client):
        if (client.meta.config.retries or {}).get('total_max_attempts') != 1:
            raise ValueError('legacy image writes require a dedicated single-attempt client')
        self.client = client

    def observe(self, path):
        if path not in ROOTS:
            raise ValueError('unexpected legacy image publication path')

        def read():
            response = self.client.get_object(Bucket=BUCKET, Key=path)
            stream = response['Body']
            try:
                if response['ResponseMetadata']['HTTPStatusCode'] != 200:
                    raise ValueError('unexpected legacy root response')
                size = _size(path, response['ContentLength'])
                etag = strong_etag(response.get('ETag'))
                return Observed(_digest(stream, size), size, etag)
            finally:
                stream.close()

        return retry_s3(read, attempts=2)

    def put_once(self, path, stream, *, size, digest, parent_etag):
        """Hash a sealed private stream, rewind, and issue one conditional PUT.

        Preserve exclusive ownership of the stream/file for this entire call.
        The coordinator must still perform a new observation under the fence.
        """
        _size(path, size)
        strong_etag(parent_etag)
        stream.seek(0)
        if _digest(stream, size) != digest:
            raise ValueError('legacy target bytes differ from seal')
        stream.seek(0)
        response = self.client.put_object(Bucket=BUCKET, Key=path, Body=stream, ContentLength=size,
                                          ContentType=ROOTS[path][1], IfMatch=parent_etag)
        meta = response['ResponseMetadata']
        if meta['HTTPStatusCode'] not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
            raise RuntimeError('legacy root PUT outcome unconfirmed; observe without replay')
        target_etag = strong_etag(response.get('ETag'))
        if target_etag == parent_etag:
            raise RuntimeError('legacy root ETag did not change; observe without replay')
        return target_etag
