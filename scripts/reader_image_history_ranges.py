"""Bounded, ordered reads of one pinned S3 legacy catalog version."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256

from .reader_bucket_store import retry_s3
from .reader_image_history_catalog import MAX_CATALOG_BYTES
from .reader_image_history_publish_s3 import BUCKET, CATALOG
from .reader_stream_checkpoint_cas import strong_etag

BLOCK = 4 * 1024 * 1024
WORKERS = 8


class _Ranges:
    def __init__(self, receive, pool, size):
        self.receive, self.pool, self.size = receive, pool, size
        self.count = (size + BLOCK - 1) // BLOCK
        self.pending = {i: pool.submit(self._fetch, i) for i in range(min(self.count, WORKERS))}
        self.index = self.offset = 0
        self.block = b''

    def _fetch(self, index):
        start, end = index * BLOCK, min(self.size, (index + 1) * BLOCK) - 1
        data = self.receive(start, end)
        if not isinstance(data, bytes) or len(data) != end - start + 1:
            raise ValueError('catalog range returned the wrong byte count')
        return data

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
def _open(receive, size):
    if type(size) is not int or not 0 < size <= MAX_CATALOG_BYTES:
        raise ValueError('catalog exceeds the reviewed range read ceiling')
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        reader = _Ranges(receive, pool, size)
        try:
            yield reader
        finally:
            for task in reader.pending.values():
                task.cancel()


def open_catalog(client, *, size, etag):
    """Return ordered S3 ranges, pinned by If-Match and readback metadata."""
    strong_etag(etag)

    def receive(start, end):
        length = end - start + 1

        def read():
            r = client.get_object(Bucket=BUCKET, Key=CATALOG, IfMatch=etag, Range=f'bytes={start}-{end}')
            body = r['Body']
            try:
                if (r['ResponseMetadata']['HTTPStatusCode'] != 206 or r.get('ETag') != etag
                        or r.get('ContentRange') != f'bytes {start}-{end}/{size}'
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

        return retry_s3(read, attempts=2)

    return _open(receive, size)


def resolver_digest(http, url, *, size, etag):
    """Hash the complete independent resolver view and reject mixed ranges."""
    import httpx

    strong_etag(etag)

    def receive(start, end):
        length = end - start + 1
        headers = {'Range': f'bytes={start}-{end}', 'If-Match': etag,
                   'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'}
        for attempt in range(2):
            try:
                with http.stream('GET', url, headers=headers, follow_redirects=True) as r:
                    if r.status_code in {429, 502, 503, 504} and attempt == 0:
                        continue
                    if (r.status_code != 206 or r.headers.get('ETag') != etag
                            or r.headers.get('Content-Range') != f'bytes {start}-{end}/{size}'
                            or r.headers.get('Content-Length') != str(length)
                            or r.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
                        raise ValueError('resolver range does not bind the reviewed catalog version')
                    raw = bytearray()
                    for block in r.iter_bytes(chunk_size=256 * 1024):
                        if len(raw) + len(block) > length:
                            raise ValueError('resolver catalog range exceeds its expected length')
                        raw.extend(block)
                    if len(raw) != length:
                        raise OSError('short resolver catalog range')
                    return bytes(raw)
            except (httpx.TransportError, OSError):
                if attempt == 1:
                    raise
        raise AssertionError('resolver range did not return')

    with _open(receive, size) as stream:
        return _stream_digest(stream)


def _stream_digest(stream):
    digest = sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
    return digest.hexdigest()
