"""One random Reader key only; unknown writes are never retried."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import reader_bucket_probe as subject
from scripts.reader_bucket_store import S3NotFound


class Conflict(Exception):
    def __init__(self):
        self.response = {'ResponseMetadata': {'HTTPStatusCode': 412}, 'Error': {'Code': 'PreconditionFailed'}}
        super().__init__('precondition failed')


class FakeClient:
    def __init__(self):
        self.raw = self.etag = None
        self.calls = []
        self.fail = None

    def put_object(self, **args):
        self.calls.append(('put', args))
        if args.get('IfNoneMatch') == '*' and self.raw is not None: raise Conflict()
        if 'IfMatch' in args and args['IfMatch'] != self.etag: raise Conflict()
        if self.fail == 'before-put': raise TimeoutError('before')
        self.raw = args['Body'];self.etag = '"version-' + str(len(self.calls)) + '"'
        if self.fail == 'after-put': raise TimeoutError('after')

    def delete_object(self, **args):
        self.calls.append(('delete', args))
        if args.get('IfMatch') != self.etag: raise Conflict()
        if self.fail == 'before-delete': raise TimeoutError('before')
        self.raw = self.etag = None
        if self.fail == 'after-delete': raise TimeoutError('after')


class Store:
    def __init__(self):self.client = FakeClient();self.read_etags = {};self.reads = 0
    def _location(self, bucket):
        if bucket != subject.BUCKET:raise AssertionError('unexpected bucket')
        return ('vomebook', 'reader-assets-v2')
    def _client(self, namespace):
        if namespace != 'vomebook':raise AssertionError('unexpected namespace')
        return self.client
    def read_bytes(self, bucket, path):
        self.reads += 1
        if self.client.raw is None: raise S3NotFound(path)
        self.read_etags[(bucket, path)] = self.client.etag
        return self.client.raw


class ProbeTests(unittest.TestCase):
    def test_full_probe_only_mutates_one_random_probe_key(self):
        store=Store();result=subject.probe(store,store,'a'*32)
        self.assertTrue(result['if_none_match_verified']);self.assertTrue(result['if_match_verified'])
        self.assertTrue(result['stale_match_rejected']);self.assertTrue(result['conditional_delete_verified'])
        self.assertTrue(result['hub_s3_reads_match']);self.assertFalse(result['production_roots_written'])
        self.assertFalse(result['external_writers_isolated']);self.assertEqual(len(store.client.calls),5)
        for _,call in store.client.calls:
            self.assertEqual(call['Bucket'],'reader-assets-v2')
            self.assertEqual(call['Key'],'probes/reader-s3-conditional-'+'a'*32+'.json')
        self.assertIsNone(store.client.raw)

    def test_existing_probe_or_invalid_id_never_writes(self):
        store=Store();store.client.raw=b'foreign'
        with self.assertRaisesRegex(RuntimeError,'already exists'):subject.probe(store,store,'a'*32)
        self.assertEqual(store.client.calls,[])
        with self.assertRaises(ValueError):subject.probe(store,store,'../reader-index/current')

    def test_unknown_create_reads_exact_state_and_stops_without_retry_or_delete(self):
        for phase in ('before-put','after-put'):
            store=Store();store.client.fail=phase
            with self.subTest(phase=phase),self.assertRaisesRegex(RuntimeError,'requires inspection'):
                subject.probe(store,store,'b'*32)
            self.assertEqual(len(store.client.calls),1)
            self.assertEqual(store.client.calls[0][0],'put')
            self.assertGreaterEqual(store.reads,2)

    def test_unknown_update_is_inspected_and_never_replayed(self):
        for write_first in (False, True):
            store = Store();real = store.client.put_object; seen = []
            def put(**kwargs):
                if 'IfMatch' not in kwargs:
                    return real(**kwargs)
                seen.append(kwargs)
                if write_first:
                    real(**kwargs)
                raise TimeoutError('lost update response')
            store.client.put_object = put
            with self.subTest(write_first=write_first), self.assertRaisesRegex(RuntimeError, 'requires inspection'):
                subject.probe(store, store, 'd'*32)
            self.assertEqual(len(seen), 1)
            self.assertEqual(sum(kind == 'delete' for kind, _ in store.client.calls), 0)
            self.assertGreaterEqual(store.reads, 3)

    def test_unknown_delete_reads_absence_but_never_retries(self):
        store=Store()
        def delete(**kwargs):
            store.client.calls.append(('delete',kwargs));store.client.raw=None;store.client.etag=None
            raise TimeoutError('lost delete response')
        store.client.delete_object=delete
        with self.assertRaisesRegex(RuntimeError,'requires inspection'):subject.probe(store,store,'c'*32)
        self.assertEqual(sum(kind=='delete' for kind,_ in store.client.calls),1)
        self.assertIsNone(store.client.raw)

    def test_default_is_readonly_and_workflow_is_manual(self):
        with patch.object(subject,'S3BucketStore',side_effect=AssertionError('no store')):
            self.assertEqual(subject.main([]),0)
        path=Path(__file__).resolve().parents[1]/'.github/workflows/reader-bucket-probe.yml'
        flow=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']),{'workflow_dispatch'})
        self.assertEqual(flow['permissions'],{'contents':'read'})
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['apply']['default'],'false')
        commands='\n'.join(x.get('run','') for x in flow['jobs']['probe']['steps'])
        self.assertIn('scripts/reader_bucket_probe.py',commands)
        self.assertNotIn('reader_epub_immutable.py',commands)


if __name__ == '__main__':unittest.main()
