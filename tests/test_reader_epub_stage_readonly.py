"""EPUB stage inspection cannot confuse missing or changed bytes for a receipt."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from scripts import reader_epub_stage_readonly as subject,reader_epub_shadow as shadow,reader_epub_plan
from scripts import reader_epub_stage_journal as journal,reader_epub_protection,search_snapshot
from tests.test_reader_epub_shadow import epub,plan
from tests.test_reader_epub_stage_journal import MemoryJournal,WHEN


class Reader:
    def __init__(self):self.records={};self.objects={};self.events=[]
    def protection(self,path):
        self.events.append(('protection',path))
        if path not in self.records:raise FileNotFoundError(path)
        return self.records[path]
    def object_identity(self,root,path,expected):
        self.events.append(('object',root+'/'+path))
        if root+'/'+path not in self.objects:raise FileNotFoundError(path)
        return self.objects[root+'/'+path]


class StageReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'shadow';raw=epub();self.plan=plan(raw)
        source=self.plan['sources'][0];self.key=source['key']
        self.canonical={'version':1,'kind':'ebook-chapter-stream-index','files':[
            {'key':self.key,'source_revision':source['previous_revision'],
             'source_sha256':source['previous_source_sha256'],'manifest':source['previous_manifest'],
             'bucket':subject.backlog.READER_BUCKET}],'failures':[]}
        self.shard={'version':1,'kind':'ebook-chapter-stream-index','files':[dict(self.canonical['files'][0])],'failures':[]}
        self.revisions={source['repo']:source['source_revision']}
        self.plan['source_revisions_sha256']=__import__('hashlib').sha256(search_snapshot.canonical(self.revisions)).hexdigest()
        self.plan['canonical_index_sha256']=__import__('hashlib').sha256(search_snapshot.canonical(self.canonical)).hexdigest()
        self.plan['shard_index_sha256']=__import__('hashlib').sha256(search_snapshot.canonical(self.shard)).hexdigest()
        self.plan['plan_sha256']=__import__('hashlib').sha256(search_snapshot.canonical({k:v for k,v in self.plan.items() if k!='plan_sha256'})).hexdigest()
        self.report=shadow.build(self.plan,self.root,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.preview=shadow.staging_plan(self.plan,self.report,self.root)
        self.store=MemoryJournal();self.prepared=journal.begin(self.store,self.plan,self.preview,WHEN)
        self.reader=Reader();action=self.preview['actions'][0]
        self.reader.records[action['protection_path']]=search_snapshot.canonical(reader_epub_protection.make_record(
            self.plan['operation'],self.plan['plan_sha256'],action['root'],WHEN))
        for row in action['files']:self.reader.objects[row['remote']]={'sha256':row['sha256'],'bytes':row['bytes']}
        self.ptr=subject.backlog.search_publication_pointer.make_pointer(self.plan['search_generation'],None,'1'*32)
        self.hub=Mock();self.hub.read_bytes.side_effect=lambda _b,p:self.ptr if p==subject.backlog.CURRENT else b'manifest'
        self.api=Mock();self.api.repo_info.return_value=Mock(sha=source['source_revision'])
        self.fs=Mock();self.client=Mock();self.reader_pointer={'generation':self.plan['reader_generation']}
        self.catalog={'base_sidecar':{'sha256':'fixture'}}
        self.route={'s':2,'m':'e','b':subject.backlog.READER_BUCKET,'p':source['previous_manifest']}
        self.receipt={'generation':self.plan['reader_generation'],'projection_verified':True}
        self.patches=[patch.object(subject.backlog,'capture_search',return_value=(self.ptr,b'manifest',{},self.revisions,{})),
            patch.object(subject.backlog,'source_records',return_value={self.key:{'revision':source['source_revision']}}),
            patch.object(reader_epub_plan,'inspect',side_effect=lambda **kw:self.plan),
            patch.object(subject.backlog.publication,'current',return_value=(self.reader_pointer,self.catalog)),
            patch.object(subject.backlog.publication,'pinned_base',return_value={'f':{self.key:self.route}}),
            patch.object(subject.backlog.publication,'project_sidecar',return_value={'f':{self.key:self.route}}),
            patch.object(subject.backlog.publication,'observe_consumer',return_value=self.receipt),
            patch.object(subject.backlog.matrix,'read_json',side_effect=lambda _f,_b,p:self.canonical if p.endswith('/index.json') else self.shard),
            patch.object(subject.backlog,'_metadata',return_value={(source['repo'],source['source_revision'],source['path']):
                {'lfs':{'sha256':source['current_source_sha256']}}})]
        for item in self.patches:item.start();self.addCleanup(item.stop)
    def inspect(self):
        return subject.inspect(self.store,self.plan['operation'],hub=self.hub,api=self.api,fs=self.fs,
                               client=self.client,reader=self.reader)
    def test_workflow_is_manual_and_has_no_mutating_command(self):
        import yaml
        path=Path(__file__).resolve().parents[1]/'.github/workflows/reader-epub-stage-inspect.yml'
        flow=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']),{'workflow_dispatch'})
        self.assertEqual(flow['permissions'],{'contents':'read'})
        commands='\n'.join(x.get('run','') for x in flow['jobs']['inspect']['steps'])
        self.assertIn('reader_epub_stage_readonly.py',commands)
        for unsafe in ('--apply','git push','workflow run','delete'):
            self.assertNotIn(unsafe,commands)

    def test_complete_stage_is_independently_read_and_never_written(self):
        claimed=journal.claim(self.store,self.prepared);action=self.preview['actions'][0]
        outcomes=[{'kind':'protection','path':action['protection_path'],'status':'written'}]
        outcomes += [{'kind':'object','path':x['remote'],'status':'written'} for x in action['files']]
        receipt={'version':1,'kind':'reader-epub-protected-staging','operation':self.plan['operation'],
                 'plan_sha256':self.plan['plan_sha256'],'staging_sha256':self.preview['staging_sha256'],
                 'protected_sources':1,'verified_objects':len(action['files']),'outcomes':outcomes,
                 'index_written':False,'pointer_written':False,'publication_ready':False,'external_writers_verified':False}
        journal.record_staged(self.store,claimed,receipt);before=len(self.store.puts)
        report=self.inspect();self.assertTrue(report['candidate_complete']);self.assertEqual(report['missing_objects'],0)
        self.assertEqual(report['remote_writes'],0);self.assertFalse(report['publication_ready'])
        self.assertEqual(len(self.store.puts),before)
    def test_incomplete_pending_operation_reports_missing_without_writing(self):
        journal.claim(self.store,self.prepared);self.reader.objects.clear();before=len(self.store.puts)
        report=self.inspect();self.assertFalse(report['candidate_complete']);self.assertEqual(report['present_objects'],0)
        self.assertGreater(report['missing_objects'],0);self.assertEqual(len(self.store.puts),before)
    def test_changed_object_or_fresh_plan_fails_closed(self):
        action=self.preview['actions'][0];key=action['files'][0]['remote'];self.reader.objects[key]={'sha256':'f'*64,'bytes':1}
        with self.assertRaisesRegex(ValueError,'identity differs'):self.inspect()
        self.reader.objects[key]={'sha256':action['files'][0]['sha256'],'bytes':action['files'][0]['bytes']}
        with patch.object(reader_epub_plan,'inspect',return_value={}),self.assertRaisesRegex(ValueError,'fresh EPUB backlog proof'):
            self.inspect()
    def test_consumer_or_index_drift_never_reports_ready(self):
        with patch.object(subject.backlog.publication,'observe_consumer',return_value={'generation':'f'*64,'projection_verified':True}), \
                self.assertRaisesRegex(ValueError,'consumer changed'):
            self.inspect()
        self.canonical['files'][0]['source_sha256']='d'*64
        with self.assertRaisesRegex(ValueError,'parent indexes changed'):self.inspect()


if __name__=='__main__':unittest.main()
