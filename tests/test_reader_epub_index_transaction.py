"""A staged EPUB shard update has a durable single-use CAS intent."""
import copy
import hashlib
import json
import unittest
from unittest.mock import Mock,patch

from scripts import reader_epub_index_transaction as tx, reader_epub_stage_journal as stage,reader_stream_checkpoint_cas as cas,search_snapshot
from tests.test_reader_epub_index_preview import IndexPreviewTests
from tests.test_reader_epub_stage_journal import MemoryJournal


class IndexTransactionTests(IndexPreviewTests):
    def setUp(self):
        super().setUp();self.preview_result=self.inspect();self.validate.reset_mock();self.txstore=MemoryJournal()
    def begin(self):return tx.begin(self.txstore,self.current,self.preview_result,self.canonical,self.shard,'"parent"')
    def test_durable_intent_claim_and_observation(self):
        prepared=self.begin();self.assertEqual(prepared['record']['state'],'prepared')
        claimed=tx.claim(self.txstore,prepared);self.assertEqual(claimed['record']['state'],'write-intent')
        with self.assertRaisesRegex(ValueError,'already claimed'):tx.claim(self.txstore,claimed)
        raw=(json.dumps(claimed['intent']['candidate'],ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
        snapshot=cas.Snapshot(claimed['intent']['candidate'],'"new"',hashlib.sha256(raw).hexdigest())
        observed=tx.record_observed(self.txstore,claimed,snapshot=snapshot);self.assertEqual(observed['record']['state'],'observed')
        self.assertEqual(tx.load(self.txstore,self.plan['operation']),observed)
        self.assertEqual(observed['intent']['candidate'],self.preview_result['candidate'])
    def test_stale_claim_or_forged_candidate_never_advances(self):
        initial=self.begin();winner=tx.claim(self.txstore,initial)
        with self.assertRaisesRegex(ValueError,'head changed'):tx.claim(self.txstore,initial)
        self.assertEqual(tx.load(self.txstore,self.plan['operation']),winner)
        bad=copy.deepcopy(self.preview_result);bad['candidate']['files'][0]['profile']='forged'
        fresh=MemoryJournal()
        with self.assertRaisesRegex(ValueError,'candidate identity'):tx.begin(fresh,self.current,bad,self.canonical,self.shard,'"parent"')
    def test_lost_journal_responses_are_read_once_no_replay(self):
        self.txstore.lose_record=True;self.txstore.lose_head=True
        initial=self.begin();claimed=tx.claim(self.txstore,initial)
        self.assertEqual(claimed['record']['state'],'write-intent')
        self.assertEqual(sum(kind=='head' for kind,_ in self.txstore.puts),2)
    def test_missing_shard_can_be_bound_for_conditional_creation(self):
        self.plan['shard_index_sha256']=tx.digest({})
        self.current.intent['plan']['shard_index_sha256']=self.plan['shard_index_sha256']
        candidate={'version':1,'kind':'ebook-chapter-stream-index',
                   'files':[self.preview_result['candidate']['files'][0]],'failures':[],
                   'checkpoint_operation':self.plan['operation']}
        self.preview_result['shard_index_sha256']=self.plan['shard_index_sha256']
        self.preview_result['candidate']=candidate
        self.preview_result['candidate_sha256']=tx.digest(candidate)
        result=tx.begin(self.txstore,self.current,self.preview_result,self.canonical,{},None)
        self.assertEqual(result['intent']['parent_shard'],{})
        self.assertEqual(result['intent']['parent_etag_sha256'],hashlib.sha256(b'absent').hexdigest())

    def test_forged_preview_cannot_remove_or_change_unrelated_rows(self):
        unrelated={'key':'VoiceOfML/Test\0untouched.epub','source_revision':'older'}
        self.shard['files'].append(unrelated)
        self.plan['shard_index_sha256']=tx.digest(self.shard)
        # A separately recomputed plan would bind the full shard. Merely
        # altering the preview and its hash must not discard unrelated rows.
        bad=copy.deepcopy(self.preview_result)
        bad['shard_index_sha256']=self.plan['shard_index_sha256']
        self.current.intent['plan']['shard_index_sha256']=self.plan['shard_index_sha256']
        bad['candidate_sha256']=tx.digest(bad['candidate'])
        with self.assertRaisesRegex(ValueError,'unrelated rows'):
            tx.begin(MemoryJournal(),self.current,bad,self.canonical,self.shard,'"parent"')

    def test_staged_observation_requires_exact_bytes_not_boolean(self):
        claimed=tx.claim(self.txstore,self.begin())
        for snapshot in (None,True,cas.Snapshot(None,None,None),
                         cas.Snapshot(claimed['intent']['candidate'],'"v"','f'*64)):
            with self.subTest(snapshot=snapshot),self.assertRaises(ValueError):
                tx.record_observed(self.txstore,claimed,snapshot=snapshot)
        self.assertEqual(tx.load(self.txstore,self.plan['operation'])['record']['state'],'write-intent')

    def test_missing_history_and_changed_intent_fail_closed(self):
        claimed=tx.claim(self.txstore,self.begin())
        intent_key=tx.key(self.plan['operation'],'intents',claimed['record']['intent_sha256'])
        self.txstore.objects[intent_key]=b'foreign'
        with self.assertRaisesRegex(ValueError,'missing or changed'):tx.load(self.txstore,self.plan['operation'])
    def test_adapter_confines_its_own_namespace(self):
        from botocore.config import Config
        raw=Mock();raw._namespace='vomebook';raw._config=Config(retries={'total_max_attempts':8});raw._location.return_value=('vomebook','reader-assets-v2')
        with patch.object(tx.stage_journal,'S3BucketStore',return_value=raw):store=tx.S3Journal()
        self.assertEqual(raw._config.retries['total_max_attempts'],1)
        value=search_snapshot.canonical({'version':1,'kind':'fixture'})
        path=tx.key(self.plan['operation'],'intents',hashlib.sha256(value).hexdigest())
        with self.assertRaises(ValueError):store.create_immutable(path,value)
        with self.assertRaises(ValueError):store.read_immutable('reader-index/v3/current.json')
        raw._client.return_value.put_object.assert_not_called()


if __name__=='__main__':unittest.main()
