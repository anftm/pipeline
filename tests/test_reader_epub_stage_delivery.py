"""Durable EPUB intent, single attempt and read-only interrupted recovery."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import reader_epub_shadow as shadow,reader_epub_stage_delivery as subject
from scripts import reader_epub_stage_journal as journal
from tests.test_reader_epub_shadow import epub,plan
from tests.test_reader_epub_stage_journal import MemoryJournal,WHEN
from tests.test_reader_epub_protected_stage import MemoryProtections,MemoryObjects


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'shadow';raw=epub();self.plan=plan(raw)
        self.report=shadow.build(self.plan,self.root,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.store=MemoryJournal();self.events=[];self.protections=MemoryProtections(self.events)
        self.objects=MemoryObjects(self.events);self.revalidate=Mock()
    def prepare(self):return subject.prepare(self.store,self.plan,self.report,self.root,created_at=WHEN,revalidate=self.revalidate)
    def apply(self,current):
        return subject.apply_once(self.store,current,self.report,self.root,protections=self.protections,
                                  objects=self.objects,revalidate=self.revalidate)
    def recover(self):
        return subject.recover(self.store,self.plan['operation'],protections=self.protections,
                               objects=self.objects,revalidate=self.revalidate)
    def test_prepare_claim_stage_and_durable_receipt(self):
        prepared=self.prepare();self.assertEqual(prepared.record['state'],'prepared')
        done=self.apply(prepared);self.assertEqual(done.record['state'],'staged')
        self.assertIsNotNone(done.record['receipt_sha256']);self.assertEqual(self.revalidate.call_count,6)
        self.assertFalse(any('v3/current' in k for k in self.objects.values))
        with self.assertRaisesRegex(ValueError,'not prepared'):self.apply(done)
    def test_interrupted_claim_never_replays_and_missing_objects_remain_unresolved(self):
        prepared=self.prepare();self.objects.fail=True
        with self.assertRaises(Exception):self.apply(prepared)
        pending=journal.load(self.store,self.plan['operation']);self.assertEqual(pending.record['state'],'write-intent')
        creates=len([x for x in self.events if x[0].startswith('create-')])
        with self.assertRaisesRegex(ValueError,'missing or changed'):self.recover()
        self.assertEqual(len([x for x in self.events if x[0].startswith('create-')]),creates)
        with self.assertRaisesRegex(ValueError,'not prepared'):self.apply(pending)
    def test_interrupted_after_all_objects_reconciles_without_object_writes(self):
        prepared=self.prepare();calls=0
        def revalidate(_p,_v):
            nonlocal calls
            calls+=1
            # apply pre-claim + stage before/protection/final
            if calls==4:raise TimeoutError('after object writes')
        self.revalidate.side_effect=revalidate
        with self.assertRaises(TimeoutError):self.apply(prepared)
        self.assertEqual(journal.load(self.store,self.plan['operation']).record['state'],'write-intent')
        self.revalidate.side_effect=None
        creates=len([x for x in self.events if x[0].startswith('create-')])
        done=self.recover()
        self.assertEqual(done.record['state'],'staged')
        self.assertEqual(len([x for x in self.events if x[0].startswith('create-')]),creates)
    def test_recovery_never_advances_on_foreign_content_or_reader_drift(self):
        prepared=self.prepare();self.objects.fail=True
        with self.assertRaises(Exception):self.apply(prepared)
        self.revalidate.side_effect=ValueError('consumer changed')
        with self.assertRaisesRegex(ValueError,'consumer changed'):self.recover()
        self.assertEqual(journal.load(self.store,self.plan['operation']).record['state'],'write-intent')


if __name__=='__main__':unittest.main()
