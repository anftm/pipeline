"""Unmapped image/media windows never pretend to publish or lose other items."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PIL import Image

from scripts import reader_unmapped_batches as subject,reader_threshold_autobuild as conversion
from tests.test_reader_threshold_autobuild import source


class WindowTests(unittest.TestCase):
    def test_windows_are_distinct_and_unverified_items_remain_unselected(self):
        records={source(i)['key']:source(i) for i in range(12)}
        canonical={'version':1,'kind':'image-stream-index','files':[]};shards={i:{} for i in range(8)}
        def metadata(_api,requests):
            return {(r,v,p):SimpleNamespace(path=p,size=5,lfs={'sha256':hashlib.sha256(p.encode()).hexdigest()},blob_id=None)
                    for r,v,p in requests}
        with patch.object(conversion.backlog,'_metadata',side_effect=metadata):
            a=conversion.select_unmapped('jpg',records,{},canonical,shards,Mock(),start=0,window=4,limit=4)
            b=conversion.select_unmapped('jpg',records,{},canonical,shards,Mock(),start=4,window=4,limit=4)
        self.assertEqual(len(a),4);self.assertEqual(len(b),4)
        self.assertFalse({r['key'] for r in a}&{r['key'] for r in b})
        for bad in (-1,True,1.2):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                conversion.select_unmapped('jpg',records,{},canonical,shards,Mock(),start=bad)


class UnmappedBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.out=self.root/'out';self.dest=self.root/'export'
        buffer=io.BytesIO();Image.new('RGB',(16,9),'blue').save(buffer,'JPEG');self.raw=buffer.getvalue()
        self.row={**source(1,size=len(self.raw)), 'source_bytes':len(self.raw),'source_proof':'lfs-sha256',
                  'source_identity':hashlib.sha256(self.raw).hexdigest(),'shard':0}
        self.plan={'version':1,'kind':'reader-unmapped-shadow-plan','extension':'jpg',
                   'search_generation':'a'*64,'reader_generation':'b'*64,
                   'selection_window':{'start':0,'window':100,'limit':100},'sources':[self.row],
                   'remote_writes':0,'publication_ready':False}
    def run_batch(self,fetch=None):
        return subject.run('jpg',hub=Mock(),api=Mock(),fs=Mock(),client=Mock(),start=0,window=100,output=self.out,
                           fetch=fetch or (lambda _u,p,_t:p.write_bytes(self.raw)))
    def test_real_jpg_export_matches_and_excludes_native_file(self):
        with patch.object(conversion,'inspect_unmapped',side_effect=[self.plan,self.plan]):result=self.run_batch()
        self.assertEqual(result['converted_sources'],1);self.assertFalse(result['publication_ready'])
        exported=subject.export(self.out,self.dest);self.assertEqual(exported['converted_sources'],1)
        self.assertTrue((self.dest/'reader-converted/item-000/document.webp').is_file())
        self.assertFalse(any(p.name=='source.jpg' for p in self.dest.rglob('*')))
    def test_failing_one_does_not_skip_next(self):
        second={**self.row,'key':self.row['key']+'2'};self.plan['sources'].append(second)
        calls=0
        def fetch(_u,p,_t):
            nonlocal calls;calls+=1
            if calls==1:raise TimeoutError('source retry needed')
            p.write_bytes(self.raw)
        with patch.object(conversion,'inspect_unmapped',side_effect=[self.plan,self.plan]):result=self.run_batch(fetch)
        self.assertEqual(result['failed_sources'],1);self.assertEqual(result['converted_sources'],1)
        self.assertEqual(subject.export(self.out,self.dest)['converted_sources'],1)
    def test_workflow_preserves_verified_outputs_and_does_not_publish(self):
        import yaml
        path=Path(__file__).resolve().parents[1]/'.github/workflows/reader-unmapped-batches.yml'
        flow=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']),{'workflow_dispatch'});self.assertEqual(flow['permissions'],{'contents':'read'})
        steps=flow['jobs']['convert']['steps'];commands='\n'.join(x.get('run','') for x in steps)
        self.assertIn('reader_unmapped_batches.py',commands)
        for forbidden in ('--apply','git push','workflow run'):
            self.assertNotIn(forbidden,commands)
        artifact=next(x for x in steps if x.get('with',{}).get('path')=='output/export/reader-converted/')
        self.assertEqual(artifact['with']['include-hidden-files'],'true')

    def test_snapshot_drift_and_tampered_file_never_export(self):
        changed={**self.plan,'reader_generation':'c'*64}
        with patch.object(conversion,'inspect_unmapped',side_effect=[self.plan,changed]):
            with self.assertRaisesRegex(ValueError,'changed during'):self.run_batch()
        with self.assertRaisesRegex(ValueError,'incomplete or altered'):subject.export(self.out,self.dest)
        with tempfile.TemporaryDirectory() as tmp:
            self.out=Path(tmp)/'out';self.dest=Path(tmp)/'export'
            with patch.object(conversion,'inspect_unmapped',side_effect=[self.plan,self.plan]):self.run_batch()
            f=self.out/'item-000/0/document.webp';f.write_bytes(f.read_bytes()+b'x')
            with self.assertRaisesRegex(ValueError,'changed before export'):subject.export(self.out,self.dest)


if __name__=='__main__':unittest.main()
