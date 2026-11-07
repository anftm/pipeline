import io
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from scripts import reader_page_cas as cas
from scripts.reader_bucket_store import S3BucketStore


ASSETS = 'vomebook/reader-assets-v2'
ROOT = 'reader-index/reader_assets.json.gz'


class PageCASTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.s3 = S3BucketStore.__new__(S3BucketStore)
        self.s3._location = Mock(return_value=('vomebook', 'reader-assets-v2'))
        self.s3._client = Mock(return_value=self.client)
        self.primary = Mock()
        self.store = cas.ConditionalPageStore(self.primary, self.s3)

    def read_response(self, etag='"actual"'):
        return {'Body': io.BytesIO(b'root'), 'ContentLength': 4, 'ETag': etag}

    def test_get_returns_etag_of_exact_bytes_and_put_uses_if_match(self):
        self.client.get_object.return_value = self.read_response()
        raw, etag = self.store.read_for_update(ASSETS, ROOT)
        self.assertEqual((raw, etag), (b'root', '"actual"'))
        self.store.put_if_match(ASSETS, ROOT, b'new', etag)
        self.client.put_object.assert_called_once_with(Bucket='reader-assets-v2', Key=ROOT,
            Body=b'new', ContentType='application/gzip', IfMatch='"actual"')

    def test_missing_weak_or_malformed_etag_and_unknown_root_fail_closed(self):
        for etag in (None, '', 'unquoted', 'W/"weak"', '""', '"bad\r\n"'):
            with self.subTest(etag=etag):
                self.client.get_object.return_value = self.read_response(etag)
                with self.assertRaisesRegex(ValueError, 'exact strong'):
                    self.store.read_for_update(ASSETS, ROOT)
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.store.read_for_update(ASSETS, 'other/path')
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.store.put_if_match(ASSETS, 'other/path', b'x', '"v"')
        self.client.put_object.assert_not_called()

    def test_prior_etag_never_survives_an_unversioned_response(self):
        self.client.get_object.side_effect = [self.read_response(), self.read_response(None)]
        self.store.read_for_update(ASSETS, ROOT)
        with self.assertRaisesRegex(ValueError, 'exact strong'):
            self.store.read_for_update(ASSETS, ROOT)

    def test_preflight_checks_every_root_and_hub_mismatch_fails_without_write(self):
        self.client.get_object.side_effect = lambda **kwargs: self.read_response()
        self.primary.read_bytes.return_value = b'root'
        self.store.preflight()
        self.assertEqual(self.client.get_object.call_count, len(cas.ROOTS))
        self.assertEqual(self.primary.read_bytes.call_count, len(cas.ROOTS))
        self.primary.read_bytes.return_value = b'changed'
        with self.assertRaisesRegex(ValueError, 'differs between S3 and Hub'):
            self.store.preflight()
        self.client.put_object.assert_not_called()

    def test_precondition_failure_and_transport_uncertainty_are_not_retried(self):
        for code, status in [('PreconditionFailed', 412), ('ConditionalRequestConflict', 409)]:
            self.client.put_object.reset_mock()
            self.client.put_object.side_effect = ClientError(
                {'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'PutObject')
            with self.subTest(code=code), self.assertRaisesRegex(RuntimeError, 'changed during conditional write'):
                self.store.put_if_match(ASSETS, ROOT, b'new', '"old"')
            self.client.put_object.assert_called_once()
        self.client.put_object.reset_mock()
        self.client.put_object.side_effect = OSError('response lost')
        with self.assertRaisesRegex(OSError, 'response lost'):
            self.store.put_if_match(ASSETS, ROOT, b'new', '"old"')
        self.client.put_object.assert_called_once()

    def test_default_client_has_single_put_attempt_and_hf_s3_checksum_settings(self):
        s3 = Mock()
        from botocore.config import Config
        s3._namespace = 'vomebook'
        s3._config = Config(region_name='us-east-1', retries={'total_max_attempts': 9})
        with patch('scripts.reader_page_cas.S3BucketStore', return_value=s3):
            cas.ConditionalPageStore(self.primary)
        self.assertEqual(s3._config.retries['total_max_attempts'], 1)
        self.assertEqual(s3._config.request_checksum_calculation, 'when_required')
        self.assertEqual(s3._config.response_checksum_validation, 'when_required')

    def test_remapped_input_namespace_cannot_supply_the_page_writer(self):
        s3 = Mock()
        s3._namespace = 'other'
        with patch('scripts.reader_page_cas.S3BucketStore', return_value=s3):
            with self.assertRaisesRegex(ValueError, 'vomebook S3 namespace'):
                cas.ConditionalPageStore(self.primary)


if __name__ == '__main__':
    unittest.main()
