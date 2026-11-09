"""Canonical EPUB merge admits only an already-observed exact shard update."""
import copy
import unittest
from unittest.mock import Mock

from scripts import reader_epub_canonical_preview as subject,reader_epub_index_delivery as delivery
from tests import test_reader_epub_index_delivery as fixtures


class CanonicalPreviewTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.IndexDeliveryTests(methodName='test_exact_shard_cas_records_actual_read_and_retains_parent')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        pending=fixture.prepare_index();self.done=fixture.apply_index(pending)
        for name in ('canonical','old','other','plan','preview','checkpoints','path','index_store','store',
                     'protections','objects','validator'):
            setattr(self,name,getattr(fixture,name))
        self.validator.reset_mock();self.validator.after_write.reset_mock()
    def canonical_preview(self):
        return subject.inspect(self.index_store,self.store,self.plan['operation'],self.canonical,
            self.checkpoints.values[self.path][0],protections=self.protections,objects=self.objects,revalidate=self.validator)
    def test_preview_changes_only_selected_sources_and_preserves_full_parent_rows(self):
        before=copy.deepcopy(self.canonical);result=self.canonical_preview()
        self.assertEqual(self.canonical,before)
        self.assertEqual(result['selected_keys'],[self.old['key']])
        self.assertEqual(result['previous_rows'][self.old['key']],self.old)
        candidate={x['key']:x for x in result['candidate']['files']}
        self.assertEqual(candidate[self.other['key']],self.other)
        self.assertNotEqual(candidate[self.old['key']]['manifest'],self.old['manifest'])
        self.assertFalse(result['index_written']);self.assertFalse(result['pointer_written'])
        self.assertEqual(result['remote_writes'],0)
        self.assertEqual(self.validator.after_write.call_count,2)
    def test_parent_or_candidate_drift_fails_closed(self):
        self.canonical['files'][1]['manifest']='foreign'
        with self.assertRaisesRegex(ValueError,'canonical parent changed'):self.canonical_preview()
        self.canonical['files'][1]['manifest']='preserve'
        self.checkpoints.values[self.path][0]['files'][0]['source_revision']='f'*40
        with self.assertRaisesRegex(ValueError,'exact observed candidate'):self.canonical_preview()
    def test_missing_protection_or_object_never_advances_journal(self):
        action=self.preview['actions'][0];self.protections.values.pop(action['protection_path'])
        with self.assertRaisesRegex(ValueError,'protection differs'):self.canonical_preview()
        self.assertEqual(self.done['record']['state'],'observed')
    def test_revalidation_failure_fails_before_canonical_candidate(self):
        self.validator.after_write.side_effect=ValueError('source changed')
        with self.assertRaisesRegex(ValueError,'source changed'):self.canonical_preview()


if __name__=='__main__':unittest.main()
