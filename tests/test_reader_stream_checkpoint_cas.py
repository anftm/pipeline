"""Reader stream index writes are single-root, conditional and bounded."""
import io
import json
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from scripts import reader_stream_checkpoint_cas as subject
from scripts.reader_bucket_store import S3BucketStore

BUCKET = 'vomebook/reader-assets-v2'
INDEX = 'chapters/ebook/epub/index-00.json'
OP = 'a' * 32
VALUE = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': [], 'failures': [],
         'checkpoint_operation': OP}
RAW = (json.dumps(VALUE, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


class ConditionalCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.s3 = S3BucketStore.__new__(S3BucketStore)
        self.s3._location = Mock(return_value=('vomebook', 'reader-assets-v2'))
        self.s3._client = Mock(return_value=self.client)
        self.store = subject.ConditionalCheckpointStore(self.s3)

    @staticmethod
    def response(raw=RAW, etag='"v1"'):
        return {'Body': io.BytesIO(raw), 'ContentLength': len(raw), 'ETag': etag}

    @staticmethod
    def missing():
        return ClientError({'Error': {'Code': 'NoSuchKey'}, 'ResponseMetadata': {'HTTPStatusCode': 404}}, 'GetObject')

    def test_existing_parent_uses_exact_validator_and_reads_back(self):
        self.client.get_object.side_effect = [self.response(), self.response()]
        snapshot = self.store.read(BUCKET, INDEX)
        self.assertEqual((snapshot.value, snapshot.etag), (VALUE, '"v1"'))
        self.assertEqual(self.store.write_once(BUCKET, INDEX, snapshot.value, snapshot.etag, operation_id=OP), 'written')
        self.client.put_object.assert_called_once_with(Bucket='reader-assets-v2', Key=INDEX, Body=RAW,
                                                       ContentType='application/json', IfMatch='"v1"')

    def test_absent_create_is_conditional_and_permission_is_not_absent(self):
        self.client.get_object.side_effect = [self.missing(), self.response()]
        self.assertEqual(self.store.read(BUCKET, INDEX), subject.Snapshot(None, None, None))
        self.assertEqual(self.store.write_once(BUCKET, INDEX, VALUE, None, operation_id=OP), 'written')
        self.assertEqual(self.client.put_object.call_args.kwargs['IfNoneMatch'], '*')
        denied = ClientError({'Error': {'Code': 'AccessDenied'}, 'ResponseMetadata': {'HTTPStatusCode': 403}}, 'GetObject')
        self.client.get_object.side_effect = denied
        with self.assertRaises(ClientError): self.store.read(BUCKET, INDEX)

    def test_weak_missing_validator_and_foreign_roots_are_rejected(self):
        for etag in (None, '', 'unquoted', 'W/"weak"', '""'):
            with self.subTest(etag=etag):
                self.client.get_object.return_value = self.response(etag=etag)
                with self.assertRaises(ValueError): self.store.read(BUCKET, INDEX)
        for bucket, path in ((BUCKET, 'reader-index/v3/current.json'), (BUCKET, 'documents/pdf/pdf/index.json'),
                             (BUCKET, 'chapters/ebook/epub/index-08.json'), ('vomebook/search-data-v1', INDEX)):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'allowlist'):
                self.store.write_once(bucket, path, VALUE, '"v"', operation_id=OP)
        self.client.put_object.assert_not_called()

    def test_lost_response_reads_exact_state_and_never_replays(self):
        self.client.put_object.side_effect = TimeoutError('response lost')
        self.client.get_object.return_value = self.response()
        self.assertEqual(self.store.write_once(BUCKET, INDEX, VALUE, '"old"', operation_id=OP), 'written-response-unknown')
        self.client.put_object.assert_called_once()
        self.client.put_object.reset_mock()
        self.client.get_object.return_value = self.response(b'{"version":1,"kind":"x","files":[]}')
        with self.assertRaisesRegex(RuntimeError, 'inspect before recovery'):
            self.store.write_once(BUCKET, INDEX, VALUE, '"old"', operation_id=OP)
        self.client.put_object.assert_called_once()

    def test_conflict_and_invalid_payload_fail_closed(self):
        conflict = ClientError({'Error': {'Code': 'PreconditionFailed'},
                                'ResponseMetadata': {'HTTPStatusCode': 412}}, 'PutObject')
        self.client.put_object.side_effect = conflict
        self.client.get_object.return_value = self.response(b'{"version":1,"kind":"x","files":[]}')
        with self.assertRaisesRegex(RuntimeError, 'conflicted'):
            self.store.write_once(BUCKET, INDEX, VALUE, '"old"', operation_id=OP)
        self.client.put_object.assert_called_once()
        duplicate = {**VALUE, 'files': [{'key': 'same'}, {'key': 'same'}]}
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.store.write_once(BUCKET, INDEX, duplicate, '"old"', operation_id=OP)
        self.assertEqual(self.client.put_object.call_count, 1)

    def test_merge_never_discards_existing_rows_or_accepts_stale_target(self):
        current = {'files': [{'key': 'repo\0old.epub', 'source_revision': 'v1'}], 'failures': []}
        update = {'key': 'repo\0new.epub', 'source_revision': 'v2'}
        merged = subject.merge_observed(current, current, [update], [], kind='ebook-chapter-stream-index',
                                        operation_id=OP)
        self.assertEqual({row['key'] for row in merged['files']}, {'repo\0old.epub', 'repo\0new.epub'})
        self.assertEqual(merged['checkpoint_operation'], OP)
        with self.assertRaisesRegex(ValueError, 'lost an observed row'):
            subject.merge_observed(current, {'files': [], 'failures': []}, [update], [],
                                   kind='fixture', operation_id=OP)
        stale = {'files': [{**current['files'][0], 'source_revision': 'stale'}], 'failures': []}
        with self.assertRaisesRegex(ValueError, 'source changed'):
            subject.merge_observed(current, stale, [{**current['files'][0], 'source_revision': 'v3'}], [],
                                   kind='fixture', operation_id=OP)
        with self.assertRaisesRegex(ValueError, 'invalid or duplicate'):
            subject.merge_observed(current, current, [update, update], [], kind='fixture', operation_id=OP)

    def test_operation_identity_must_be_unique_and_bound_to_payload(self):
        with self.assertRaisesRegex(ValueError, 'exact unique operation ID'):
            self.store.write_once(BUCKET, INDEX, {k: v for k, v in VALUE.items() if k != 'checkpoint_operation'},
                                  '"old"', operation_id=OP)
        with self.assertRaisesRegex(ValueError, 'exact unique operation ID'):
            self.store.write_once(BUCKET, INDEX, VALUE, '"old"', operation_id='b' * 32)
        self.client.put_object.assert_not_called()

    def test_default_store_has_no_automatic_write_retry(self):
        from botocore.config import Config
        raw = Mock(); raw._namespace = 'vomebook'; raw._config = Config(retries={'total_max_attempts': 8})
        with patch.object(subject, 'S3BucketStore', return_value=raw): subject.ConditionalCheckpointStore()
        self.assertEqual(raw._config.retries['total_max_attempts'], 1)


if __name__ == '__main__': unittest.main()
