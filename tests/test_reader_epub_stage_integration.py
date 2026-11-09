"""Exercise real S3 adapters and staging coordinator against an in-memory S3 API."""
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from botocore.exceptions import ClientError

from scripts import reader_epub_immutable as immutable, reader_epub_protected_stage as stage
from scripts import reader_epub_protection as protection, reader_epub_shadow as shadow
from scripts.reader_bucket_store import S3BucketStore
from tests.test_reader_epub_shadow import epub, plan

WHEN='2026-11-09T00:00:00Z'


def failure(code, status):
    return ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'fixture')


class AtomicFakeS3:
    def __init__(self):
        self.values={};self.calls=[];self.lock=threading.Lock();self.lose_once=set();self.before_once=set()
        self.corrupt_read=set()
    def get_object(self, *, Bucket, Key):
        if Bucket!='reader-assets-v2':raise AssertionError('unexpected bucket')
        with self.lock:
            self.calls.append(('get',Key));raw=self.values.get(Key)
            if raw is None:raise failure('NoSuchKey',404)
            if Key in self.corrupt_read:raw=b'foreign'
            return {'Body':io.BytesIO(raw),'ContentLength':len(raw),'ETag':'"fixture"'}
    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, **kwargs):
        if Bucket!='reader-assets-v2' or IfNoneMatch!='*':raise AssertionError('unexpected write boundary')
        with self.lock:
            self.calls.append(('put',Key))
            if Key in self.before_once:
                self.before_once.remove(Key);raise TimeoutError('before commit')
            if Key in self.values:raise failure('PreconditionFailed',412)
            raw=Body if isinstance(Body,bytes) else Body.read()
            if 'ContentLength' in kwargs and kwargs['ContentLength']!=len(raw):raise ValueError('wrong length')
            self.values[Key]=bytes(raw)
            if Key in self.lose_once:
                self.lose_once.remove(Key);raise TimeoutError('after commit')
            return {'ETag':'"written"'}


class S3StagingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.output=Path(self.temp.name)/'shadow';raw=epub();self.plan=plan(raw)
        self.report=shadow.build(self.plan,self.output,fetch=lambda _url,path,_token:path.write_bytes(raw))
        self.preview=shadow.staging_plan(self.plan,self.report,self.output)
        self.client=AtomicFakeS3();self.raw=S3BucketStore.__new__(S3BucketStore)
        self.raw._location=Mock(return_value=('vomebook','reader-assets-v2'))
        self.raw._client=Mock(return_value=self.client)
        self.objects=immutable.S3EpubObjects(self.raw);self.protections=protection.S3EpubProtections(self.raw)
        self.readonly=Mock()
    def run_stage(self):
        return stage.stage(self.plan,self.report,self.output,created_at=WHEN,protections=self.protections,
                           objects=self.objects,revalidate=self.readonly)
    def test_exact_objects_protected_first_and_resume_has_no_puts(self):
        result=self.run_stage();self.assertEqual(result['protected_sources'],1)
        self.assertEqual(result['verified_objects'],len(self.preview['actions'][0]['files']))
        writes=[key for kind,key in self.client.calls if kind=='put']
        self.assertEqual(writes[0],self.preview['actions'][0]['protection_path'])
        self.assertTrue(all(k.startswith(self.preview['actions'][0]['root']+'/') for k in writes[1:]))
        self.assertFalse(result['index_written']);self.assertFalse(result['pointer_written'])
        self.assertFalse(result['publication_ready']);self.assertFalse(result['external_writers_verified'])
        before=len(writes);again=self.run_stage()
        self.assertEqual(len([1 for kind,_ in self.client.calls if kind=='put']),before)
        self.assertTrue(all(x['status']=='already-present' for x in again['outcomes']))
    def test_lost_protection_and_member_responses_are_read_back_once(self):
        action=self.preview['actions'][0];first=action['files'][0]['remote']
        self.client.lose_once={action['protection_path'],first}
        result=self.run_stage()
        self.assertEqual(sum(x['status']=='written-response-unknown' for x in result['outcomes']),2)
        for path in (action['protection_path'],first):
            self.assertEqual(self.client.calls.count(('put',path)),1)
    def test_lost_before_first_protection_never_uploads_content(self):
        path=self.preview['actions'][0]['protection_path'];self.client.before_once.add(path)
        with self.assertRaises(stage.UnknownOutcome):self.run_stage()
        self.assertEqual([key for kind,key in self.client.calls if kind=='put'],[path])
        self.assertEqual(self.client.values,{})
    def test_lost_before_object_stays_protected_and_can_be_inspected(self):
        action=self.preview['actions'][0];target=action['files'][0]['remote'];self.client.before_once.add(target)
        with self.assertRaises(stage.UnknownOutcome):self.run_stage()
        self.assertIn(action['protection_path'],self.client.values);self.assertNotIn(target,self.client.values)
        self.assertEqual(self.client.calls.count(('put',target)),1)
        result=self.run_stage();self.assertGreater(result['verified_objects'],0)
    def test_foreign_protection_or_content_is_never_overwritten(self):
        action=self.preview['actions'][0];target=action['files'][0]['remote']
        self.client.values[target]=b'foreign'
        with self.assertRaisesRegex(ValueError,'conflicts'):self.run_stage()
        self.assertEqual(self.client.values[target],b'foreign')
        self.assertNotIn(('put',target),self.client.calls)
    def test_revalidation_failure_between_protection_and_objects_stops(self):
        self.readonly.side_effect=[None,ValueError('source moved')]
        with self.assertRaisesRegex(ValueError,'source moved'):self.run_stage()
        action=self.preview['actions'][0];self.assertIn(action['protection_path'],self.client.values)
        self.assertTrue(all(k==action['protection_path'] for k in self.client.values))
    def test_concurrent_conditional_creates_do_not_replace_a_winner(self):
        action=self.preview['actions'][0];item=action['files'][0]
        local=self.output/'0'/'bundle'/item['path'];expected={'sha256':item['sha256'],'bytes':item['bytes']}
        barrier=threading.Barrier(2);out=[]
        def worker():
            try:
                barrier.wait(timeout=5)
                out.append(self.objects.create_once(action['root'],item['path'],local,expected))
            except Exception as error:out.append(error)
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=10)
        self.assertEqual(len(out),2)
        self.assertIn('written',out);self.assertIn('written-response-unknown',out)
        self.assertEqual(self.objects.inspect(action['root'],item['path']),expected)
        self.assertEqual(sum(kind=='put' and key==item['remote'] for kind,key in self.client.calls),2)


if __name__=='__main__':unittest.main()
