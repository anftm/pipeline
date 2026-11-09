"""EPUB staging intents are durable and claims never infer promotion."""
import hashlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import reader_epub_stage_journal as subject, reader_epub_shadow as shadow, search_snapshot
from tests.test_reader_epub_shadow import epub, plan

WHEN='2026-11-09T00:00:00Z'


class MemoryJournal:
    def __init__(self):
        self.objects={};self.heads={};self.etags={};self.puts=[];self.lose_record=False;self.lose_head=False
    def read_head(self,key):return self.heads.get(key),self.etags.get(key)
    def write_head(self,key,raw,etag):
        self.puts.append(('head',key))
        if self.etags.get(key)!=etag:raise ValueError('CAS conflict')
        self.heads[key]=raw;self.etags[key]='"v'+str(len(self.puts))+'"'
        if self.lose_head:raise TimeoutError('lost head response')
    def read_immutable(self,key):return self.objects.get(key)
    def create_immutable(self,key,raw):
        self.puts.append(('immutable',key))
        if key in self.objects:raise ValueError('immutable conflict')
        self.objects[key]=raw
        if self.lose_record:raise TimeoutError('lost create response')


class EpubJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'shadow';raw=epub();self.plan=plan(raw)
        self.report=shadow.build(self.plan,self.root,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.preview=shadow.staging_plan(self.plan,self.report,self.root);self.store=MemoryJournal()
    def begin(self):return subject.begin(self.store,self.plan,self.preview,WHEN)
    def receipt(self):
        actions=self.preview['actions'];out=[]
        for action in actions:out.append({'kind':'protection','path':action['protection_path'],'status':'written'})
        for action in actions:
            for x in action['files']:out.append({'kind':'object','path':x['remote'],'status':'written'})
        return {'version':1,'kind':'reader-epub-protected-staging','operation':self.plan['operation'],
                'plan_sha256':self.plan['plan_sha256'],'staging_sha256':self.preview['staging_sha256'],
                'protected_sources':len(actions),'verified_objects':sum(len(x['files']) for x in actions),
                'outcomes':out,'index_written':False,'pointer_written':False,
                'publication_ready':False,'external_writers_verified':False}
    def test_durable_intent_unique_claim_and_staged_receipt(self):
        c=self.begin();self.assertEqual(c.record['state'],'prepared')
        claimed=subject.claim(self.store,c);self.assertEqual(claimed.record['state'],'write-intent')
        with self.assertRaisesRegex(ValueError,'already claimed'):subject.claim(self.store,claimed)
        staged=subject.record_staged(self.store,claimed,self.receipt())
        self.assertEqual(staged.record['state'],'staged');self.assertEqual(subject.load(self.store,self.plan['operation']),staged)
        self.assertEqual(staged.intent['plan']['sources'][0]['previous_manifest'],self.plan['sources'][0]['previous_manifest'])
    def test_two_claims_from_same_head_cannot_both_win_or_replay(self):
        c=self.begin();winner=subject.claim(self.store,c)
        with self.assertRaisesRegex(ValueError,'head changed'):subject.claim(self.store,c)
        self.assertEqual(subject.load(self.store,self.plan['operation']),winner)
    def test_lost_immutable_and_head_responses_are_read_back_once(self):
        self.store.lose_record=True;self.store.lose_head=True
        c=self.begin();self.assertEqual(c.record['state'],'prepared')
        self.assertEqual(sum(k=='head' for k,_ in self.store.puts),1)
        claimed=subject.claim(self.store,c);self.assertEqual(claimed.record['state'],'write-intent')
        self.assertEqual(sum(k=='head' for k,_ in self.store.puts),2)
    def test_changed_preview_or_foreign_root_cannot_create_intent(self):
        self.preview['actions'][0]['files'][0]['remote']='reader-index/v3/current.json'
        self.preview['staging_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in self.preview.items() if k!='staging_sha256'})).hexdigest()
        with self.assertRaisesRegex(ValueError,'foreign EPUB staging member'):self.begin()
        self.assertEqual(self.store.puts,[])
    def test_missing_changed_receipt_or_history_fails_closed(self):
        c=subject.claim(self.store,self.begin());receipt=self.receipt();receipt['outcomes'].pop()
        with self.assertRaisesRegex(ValueError,'coverage differs'):subject.record_staged(self.store,c,receipt)
        done=subject.record_staged(self.store,c,self.receipt())
        receipt_key=subject.key(self.plan['operation'],'receipts',done.record['receipt_sha256'])
        self.store.objects[receipt_key]=b'bad'
        with self.assertRaisesRegex(ValueError,'missing or changed'):subject.load(self.store,self.plan['operation'])
    def test_adapter_confines_paths_and_writes_single_conditions(self):
        from botocore.config import Config
        raw=Mock();raw._namespace='vomebook';raw._config=Config(retries={'total_max_attempts':8});raw._location.return_value=('vomebook','reader-assets-v2')
        with patch.object(subject,'S3BucketStore',return_value=raw):store=subject.S3Journal()
        self.assertEqual(raw._config.retries['total_max_attempts'],1)
        client=raw._client.return_value
        value=search_snapshot.canonical({'version':1,'kind':'fixture'})
        digest=hashlib.sha256(value).hexdigest();path=subject.key(self.plan['operation'],'intents',digest)
        store.create_immutable(path,value)
        self.assertEqual(client.put_object.call_args.kwargs['IfNoneMatch'],'*')
        with self.assertRaises(ValueError):store.create_immutable(path,b'wrong')
        with self.assertRaises(ValueError):store.read_immutable('reader-index/v3/current.json')
        self.assertEqual(client.put_object.call_count,1)


if __name__=='__main__':unittest.main()
