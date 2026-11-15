"""S3 GC uses bounded reads, complete listing and one conditional delete."""
import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from botocore.config import Config
from botocore.exceptions import ClientError
from scripts import search_gc as gc
from scripts.search_gc_s3 import S3SearchGCStore


class S3GCTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.meta.config = Config(retries={'total_max_attempts': 1})
        self.base = SimpleNamespace(_clients={}, _config=Config(retries={'max_attempts': 8}),
                                    _location=Mock(return_value=('vomebook', 'search-data-v1')),
                                    _client=Mock(return_value=self.client))
        self.store = S3SearchGCStore(self.base)
        self.key = 'generations/' + 'a' * 64 + '/manifest.json'
        self.when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.response = {'ETag': '"exact"', 'ContentLength': 2, 'LastModified': self.when}
        self.item = {'key': self.key, 'etag': '"exact"', 'size': 2, 'mtime': self.when.timestamp()}
        self.client.head_object.return_value = self.response

    def error(self, code='AccessDenied', status=403):
        return ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'request')

    def test_fixed_bucket_and_disabled_retries_including_injected_store(self):
        self.base._location.assert_called_once_with('vomebook/search-data-v1')
        self.assertEqual(self.base._config.retries, {'total_max_attempts': 1})
        self.base._location.return_value = ('outsider', 'search-data-v1')
        with self.assertRaisesRegex(ValueError, 'bucket mapping'):
            S3SearchGCStore(self.base)
        self.base._location.return_value = ('vomebook', 'search-data-v1')
        self.base._clients = {'cached': self.client}
        with self.assertRaisesRegex(ValueError, 'fresh'):
            S3SearchGCStore(self.base)
        self.base._clients = {}
        self.client.meta.config = Config(retries={'total_max_attempts': 8})
        with self.assertRaisesRegex(ValueError, 'disabled'):
            S3SearchGCStore(self.base)

    def test_complete_listing_paginates_without_prefix_filter(self):
        def listed(key):
            return {'Key': key, 'ETag': '"exact"', 'Size': 2, 'LastModified': self.when}
        self.client.list_objects_v2.side_effect = [
            {'IsTruncated': True, 'Contents': [listed(self.key)], 'NextContinuationToken': 'next'},
            {'IsTruncated': False, 'Contents': [listed('other/keep')]}]
        result = list(self.store.inventory())
        self.assertEqual(result[0], self.item)
        self.assertEqual(result[1]['key'], 'other/keep')
        self.assertEqual(self.client.list_objects_v2.call_args_list[0].kwargs,
                         {'Bucket': 'search-data-v1', 'MaxKeys': 1000})
        self.assertEqual(self.client.list_objects_v2.call_args_list[1].kwargs,
                         {'Bucket': 'search-data-v1', 'MaxKeys': 1000, 'ContinuationToken': 'next'})

    def test_fractional_list_matches_integral_http_head_and_get(self):
        # Exact discrepancy observed in preview 43691116382.
        listed = {'Key': self.key, 'ETag': '"exact"', 'Size': 2,
                  'LastModified': datetime.fromtimestamp(1794376476.731, timezone.utc)}
        response = {**self.response, 'LastModified': datetime.fromtimestamp(1794376476, timezone.utc)}
        self.client.list_objects_v2.return_value = {'IsTruncated': False, 'Contents': [listed]}
        self.client.head_object.return_value = response
        self.client.get_object.return_value = {**response, 'Body': io.BytesIO(b'{}')}
        item = list(self.store.inventory())[0]
        self.assertEqual(item['mtime'], 1794376476)
        self.assertIs(type(item['mtime']), int)
        self.assertEqual(self.store.head(self.key), item)
        self.assertEqual(self.store.read(self.key, item), b'{}')
        self.store.delete(item)
        self.client.get_object.assert_called_once_with(Bucket='search-data-v1', Key=self.key, IfMatch='"exact"')
        self.client.delete_object.assert_called_once_with(Bucket='search-data-v1', Key=self.key, IfMatch='"exact"')

    def test_seconds_normalization_keeps_all_identity_checks_strict(self):
        listed = self.store._meta(self.key, {**self.response, 'Size': 2,
                                            'LastModified': self.when.replace(microsecond=731000)}, listing=True)
        changes = ({'LastModified': datetime.fromtimestamp(self.when.timestamp() + 1, timezone.utc)},
                   {'LastModified': datetime.fromtimestamp(self.when.timestamp() - 0.001, timezone.utc)},
                   {'ETag': '"different"'}, {'ETag': '"EXACT"'}, {'ContentLength': 3})
        for changed in changes:
            with self.subTest(changed=changed):
                self.client.head_object.return_value = {**self.response, **changed}
                with self.assertRaisesRegex(ValueError, 'metadata changed before read'):
                    self.store.read(self.key, listed)
                with self.assertRaisesRegex(ValueError, 'delete identity changed'):
                    self.store.delete(listed)
        self.client.head_object.return_value = self.response
        with self.assertRaisesRegex(ValueError, 'metadata changed before read'):
            self.store.read(self.key, {**listed, 'key': 'generations/' + 'b' * 64 + '/manifest.json'})
        self.client.get_object.assert_not_called()
        self.client.delete_object.assert_not_called()
        for changed in changes:
            body = io.BytesIO(b'{}')
            self.client.get_object.return_value = {**self.response, **changed, 'Body': body}
            with self.assertRaisesRegex(ValueError, 'GET metadata differs from HEAD'):
                self.store.read(self.key, listed)
            self.assertTrue(body.closed)

    def test_incomplete_malformed_and_repeated_token_listing_fails(self):
        for response in ({}, {'IsTruncated': True}, {'IsTruncated': False, 'Contents': [{}]}):
            self.client.list_objects_v2.side_effect = None
            self.client.list_objects_v2.return_value = response
            with self.assertRaises(ValueError):
                list(self.store.inventory())
        self.client.list_objects_v2.side_effect = [
            {'IsTruncated': True, 'NextContinuationToken': 'same'},
            {'IsTruncated': True, 'NextContinuationToken': 'same'}]
        with self.assertRaisesRegex(ValueError, 'pagination token'):
            list(self.store.inventory())

    def test_external_paths_and_protected_deletes_rejected_before_requests(self):
        bad = ['/tmp/a', '../manifest.json', 'https://s3.hf.co/vomebook/search-data-v1/x',
               'other-bucket/objects/a', 'generations/../manifest.json', 'objects/sha256/ff/' + 'a' * 64]
        for key in bad:
            for operation in (self.store.read, self.store.head):
                with self.assertRaises(ValueError):
                    operation(key)
        for key in bad + ['published/current.json', 'published/transition.json', 'gc/plans/1.json',
                          'probes/test', 'receipts/test', 'other/test']:
            with self.assertRaises(ValueError):
                self.store.delete({**self.item, 'key': key})
        self.client.head_object.assert_not_called()
        self.client.delete_object.assert_not_called()
        with self.assertRaises(ValueError):
            self.store.create('published/current.json', b'{}')
        self.client.put_object.assert_not_called()

    def test_read_head_cap_conditional_get_and_body_close(self):
        body = io.BytesIO(b'{}')
        self.client.get_object.return_value = {**self.response, 'Body': body}
        self.assertEqual(self.store.read(self.key, self.item), b'{}')
        self.assertTrue(body.closed)
        self.client.get_object.assert_called_once_with(Bucket='search-data-v1', Key=self.key, IfMatch='"exact"')
        self.client.head_object.return_value = {**self.response, 'ContentLength': gc.MAX_JSON + 1}
        with self.assertRaisesRegex(ValueError, 'cap'):
            self.store.read(self.key)
        self.assertEqual(self.client.get_object.call_count, 1)

    def test_missing_is_distinct_from_unknown_and_forbidden(self):
        self.client.head_object.side_effect = self.error('NoSuchKey', 404)
        self.assertIsNone(self.store.read(self.key))
        with self.assertRaisesRegex(ValueError, 'disappeared'):
            self.store.read(self.key, self.item)
        for error in (self.error(), TimeoutError('unknown')):
            self.client.head_object.side_effect = error
            with self.assertRaises(type(error)):
                self.store.read(self.key)
        self.client.get_object.assert_not_called()

    def test_get_changed_size_and_missing_metadata_rejected(self):
        for response in ({**self.response, 'ContentLength': 3}, {**self.response, 'ETag': None}):
            body = io.BytesIO(b'{}')
            self.client.get_object.return_value = {**response, 'Body': body}
            with self.assertRaises(ValueError):
                self.store.read(self.key)
            self.assertTrue(body.closed)
        body = io.BytesIO(b'x')
        self.client.get_object.return_value = {**self.response, 'Body': body}
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            self.store.read(self.key)

    def test_delete_one_ifmatch_request_unknown_propagates_without_retry(self):
        self.client.delete_object.side_effect = TimeoutError('unknown')
        with self.assertRaises(TimeoutError):
            self.store.delete(self.item)
        self.client.delete_object.assert_called_once_with(Bucket='search-data-v1', Key=self.key, IfMatch='"exact"')
        self.client.head_object.return_value = {**self.response, 'ETag': '"different"'}
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.store.delete(self.item)
        self.assertEqual(self.client.delete_object.call_count, 1)

    def test_gc_metadata_create_is_immutable_and_no_retry(self):
        self.client.put_object.side_effect = TimeoutError('unknown')
        with self.assertRaises(TimeoutError):
            self.store.create('gc/plans/123.json', gc.canonical({}))
        self.client.put_object.assert_called_once_with(Bucket='search-data-v1', Key='gc/plans/123.json',
                                                       Body=gc.canonical({}), ContentType='application/json', IfNoneMatch='*')


if __name__ == '__main__':
    unittest.main()
