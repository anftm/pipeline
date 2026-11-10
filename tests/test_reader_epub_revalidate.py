"""Live plan revalidation cannot be substituted by a stored boolean."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import reader_epub_revalidate as subject, reader_epub_shadow as shadow, search_snapshot
from tests.test_reader_epub_shadow import epub, plan


class LiveRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'shadow';raw=epub();self.plan=plan(raw)
        self.report=shadow.build(self.plan,self.root,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.preview=shadow.staging_plan(self.plan,self.report,self.root)
        self.hub,self.api,self.fs,self.client=Mock(),Mock(),Mock(),Mock()
        self.subject=subject.LivePlanRevalidator(hub=self.hub,api=self.api,fs=self.fs,client=self.client)
    def test_exact_live_inspection_is_repeated_and_returns_no_boolean(self):
        with patch.object(subject.planning,'inspect',return_value=self.plan) as inspect:
            self.assertIsNone(self.subject(self.plan,self.preview))
        self.assertEqual(inspect.call_args.kwargs,{'hub':self.hub,'api':self.api,'fs':self.fs,'client':self.client,
                                                    'shard':self.plan['shard'],'operation':self.plan['operation']})
    def test_one_source_plan_uses_exact_same_mode_during_revalidation(self):
        self.plan['max_attempts']=1
        self.plan['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in self.plan.items() if k!='plan_sha256'})).hexdigest()
        self.preview=shadow.staging_plan(self.plan,{**self.report,'plan_sha256':self.plan['plan_sha256']},self.root)
        with patch.object(subject.planning,'inspect_one',return_value=self.plan) as inspect, \
                patch.object(subject.planning,'inspect') as full:
            self.assertIsNone(self.subject(self.plan,self.preview))
        inspect.assert_called_once();full.assert_not_called()

    def test_explicit_source_mode_rechecks_exact_keys_not_offset(self):
        self.plan['selection_mode']='explicit-source-keys-v1';self.plan['max_attempts']=1
        self.plan['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in self.plan.items() if k!='plan_sha256'})).hexdigest()
        self.preview=shadow.staging_plan(self.plan,{**self.report,'plan_sha256':self.plan['plan_sha256']},self.root)
        with patch.object(subject.planning,'inspect_explicit',return_value=self.plan) as explicit, \
                patch.object(subject.planning,'inspect_one') as positional:
            self.assertIsNone(self.subject(self.plan,self.preview))
        self.assertEqual(explicit.call_args.kwargs['source_keys'],(self.plan['sources'][0]['key'],))
        positional.assert_not_called()
        with patch.object(subject.planning,'inspect_explicit',return_value={}),self.assertRaisesRegex(ValueError,'source, consumers or checkpoint changed'):
            self.subject(self.plan,self.preview)

    def test_live_plan_or_preview_drift_fails_closed(self):
        for changed in ({**self.plan,'reader_generation':'f'*64},{**self.plan,'sources':[]}):
            with self.subTest(changed=changed),patch.object(subject.planning,'inspect',return_value=changed):
                with self.assertRaisesRegex(ValueError,'source, consumers or checkpoint changed'):
                    self.subject(self.plan,self.preview)
        preview=copy.deepcopy(self.preview);preview['actions'][0]['files'][0]['sha256']='0'*64
        with patch.object(subject.planning,'inspect') as inspect,self.assertRaisesRegex(ValueError,'digest differs'):
            self.subject(self.plan,preview)
        inspect.assert_not_called()
    def test_untrusted_success_flags_never_replace_live_reads(self):
        preview=copy.deepcopy(self.preview);preview['external_writers_verified']=True
        preview['staging_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in preview.items() if k!='staging_sha256'})).hexdigest()
        with patch.object(subject.planning,'inspect') as inspect,self.assertRaisesRegex(ValueError,'unpromoted plan'):
            self.subject(self.plan,preview)
        inspect.assert_not_called()


if __name__=='__main__':unittest.main()
