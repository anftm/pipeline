"""Bounded, ordered reads of one pinned S3 legacy catalog version."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from .reader_bucket_store import retry_s3
from .reader_image_history_catalog import MAX_CATALOG_BYTES
from .reader_image_history_publish_s3 import BUCKET, CATALOG
from .reader_stream_checkpoint_cas import strong_etag

BLOCK = 4 * 1024 * 1024
WORKERS = 8


class _Ranges:
    def __init__(self, client, pool, size, etag):
        self.client, self.pool, self.size, self.etag = client, pool, size, etag
        self.count = (size + BLOCK - 1) // BLOCK
        self.pending = {i: pool.submit(self._fetch, i) for i in range(min(self.count, WORKERS))}
        self.index = self.offset = 0
        self.block = b''

    def _fetch(self, index):
        start, end = index * BLOCK, min(self.size, (index + 1) * BLOCK) - 1
        length = end - start + 1

        def receive():
            r = self.client.get_object(Bucket=BUCKET, Key=CATALOG, IfMatch=self.etag, Range=f'bytes={start}-{end}')
            body = r['Body']
            try:
                if (r['ResponseMetadata']['HTTPStatusCode'] != 206 or r.get('ETag') != self.etag
                        or r.get('ContentRange') != f'bytes {start}-{end}/{self.size}'
                        or r.get('ContentLength') != length):
                    raise ValueError('catalog range does not bind the reviewed version')
                data = bytearray()
                while len(data) < length:
                    n = min(256 * 1024, length - len(data))
                    part = body.read(n)
                    if not isinstance(part, bytes) or not 0 < len(part) <= n:
                        raise OSError('short or invalid catalog range')
                    data.extend(part)
                if body.read(1) != b'':
                    raise ValueError('catalog range exceeds its expected length')
                return bytes(data)
            finally:
                body.close()

        return retry_s3(receive, attempts=2)

    def read(self, n):
        if type(n) is not int or n < 0:
            raise ValueError('catalog ranges require bounded reads')
        if n == 0:
            return b''
        if self.offset == len(self.block):
            if self.index == self.count:
                return b''
            self.block = self.pending.pop(self.index).result()
            self.offset = 0
            later = self.index + WORKERS
            if later < self.count:
                self.pending[later] = self.pool.submit(self._fetch, later)
            self.index += 1
        end = min(len(self.block), self.offset + n)
        data = self.block[self.offset:end]
        self.offset = end
        return data


@contextmanager
def open_catalog(client, *, size, etag):
    """All ranges must return the same exact ETag, offsets and total length.

    Callers still check the whole-stream SHA and verify the live HEAD after
    reading. This helper does not cover the separate public resolver view.
    """
    strong_etag(etag)
    if type(size) is not int or not 0 < size <= MAX_CATALOG_BYTES:
        raise ValueError('catalog exceeds the reviewed range read ceiling')
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        reader = _Ranges(client, pool, size, etag)
        try:
            yield reader
        finally:
            for task in reader.pending.values():
                task.cancel()
