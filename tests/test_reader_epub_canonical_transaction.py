"""Canonical intent binds only observed shard changes and exact raw parent."""
import copy
import hashlib
import json
import unittest
from unittest.mock import Mock,patch

from scripts import reader_epub_canonical_transaction as subject,reader_epub_stage_journal as staging
from scripts import reader_stream_checkpoint_cas as cas,search_snapshot
from tests.test_reader_epub_canonical_preview import CanonicalPreviewTests
from tests.test_reader_epub_stage_journal import MemoryJournal


class CanonicalTransactionTests(CanonicalPreviewTests):
    def setUp(self):
        super().setUp();self.canonical_store=MemoryJournal()
        self.stage=staging.load(self.store,self.plan['operation'])
        self.artifact=self.canonical_preview()
        self.validator.reset_mock();self.validator.after_write.reset_mock()
        self.parent=self.checkpoints.read('vomebook/reader-assets-v2',subject.CANONICAL)
    def begin(self):return subject.begin(self.canonical_store,self.stage,self.done,self.artifact,self.parent)
    def candidate_snapshot(self,current):
        value=current['intent']['candidate'];raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
        return cas.Snapshot(value,'"candidate"',hashlib.sha256(raw).hexdigest())
    def test_intent_keeps_full_parent_old_row_and_observed_shard(self):
        current=self.begin();self.assertEqual(current['record']['state'],'prepared')
        self.assertEqual(current['intent']['parent_canonical'],self.canonical)
        self.assertEqual(current['intent']['previous_rows'][self.old['key']],self.old)
        self.assertEqual(current['intent']['parent_raw_sha256'],self.parent.raw_sha256)
        self.assertEqual(current['intent']['parent_etag_sha256'],hashlib.sha256(self.parent.etag.encode()).hexdigest())
        claimed=subject.claim(self.canonical_store,current)
        done=subject.record_observed(self.canonical_store,claimed,self.candidate_snapshot(claimed))
        self.assertEqual(done['record']['state'],'observed')
        with self.assertRaisesRegex(ValueError,'already claimed'):subject.claim(self.canonical_store,done)
    def test_invalid_candidate_or_foreign_source_never_creates_intent(self):
        artifact=copy.deepcopy(self.artifact);artifact['candidate']['files'][1]['manifest']='foreign'
        artifact['candidate_sha256']=subject.digest(artifact['candidate'])
        artifact['preview_sha256']=subject.digest({k:v for k,v in artifact.items() if k!='preview_sha256'})
        with self.assertRaises(ValueError):subject.begin(self.canonical_store,self.stage,self.done,artifact,self.parent)
        self.assertEqual(self.canonical_store.puts,[])
    def test_lost_head_response_and_stale_or_unknown_snapshot_never_retry(self):
        self.canonical_store.lose_head=True;current=self.begin();claimed=subject.claim(self.canonical_store,current)
        with self.assertRaisesRegex(ValueError,'snapshot differs'):
            subject.record_observed(self.canonical_store,claimed,self.parent)
        self.assertEqual(subject.load(self.canonical_store,self.plan['operation'])['record']['state'],'write-intent')
        done=subject.record_observed(self.canonical_store,claimed,self.candidate_snapshot(claimed))
        self.assertEqual(done['record']['state'],'observed')
    def test_modified_journal_or_shard_binding_is_rejected(self):
        current=self.begin();wrong=copy.deepcopy(self.done);wrong['intent']['candidate']['files'][0]['manifest']='foreign'
        # Digest and candidate are independently checked by shard journal load;
        # here direct bind must at least reject differing selected row.
        with self.assertRaises(ValueError):subject._bind(self.stage,wrong,current['intent'])
        path=subject.key(self.plan['operation'],'intents',current['record']['intent_sha256'])
        self.canonical_store.objects[path]=b'bad'
        with self.assertRaisesRegex(ValueError,'missing or changed'):subject.load(self.canonical_store,self.plan['operation'])
    def test_adapter_allowlist_never_permits_pointer_or_unconditional_write(self):
        from botocore.config import Config
        raw=Mock();raw._namespace='vomebook';raw._config=Config(retries={'total_max_attempts':8});raw._location.return_value=('vomebook','reader-assets-v2')
        with patch.object(staging,'S3BucketStore',return_value=raw):store=subject.S3Journal()
        self.assertEqual(raw._config.retries['total_max_attempts'],1)
        current=self.begin();intent=search_snapshot.canonical(current['intent']);sha=hashlib.sha256(intent).hexdigest()
        store.create_immutable(subject.key(self.plan['operation'],'intents',sha),intent)
        self.assertEqual(raw._client.return_value.put_object.call_args.kwargs['IfNoneMatch'],'*')
        with self.assertRaises(ValueError):store.read_head('reader-index/v3/current.json')


if __name__=='__main__':unittest.main()
