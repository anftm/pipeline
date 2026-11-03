import io
import hashlib
import unittest
from unittest.mock import Mock

from scripts import reader_producer_audit_store as store


PATH = ('reader-index/gc/producer-scope-transition/v2/receipts/' + hashlib.sha256(b'payload').hexdigest() + '.json')


class S3Error(Exception):
    def __init__(self, code, status):
        self.response = {'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}


class AuditStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.store = store.AuditStore(self.client)

    def test_only_fixed_content_addressed_paths_are_allowed(self):
        for path in ('reader-index/gc/other/a.json', PATH + '/more', '../' + PATH,
                     'reader-index/reader_catalog.json'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.store.create(path, b'content')
        self.client.put_object.assert_not_called()

    def test_missing_key_requires_successful_head_bucket(self):
        self.client.get_object.side_effect = S3Error('NoSuchKey', 404)
        with self.assertRaises(FileNotFoundError):
            self.store.read(PATH)
        self.client.head_bucket.assert_called_once_with(Bucket=store.BUCKET)

    def test_missing_bucket_and_denied_reads_are_never_absence(self):
        for code, status in (('NoSuchBucket', 404), ('AccessDenied', 403)):
            self.client.reset_mock()
            self.client.get_object.side_effect = S3Error(code, status)
            with self.subTest(code=code), self.assertRaises(S3Error):
                self.store.read(PATH)
            self.client.head_bucket.assert_not_called()

    def test_create_is_conditional_and_single_attempt(self):
        self.client.put_object.return_value = {'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 0}}
        self.store.create(PATH, b'payload')
        self.client.put_object.assert_called_once_with(Bucket=store.BUCKET, Key=PATH, Body=b'payload',
                                                       ContentType='application/json', IfNoneMatch='*')

    def test_ambiguous_create_is_not_retried(self):
        self.client.put_object.return_value = {'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 1}}
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            self.store.create(PATH, b'payload')
        self.client.put_object.assert_called_once()

    def test_read_returns_actual_bytes(self):
        self.client.get_object.return_value = {'Body': io.BytesIO(b'contents')}
        self.assertEqual(self.store.read(PATH), b'contents')


if __name__ == '__main__':
    unittest.main()
