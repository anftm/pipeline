"""Retain one exact catalog parent for recovery, outside the GC root graph.

This archive is transaction input, not an asset-retention pin. The caller
must own a verified writer window and a private bounded spool before staging.
"""
from __future__ import annotations

from hashlib import sha256
from typing import BinaryIO

from . import reader_image_history as history
from .reader_bucket_store import S3NotFound, retry_s3, s3_error_code
from .reader_image_history_catalog import MAX_CATALOG_BYTES
from .reader_image_history_publish_s3 import BLOCK, BUCKET, CATALOG
from .reader_stream_checkpoint_cas import strong_etag

PREFIX = 'reader-index/gc/image-history-catalog-parents/v1/'


def _copy(source, output, size):
    h = sha256()
    left = size
    while left:
        n = min(BLOCK, left)
        chunk = source.read(n)
        if not isinstance(chunk, bytes) or not 0 < len(chunk) <= n:
            raise ValueError('short or invalid catalog parent stream')
        if output is not None and output.write(chunk) != len(chunk):
            raise OSError('short local catalog spool write')
        h.update(chunk)
        left -= len(chunk)
    if source.read(1) != b'':
        raise ValueError('catalog parent exceeds its reviewed length')
    return h.hexdigest()


class CatalogParent:
    def __init__(self, client, *, digest: str, size: int, etag: str):
        if (client.meta.config.retries or {}).get('total_max_attempts') != 1:
            raise ValueError('catalog parent requires a single-attempt S3 client')
        history._hex(digest)
        if type(size) is not int or not 0 < size <= MAX_CATALOG_BYTES:
            raise ValueError('invalid catalog parent length')
        self.client, self.digest, self.size, self.etag = client, digest, size, strong_etag(etag)
        self.path = PREFIX + digest + '.json'

    def _download(self, key, output, *, condition=None):
        def read():
            if output is not None:
                output.seek(0)
                output.truncate(0)
            args = {} if condition is None else {'IfMatch': condition}
            response = self.client.get_object(Bucket=BUCKET, Key=key, **args)
            body = response['Body']
            try:
                tag = strong_etag(response.get('ETag'))
                if (response['ResponseMetadata']['HTTPStatusCode'] != 200
                        or response['ContentLength'] != self.size
                        or condition is not None and tag != condition):
                    raise ValueError('catalog parent response does not match the review')
                if _copy(body, output, self.size) != self.digest:
                    raise ValueError('catalog parent digest differs from the review')
                if output is not None:
                    output.seek(0)
                return tag
            finally:
                body.close()
        return retry_s3(read, attempts=2)

    def load(self, output: BinaryIO):
        """Recovery reads only the archived parent. Never substitute live bytes."""
        return self._download(self.path, output)

    def stage(self, output: BinaryIO, *, guard):
        """Stage once or observe an existing exact archive; never overwrite it."""
        if guard() is not True:
            raise ValueError('catalog archival requires the active migration fence')
        self._download(CATALOG, output, condition=self.etag)
        try:
            existing = self._download(self.path, None)
        except S3NotFound:
            existing = None
        if existing is not None:
            return existing
        if guard() is not True:
            raise ValueError('catalog archive fence changed before write')
        try:
            response = self.client.put_object(Bucket=BUCKET, Key=self.path, Body=output, ContentLength=self.size,
                                              ContentType='application/json', IfNoneMatch='*')
        except Exception as error:
            # A definite conditional conflict can only be reconciled by a
            # fresh exact GET. Unknown transport outcomes must leave the run.
            if s3_error_code(error)[1] not in {409, 412}:
                raise
            tag = None
        else:
            meta = response['ResponseMetadata']
            if meta['HTTPStatusCode'] not in (200, 201) or meta.get('RetryAttempts', 0) != 0:
                raise RuntimeError('catalog archive outcome unknown; observe without replay')
            tag = strong_etag(response.get('ETag'))
        observed = self._download(self.path, None)
        if tag is not None and tag != observed:
            raise ValueError('catalog archive acknowledgement differs from the independent readback')
        output.seek(0)
        return observed
