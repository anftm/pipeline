"""Daily staging refuses unfinished, altered or changing publication journals."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import search_daily_journal as subject
from scripts import search_transition as tx
from tests.test_search_transition import A, B, C, MemoryStore, SearchTransitionTests, default_receipt, receipt


class DailyJournalTests(unittest.TestCase):
    def complete(self):
        f=SearchTransitionTests();f.setUp();c=f.ready()
        c=tx.mark_publishing(f.s,c,pages_receipt=receipt('pages',B),space_receipt=receipt('space',B))
        c=tx.publish_pointer(f.s,f.s,c,pages_receipt=receipt('pages',B),space_receipt=receipt('space',B))
        for site in ('pages','space'):
            c=f.start_default(c,site=site,operation_id='8'*32)
            c=tx.observe_default(f.s,c,site=site,receipt=default_receipt(site,B))
        c=tx.finish(f.s,c,pages_receipt=receipt('pages',B),space_receipt=receipt('space',B),
                    pages_default=default_receipt('pages',B),space_default=default_receipt('space',B))
        return f.s,c
    def test_matching_complete_journal_is_read_only(self):
        store,current=self.complete();before=(store.writes,store.pointer_writes)
        result=subject.inspect(store)
        self.assertEqual(result['state'],'complete');self.assertEqual(result['sequence'],current.record['sequence'])
        self.assertEqual(result['generation'],B);self.assertFalse(result['external_writers_verified'])
        self.assertEqual(result['remote_writes'],0);self.assertEqual((store.writes,store.pointer_writes),before)
        subject.verify_unchanged(result,subject.inspect(store))
    def test_missing_or_unfinished_journal_fails_before_staging(self):
        store=MemoryStore()
        with self.assertRaisesRegex(ValueError,'journal is absent'):subject.inspect(store)
        f=SearchTransitionTests();f.setUp();f.begin()
        with self.assertRaisesRegex(ValueError,'unfinished work'):subject.inspect(f.s)
    def test_rolled_back_exact_parent_is_accepted(self):
        f=SearchTransitionTests();f.setUp();c=f.begin()
        c=f.start_site(c,site='space',operation_id='3'*32)
        c=f.observe(c,'space',B,b'new')
        c=f.start_site(c,site='space',operation_id='4'*32,rollback=True)
        c=f.observe(c,'space',A,b'old')
        c=tx.finish_rollback(f.s,c,pages_receipt=receipt('pages',A),space_receipt=receipt('space',A),
                             pages_default=default_receipt('pages',A),space_default=default_receipt('space',A))
        result=subject.inspect(f.s);self.assertEqual(result['state'],'rolled-back');self.assertEqual(result['generation'],A)
    def test_foreign_pointer_history_or_etag_change_never_passes(self):
        store,current=self.complete()
        store.pointer=tx.make_pointer(B,A,'f'*32)
        with self.assertRaisesRegex(ValueError,'exact current pointer'):subject.inspect(store)
        store.pointer=tx.make_pointer(B,A,current.record['transition'])
        first=tx.load(store);changed=type(first)(first.head,first.head_raw,'"foreign"',first.record,first.record_raw)
        with patch.object(tx,'load',side_effect=[first,changed]),self.assertRaisesRegex(ValueError,'changed during inspection'):
            subject.inspect(store)
        store.records.clear()
        with self.assertRaises(ValueError):subject.inspect(store)
    def test_receipt_changes_and_existing_output_fail_closed(self):
        store,_=self.complete();before=subject.inspect(store)
        with self.assertRaisesRegex(ValueError,'changed during the daily run'):
            subject.verify_unchanged(before,{**before,'head_sha256':'f'*64})
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'receipt.json'
            with patch.object(tx,'S3TransitionStore',return_value=store):
                self.assertEqual(subject.main(['--output',str(p)]),0)
                self.assertEqual(subject.main(['--expected',str(p)]),0)
                with self.assertRaisesRegex(ValueError,'must be new'):subject.main(['--output',str(p)])
                p.write_text('{}')
                with self.assertRaisesRegex(ValueError,'changed during the daily run'):subject.main(['--expected',str(p)])


if __name__=='__main__':unittest.main()
