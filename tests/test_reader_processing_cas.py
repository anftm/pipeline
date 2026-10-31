import json
import unittest

from botocore.exceptions import ClientError

from scripts.reader_processing_cas import ProcessingWriter
from scripts.shared import READER_ASSETS_BUCKET


class ConditionalStore:
    def __init__(self):
        self.files = {}
        self.versions = {}
        self.read_etags = {}

    def set(self, path, value):
        self.files[path] = (json.dumps(value) + '\n').encode()
        self.versions[path] = self.versions.get(path, 0) + 1

    def tag(self, path):
        return f'"version-{self.versions[path]}"'

    def read_bytes(self, bucket, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        self.read_etags[(bucket, path)] = self.tag(path)
        return self.files[path]

    def _location(self, bucket):
        self.assert_bucket = bucket
        return 'vomebook', 'reader-assets-v2'

    def _client(self, _namespace):
        return self

    def put_object(self, *, Bucket, Key, Body, ContentType, **condition):
        present = Key in self.files
        if (condition.get('IfNoneMatch') == '*' and present
                or 'IfMatch' in condition and (not present or condition['IfMatch'] != self.tag(Key))):
            raise ClientError({'Error': {'Code': 'PreconditionFailed'},
                               'ResponseMetadata': {'HTTPStatusCode': 412}}, 'PutObject')
        self.files[Key] = Body
        self.versions[Key] = self.versions.get(Key, 0) + 1


class ProcessingCompareBeforeWriteTests(unittest.TestCase):
    def setUp(self):
        self.store = ConditionalStore()
        self.path = 'reader-index/processing/stream-upload-' + 'a' * 32 + '.json'
        self.active = {'kind': 'reader-processing-roots', 'status': 'processing', 'run': {'run_id': 'first'}}
        self.released = {**self.active, 'status': 'released', 'generation': 'a' * 32}
        self.store.set(self.path, self.active)
        self.writer = ProcessingWriter(self.store)

    def test_releases_only_the_exact_read_record(self):
        self.assertEqual(self.writer.read(self.path), self.active)
        self.writer.publish(self.path, self.released)
        self.assertEqual(json.loads(self.store.files[self.path]), self.released)
        self.assertEqual(self.store.assert_bucket, READER_ASSETS_BUCKET)

    def test_concurrent_new_producer_is_not_overwritten(self):
        self.writer.read(self.path)
        second = {**self.active, 'run': {'run_id': 'second'}}
        self.store.set(self.path, second)
        with self.assertRaisesRegex(RuntimeError, 'changed before handoff'):
            self.writer.publish(self.path, self.released)
        self.assertEqual(json.loads(self.store.files[self.path]), second)

    def test_unknown_or_unread_release_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'no observed'):
            self.writer.publish(self.path, self.released)
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            self.writer.publish('reader-index/reader_assets.json', {})

    def test_content_addressed_hint_is_only_created_or_read_back_identically(self):
        path = 'reader-index/gc/retirements/' + 'a' * 64 + '.json'
        hint = {'kind': 'reader-retirement-hints', 'value': 'original'}
        self.writer.publish(path, hint)
        self.writer.publish(path, hint)
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.writer.publish(path, {'kind': 'reader-retirement-hints', 'value': 'different'})
        self.assertEqual(json.loads(self.store.files[path]), hint)


if __name__ == '__main__':
    unittest.main()
