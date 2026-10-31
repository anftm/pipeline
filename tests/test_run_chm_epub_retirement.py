import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from scripts import run_chm_epub_retirement as runner
from scripts import chm_epub_publication_safety as safety


class RunChmRetirementTests(unittest.TestCase):
    def test_four_continuations_delete_only_after_actual_graph_and_restore_on_final(self):
        rows=[{'bucket':safety.chm.ASSETS,'path':f'native/ebook/chm/{i}/document.epub',
               'bytes':1,'sha256':safety.chm.digest(b'x')} for i in range(10)]
        objects={row['path']:b'x' for row in rows}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self,bucket,path):
                if path not in objects:raise FileNotFoundError(path)
                return objects[path]
            def delete(self,bucket,paths):
                for path in paths:objects.pop(path)
        class Fence:
            def check(self):pass
        proof={'mode':'actual','graph_complete':True,'target_count':len(rows),'retained_targets':0,
               'origins':[],'ambiguous_paths':[],'blockers':[],'root_recheck_complete':True,
               'reader_acceptance_complete':True,'root_members':[],'root_sha256':[],
               'closure_sha256':[],'retired_roots':[]}
        state={'kind':'chm-epub-retirement','maintenance_id':'window','complete':False,
               'handoff_complete':True,'retirement_started':True,'both_surfaces_accepted':True,
               'staged_controls':[],'post_ack_controls':[],'batches':[]}
        maintenance={'maintenance_id':'window','callback_plan_sha256':'digest',
                     'callback_mutation_started':True,'callback_recovery_required':True}
        plan={};updates={};events=[]
        def persist(store,phase,value,output):value['phase']=phase;events.append(phase)
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(runner,'OUTPUT',Path(directory)))
            stack.enter_context(patch.object(runner.journal,'load_plan',return_value=(plan,{},updates,'digest')))
            stack.enter_context(patch.object(runner.journal,'load_progress',return_value=state))
            stack.enter_context(patch.object(runner.journal,'verify_initial_chapter_evidence'))
            stack.enter_context(patch.object(runner.journal,'save_progress',side_effect=persist))
            for name in ('validate_publication_order','verify_live_chm_controls','verify_live_processing_controls',
                         'verify_predelete_chapter_inventory','verify_retired_rollback_aliases',
                         'validate_document_reset_identity','verify_document_reset_prerequisites',
                         'recheck_actual_graph_by_version'):
                stack.enter_context(patch.object(safety,name))
            stack.enter_context(patch.object(safety,'observe_planned_projection',return_value={}))
            graph=stack.enter_context(patch.object(safety,'actual_graph_evidence',return_value=(proof,object())))
            stack.enter_context(patch.object(safety,'capture_graph_versions',return_value={}))
            saved=stack.enter_context(patch.object(runner.maintenance,'journal'))
            for stage in range(4):
                result=runner.continue_staged(Store(),Fence(),None,rows,maintenance,max_batches=3,batch_size=1)
                self.assertEqual(result['confirmed_deleted_objects'],min(3*(stage+1),10))
                self.assertEqual(result['complete'],stage==3)
                if stage<3:self.assertFalse(maintenance.get('callback_committed',False))
            self.assertEqual(graph.call_count,4)
            self.assertTrue(maintenance['callback_committed'])
            self.assertFalse(maintenance['callback_recovery_required'])
            self.assertEqual(objects,{})
            self.assertEqual(saved.call_count,1)

    def test_partial_stage_rejects_a_changed_complete_graph_before_another_delete(self):
        row={'bucket':safety.chm.ASSETS,'path':'native/ebook/chm/0/document.epub',
             'bytes':1,'sha256':safety.chm.digest(b'x')}
        state={'maintenance_id':'window','complete':False,'handoff_complete':True,
               'retirement_started':True,'both_surfaces_accepted':True,'delete_started':True,'batches':[],
               'actual_graph':{'mode':'actual','graph_complete':True,'target_count':1,'retained_targets':0,
                               'origins':[],'ambiguous_paths':[],'blockers':[],'root_recheck_complete':True,
                               'reader_acceptance_complete':True,'root_members':['old'],'root_sha256':[],
                               'closure_sha256':[],'retired_roots':[]}}
        current={'maintenance_id':'window','callback_plan_sha256':'digest'}
        proof={'mode':'actual','graph_complete':True,'target_count':1,'retained_targets':0,'origins':[],
               'ambiguous_paths':[],'blockers':[],'root_recheck_complete':True,'reader_acceptance_complete':True,
               'root_members':['changed'],'root_sha256':[],'closure_sha256':[],'retired_roots':[]}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self,bucket,path):return b'x'
            def delete(self,*args):raise AssertionError('must not delete')
        class Fence:
            def check(self):pass
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch.object(runner.journal,'load_plan',return_value=({}, {}, {}, 'digest')))
            stack.enter_context(patch.object(runner.journal,'load_progress',return_value=state))
            stack.enter_context(patch.object(runner.journal,'verify_initial_chapter_evidence'))
            for name in ('validate_publication_order','verify_live_chm_controls','verify_live_processing_controls',
                         'verify_predelete_chapter_inventory','verify_retired_rollback_aliases',
                         'validate_document_reset_identity','verify_document_reset_prerequisites'):
                stack.enter_context(patch.object(safety,name))
            stack.enter_context(patch.object(safety,'observe_planned_projection',return_value={}))
            stack.enter_context(patch.object(safety,'actual_graph_evidence',return_value=(proof,object())))
            with self.assertRaisesRegex(ValueError,'root closure changed after partial deletion'):
                runner.continue_staged(Store(),Fence(),None,[row],current)

    def test_continuation_rejects_wrong_owner_or_an_active_prior_run(self):
        current={'kind':'reader-maintenance-journal','callback_owner_run_id':'123',
                 'callback_mutation_started':True,'callback_handoff_ready':True}
        owner={'id':123,'path':runner.WORKFLOW,'head_branch':'main','event':'workflow_dispatch',
               'status':'in_progress'}
        self.assertTrue(runner.require_owned_continuation(current,owner,'123','continue'))
        with self.assertRaisesRegex(ValueError,'still active'):
            runner.require_owned_continuation(current,owner,'124','initial')
        with self.assertRaisesRegex(ValueError,'does not own'):
            runner.require_owned_continuation(current,{**owner,'id':124},'123','continue')
        with self.assertRaisesRegex(ValueError,'does not own'):
            runner.require_owned_continuation(current,{**owner,'path':'.github/workflows/foreign.yml'},'123','continue')
        self.assertTrue(runner.require_owned_continuation(current,{**owner,'status':'completed'},'124','initial'))
        with self.assertRaisesRegex(ValueError,'share'):
            runner.require_owned_continuation(current,{**owner,'status':'completed'},'124','continue')

    def test_resume_never_adopts_a_journal_from_another_maintenance_window(self):
        current={'kind':'reader-maintenance-journal','maintenance_id':'protected-window',
                 'callback_owner_run_id':'123','callback_mutation_started':True,
                 'callback_plan_sha256':'digest','repositories':{}}
        owner={'id':123,'path':runner.WORKFLOW,'head_branch':'main','event':'workflow_dispatch',
               'status':'completed'}
        class Store:
            def read_bytes(self,bucket,path):
                return json.dumps(current if path==runner.maintenance.DURABLE_JOURNAL
                                  else {'handoff_complete':True}).encode()
        fence=Mock();fence.single.return_value=owner
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(runner,'OUTPUT',Path(directory)), \
             patch.object(runner,'MAINTENANCE',Path(directory)/'maintenance.json'), \
             patch.dict('os.environ',{'RESUME':'true','CHM_STAGE':'initial','GITHUB_RUN_ID':'124'}), \
             patch.object(runner.sys,'argv',['run_chm_epub_retirement.py']), \
             patch.object(runner,'assert_workflow_context'), \
             patch.object(runner.chm.preview,'load_inventory',return_value={'targets':[]}), \
             patch.object(runner,'ExactStore',return_value=Store()), \
             patch.object(runner,'GitHubProducerFence',return_value=fence), \
             patch.object(runner.journal,'load_plan',return_value=({}, {}, {}, 'digest')), \
             patch.object(runner.journal,'load_progress',return_value={
                 'handoff_complete':True,'maintenance_id':'foreign-window'}), \
             patch.object(runner.maintenance,'journal') as saved:
            with self.assertRaisesRegex(ValueError,'different transaction'):
                runner.main()
            saved.assert_not_called()

    def test_exact_store_cannot_delete_any_foreign_bucket_or_key(self):
        row={'bucket':'vomebook/reader-assets-v2','path':'native/ebook/chm/reviewed/document.epub'}
        store=runner.ExactStore.__new__(runner.ExactStore);store.allowed={(row['bucket'],row['path'])}
        with patch.object(runner.S3BucketStore,'delete') as deleted:
            for bucket,paths in [('vomebook/pdf-pages-v2',[row['path']]),
                                 (row['bucket'],['documents/pdf/ps/book.pdf']),
                                 (row['bucket'],[row['path'],row['path']])]:
                with self.assertRaisesRegex(ValueError,'exceeds'):
                    store.delete(bucket,paths)
            deleted.assert_not_called()
            store.delete(row['bucket'],[row['path']])
            deleted.assert_called_once_with(store,row['bucket'],[row['path']])

    def test_pages_dispatch_requires_exact_repo_pointer_and_fence(self):
        pointer={'generation':'reviewed'}
        plan={'overrides':{runner.publication.POINTER:json.dumps(pointer).encode()}}
        fence=Mock()
        with patch.dict('os.environ',{'PAGES_REPO':'vomebook/search','PAGES_TOKEN':'token'}), \
             patch.object(runner.publication,'current',return_value=(pointer,{})), \
             patch.object(runner.httpx,'post') as post:
            post.return_value.raise_for_status=Mock()
            runner.request_pages_deploy(object(),fence,plan)
            fence.check.assert_called_once()
            self.assertIn('/repos/vomebook/search/actions/workflows/static.yml/dispatches',post.call_args.args[0])
        with patch.dict('os.environ',{'PAGES_REPO':'foreign/repo','PAGES_TOKEN':'token'}), \
             patch.object(runner.httpx,'post') as post:
            with self.assertRaisesRegex(ValueError,'unexpected Pages'):
                runner.request_pages_deploy(object(),fence,plan)
            post.assert_not_called()

    def test_workflow_serializes_and_exposes_explicit_resume(self):
        path=Path(__file__).resolve().parents[1]/'.github/workflows/chm-epub-retirement.yml'
        workflow=yaml.safe_load(path.read_text())
        self.assertEqual(workflow['concurrency']['group'],'publish-ccrd-index')
        job=workflow['jobs']['retire']
        self.assertEqual(job['concurrency']['group'],'reader-global-maintenance')
        self.assertIn("inputs.confirm == 'RETIRE-972'",job['if'])
        trigger=workflow.get('on',workflow.get(True))['workflow_dispatch']['inputs']
        self.assertIn('resume',trigger)
        self.assertIn('tests.test_chm_epub_journal',next(s['run'] for s in job['steps'] if 'Test exact' in s.get('name','')))
        self.assertEqual(job['timeout-minutes'],330)
        for number, predecessor in ((1,'retire'),(2,'continue_1'),(3,'continue_2'),(4,'continue_3')):
            child=workflow['jobs'][f'continue_{number}']
            self.assertEqual(child['needs'],predecessor)
            self.assertEqual(child['timeout-minutes'],330)
            self.assertEqual(child['env']['CHM_STAGE'],'continue')
        self.assertEqual(workflow['jobs']['restore_after_stages']['needs'][-1],'continue_4')


if __name__=='__main__':
    unittest.main()
