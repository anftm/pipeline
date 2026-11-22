"""Retain one exact catalog parent for recovery, outside the GC root graph.

This archive is transaction input, not an asset-retention pin. The caller
must own a verified writer window and a private bounded spool before staging.
"""
from __future__ import annotations

import gzip
from hashlib import sha256
from tempfile import TemporaryFile
from time import sleep
from typing import BinaryIO
import zlib

from botocore.exceptions import ClientError

from . import reader_image_history as history
from .reader_bucket_store import S3NotFound, retry_s3, s3_error_code
from .reader_image_history_catalog import MAX_CATALOG_BYTES
from .reader_image_history_publish_s3 import BLOCK, BUCKET, CATALOG
from .reader_stream_checkpoint_cas import strong_etag

V1_PREFIX = 'reader-index/gc/image-history-catalog-parents/v1/'
PREFIX = 'reader-index/gc/image-history-catalog-parents/v2/'


def _packed_bound(size):
    # Allow for gzip/deflate overhead even on incompressible input.
    return size + size // 100 + 64


def _chunks(source, size):
    left = size
    while left:
        n = min(BLOCK, left)
        chunk = source.read(n)
        if not isinstance(chunk, bytes) or not 0 < len(chunk) <= n:
            raise ValueError('short or invalid catalog parent stream')
        left -= len(chunk)
        yield chunk
    if source.read(1) != b'':
        raise ValueError('catalog parent exceeds its declared length')


def _inflate(source, packed_size, size):
    # One gzip member, including its checksum/trailer; no padding or trailing
    # members. Bound each expansion as well as the total reviewed raw length.
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    left = size
    try:
        for packed in _chunks(source, packed_size):
            if decoder.eof:
                raise ValueError('trailing bytes in gzip catalog parent')
            while packed:
                chunk = decoder.decompress(packed, min(BLOCK, left + 1))
                packed = decoder.unconsumed_tail
                if decoder.unused_data:
                    raise ValueError('trailing bytes in gzip catalog parent')
                if len(chunk) > left:
                    raise ValueError('gzip catalog parent exceeds its reviewed length')
                left -= len(chunk)
                if chunk:
                    yield chunk
    except zlib.error as error:
        raise ValueError('invalid gzip catalog parent') from error
    if not decoder.eof or left:
        raise ValueError('incomplete gzip catalog parent or incorrect raw length')


def _copy(chunks, output):
    h = sha256()
    for chunk in chunks:
        if output is not None and output.write(chunk) != len(chunk):
            raise OSError('short local catalog spool write')
        h.update(chunk)
    return h.hexdigest()


class CatalogParent:
    def __init__(self, client, *, digest: str, size: int, etag: str):
        if (client.meta.config.retries or {}).get('total_max_attempts') != 1:
            raise ValueError('catalog parent requires a single-attempt S3 client')
        history._hex(digest)
        if type(size) is not int or not 0 < size <= MAX_CATALOG_BYTES:
            raise ValueError('invalid catalog parent length')
        self.client, self.digest, self.size, self.etag = client, digest, size, strong_etag(etag)
        self._v1_path = V1_PREFIX + digest + '.json'
        self._v2_path = PREFIX + digest + '.json.gz'
        self.path = self._v2_path

    def _download(self, key, output, *, condition=None, compressed=False):
        def read():
            if output is not None:
                output.seek(0)
                output.truncate(0)
            args = {} if condition is None else {'IfMatch': condition}
            try:
                response = self.client.get_object(Bucket=BUCKET, Key=key, **args)
            except S3NotFound:
                return None
            except ClientError as error:
                code, status = s3_error_code(error)
                if status == 404 and code in {'NoSuchKey', 'NotFound', '404'}:
                    return None
                raise
            body = response['Body']
            try:
                tag = strong_etag(response.get('ETag'))
                length = response.get('ContentLength')
                if (response['ResponseMetadata']['HTTPStatusCode'] != 200
                        or type(length) is not int
                        or not (0 < length <= _packed_bound(self.size) if compressed else length == self.size)
                        or condition is not None and tag != condition):
                    raise ValueError('catalog parent response does not match the review')
                chunks = _inflate(body, length, self.size) if compressed else _chunks(body, length)
                if _copy(chunks, output) != self.digest:
                    raise ValueError('catalog parent digest differs from the review')
                if output is not None:
                    output.seek(0)
                return tag
            finally:
                body.close()
        try:
            try:
                tag = retry_s3(read, attempts=2)
            except S3NotFound as error:
                # The shared helper also maps generic HTTP 404s, including
                # errors not raised by the GET itself. Never fall back on those.
                raise ValueError('catalog parent GET did not confirm a missing key') from error
            if tag is None:
                raise S3NotFound(key)
            return tag
        except Exception:
            if output is not None:
                output.seek(0)
                output.truncate(0)
            raise

    def load(self, output: BinaryIO):
        """Recovery reads only the archived parent. Never substitute live bytes."""
        try:
            tag = self._download(self._v2_path, output, compressed=True)
        except S3NotFound:
            tag = self._download(self._v1_path, output)
            self.path = self._v1_path
        else:
            self.path = self._v2_path
        return tag

    def _readback(self):
        for delay in (0.25, 0.5, None):
            try:
                return self._download(self._v2_path, None, compressed=True)
            except S3NotFound:
                if delay is None:
                    raise
                sleep(delay)

    def stage(self, output: BinaryIO, *, guard):
        """Stage once or observe an existing archive; return the ETag for self.path."""
        if guard() is not True:
            raise ValueError('catalog archival requires the active migration fence')
        self._download(CATALOG, output, condition=self.etag)
        # Prefer a verified old archive rather than duplicating it in v2.
        for key, compressed in ((self._v1_path, False), (self._v2_path, True)):
            try:
                existing = self._download(key, None, compressed=compressed)
            except S3NotFound:
                continue
            self.path = key
            return existing
        with TemporaryFile() as packed:
            with gzip.GzipFile(filename='', fileobj=packed, mode='wb', compresslevel=6, mtime=0) as encoder:
                if _copy(_chunks(output, self.size), encoder) != self.digest:
                    raise ValueError('local catalog parent digest differs from the review')
            length = packed.tell()
            if not 0 < length <= _packed_bound(self.size):
                raise ValueError('invalid gzip catalog parent length')
            packed.seek(0)
            output.seek(0)
            if guard() is not True:
                raise ValueError('catalog archive fence changed before write')
            try:
                response = self.client.put_object(Bucket=BUCKET, Key=self._v2_path, Body=packed, ContentLength=length,
                                                  ContentType='application/gzip', IfNoneMatch='*')
            except Exception as error:
                # A conditional conflict can only be reconciled by a fresh
                # GET. An unknown transport outcome must leave the run.
                if s3_error_code(error)[1] not in {409, 412}:
                    raise
                tag = None
            else:
                meta = response['ResponseMetadata']
                if meta['HTTPStatusCode'] not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
                    raise RuntimeError('catalog archive outcome unknown; observe without replay')
                tag = strong_etag(response.get('ETag'))
        observed = self._readback()
        if tag is not None and tag != observed:
            raise ValueError('catalog archive acknowledgement differs from the independent readback')
        self.path = self._v2_path
        return observed
