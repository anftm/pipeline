"""Version-bound GC read cache and shared request pacing across all formats."""
from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time


def cacheable(path):
    name = path.rsplit('/', 1)[-1]
    return (path.endswith(('.json', '.json.gz')) and (not path.startswith('reader-index/gc/')
            or path.startswith('reader-index/gc/retirements/'))
            and (path.startswith(('reader-index/', 'control/', 'releases/', 'text/exports/', 'generations/'))
                 or name.endswith('-manifest.json') or name.startswith(('index', 'render-range-'))))


class ReadCache:
    """Cache evidence, never deletion authority; each hit needs a fresh listed ETag."""
    def __init__(self, path, max_bytes=512 * 1024 * 1024):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS reads ('
                        'bucket TEXT, path TEXT, parser TEXT, etag TEXT, digest TEXT, '
                        'packed BLOB, packed_sha TEXT, accessed REAL, PRIMARY KEY(bucket,path,parser))')
        self.max_bytes = max_bytes
        self.hits = self.misses = self.writes = 0

    def get(self, bucket, path, parser, etag):
        with self.lock:
            row = self.db.execute('SELECT etag,digest,packed,packed_sha FROM reads '
                                  'WHERE bucket=? AND path=? AND parser=?', (bucket, path, parser)).fetchone()
            if not etag or not row or row[0] != etag or hashlib.sha256(row[2]).hexdigest() != row[3]:
                self.misses += 1
                return None
            try:
                raw = gzip.decompress(row[2])
            except (OSError, EOFError):
                self.misses += 1
                return None
            self.hits += 1
            return raw, row[1]

    def put(self, bucket, path, parser, etag, raw, digest):
        if not etag or len(raw) > 8 * 1024 * 1024:
            return
        packed = gzip.compress(raw, compresslevel=1, mtime=0)
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO reads VALUES (?,?,?,?,?,?,?,?)',
                            (bucket, path, parser, etag, digest, packed,
                             hashlib.sha256(packed).hexdigest(), time.time()))
            self.writes += 1
            if self.writes % 1024 == 0:
                size = self.db.execute('SELECT COALESCE(SUM(length(packed)),0) FROM reads').fetchone()[0]
                if size > self.max_bytes:
                    self.db.execute('DELETE FROM reads WHERE rowid IN '
                                    '(SELECT rowid FROM reads ORDER BY accessed LIMIT 1024)')

    def close(self):
        with self.lock:
            self.db.close()


class RequestPacer:
    """One budget for all threads and botocore retries in a storage namespace."""
    def __init__(self, rate=6, *, clock=time.monotonic, sleep=time.sleep):
        if not 0 < rate <= 32:
            raise ValueError('GC request rate must be in (0, 32]')
        self.interval = self.minimum = 1 / rate
        self.clock, self.sleep = clock, sleep
        self.next = self.cooldown = 0.0
        self.lock = threading.Lock()
        self.successes = self.throttles = 0

    def before_send(self, **_):
        while True:
            with self.lock:
                now = self.clock()
                delay = max(self.next, self.cooldown) - now
                if delay <= 0:
                    self.next = now + self.interval
                    return
            self.sleep(min(delay, 30))

    def after_response(self, response=None, **_):
        if not response:
            return
        http, payload = response
        code = (payload or {}).get('Error', {}).get('Code')
        with self.lock:
            if http.status_code in {429, 503} or code in {'SlowDown', 'Throttling', 'ThrottlingException'}:
                self.interval = min(2, self.interval * 2)
                self.successes = 0
                self.throttles += 1
                try:
                    delay = float(http.headers.get('retry-after', 30))
                except (TypeError, ValueError):
                    delay = 30
                self.cooldown = max(self.cooldown, self.clock() + max(1, min(delay, 300)))
            elif 200 <= http.status_code < 300:
                self.successes += 1
                if self.successes >= 200:
                    self.interval = max(self.minimum, self.interval * 0.9)
                    self.successes = 0


def configured_cache():
    path = os.environ.get('READER_GC_CACHE')
    return ReadCache(path) if path else None
