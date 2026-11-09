"""Protection writes are conditional, bounded and block replacement."""
import copy
import io
import json
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from scripts import reader_epub_protection as subject, reader_epub_stage_simulation as sim, reader_gc_graph as gc
from scripts import search_snapshot
from scripts.reader_bucket_store import S3BucketStore
from tests.test_reader_gc_graph import MemoryStore

OP, DIGEST = 'a'*32, 'b'*64
ROOT = 'chapters/ebook/epub/' + 'c'*64 + '/' + 'd'*16
PATH = 'reader-index/processing/epub-exact-' + OP + '-00.json'
WHEN = '2026-11-09T00:00:00Z'


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.client=Mock();self.raw=S3BucketStore.__new__(S3BucketStore)
        self.raw._location=Mock(return_value=('vomebook','reader-assets-v2'))
        self.raw._client=Mock(return_value=self.client)
        self.store=subject.S3EpubProtections(self.raw)
        self.record=subject.make_record(OP,DIGEST,ROOT,WHEN)
        self.encoded=search_snapshot.canonical(self.record)
    def response(self,raw=None,etag='"v"'):
        raw=self.encoded if raw is None else raw
        return {'Body':io.BytesIO(raw),'ContentLength':len(raw),'ETag':etag}
    def test_exact_conditional_create_and_independent_readback(self):
        self.client.get_object.return_value=self.response()
        self.assertEqual(self.store.create_once(PATH,self.encoded),'written')
        self.client.put_object.assert_called_once_with(Bucket='reader-assets-v2',Key=PATH,Body=self.encoded,
                                                       ContentType='application/json',IfNoneMatch='*')
    def test_uncertain_response_checks_exact_bytes_and_does_not_retry(self):
        self.client.put_object.side_effect=TimeoutError('lost');self.client.get_object.return_value=self.response()
        self.assertEqual(self.store.create_once(PATH,self.encoded),'written-response-unknown')
        self.client.put_object.assert_called_once()
        self.client.put_object.reset_mock()
        other=subject.make_record(OP,'e'*64,ROOT,WHEN)
        self.client.get_object.return_value=self.response(search_snapshot.canonical(other))
        with self.assertRaisesRegex(RuntimeError,'inspect before recovery'):self.store.create_once(PATH,self.encoded)
        self.client.put_object.assert_called_once()
    def test_only_exact_key_resource_operation_and_canonical_bytes_allowed(self):
        for path in ('reader-index/v3/current.json',PATH.replace('-00.json','-03.json'),PATH.replace(OP,'f'*32)):
            with self.subTest(path=path),self.assertRaises(ValueError):self.store.create_once(path,self.encoded)
        for change in ({'status':'released'},{'operation':'other'},{'created_at':'bad'},
                       {'resources':[{'bucket':'other','root':ROOT}]},{'extra':'x'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.store.create_once(PATH,search_snapshot.canonical({**self.record,**change}))
        with self.assertRaises(ValueError):self.store.create_once(PATH,json.dumps(self.record).encode())
        self.client.put_object.assert_not_called()
    def test_missing_and_access_denied_are_distinct(self):
        missing=ClientError({'Error':{'Code':'NoSuchKey'},'ResponseMetadata':{'HTTPStatusCode':404}},'GetObject')
        self.client.get_object.side_effect=missing;self.assertIsNone(self.store.inspect(PATH))
        denied=ClientError({'Error':{'Code':'AccessDenied'},'ResponseMetadata':{'HTTPStatusCode':403}},'GetObject')
        self.client.get_object.side_effect=denied
        with self.assertRaises(ClientError):self.store.inspect(PATH)
    def test_bounded_readback_closes_body_and_rejects_truncation(self):
        for length in (None, 0, subject.MAX_RECORD + 1, len(self.encoded) + 1):
            body = io.BytesIO(self.encoded)
            self.client.get_object.return_value = {'Body': body, 'ContentLength': length}
            with self.subTest(length=length), self.assertRaises(ValueError):
                self.store.inspect(PATH)
            self.assertTrue(body.closed)

    def test_memory_and_production_record_contract_are_identical(self):
        plan={'operation':OP,'plan_sha256':DIGEST};action={'root':ROOT}
        self.assertEqual(sim._protect_record(plan,action,WHEN),self.record)
        self.assertEqual(subject.validate(PATH,self.encoded),self.record)
    def test_active_record_is_a_reader_gc_protection_root(self):
        store=MemoryStore();object_path=ROOT+'/chapter-manifest.json'
        store.put(subject.READER_ASSETS_BUCKET,object_path,b'candidate')
        store.put(subject.READER_ASSETS_BUCKET,PATH,self.record)
        graph=gc.ReferenceGraph(store).build()
        self.assertNotIn(object_path,graph['buckets'][subject.READER_ASSETS_BUCKET]['candidates'])
    def test_default_s3_put_is_one_attempt(self):
        from botocore.config import Config
        raw=Mock();raw._namespace='vomebook';raw._config=Config(retries={'total_max_attempts':9})
        with patch.object(subject,'S3BucketStore',return_value=raw):subject.S3EpubProtections()
        self.assertEqual(raw._config.retries['total_max_attempts'],1)


if __name__=='__main__':unittest.main()
