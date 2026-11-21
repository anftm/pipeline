import io
import unittest
from hashlib import sha256
from types import SimpleNamespace

from scripts.reader_image_history_publish_s3 import CATALOG, SIDECAR, LegacyRoots, Observed


class Client:
    def __init__(self):
        self.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts': 1}))
        self.requests = []
        self.raw, self.tag, self.lost = b'old', '"old"', False

    def get_object(self, **attrs):
        self.requests.append(('get', attrs))
        self.body = io.BytesIO(self.raw)
        return dict(Body=self.body, ETag=self.tag, ContentLength=len(self.raw),
                    ResponseMetadata=dict(HTTPStatusCode=200))

    def put_object(self, **attrs):
        self.requests.append(('put', attrs))
        assert attrs['IfMatch'] == self.tag
        self.raw, self.tag = attrs['Body'].read(), '"new"'
        if self.lost:
            raise TimeoutError('lost response after write')
        return dict(ETag=self.tag, ResponseMetadata=dict(HTTPStatusCode=200))


class LegacyRootsTests(unittest.TestCase):
    def test_exact_stream_cas_and_independent_readback(self):
        c = Client()
        roots = LegacyRoots(c)
        raw = b'new' * (1 << 20)
        stream = io.BytesIO(raw)
        dg = sha256(raw).hexdigest()
        self.assertEqual(roots.observe(CATALOG), Observed(sha256(b'old').hexdigest(), 3, '"old"'))
        self.assertTrue(c.body.closed)
        self.assertEqual(roots.put_once(CATALOG, stream, size=len(raw), digest=dg, parent_etag='"old"'), '"new"')
        self.assertEqual(c.requests[1], ('put', dict(Bucket='reader-assets-v2', Key=CATALOG, Body=stream,
                                                     ContentLength=len(raw), ContentType='application/json',
                                                     IfMatch='"old"')))
        self.assertEqual(roots.observe(CATALOG), Observed(dg, len(raw), '"new"'))

    def test_bad_bytes_path_and_retry_client_fail_before_put(self):
        c = Client()
        roots = LegacyRoots(c)
        for path, size, etag in ((CATALOG, 2, '"old"'), (CATALOG, 4, '"old"'), (CATALOG, 3, 'weak'),
                                 ('reader-index/v3/current.json', 3, '"old"'), (SIDECAR, 3, '"old"')):
            with self.subTest(path=path, size=size, etag=etag), self.assertRaises(ValueError):
                roots.put_once(path, io.BytesIO(b'old'), size=size, digest='f' * 64, parent_etag=etag)
        self.assertFalse(c.requests)
        c.meta.config.retries['total_max_attempts'] = 2
        with self.assertRaises(ValueError):
            LegacyRoots(c)

    def test_lost_put_response_is_not_replayed(self):
        c = Client()
        c.lost = True
        roots = LegacyRoots(c)
        dg = sha256(b'new').hexdigest()
        with self.assertRaises(TimeoutError):
            roots.put_once(SIDECAR, io.BytesIO(b'new'), size=3, digest=dg, parent_etag='"old"')
        self.assertEqual([k for k, _ in c.requests], ['put'])
        self.assertEqual(roots.observe(SIDECAR), Observed(dg, 3, '"new"'))
        self.assertEqual(c.requests[0][1]['ContentType'], 'application/gzip')
