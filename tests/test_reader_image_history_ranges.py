import io
import unittest
from unittest.mock import patch

import httpx

from scripts import reader_image_history_ranges as ranges


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.raw = b'0123456789abcdefghijklmnopqrstuvwxy'
        self.calls = []
        self.bad = {}
        self.p = patch.multiple(ranges, BLOCK=8, WORKERS=2)
        self.p.start()
        self.addCleanup(self.p.stop)

    def get_object(self, **args):
        lo, hi = map(int, args['Range'].removeprefix('bytes=').split('-'))
        self.calls.append((lo, hi, args['IfMatch']))
        return dict(ResponseMetadata={'HTTPStatusCode': 206}, ETag='"reviewed"',
                    ContentRange=f'bytes {lo}-{hi}/{len(self.raw)}', ContentLength=hi-lo+1,
                    Body=io.BytesIO(self.raw[lo:hi+1])) | self.bad

    def read(self):
        with ranges.open_catalog(self, size=len(self.raw), etag='"reviewed"') as stream:
            self.assertEqual(stream.read(0), b'')
            return b''.join(iter(lambda: stream.read(3), b''))

    def test_reads_exact_order_and_pins_each_request(self):
        self.assertEqual(self.read(), self.raw)
        self.assertEqual(sorted(self.calls), [(n, min(n+7, len(self.raw)-1), '"reviewed"')
                                             for n in range(0, len(self.raw), 8)])

    def test_rejects_different_version_or_ignored_range(self):
        for bad in ({'ETag': '"foreign"'}, {'ContentRange': 'foreign'}, {'ResponseMetadata': {'HTTPStatusCode': 200}}):
            self.bad = bad
            with self.assertRaisesRegex(ValueError, 'reviewed version'):
                self.read()

    def test_only_transient_range_reads_repeat(self):
        real = self.get_object
        attempts = []
        def lose_once_then_read(**args):
            if args['Range'] == 'bytes=0-7':
                attempts.append(args['IfMatch'])
                if len(attempts) == 1:
                    raise OSError('transient')
            return real(**args)
        with patch.object(self, 'get_object', side_effect=lose_once_then_read):
            self.assertEqual(self.read(), self.raw)
        self.assertEqual(attempts, ['"reviewed"', '"reviewed"'])

    def test_independent_resolver_hash_and_weak_or_foreign_etag(self):
        tag = '"reviewed"'
        def handle(request):
            self.assertEqual(request.headers['If-Match'], '"reviewed"')
            lo, hi = map(int, request.headers['Range'].removeprefix('bytes=').split('-'))
            return httpx.Response(206, content=self.raw[lo:hi+1], headers={
                'Content-Range': f'bytes {lo}-{hi}/{len(self.raw)}', 'ETag': tag})
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            run = lambda: ranges.resolver_digest(http, 'https://example.org/catalog', size=len(self.raw), etag='"reviewed"')
            self.assertEqual(run(), ranges.sha256(self.raw).hexdigest())
            tag = 'W/"reviewed"'
            with self.assertRaisesRegex(ValueError, 'resolver range'):
                run()
