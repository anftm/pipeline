"""One-source shadow retains exact plan and never writes remotely."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import yaml
from scripts import reader_epub_one_shadow as subject,search_snapshot
from tests.test_reader_epub_shadow import epub,plan


class OneShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.output=Path(self.tmp.name)/'shadow';self.raw=epub();self.plan=plan(self.raw)
        self.plan['max_attempts']=1
        self.plan['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in self.plan.items() if k!='plan_sha256'})).hexdigest()
        self.hub,self.api,self.fs,self.client=Mock(),Mock(),Mock(),Mock()
    def run_shadow(self):
        return subject.build(hub=self.hub,api=self.api,fs=self.fs,client=self.client,
            shard=self.plan['shard'],operation=self.plan['operation'],output=self.output)
    def test_one_source_receipts_and_exact_second_observation(self):
        with patch.object(subject.planning,'inspect_one',return_value=self.plan) as inspect, \
                patch.object(subject.shadow,'download',side_effect=AssertionError('not used')):
            # Bind a local fake downloader while keeping the real parser.
            original=subject.shadow.build
            with patch.object(subject.shadow,'build',side_effect=lambda plan,output,token:original(
                    plan,output,fetch=lambda _u,p,_t:p.write_bytes(self.raw))):
                result=self.run_shadow()
        self.assertEqual(inspect.call_count,2);self.assertEqual(result['source_count'],1)
        self.assertEqual(result['remote_writes'],0);self.assertFalse(result['publication_ready'])
        self.assertFalse(result['index_written']);self.assertGreater(result['object_count'],1)
        self.assertEqual(json.loads((self.output/'plan.json').read_bytes()),self.plan)
        self.assertTrue((self.output/'staging-preview.json').is_file())
    def test_source_drift_stops_before_success_receipt(self):
        changed={**self.plan,'search_generation':'f'*64}
        original=subject.shadow.build
        with patch.object(subject.planning,'inspect_one',side_effect=[self.plan,changed]), \
                patch.object(subject.shadow,'build',side_effect=lambda plan,output,token:original(
                    plan,output,fetch=lambda _u,p,_t:p.write_bytes(self.raw))):
            with self.assertRaisesRegex(ValueError,'changed during'):self.run_shadow()
        self.assertFalse((self.output/'summary.json').exists())
    def test_workflow_is_manual_and_cannot_write_bucket(self):
        path=Path(__file__).resolve().parents[1]/'.github/workflows/reader-epub-one-shadow.yml'
        flow=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']),{'workflow_dispatch'});self.assertEqual(flow['permissions'],{'contents':'read'})
        steps=flow['jobs']['build']['steps'];commands='\n'.join(x.get('run','') for x in steps)
        self.assertIn('reader_epub_one_shadow.py',commands)
        for forbidden in ('--apply','workflow run','git push','delete','HF_S3_SECRET_ACCESS_KEY'):
            self.assertNotIn(forbidden,commands)
        artifact=next(x for x in steps if x.get('uses')=='actions/upload-artifact@v7')
        self.assertNotIn('source.epub',artifact['with']['path'])


if __name__=='__main__':unittest.main()
