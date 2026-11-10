"""Retained converted chapters may be rebound only to freshly checked sources."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from scripts import reader_epub_bundle_refresh as subject,reader_epub_shadow as shadow,search_snapshot
from tests.test_reader_epub_shadow import epub,plan


class BundleRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.retained=self.root/'retained';self.output=self.root/'refreshed'
        raw=epub();self.old=plan(raw)
        self.report=shadow.build(self.old,self.retained,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.preview=shadow.staging_plan(self.old,self.report,self.retained)
        for name,value in (('plan.json',self.old),('report.json',self.report),('staging-preview.json',self.preview)):
            (self.retained/name).write_text(json.dumps(value))
        # Exports intentionally do not contain native EPUBs.
        (self.retained/'0'/'source.epub').unlink()
        self.current=copy.deepcopy(self.old);self.current['max_attempts']=1
        self.current['selection_mode']='explicit-source-keys-v1'
        self.current['search_generation']='6'*64;self.current['reader_generation']='7'*64
        self.current['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in self.current.items() if k!='plan_sha256'})).hexdigest()
    def refresh(self):
        return subject.refresh(self.retained,self.output,hub=Mock(),api=Mock(),fs=Mock(),client=Mock())
    def test_valid_bundle_rebinds_fresh_plan_without_native_source(self):
        with patch.object(subject.planning,'inspect_explicit',return_value=self.current), \
                patch.object(subject.live,'revalidate_staging_plan') as verify:
            result=self.refresh()
        self.assertEqual(verify.call_count,4);self.assertEqual(result['source_count'],1)
        self.assertEqual(result['objects'],len(self.report['sources'][0]['files']))
        self.assertFalse((self.output/'0'/'source.epub').exists());self.assertEqual(result['remote_writes'],0)
        self.assertEqual(json.loads((self.output/'plan.json').read_bytes()),self.current)
        self.assertEqual(json.loads((self.output/'staging-preview.json').read_bytes())['actions'][0]['previous_manifest'],self.old['sources'][0]['previous_manifest'])
    def test_changed_source_or_stale_identity_blocks_before_output(self):
        current=copy.deepcopy(self.current);current['sources'][0]['current_source_sha256']='f'*64
        with patch.object(subject.planning,'inspect_explicit',return_value=current),self.assertRaisesRegex(ValueError,'no longer matches'):
            self.refresh()
        self.assertFalse(self.output.exists())
        with patch.object(subject.planning,'inspect_explicit',return_value=self.current), \
                patch.object(subject.live,'revalidate_staging_plan',side_effect=ValueError('source drift')):
            with self.assertRaisesRegex(ValueError,'source drift'):self.refresh()
        self.assertFalse(self.output.exists())
    def test_tampered_bundle_or_missing_live_check_is_rejected(self):
        member=self.retained/'0'/'bundle'/'chapter-manifest.json';member.write_bytes(member.read_bytes()+b' ')
        with patch.object(subject.planning,'inspect_explicit',return_value=self.current), \
                patch.object(subject.live,'revalidate_staging_plan'):
            with self.assertRaises(ValueError):self.refresh()
        with self.assertRaisesRegex(ValueError,'fresh EPUB source'):
            shadow.bound_staging_plan(self.old,self.report,self.retained)


if __name__=='__main__':unittest.main()
