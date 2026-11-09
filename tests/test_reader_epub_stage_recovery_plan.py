"""Recovery inventories identify missing objects and never replay mutations."""
import unittest
from unittest.mock import patch

from scripts import reader_epub_stage_recovery_plan as subject,reader_epub_stage_journal as journal
from tests import test_reader_epub_stage_readonly as fixtures


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.StageReadonlyTests(methodName='test_incomplete_pending_operation_reports_missing_without_writing')
        fixture.setUp();self.addCleanup(fixture.doCleanups);self.fixture=fixture
        journal.claim(fixture.store,fixture.prepared)
    def inspect(self):
        f=self.fixture
        return subject.inspect(f.store,f.plan['operation'],hub=f.hub,api=f.api,fs=f.fs,client=f.client,reader=f.reader)
    def test_partial_stage_lists_only_exact_missing_objects_without_writes(self):
        f=self.fixture;action=f.preview['actions'][0];missing=action['files'][0]
        del f.reader.objects[missing['remote']];before=len(f.store.puts)
        result=self.inspect()
        self.assertEqual(result['missing_objects'],1);self.assertEqual(result['missing_protections'],0)
        self.assertEqual([x['path'] for x in result['objects'] if x['state']=='missing'],[missing['remote']])
        self.assertFalse(result['resume_authorized']);self.assertEqual(result['remote_writes'],0)
        self.assertEqual(len(f.store.puts),before)
    def test_workflow_never_resumes_or_writes_objects(self):
        import yaml
        from pathlib import Path
        path=Path(__file__).resolve().parents[1]/'.github/workflows/reader-epub-stage-recovery-plan.yml'
        flow=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']),{'workflow_dispatch'})
        self.assertEqual(flow['permissions'],{'contents':'read'})
        commands='\n'.join(x.get('run','') for x in flow['jobs']['inspect']['steps'])
        self.assertIn('reader_epub_stage_recovery_plan.py',commands)
        for unsafe in ('--apply','workflow run','git push','delete'):
            self.assertNotIn(unsafe,commands)

    def test_complete_objects_are_not_inferred_as_published(self):
        result=self.inspect();self.assertEqual(result['missing_objects'],0)
        self.assertFalse(result['publication_ready']);self.assertFalse(result['resume_authorized'])
    def test_conflict_and_changed_source_fail_closed(self):
        f=self.fixture;item=f.preview['actions'][0]['files'][0];f.reader.objects[item['remote']]={'sha256':'f'*64,'bytes':1}
        with self.assertRaisesRegex(ValueError,'conflicts'):self.inspect()
        f.reader.objects[item['remote']]={'sha256':item['sha256'],'bytes':item['bytes']}
        with patch.object(subject.planning,'inspect',return_value={}),self.assertRaisesRegex(ValueError,'changed before'):
            self.inspect()
    def test_prepared_or_staged_state_cannot_be_called_interrupted(self):
        f=self.fixture
        f2=fixtures.StageReadonlyTests(methodName='test_incomplete_pending_operation_reports_missing_without_writing')
        f2.setUp();self.addCleanup(f2.doCleanups)
        with self.assertRaisesRegex(ValueError,'no unresolved write intent'):
            subject.inspect(f2.store,f2.plan['operation'],hub=f2.hub,api=f2.api,fs=f2.fs,client=f2.client,reader=f2.reader)


if __name__=='__main__':unittest.main()
