"""A CCRD source snapshot must be bounded and pinned on both sides."""
import hashlib
import unittest
import urllib.error
from unittest.mock import Mock, patch

from scripts import ccrd_source_snapshot as snapshot

REV = 'a' * 40


class CcrdSourceSnapshotTests(unittest.TestCase):
    def test_capture_selects_exact_revision_and_checks_head_twice(self):
        calls = []
        def revision(_token): calls.append('head'); return REV
        def read(url): calls.append(url); return '文本'.encode()
        raw, report = snapshot.capture('token', revision=revision, read=read)
        self.assertEqual(calls[0], 'head'); self.assertEqual(calls[-1], 'head')
        self.assertIn('/' + REV + '/', report['url'])
        self.assertEqual(report['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(report['source_stable']); self.assertEqual(report['remote_writes'], 0)

    def test_fallback_only_on_404_and_no_success_for_drift(self):
        calls = []
        def fallback(url):
            calls.append(url)
            if len(calls) == 1: raise urllib.error.HTTPError(url, 404, 'missing', {}, None)
            return b'text'
        raw, report = snapshot.capture('token', revision=lambda _: REV, read=fallback)
        self.assertEqual(raw, b'text'); self.assertEqual(len(calls), 2)
        self.assertIn('/' + REV + '/', report['url'])
        revisions = iter([REV, 'b' * 40])
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            snapshot.capture('token', revision=lambda _: next(revisions), read=lambda _: b'text')
        with self.assertRaises(urllib.error.HTTPError):
            snapshot.capture('token', revision=lambda _: REV,
                             read=lambda url: (_ for _ in ()).throw(urllib.error.HTTPError(url, 503, 'busy', {}, None)))

    def test_no_source_or_invalid_bytes_fails_closed(self):
        def missing(url): raise urllib.error.HTTPError(url, 404, 'missing', {}, None)
        with self.assertRaisesRegex(ValueError, 'none of the approved'):
            snapshot.capture('token', revision=lambda _: REV, read=missing)
        for raw in (b'', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeDecodeError)):
                snapshot.capture('token', revision=lambda _: REV, read=lambda _url, raw=raw: raw)

    def test_http_reads_are_bounded(self):
        response = Mock(); response.status = 200; response.read.return_value = b'bad'
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *_args: None
        with patch.object(snapshot.urllib.request, 'urlopen', return_value=response):
            with self.assertRaises(ValueError): snapshot.read_revision('token')
            with patch.object(snapshot.source, 'MAX_OVERRIDE_BYTES', 2), self.assertRaisesRegex(ValueError, 'oversized'):
                snapshot.read_text('https://example.invalid')


if __name__ == '__main__':
    unittest.main()
