"""EPUB immutable create is bounded, conditional and read-after-write."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from scripts import reader_epub_immutable as subject

ROOT = 'chapters/ebook/epub/' + 'a' * 64 + '/' + 'b' * 16
MEMBER = 'chapters/chapter-0001.xhtml'
BODY = b'<html>complete chapter</html>'
IDENTITY = {'sha256': hashlib.sha256(BODY).hexdigest(), 'bytes': len(BODY)}


class EpubImmutableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'chapter';self.path.write_bytes(BODY)
        self.raw, self.client = Mock(), Mock()
        self.raw._location.return_value = ('vomebook', 'reader-assets-v2')
        self.raw._client.return_value = self.client
        self.store = subject.S3EpubObjects(self.raw)

    def response(self, raw=BODY, length=None):
        return {'Body': io.BytesIO(raw), 'ContentLength': len(raw) if length is None else length}

    def test_conditional_create_streams_readback_and_never_overwrites(self):
        self.client.get_object.return_value = self.response()
        self.assertEqual(self.store.create_once(ROOT, MEMBER, self.path, IDENTITY), 'written')
        put = self.client.put_object.call_args.kwargs
        self.assertEqual(put['IfNoneMatch'], '*')
        self.assertEqual((put['Bucket'], put['Key'], put['ContentLength']),
                         ('reader-assets-v2', ROOT + '/' + MEMBER, len(BODY)))
        self.client.put_object.assert_called_once()
        self.client.get_object.assert_called_once_with(Bucket='reader-assets-v2', Key=ROOT + '/' + MEMBER)

    def test_unknown_response_reads_exact_object_once_without_retry(self):
        self.client.put_object.side_effect = TimeoutError('lost')
        self.client.get_object.return_value = self.response()
        self.assertEqual(self.store.create_once(ROOT, MEMBER, self.path, IDENTITY), 'written-response-unknown')
        self.client.put_object.assert_called_once();self.client.get_object.assert_called_once()
        self.client.put_object.reset_mock();self.client.get_object.reset_mock()
        self.client.get_object.return_value = self.response(b'foreign')
        with self.assertRaisesRegex(RuntimeError, 'inspect before recovery'):
            self.store.create_once(ROOT, MEMBER, self.path, IDENTITY)
        self.client.put_object.assert_called_once();self.client.get_object.assert_called_once()

    def test_missing_key_only_is_absent_and_access_errors_fail(self):
        for code, result in [('NoSuchKey', None), ('NotFound', None)]:
            self.client.get_object.side_effect = ClientError({'Error': {'Code': code}}, 'GetObject')
            self.assertIs(self.store.inspect(ROOT, MEMBER), result)
        for code in ('NoSuchBucket', 'AccessDenied', 'SlowDown'):
            self.client.get_object.side_effect = ClientError({'Error': {'Code': code}}, 'GetObject')
            with self.subTest(code=code), self.assertRaises(ClientError): self.store.inspect(ROOT, MEMBER)

    def test_readback_must_have_exact_bounded_length(self):
        for raw, length in ((b'truncated', 99), (b'longer', 3), (BODY, None)):
            response = {'Body': io.BytesIO(raw), 'ContentLength': length}
            self.client.get_object.return_value = response
            with self.subTest(raw=raw, length=length), self.assertRaises(ValueError):
                self.store.inspect(ROOT, MEMBER)
            self.assertTrue(response['Body'].closed)

    def test_input_or_path_drift_and_foreign_roots_never_call_put(self):
        for root, path in ((ROOT, '../secret'), (ROOT, '/root'), (ROOT, 'native.epub'),
                           ('reader-index/v3/current.json', MEMBER), (ROOT, 'a//b'), (ROOT, 'a\\b')):
            with self.subTest(root=root, path=path), self.assertRaises(ValueError):
                self.store.create_once(root, path, self.path, IDENTITY)
        self.path.write_bytes(b'other')
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.store.create_once(ROOT, MEMBER, self.path, IDENTITY)
        self.client.put_object.assert_not_called()

    def test_conflict_or_corrupt_success_keeps_old_object_and_never_retries(self):
        conflict = ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        self.client.put_object.side_effect = conflict;self.client.get_object.return_value = self.response(b'old')
        with self.assertRaisesRegex(RuntimeError, 'conflicted'):
            self.store.create_once(ROOT, MEMBER, self.path, IDENTITY)
        self.client.put_object.assert_called_once()
        self.client.put_object.reset_mock();self.client.put_object.side_effect = None
        self.client.get_object.return_value = self.response(b'old')
        with self.assertRaisesRegex(RuntimeError, 'readback differs'):
            self.store.create_once(ROOT, MEMBER, self.path, IDENTITY)
        self.client.put_object.assert_called_once()

    def test_missing_local_symlink_oversize_and_default_one_attempt(self):
        with self.assertRaises(ValueError):subject.local_identity(Path(self.temp.name) / 'missing')
        link = Path(self.temp.name) / 'link';link.symlink_to(self.path)
        with self.assertRaises(ValueError):subject.local_identity(link)
        with patch.object(subject, 'MAX_OBJECT_BYTES', 1), self.assertRaises(ValueError):
            subject.local_identity(self.path)
        from botocore.config import Config
        raw = Mock();raw._namespace = 'vomebook';raw._config = Config(retries={'total_max_attempts': 8})
        with patch.object(subject, 'S3BucketStore', return_value=raw):subject.S3EpubObjects()
        self.assertEqual(raw._config.retries['total_max_attempts'], 1)


if __name__ == '__main__': unittest.main()
