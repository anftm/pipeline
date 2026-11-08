"""CCRD condition probe is limited to a single exact nonproduction key."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import ccrd_bucket_probe as subject
from scripts.reader_bucket_store import S3NotFound


class Conflict(Exception):
    def __init__(self):
        self.response = {'ResponseMetadata': {'HTTPStatusCode': 412}, 'Error': {'Code': 'PreconditionFailed'}}
        super().__init__('precondition failed')


class Client:
    def __init__(self):
        self.raw = None
        self.etag = None
        self.calls = []

    def put_object(self, **values):
        self.calls.append(('put', values))
        if values.get('IfNoneMatch') == '*' and self.raw is not None:
            raise Conflict()
        if 'IfMatch' in values and values['IfMatch'] != self.etag:
            raise Conflict()
        self.raw = values['Body']; self.etag = f'"etag-{len(self.calls)}"'

    def delete_object(self, **values):
        self.calls.append(('delete', values))
        if values.get('IfMatch') != self.etag:
            raise Conflict()
        self.raw = self.etag = None


class Store:
    def __init__(self):
        self.client = Client(); self.read_etags = {}

    def _location(self, bucket):
        if bucket != subject.BUCKET:
            raise AssertionError('wrong bucket')
        return 'vomebook', 'ccrd-index'

    def _client(self, namespace):
        if namespace != 'vomebook':
            raise AssertionError('wrong namespace')
        return self.client

    def read_bytes(self, bucket, key):
        if self.client.raw is None:
            raise S3NotFound(key)
        self.read_etags[(bucket, key)] = self.client.etag
        return self.client.raw


class Hub:
    def __init__(self, store):
        self.store = store

    def read_bytes(self, bucket, key):
        return self.store.read_bytes(bucket, key)


class CcrdBucketProbeTests(unittest.TestCase):
    def test_conditionals_cross_read_and_exact_delete(self):
        store = Store(); result = subject.probe(store, Hub(store), 'a' * 32)
        self.assertTrue(result['if_none_match_verified']); self.assertTrue(result['if_match_verified'])
        self.assertTrue(result['stale_match_rejected']); self.assertTrue(result['conditional_delete_verified'])
        self.assertTrue(result['hub_s3_reads_match']); self.assertFalse(result['production_pointer_written'])
        self.assertFalse(result['external_writers_isolated']); self.assertEqual(len(store.client.calls), 5)
        for _, values in store.client.calls:
            self.assertEqual(values['Bucket'], 'ccrd-index')
            self.assertEqual(values['Key'], 'probes/ccrd-conditional-' + 'a' * 32 + '.json')
        self.assertIsNone(store.client.raw)

    def test_existing_or_invalid_probe_never_writes(self):
        store = Store(); store.client.raw = b'already here'
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            subject.probe(store, Hub(store), 'a' * 32)
        self.assertEqual(store.client.calls, [])
        with self.assertRaises(ValueError): subject.probe(store, Hub(store), '../current.json')

    def test_unknown_write_is_not_replayed_or_deleted(self):
        store = Store(); store.client.put_object = Mock(side_effect=TimeoutError('lost'))
        with self.assertRaises(TimeoutError): subject.probe(store, Hub(store), 'b' * 32)
        store.client.put_object.assert_called_once(); self.assertEqual(store.client.calls, [])

    def test_workflow_is_manual_dry_by_default_and_serialized_with_ccrd_monitor(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/ccrd-bucket-probe.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['apply']['default'], 'false')
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency'], {'group': 'publish-ccrd-index',
                                               'cancel-in-progress': 'false', 'queue': 'max'})
        commands = '\n'.join(step.get('run', '') for step in flow['jobs']['probe']['steps'])
        self.assertIn('scripts/ccrd_bucket_probe.py --apply', commands)
        self.assertNotIn('current.json', commands)

    def test_default_cli_does_not_open_storage(self):
        with patch.object(subject, 'S3BucketStore', side_effect=AssertionError('must not open')):
            self.assertEqual(subject.main([]), 0)


if __name__ == '__main__':
    unittest.main()
