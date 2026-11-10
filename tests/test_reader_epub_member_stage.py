"""Per-member claims preserve protections and never replay unknown writes."""
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from scripts import reader_epub_member_stage as subject,reader_epub_stage_journal as staging
from scripts import reader_epub_shadow as shadow,reader_epub_immutable,reader_epub_protection,search_snapshot
from tests.test_reader_epub_shadow import epub,plan
from tests.test_reader_epub_stage_journal import MemoryJournal,WHEN
from tests.test_reader_epub_protected_stage import MemoryObjects,MemoryProtections


class MemberStageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'shadow';raw=epub();self.plan=plan(raw)
        self.report=shadow.build(self.plan,self.root,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.preview=shadow.staging_plan(self.plan,self.report,self.root)
        self.stage_store=MemoryJournal();prepared=staging.begin(self.stage_store,self.plan,self.preview,WHEN)
        self.stage=staging.claim(self.stage_store,prepared);self.store=MemoryJournal()
        self.events=[];self.protections=MemoryProtections(self.events);self.objects=MemoryObjects(self.events)
        self.validate=Mock();self.action=self.preview['actions'][0]
    def prepare(self,path):return subject.prepare(self.store,self.stage_store,self.stage,path)
    def apply(self,current):return subject.apply_once(self.store,self.stage_store,self.stage,current,directory=self.root,
        protections=self.protections,objects=self.objects,revalidate=self.validate)
    def reconcile(self,current):return subject.reconcile(self.store,self.stage_store,self.stage,current.record['member_sha256'],
        protections=self.protections,objects=self.objects,revalidate=self.validate)
    def protect(self):return self.apply(self.prepare(self.action['protection_path']))
    def test_each_member_requires_protection_and_single_claim(self):
        first=self.action['files'][0]['remote'];item=self.prepare(first)
        with self.assertRaisesRegex(ValueError,'protection is absent'):self.apply(item)
        self.assertEqual(subject.load(self.store,self.plan['operation'],item.record['member_sha256']).record['state'],'prepared')
        self.protect();observed=self.apply(item)
        self.assertEqual(observed.record['state'],'observed')
        with self.assertRaisesRegex(ValueError,'already claimed'):self.apply(observed)
        self.assertEqual(self.events.count(('create-object',first)),1)
        self.assertFalse(any('index.json'==p for _kind,p in self.events))
    def test_missing_after_lost_response_remains_claimed_without_replay(self):
        self.protect();first=self.action['files'][0]['remote'];item=self.prepare(first)
        self.objects.fail=True
        with self.assertRaises(TimeoutError):self.apply(item)
        pending=subject.load(self.store,self.plan['operation'],item.record['member_sha256'])
        self.assertEqual(pending.record['state'],'claimed');self.objects.fail=False
        with self.assertRaisesRegex(ValueError,'missing or differs'):self.reconcile(item)
        with self.assertRaisesRegex(ValueError,'already claimed'):self.apply(pending)
        self.assertEqual(self.events.count(('create-object',first)),1)
        self.assertIsNone(self.objects.inspect(self.action['root'],self.action['files'][0]['path']))
    def test_lost_success_is_observed_without_another_write(self):
        self.protect();first=self.action['files'][0]['remote'];item=self.prepare(first)
        self.objects.lose=True
        with self.assertRaises(TimeoutError):self.apply(item)
        self.objects.lose=False;done=self.reconcile(item)
        self.assertEqual(done.record['state'],'observed');self.assertEqual(self.events.count(('create-object',first)),1)
    def test_protection_claim_unknown_never_allows_content(self):
        item=self.prepare(self.action['protection_path']);self.protections.fail=True
        with self.assertRaises(TimeoutError):self.apply(item)
        self.protections.fail=False
        with self.assertRaisesRegex(ValueError,'missing or differs'):self.reconcile(item)
        first=self.prepare(self.action['files'][0]['remote'])
        with self.assertRaisesRegex(ValueError,'protection is absent'):self.apply(first)
        self.assertFalse(self.objects.values)
    def test_finalize_requires_all_observed_members_and_never_replays_objects(self):
        self.protect();first=self.prepare(self.action['files'][0]['remote']);self.apply(first)
        with self.assertRaisesRegex(ValueError,'not fully observed'):
            subject.finalize(self.store,self.stage_store,self.stage,protections=self.protections,objects=self.objects,revalidate=self.validate)
        for item in self.action['files'][1:]:self.apply(self.prepare(item['remote']))
        before=list(event for event in self.events if event[0].startswith('create-'))
        done=subject.finalize(self.store,self.stage_store,self.stage,protections=self.protections,objects=self.objects,revalidate=self.validate)
        after=list(event for event in self.events if event[0].startswith('create-'))
        self.assertEqual(before,after);self.assertEqual(done.record['state'],'staged')
        self.assertEqual(staging.load(self.stage_store,self.plan['operation']).record['state'],'staged')
        with self.assertRaisesRegex(ValueError,'staging journal changed'):
            subject.finalize(self.store,self.stage_store,self.stage,protections=self.protections,objects=self.objects,revalidate=self.validate)

    def test_foreign_member_or_revalidation_drift_never_mutates_content(self):
        with self.assertRaisesRegex(ValueError,'not unique'):self.prepare('reader-index/v3/current.json')
        self.protect();item=self.prepare(self.action['files'][0]['remote']);self.validate.side_effect=ValueError('source drift')
        with self.assertRaisesRegex(ValueError,'source drift'):self.apply(item)
        self.assertFalse(self.objects.values)
    def test_staging_head_change_or_local_tamper_fails_closed(self):
        self.protect();item=self.prepare(self.action['files'][0]['remote'])
        local=self.root/'0'/'bundle'/self.action['files'][0]['path'];local.write_bytes(local.read_bytes()+b'x')
        with self.assertRaisesRegex(ValueError,'local member differs'):self.apply(item)
        self.assertFalse(self.objects.values)
    def test_journal_tampering_and_foreign_stage_are_rejected(self):
        item=self.prepare(self.action['protection_path']);key=subject.key(self.plan['operation'],item.record['member_sha256'],'intents',item.record['intent_sha256'])
        self.store.objects[key]=b'bad'
        with self.assertRaisesRegex(ValueError,'missing or changed'):subject.load(self.store,self.plan['operation'],item.record['member_sha256'])
    def test_two_attempts_cannot_both_use_one_prepared_claim(self):
        item=self.prepare(self.action['protection_path']);first=self.apply(item)
        self.assertEqual(first.record['state'],'observed')
        with self.assertRaisesRegex(ValueError,'journal changed'):
            self.apply(item)
        self.assertEqual(self.events.count(('create-protection',self.action['protection_path'])),1)

    def test_s3_adapter_only_writes_member_journal_paths_conditionally(self):
        from botocore.config import Config
        raw=Mock();raw._namespace='vomebook';raw._config=Config(retries={'total_max_attempts':8})
        raw._location.return_value=('vomebook','reader-assets-v2')
        with patch.object(staging,'S3BucketStore',return_value=raw):store=subject.S3Journal()
        self.assertEqual(raw._config.retries['total_max_attempts'],1)
        item=self.prepare(self.action['protection_path']);intent=search_snapshot.canonical(item.intent)
        path=subject.key(self.plan['operation'],item.record['member_sha256'],'intents',hashlib.sha256(intent).hexdigest())
        store.create_immutable(path,intent)
        self.assertEqual(raw._client.return_value.put_object.call_args.kwargs['IfNoneMatch'],'*')
        with self.assertRaises(ValueError):store.read_head('reader-index/v3/current.json')
        with self.assertRaises(ValueError):store.create_immutable(path,b'changed')


class MemberStageS3IntegrationTests(unittest.TestCase):
    def test_actual_adapters_reconcile_lost_object_write_from_fake_s3(self):
        from tests.test_reader_epub_stage_journal_integration import JournalIntegrationTests
        fixture=JournalIntegrationTests(methodName='test_all_steps_are_conditional_and_staged_is_not_published')
        fixture.setUp()
        try:
            prepared=fixture.prepare();staged=staging.claim(fixture.journal,prepared)
            members=subject.S3Journal(fixture.journal.store);action=fixture.preview['actions'][0]
            one=subject.prepare(members,fixture.journal,staged,action['protection_path'])
            one=subject.apply_once(members,fixture.journal,staged,one,directory=fixture.root,
                protections=fixture.protections,objects=fixture.objects,revalidate=fixture.validate)
            self.assertEqual(one.record['state'],'observed')
            file=action['files'][0];second=subject.prepare(members,fixture.journal,staged,file['remote'])
            # Adapter receives timeout after the atomic create. Its own exact
            # readback succeeds, and the member journal records that outcome.
            fixture.client.lose_once.add(file['remote'])
            done=subject.apply_once(members,fixture.journal,staged,second,directory=fixture.root,
                protections=fixture.protections,objects=fixture.objects,revalidate=fixture.validate)
            self.assertEqual(done.record['state'],'observed')
            self.assertEqual(fixture.client.calls.count(('put',file['remote'])),1)
            self.assertFalse(any(key=='reader-index/v3/current.json' for kind,key in fixture.client.calls if kind=='put'))
        finally:
            fixture.doCleanups()


if __name__=='__main__':unittest.main()
