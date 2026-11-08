"""Four-source status and exact CW:0 bytes must both be observed."""
import hashlib
import json
import unittest
from unittest.mock import Mock

from scripts import ccrd_consumer_inspection as check

GEN = 'ccrd-four-' + 'a' * 64
TEXT = '源文本\n'.encode()
EXPECTED = {'bytes': len(TEXT), 'sha256': hashlib.sha256(TEXT).hexdigest()}


def status(**changes):
    value = {'ready': True, 'state': 'ready', 'loaded_sources': ['CCRD', 'CW', 'RMRB', 'PLA'],
             'building_sources': [], 'bucket': {'state': 'ready', 'generation': GEN, 'pointer_error': None,
                                                'base_metadata_sha256': 'b' * 64}, **changes}
    return json.dumps(value).encode()


class CcrdConsumerInspectionTests(unittest.TestCase):
    def test_ready_twice_and_exact_download_bytes(self):
        calls = []
        def get(url, limit):
            calls.append((url, limit))
            return TEXT if url.endswith('/CW:0') else status()
        result = check.observe(GEN, EXPECTED, get=get)
        self.assertEqual(result['cw0'], EXPECTED)
        self.assertTrue(result['cw0_bytes_verified'])
        self.assertTrue(result['status_observed_twice'])
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in calls], ['index-status', 'CW:0', 'index-status'])

    def test_wrong_text_or_unknown_status_stops(self):
        bad = ({'ready': False}, {'state': 'building'}, {'building_sources': ['CW']},
               {'loaded_sources': ['CCRD', 'CW']}, {'bucket': {'state': 'ready', 'generation': 'other'}},
               {'bucket': {'state': 'ready', 'generation': GEN, 'pointer_error': 'unknown'}})
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                check.observe(GEN, EXPECTED, get=lambda url, _limit, changes=changes: status(**changes) if 'index-status' in url else TEXT)
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            check.observe(GEN, EXPECTED, get=lambda url, _limit: status() if 'index-status' in url else b'wrong')
        with self.assertRaises(ValueError):
            check.observe(GEN, EXPECTED, get=lambda *_: b'not json')

    def test_changing_consumer_status_fails_closed(self):
        values = iter([status(), TEXT, status(bucket={'state': 'ready', 'generation': GEN, 'pointer_error': None,
                                                    'base_metadata_sha256': 'c' * 64})])
        with self.assertRaisesRegex(RuntimeError, 'status changed'):
            check.observe(GEN, EXPECTED, get=lambda *_: next(values))

    def test_bounded_http_uses_get_only_and_rejects_oversized(self):
        response = Mock()
        response.status = 200
        response.read.return_value = b'abc'
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *_args: None
        opener = Mock(return_value=response)
        old = check.urllib.request.urlopen
        try:
            check.urllib.request.urlopen = opener
            self.assertEqual(check.get_bytes('https://example.invalid', 3), b'abc')
            self.assertEqual(opener.call_args.args[0].get_method(), 'GET')
            response.read.return_value = b'abcd'
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                check.get_bytes('https://example.invalid', 3)
        finally:
            check.urllib.request.urlopen = old


if __name__ == '__main__':
    unittest.main()
