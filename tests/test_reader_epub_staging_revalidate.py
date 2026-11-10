"""Member checks retain the full EPUB gate and exact consumer/index observations."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from scripts import reader_epub_revalidate as subject,reader_epub_shadow as shadow
from scripts import reader_format_backlog as backlog,search_snapshot
from tests.test_reader_epub_shadow import epub,plan


class StagingLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        raw=epub();self.plan=plan(raw);source=self.plan['sources'][0];self.key=source['key']
        self.plan['selection_mode']='explicit-source-keys-v1';self.plan['max_attempts']=1
        self.revisions={source['repo']:source['source_revision']}
        self.canonical={'version':1,'kind':'ebook-chapter-stream-index','files':[{'key':self.key,
            'source_revision':source['previous_revision'],'source_sha256':source['previous_source_sha256'],
            'manifest':source['previous_manifest'],'bucket':backlog.READER_BUCKET}],'failures':[]}
        self.shard={'version':1,'kind':'ebook-chapter-stream-index','files':[dict(self.canonical['files'][0])],'failures':[]}
        for name,value in [('source_revisions_sha256',self.revisions),('canonical_index_sha256',self.canonical),('shard_index_sha256',self.shard)]:
            self.plan[name]=hashlib.sha256(search_snapshot.canonical(value)).hexdigest()
        self.plan['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in self.plan.items() if k!='plan_sha256'})).hexdigest()
        self.out=Path(self.tmp.name)/'shadow';self.report=shadow.build(self.plan,self.out,fetch=lambda _u,p,_t:p.write_bytes(raw))
        self.preview=shadow.staging_plan(self.plan,self.report,self.out)
        self.pointer=backlog.search_publication_pointer.make_pointer(self.plan['search_generation'],None,'1'*32)
        self.hub=Mock();self.hub.read_bytes.side_effect=lambda _b,p:self.pointer if p==backlog.CURRENT else b'manifest'
        self.api=Mock();self.api.repo_info.return_value=Mock(sha=source['source_revision'])
        self.fs=Mock();self.client=Mock();self.route={'s':2,'m':'e','b':backlog.READER_BUCKET,'p':source['previous_manifest']}
        self.metadata={(source['repo'],source['source_revision'],source['path']):{'path':source['path'],'lfs':{'sha256':source['current_source_sha256']}}}
        self.records={self.key:{'key':self.key,'extension':'epub','revision':source['source_revision'],'repo':source['repo'],'path':source['path']}}
        reader={'generation':self.plan['reader_generation']};catalog={'base_sidecar':{'sha256':'fixture'}}
        self.patches=[patch.object(backlog,'capture_search',return_value=(self.pointer,b'manifest',{},self.revisions,{})),
            patch.object(backlog,'source_records',return_value=self.records),patch.object(backlog.publication,'current',return_value=(reader,catalog)),
            patch.object(backlog.publication,'pinned_base',return_value={'f':{self.key:self.route}}),
            patch.object(backlog.publication,'project_sidecar',return_value={'f':{self.key:self.route}}),
            patch.object(backlog.publication,'observe_consumer',return_value={'generation':reader['generation'],'projection_verified':True}),
            patch.object(backlog.matrix,'read_json',side_effect=lambda _fs,_b,path:self.canonical if path.endswith('/index.json') else self.shard if path.endswith(f"index-{self.plan['shard']:02d}.json") else {}),
            patch.object(backlog,'_metadata',return_value=self.metadata)]
        for item in self.patches:item.start();self.addCleanup(item.stop)
        self.validator=subject.LiveStagingRevalidator(hub=self.hub,api=self.api,fs=self.fs,client=self.client)
    def test_every_call_rechecks_pinned_source_consumers_and_parent(self):
        with patch.object(subject.planning,'inspect',side_effect=AssertionError('must not run global planner')):
            self.assertIsNone(self.validator(self.plan,self.preview));self.assertIsNone(self.validator(self.plan,self.preview))
        self.assertEqual(self.api.repo_info.call_count,4)
        self.assertEqual(backlog.publication.observe_consumer.call_count,8)
    def test_stale_head_source_route_and_parent_fail_closed(self):
        self.api.repo_info.return_value=Mock(sha='f'*40)
        with self.assertRaisesRegex(ValueError,'dataset head changed'):self.validator(self.plan,self.preview)
        self.api.repo_info.return_value=Mock(sha=self.plan['sources'][0]['source_revision'])
        self.metadata.clear()
        with self.assertRaisesRegex(ValueError,'source identities are incomplete'):self.validator(self.plan,self.preview)
        source=self.plan['sources'][0];self.metadata[(source['repo'],source['source_revision'],source['path'])]={'lfs':{'sha256':source['current_source_sha256']}}
        self.route['p']='foreign'
        with self.assertRaisesRegex(ValueError,'source identities are incomplete'):self.validator(self.plan,self.preview)
        self.route['p']=source['previous_manifest'];self.shard['files'][0]['source_sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'source identities are incomplete|source, consumers or checkpoint changed'):self.validator(self.plan,self.preview)
    def test_unrelated_failure_and_unknown_identity_block_even_with_explicit_selection(self):
        source=self.plan['sources'][0]
        other=source['repo']+'\0other.epub'
        self.records[other]={'key':other,'extension':'epub','revision':source['source_revision'],
                             'repo':source['repo'],'path':'other.epub'}
        self.canonical['failures'].append({'key':other,'source_revision':source['source_revision']})
        with self.assertRaisesRegex(ValueError,'source identities are incomplete'):
            self.validator(self.plan,self.preview)
        self.canonical['failures'].clear()
        self.route_for_other={'s':2,'m':'e','b':backlog.READER_BUCKET,'p':'not-in-canonical'}
        routes={self.key:self.route,other:self.route_for_other}
        backlog.publication.project_sidecar.return_value={'f':routes}
        with self.assertRaisesRegex(ValueError,'source identities are incomplete'):
            self.validator(self.plan,self.preview)

    def test_unrelated_shard_snapshot_must_not_drift_during_check(self):
        original=backlog.matrix.read_json.side_effect
        calls={}
        # Drift an unselected shard, not the selected parent.
        n=7 if self.plan['shard']!=7 else 6
        def drift_other(fs,bucket,path):
            calls[path]=calls.get(path,0)+1
            result=original(fs,bucket,path)
            if path.endswith(f'index-{n:02d}.json') and calls[path]>1:
                return {'version':1,'kind':'ebook-chapter-stream-index','files':[],'failures':[]}
            return result
        backlog.matrix.read_json.side_effect=drift_other
        with self.assertRaisesRegex(ValueError,'shard changed during member check'):
            self.validator(self.plan,self.preview)

    def test_live_gate_blocks_member_claim_and_mutation_on_unrelated_failure(self):
        from scripts import reader_epub_member_stage as members,reader_epub_stage_journal as stage
        from tests.test_reader_epub_stage_journal import MemoryJournal,WHEN
        from tests.test_reader_epub_protected_stage import MemoryObjects,MemoryProtections
        events=[];protections=MemoryProtections(events);objects=MemoryObjects(events)
        stage_store=MemoryJournal();prepared=stage.begin(stage_store,self.plan,self.preview,WHEN)
        current=stage.claim(stage_store,prepared);member_store=MemoryJournal()
        protection=members.prepare(member_store,stage_store,current,self.preview['actions'][0]['protection_path'])
        members.apply_once(member_store,stage_store,current,protection,directory=self.out,
                           protections=protections,objects=objects,revalidate=self.validator)
        obj=self.preview['actions'][0]['files'][0]
        item=members.prepare(member_store,stage_store,current,obj['remote'])
        source=self.plan['sources'][0];other=source['repo']+'\0other.epub'
        self.records[other]={'key':other,'extension':'epub','revision':source['source_revision'],
                             'repo':source['repo'],'path':'other.epub'}
        self.canonical['failures'].append({'key':other,'source_revision':source['source_revision']})
        with self.assertRaisesRegex(ValueError,'source identities are incomplete'):
            members.apply_once(member_store,stage_store,current,item,directory=self.out,
                               protections=protections,objects=objects,revalidate=self.validator)
        after=members.load(member_store,self.plan['operation'],item.record['member_sha256'])
        self.assertEqual(after.record['state'],'prepared')
        self.assertFalse(any(kind=='create-object' for kind,_ in events))

    def test_legacy_offset_plan_is_not_silently_upgraded(self):
        value=copy.deepcopy(self.plan);value.pop('selection_mode')
        value['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in value.items() if k!='plan_sha256'})).hexdigest()
        preview={**self.preview,'plan_sha256':value['plan_sha256']}
        preview['staging_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in preview.items() if k!='staging_sha256'})).hexdigest()
        with self.assertRaisesRegex(ValueError,'requires explicit'):self.validator(value,preview)


if __name__=='__main__':unittest.main()
