import gzip
import hashlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from botocore.config import Config
from botocore.exceptions import ClientError

from scripts import reader_page_history_store as history
from scripts import reader_page_history_publish as publish
from scripts import publish_reader_v3 as publication


ASSETS = publication.ASSETS
ROOT = history.RETIREMENT


def response(raw=b'root', etag='"actual"'):
    return {'Body': io.BytesIO(raw), 'ContentLength': len(raw), 'ETag': etag}


def error(code='NoSuchKey', status=404):
    return ClientError({'Error': {'Code': code},
                        'ResponseMetadata': {'HTTPStatusCode': status}}, 'Object')


def snapshot(raw=None):
    if raw is None:
        raw = gzip.compress(publication.encode({'v': 1, 'f': {}}), mtime=0)
    return f'reader-index/snapshots/{hashlib.sha256(raw).hexdigest()}/reader_assets.json.gz', raw


def catalog(target='a' * 64):
    value = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
             'rollback_target': target}
    value['generation'] = publication.text.digest(value)
    return (history.PREFIX + 'b' * 64 + '/' + value['generation'] + '/catalog.json',
            publication.encode(value))


def intent(raw=None):
    if raw is None:
        raw = publication.encode({'version': 1, 'kind': 'reader-derived-pdf-history-publish-intent',
                                  'deletion_authorized': False})
    return history.INTENTS + hashlib.sha256(raw).hexdigest() + '.json', raw


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.s3 = Mock()
        self.s3._location.return_value = ('vomebook', 'reader-assets-v2')
        self.s3._client.return_value = self.client
        self.primary = Mock(spec=['api', 'read_bytes', 'list_files', 'assert_serialized_writer'])
        self.store = history.ConditionalPageHistoryStore(self.primary, self.s3)

    def test_reads_listings_api_and_writer_assertion_delegate_to_primary(self):
        self.assertIs(self.store.read_bytes(ASSETS, ROOT), self.primary.read_bytes.return_value)
        self.primary.read_bytes.assert_called_once_with(ASSETS, ROOT)
        prefixes = ('reader-index/v3/',)
        self.assertIs(self.store.list_files(ASSETS, prefixes), self.primary.list_files.return_value)
        self.primary.list_files.assert_called_once_with(ASSETS, prefixes)
        self.assertIs(self.store.api, self.primary.api)
        with patch.object(publication, 'assert_writer', wraps=publication.assert_writer) as assertion:
            publication.assert_writer(self.store)
        self.assertEqual(assertion.call_count, 2)
        self.primary.assert_serialized_writer.assert_called_once_with(publication.PROTOCOL)
        self.primary.assert_serialized_writer.side_effect = RuntimeError('writer window missing')
        with self.assertRaisesRegex(RuntimeError, 'writer window missing'):
            self.store.assert_serialized_writer(publication.PROTOCOL)
        with self.assertRaisesRegex(ValueError, 'protocol'):
            self.store.assert_serialized_writer('foreign')
        self.s3._client.assert_not_called()
        for method in ('put_bytes', 'put_json', 'delete', 'put_files'):
            self.assertFalse(hasattr(self.store, method))

    def test_exact_get_body_and_etag_and_conditional_update(self):
        raw = b'{ "resources": [] }\n'
        result = response(raw)
        self.client.get_object.return_value = result
        self.assertEqual(self.store.read_for_update(ASSETS, ROOT), (raw, '"actual"'))
        self.assertTrue(result['Body'].closed)
        self.client.get_object.assert_called_once_with(Bucket='reader-assets-v2', Key=ROOT)
        self.store.put_if_match(ASSETS, ROOT, b'new', '"actual"')
        self.client.put_object.assert_called_once_with(Bucket='reader-assets-v2', Key=ROOT,
            Body=b'new', ContentType='application/json', IfMatch='"actual"')
        self.primary.read_bytes.assert_not_called()
        self.s3.read_bytes.assert_not_called()

    def test_404_means_absence_and_none_token_means_atomic_creation(self):
        self.client.get_object.side_effect = error()
        self.assertEqual(self.store.read_for_update(ASSETS, ROOT), (None, None))
        self.store.put_if_match(ASSETS, ROOT, b'new', None)
        self.client.put_object.assert_called_once_with(Bucket='reader-assets-v2', Key=ROOT,
            Body=b'new', ContentType='application/json', IfNoneMatch='*')

    def test_read_errors_are_not_absence_or_retried(self):
        for failure in (error('AccessDenied', 403), error('NoSuchBucket', 404),
                        error('InternalError', 500), OSError('lost response')):
            with self.subTest(failure=str(failure)):
                self.client.get_object.reset_mock()
                self.client.get_object.side_effect = failure
                with self.assertRaises(type(failure)):
                    self.store.read_for_update(ASSETS, ROOT)
                self.client.get_object.assert_called_once()

    def test_missing_or_weak_etags_never_reuse_previous_get_validator(self):
        self.client.get_object.return_value = response()
        self.store.read_for_update(ASSETS, ROOT)
        for etag in (None, '', 'unquoted', 'W/"weak"', '""', '"bad\r\n"'):
            with self.subTest(etag=etag):
                self.client.get_object.return_value = response(etag=etag)
                with self.assertRaisesRegex(ValueError, 'exact strong'):
                    self.store.read_for_update(ASSETS, ROOT)
                if etag is not None:
                    with self.assertRaisesRegex(ValueError, 'exact strong'):
                        self.store.put_if_match(ASSETS, ROOT, b'new', etag)
        self.client.put_object.assert_not_called()

    def test_incomplete_get_fails_and_closes_body(self):
        for length in (None, True, -1, 5):
            with self.subTest(length=length):
                result = response()
                result['ContentLength'] = length
                self.client.get_object.return_value = result
                with self.assertRaises((ValueError, OSError)):
                    self.store.read_for_update(ASSETS, ROOT)
                self.assertTrue(result['Body'].closed)

    def test_every_other_mutable_root_and_bucket_is_blocked_before_io(self):
        paths = (publication.POINTER, 'reader-index/reader_assets.json.gz',
                 'reader-index/reader_catalog.json', 'reader-index/derived_pdf_manifest.json',
                 '/' + ROOT, ROOT + '/x', ROOT + '\n', 'reader-index/v3/../v3/retirement.json')
        pairs = [(ASSETS, path) for path in paths] + [('other/reader-assets-v2', ROOT)]
        for bucket, path in pairs:
            with self.subTest(bucket=bucket, path=path):
                with self.assertRaisesRegex(ValueError, 'outside'):
                    self.store.read_for_update(bucket, path)
                with self.assertRaisesRegex(ValueError, 'outside'):
                    self.store.put_if_match(bucket, path, b'new', None)
        with self.assertRaisesRegex(ValueError, 'requires bytes'):
            self.store.put_if_match(ASSETS, ROOT, {}, None)
        self.s3._location.assert_not_called()

    def test_all_three_immutable_families_create_once_and_accept_identical_bytes(self):
        for path, raw in (snapshot(), catalog(), intent()):
            with self.subTest(path=path):
                self.client.reset_mock()
                self.client.get_object.side_effect = [error(), response(raw)]
                self.store.immutable_put(ASSETS, path, raw)
                self.client.put_object.assert_called_once_with(Bucket='reader-assets-v2', Key=path,
                    Body=raw, ContentType='application/gzip' if path.endswith('.gz') else 'application/json',
                    IfNoneMatch='*')
                self.client.put_object.reset_mock()
                self.client.get_object.side_effect = [response(raw)]
                self.store.immutable_put(ASSETS, path, raw)
                self.client.put_object.assert_not_called()

    def test_intent_namespace_matches_publisher_and_rejects_old_root_before_io(self):
        self.assertEqual(history.INTENTS, 'reader-index/gc/pdf-history-replacements/')
        self.assertEqual(history.INTENTS, publish.INTENTS)
        path, raw = intent()
        ref = publication.metadata(ASSETS, path, raw)
        self.client.get_object.side_effect = [error(), response(raw)]
        self.primary.read_bytes.return_value = raw
        publish._immutable(self.store, ref, raw)
        self.client.put_object.assert_called_once_with(Bucket='reader-assets-v2', Key=path,
            Body=raw, ContentType='application/json', IfNoneMatch='*')
        self.s3.reset_mock()
        old_path = ('reader-index/v3/history-replacements/derived-pdf-intents/' +
                    hashlib.sha256(raw).hexdigest() + '.json')
        with self.assertRaisesRegex(ValueError, 'outside allowlist'):
            self.store.immutable_put(ASSETS, old_path, raw)
        self.s3._location.assert_not_called()

    def test_existing_immutable_conflict_is_never_overwritten(self):
        for path, raw in (snapshot(), catalog(), intent()):
            with self.subTest(path=path):
                self.client.get_object.return_value = response(b'foreign')
                with self.assertRaisesRegex(ValueError, 'immutable history object conflict'):
                    self.store.immutable_put(ASSETS, path, raw)
        self.client.put_object.assert_not_called()

    def test_immutable_create_race_accepts_only_identical_readback_without_retry(self):
        path, raw = snapshot()
        for code, status in (('ConditionalRequestConflict', 409), ('PreconditionFailed', 412)):
            for observed in (raw, b'foreign'):
                with self.subTest(code=code, identical=observed == raw):
                    self.client.reset_mock()
                    self.client.get_object.side_effect = [error(), response(observed)]
                    self.client.put_object.side_effect = error(code, status)
                    if observed == raw:
                        self.store.immutable_put(ASSETS, path, raw)
                    else:
                        with self.assertRaisesRegex(ValueError, 'conflict'):
                            self.store.immutable_put(ASSETS, path, raw)
                    self.client.put_object.assert_called_once()

    def test_unknown_immutable_write_outcome_is_propagated_without_readback_or_retry(self):
        path, raw = intent()
        self.client.get_object.side_effect = error()
        self.client.put_object.side_effect = OSError('response lost')
        with self.assertRaisesRegex(OSError, 'response lost'):
            self.store.immutable_put(ASSETS, path, raw)
        self.client.put_object.assert_called_once()
        self.client.get_object.assert_called_once()

    def test_immutable_path_digest_and_encoding_rejections_precede_io(self):
        s_path, s_raw = snapshot()
        c_path, c_raw = catalog()
        i_path, i_raw = intent()
        invalid = [
            (ASSETS, ROOT, s_raw),
            (ASSETS, publication.POINTER, c_raw),
            (ASSETS, c_path.replace(history.PREFIX + 'b' * 64 + '/', 'reader-index/v3/generations/'), c_raw),
            (ASSETS, 'reader-index/v3/promotions/' + 'a' * 64 + '.json', i_raw),
            ('other/reader-assets-v2', s_path, s_raw),
            (ASSETS, '/' + s_path, s_raw), (ASSETS, s_path + '\n', s_raw),
            (ASSETS, s_path.replace('/snapshots/', '/snapshots/../snapshots/'), s_raw),
            (ASSETS, s_path, s_raw + b'x'),
            (ASSETS, c_path, c_raw.replace(b'"files":{}', b'"files":{"x":{}}')),
            (ASSETS, c_path.replace(json.loads(c_raw)['generation'], 'c' * 64), c_raw),
            (ASSETS, c_path.replace('b' * 64, 'invalid'), c_raw),
            (ASSETS, c_path, c_raw + b'\n'),
            (ASSETS, i_path, i_raw + b' '),
            (ASSETS, i_path.replace(hashlib.sha256(i_raw).hexdigest(), 'c' * 64), i_raw),
            (ASSETS, *intent(i_raw + b'\n')),
            (ASSETS, *intent(b'{"x":1,"x":1}')),
            (ASSETS, *intent(b'{"x":NaN}')),
            (ASSETS, *intent(b'[]')),
            (ASSETS, *snapshot(b'not gzip')),
            (ASSETS, *snapshot(gzip.compress(b'{"v":2,"f":{}}'))),
            (ASSETS, s_path, bytearray(s_raw)),
        ]
        for bucket, path, raw in invalid:
            with self.subTest(path=path, raw=raw[:60]):
                with self.assertRaises((ValueError, OSError)):
                    self.store.immutable_put(bucket, path, raw)
        self.s3._location.assert_not_called()

    def test_catalog_rollback_target_must_be_valid_but_may_differ_from_old_path(self):
        for target in (None, '', 'invalid', 'A' * 64, 1):
            path, raw = catalog(target)
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'SHA-256'):
                self.store.immutable_put(ASSETS, path, raw)
        path, raw = catalog()
        value = json.loads(raw)
        del value['rollback_target']
        del value['generation']
        value['generation'] = publication.text.digest(value)
        path = history.PREFIX + 'b' * 64 + '/' + value['generation'] + '/catalog.json'
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.store.immutable_put(ASSETS, path, publication.encode(value))
        self.s3._location.assert_not_called()

    def test_cas_conflicts_and_unknown_outcomes_are_never_retried(self):
        for token in (None, '"before"'):
            for failure in (error('PreconditionFailed', 412), error('ConditionalRequestConflict', 409),
                            error('InternalError', 500), OSError('response lost')):
                with self.subTest(token=token, failure=str(failure)):
                    self.client.put_object.reset_mock()
                    self.client.put_object.side_effect = failure
                    expected = RuntimeError if history._conflict(failure) else type(failure)
                    with self.assertRaises(expected):
                        self.store.put_if_match(ASSETS, ROOT, b'after', token)
                    self.client.put_object.assert_called_once()
        self.client.get_object.assert_not_called()
        self.primary.read_bytes.assert_not_called()

    def test_default_s3_client_disables_transport_retries_and_checks_namespace(self):
        s3 = Mock()
        s3._namespace = 'vomebook'
        s3._config = Config(region_name='us-east-1', retries={'total_max_attempts': 9})
        with patch.object(history, 'S3BucketStore', return_value=s3):
            history.ConditionalPageHistoryStore(self.primary)
        self.assertEqual(s3._config.retries['total_max_attempts'], 1)
        self.assertEqual(s3._config.request_checksum_calculation, 'when_required')
        self.assertEqual(s3._config.response_checksum_validation, 'when_required')
        s3._client.assert_not_called()
        s3._namespace = 'other'
        with patch.object(history, 'S3BucketStore', return_value=s3):
            with self.assertRaisesRegex(ValueError, 'vomebook S3 namespace'):
                history.ConditionalPageHistoryStore(self.primary)


if __name__ == '__main__':
    unittest.main()
