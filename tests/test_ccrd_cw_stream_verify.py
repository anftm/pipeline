"""Synthetic, read-only tests of bounded CW object digest streaming."""
import hashlib
import io
import json
import sqlite3
import unittest

from scripts import ccrd_cw_stream_verify as verify


class Body(io.BytesIO):
    def __init__(self, raw, sizes):
        super().__init__(raw)
        self.sizes = sizes

    def read(self, size=-1):
        self.sizes.append(size)
        return super().read(size)


class ReadOnlyStore:
    def __init__(self, raw):
        self.raw = raw
        self.path = 'generations/ccrd-synthetic/CW.sqlite3'
        self.pointer = {'format': 2, 'kind': 'ccrd-four-source-generation', 'generation': 'ccrd-four-' + 'a' * 64,
                        'databases': {name: {} for name in ('CCRD', 'CW', 'RMRB', 'PLA')}}
        self.pointer['databases']['CW'] = {'path': self.path, 'bytes': len(raw),
                                           'sha256': hashlib.sha256(raw).hexdigest()}
        self.reads, self.bodies, self.requests = [], [], []
        self.pointer_reads, self.heads = 0, 0
        self.etag = '"synthetic-etag"'
        self.changed_pointer = False
        self.changed_head = False
        self.change_get_etag = False
        self.change_get_length = False
        self.short_object = False

    def encoded(self):
        return json.dumps(self.pointer, sort_keys=True).encode()

    def get_object(self, **kwargs):
        self.requests.append(('get', kwargs))
        if kwargs['Key'] == 'current.json':
            self.pointer_reads += 1
            raw = self.encoded()
            if self.pointer_reads == 2 and self.changed_pointer:
                raw += b' '
            response = {'ContentLength': len(raw)}
        elif kwargs['Key'] == self.path:
            if kwargs.get('IfMatch') != self.etag:
                raise ValueError('fixture missing conditional read')
            raw = self.raw[:-1] if self.short_object else self.raw
            response = {'ContentLength': len(self.raw) + int(self.change_get_length),
                        'ETag': '"unexpected"' if self.change_get_etag else self.etag}
        else:
            raise AssertionError('unexpected GET')
        body = Body(raw, self.reads)
        self.bodies.append(body)
        return {**response, 'Body': body}

    def head_object(self, **kwargs):
        self.requests.append(('head', kwargs))
        if kwargs['Key'] != self.path:
            raise AssertionError('unexpected HEAD')
        self.heads += 1
        etag = '"changed"' if self.changed_head and self.heads == 2 else self.etag
        return {'ContentLength': len(self.raw), 'ETag': etag}


class StreamingTests(unittest.TestCase):
    def setUp(self):
        if not hasattr(sqlite3.Connection, 'serialize'):
            self.skipTest('SQLite byte serialization unavailable')
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY, text TEXT)')
        db.execute('INSERT INTO sample(text) VALUES(?)', ('fixture',))
        self.raw = db.serialize()
        self.store = ReadOnlyStore(self.raw)

    def test_streams_correct_digest_in_bounded_chunks_and_never_writes(self):
        result = verify.verify(self.store)
        self.assertEqual(result['cw_sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(result['bytes_streamed'], len(self.raw))
        self.assertTrue(result['entire_object_sha256_verified'])
        self.assertFalse(result['full_sqlite_semantics_verified'])
        self.assertFalse(result['external_writers_fenced'])
        self.assertFalse(result['aba_excluded'])
        self.assertFalse(result['saved_to_disk'])
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['remote_writes'], 0)
        self.assertEqual([name for name, _ in self.store.requests], ['get', 'head', 'get', 'head', 'get'])
        self.assertTrue(all(body.closed for body in self.store.bodies))
        self.assertEqual(self.store.reads[0], verify.MAX_POINTER_BYTES + 1)
        self.assertTrue(all(size == verify.CHUNK_BYTES for size in self.store.reads[1:-1]))
        self.assertEqual(self.store.reads[-1], verify.MAX_POINTER_BYTES + 1)

    def test_incomplete_wrong_identity_or_drift_fails_closed(self):
        for mode in ('wrong-hash', 'short', 'head-change', 'pointer-change', 'get-etag', 'get-length',
                     'bad-header', 'invalid-database-path', 'unknown-source'):
            store = ReadOnlyStore(self.raw)
            if mode == 'wrong-hash':
                store.pointer['databases']['CW']['sha256'] = '0' * 64
            elif mode == 'short':
                store.short_object = True
            elif mode == 'head-change':
                store.changed_head = True
            elif mode == 'pointer-change':
                store.changed_pointer = True
            elif mode == 'get-etag':
                store.change_get_etag = True
            elif mode == 'get-length':
                store.change_get_length = True
            elif mode == 'bad-header':
                store.raw = b'x' * len(self.raw)
                store.pointer['databases']['CW']['sha256'] = hashlib.sha256(store.raw).hexdigest()
            elif mode == 'invalid-database-path':
                store.pointer['databases']['CW']['path'] = 'generations/../CW.sqlite3'
            else:
                store.pointer['databases']['OTHER'] = {}
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                verify.verify(store)
            self.assertTrue(all(body.closed for body in store.bodies))

    def test_duplicate_keys_nonfinite_numbers_and_oversized_manifest_fail(self):
        raw = self.store.encoded()
        for value in (raw.replace(b'"format": 2', b'"format": 2, "format": 2'),
                      b'{"value":NaN}', b'[]'):
            with self.subTest(raw=value[:50]), self.assertRaises(ValueError):
                verify._cw_identity(value)
        self.store.pointer['extra'] = 'a' * verify.MAX_POINTER_BYTES
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            verify.verify(self.store)
        self.assertTrue(all(body.closed for body in self.store.bodies))


if __name__ == '__main__':
    unittest.main()
