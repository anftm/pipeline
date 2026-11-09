"""Thresholds trigger only verified bounded local conversion, never bucket writes."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from PIL import Image

from scripts import reader_threshold_autobuild as subject

GEN='a'*64


def summary(counts=None):
    counts=counts or {'epub':101,'jpg':100,'mp3':0}
    rows=[]
    for ext,family in [('epub','ebook'),('jpg','image'),('mp3','media')]:
        n=counts.get(ext,0)
        rows.append({'extension':ext,'family':family,'not_mapped':n,'source_content_changed':0,'definite_backlog':n,
                     'threshold_exceeded':n>100,'all_source_identities_checked':True,
                     'conversion_dependencies_checked':True,'source_identity_unknown':0,
                     'pending_shard_checkpoint':0,'recorded_failure_current':0})
    return {'version':1,'kind':'reader-format-backlog','threshold':100,'remote_writes':0,'dispatches':0,
            'publication_ready':False,'external_writers_verified':False,'reader_generation':GEN,
            'search_generation':'b'*64,'reader_consumers':{'pages':{'generation':GEN,'projection_verified':True},
                     'hf':{'generation':GEN,'projection_verified':True}},'formats':rows}


def source(i,ext='jpg',size=5):
    repo='VoiceOfML/Test';path=str(i)+'.'+ext
    return {'key':repo+'\0'+path,'repo':repo,'path':path,'extension':ext,'revision':'c'*40,'bytes':size}


class ThresholdBuildTests(unittest.TestCase):
    def test_only_strictly_more_than_100_triggers(self):
        self.assertEqual(subject.decision(summary()),['epub'])
        self.assertEqual(subject.decision(summary({'epub':100,'jpg':101,'mp3':150})),['jpg','mp3'])
        self.assertEqual(subject.decision(summary({'epub':0,'jpg':0,'mp3':0})),[])
    def test_unverified_or_unsupported_format_is_blocked_not_faked_success(self):
        value=summary({'epub':101,'jpg':101,'mp3':101})
        value['formats'][0]['source_identity_unknown']=1
        value['formats'][0]['all_source_identities_checked']=False
        value['formats'].append({'extension':'pptx','threshold_exceeded':True})
        self.assertEqual(subject.decision(value),['jpg','mp3'])
        self.assertEqual(subject.blocked(value),['epub','pptx'])
        value['reader_consumers']['pages']['projection_verified']=False
        with self.assertRaisesRegex(ValueError,'both Reader consumers'):subject.decision(value)
    def test_threshold_and_generation_cannot_be_forged(self):
        value=summary();value['formats'][0]['definite_backlog']=100
        with self.assertRaisesRegex(ValueError,'threshold identity'):subject.decision(value)
        value=summary();value['formats'].append(dict(value['formats'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate'):subject.decision(value)
        value=summary();value['threshold']=True
        with self.assertRaises(ValueError):subject.decision(value)
    def test_select_unmapped_excludes_existing_routes_shards_and_failures(self):
        records={source(i)['key']:source(i) for i in range(8)}
        keys=list(records);canonical={'version':1,'kind':'image-stream-index',
            'files':[{'key':keys[0]}],'failures':[{'key':keys[1]}]}
        shards={i:{} for i in range(8)};shards[0]={'version':1,'kind':'image-stream-index',
            'files':[{'key':keys[2]}],'failures':[]}
        routes={keys[3]:{'s':2}}
        api=Mock()
        def metadata(_api,requests):
            return {(repo,revision,path):SimpleNamespace(path=path,size=5,lfs={'sha256':hashlib.sha256(path.encode()).hexdigest()},blob_id=None)
                    for repo,revision,path in requests}
        with patch.object(subject.backlog,'_metadata',side_effect=metadata):
            selected=subject.select_unmapped('jpg',records,routes,canonical,shards,api)
        self.assertEqual(len(selected),3)
        self.assertFalse(set(keys[:4]) & {x['key'] for x in selected})
        self.assertTrue(all(x['source_proof']=='lfs-sha256' for x in selected))
    def test_selection_deduplicates_and_fails_closed_on_bad_indexes(self):
        records={source(i)['key']:source(i) for i in range(3)};canonical={'version':1,'kind':'image-stream-index','files':[]}
        shards={i:{} for i in range(8)}
        def metadata(_api,requests):
            return {(repo,revision,path):SimpleNamespace(path=path,size=5,lfs={'sha256':'a'*64},blob_id=None)
                    for repo,revision,path in requests}
        with patch.object(subject.backlog,'_metadata',side_effect=metadata):
            self.assertEqual(len(subject.select_unmapped('jpg',records,{},canonical,shards,Mock())),1)
        shards.pop(0)
        with self.assertRaisesRegex(ValueError,'all eight'):subject.select_unmapped('jpg',records,{},canonical,shards,Mock())
    def test_real_local_jpg_conversion_checks_source_and_produces_webp(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);buf=io.BytesIO();Image.new('RGB',(16,9),'blue').save(buf,'JPEG');raw=buf.getvalue()
            row={**source(1,size=len(raw)),'source_bytes':len(raw),'source_proof':'lfs-sha256',
                 'source_identity':hashlib.sha256(raw).hexdigest(),'shard':0}
            plan={'extension':'jpg','search_generation':'a'*64,'reader_generation':'b'*64,'sources':[row]}
            out=root/'output';result=subject.build_unmapped(plan,out,token='fixture',fetch=lambda _u,p,_t:p.write_bytes(raw))
            self.assertEqual(result['sources'][0]['source_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertTrue((out/'0/document.webp').is_file());self.assertEqual(result['remote_writes'],0)
            self.assertFalse(result['index_written']);self.assertFalse(result['publication_ready'])
            with patch.object(subject,'convert_local') as convert:
                row['source_identity']='f'*64
                with self.assertRaisesRegex(ValueError,'pinned content identity'):
                    subject.build_unmapped(plan,root/'second',token=None,fetch=lambda _u,p,_t:p.write_bytes(raw),convert=convert)
                convert.assert_not_called()
    def test_source_proof_type_and_length_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'source';p.write_bytes(b'data')
            with self.assertRaisesRegex(ValueError,'unknown source identity'):
                subject._source_digest(p,{'source_bytes':4,'source_proof':'forged','source_identity':'x'})
            with self.assertRaisesRegex(ValueError,'pinned size'):
                subject._source_digest(p,{'source_bytes':3,'source_proof':'git-blob','source_identity':'x'})
    def test_completion_requires_every_report_and_records_blocked_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);decision={'kind':'reader-threshold-local-build-decision','formats':['jpg'],'blocked_formats':[]}
            with self.assertRaisesRegex(ValueError,'missing=jpg'):subject.verify_completion(decision,root)
            (root/'jpg').mkdir();(root/'jpg/report.json').write_text(json.dumps({'extension':'jpg','remote_writes':0,
                'publication_ready':False,'sources':[{'key':'x'}]}))
            subject.verify_completion(decision,root)
            decision['blocked_formats']=['pdf']
            with self.assertRaisesRegex(ValueError,'blocked=pdf'):subject.verify_completion(decision,root)
    def test_workflow_converts_threshold_formats_without_secret_write_access(self):
        path=Path(__file__).resolve().parents[1]/'.github/workflows/reader-backlog-monitor.yml'
        flow=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(flow['permissions'],{'contents':'read'})
        steps=flow['jobs']['inspect']['steps'];names={x.get('name'):x for x in steps}
        for ext in ('EPUB','JPG','MP3'):
            step=names['Build bounded '+ext+' conversion shadow']
            self.assertIn('reader_threshold_autobuild.py build',step['run'])
            self.assertNotIn('HF_S3_ACCESS_KEY_ID',step.get('env',{}))
        self.assertIn('verify',names['Verify every triggered format completed']['run'])
        commands='\n'.join(x.get('run','') for x in steps)
        for forbidden in ('--apply','git push','workflow run','delete'):
            self.assertNotIn(forbidden,commands)


if __name__=='__main__':unittest.main()
