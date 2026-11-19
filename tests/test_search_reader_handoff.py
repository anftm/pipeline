import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
import zipfile

from scripts import search_reader_handoff as h
from scripts.reader_format_daily import validate_daily_run, DAILY_NAME

SHA='1'*40; OLD='2'*64; GEN='3'*64; TX='4'*32; HEAD='5'*64
RID=44436570644; READER=44442500000
BEFORE={'generation':OLD}
AFTER={'generation':GEN,'transition':TX,'both_consumers_observed':True}
JOURNAL={'generation':GEN,'transition':TX,'pointer_transition':TX,'state':'complete','head_sha256':HEAD}
RUN={'id':RID,'head_sha':SHA,'head_branch':'main','event':'workflow_dispatch','path':h.SYNC_PATH,'run_attempt':1,
     'actor':{'login':h.BOT},'triggering_actor':{'login':h.BOT},'repository':{'full_name':h.REPOSITORY},
     'head_repository':{'full_name':h.REPOSITORY},'status':'completed','conclusion':'success',
     'created_at':'2026-11-19T01:23:44Z'}
STEP_NAMES=('Publish staged generation in this job','Verify fresh publication receipt and terminal journal',
            'Preserve verified Search handoff receipt','Start Reader once for the bot Search handoff')

def archive(d):
    f=io.BytesIO()
    with zipfile.ZipFile(f,'w') as z:z.writestr('handoff.json',json.dumps(d))
    return f.getvalue()

class Handoff(unittest.TestCase):
    def setUp(self):
        self.v=h.receipt(RID,SHA,BEFORE,AFTER,JOURNAL)
        self.api=Mock();self.api.get_run.return_value=RUN
        self.jobs=[{'name':'sync','conclusion':'success','head_sha':SHA,'run_id':RID,'run_attempt':1,
                    'steps':[{'name':n,'conclusion':'success'} for n in STEP_NAMES]}]
        def answer(method,path,**kw):
            self.assertEqual(method,'GET')
            if path.endswith('/jobs'):return {'total_count':len(self.jobs),'jobs':self.jobs}
            self.assertTrue(path.endswith('/attempts/1'));return RUN
        self.api.request.side_effect=answer
        self.api.runs.return_value=[{'id':READER,'display_title':f'reader-search-handoff/{RID}'}]
        self.api.report_artifact.return_value=(archive(self.v),{},'')

    def test_accepts_exact_source_and_waits_for_terminal(self):
        self.api.get_run.side_effect=[RUN|{'status':'in_progress','conclusion':None},RUN,RUN]
        sleep=Mock();self.assertEqual(h.observe(self.api,RID,SHA,READER,sleep=sleep),self.v)
        sleep.assert_called_once();self.api.check_main.assert_called_once_with(SHA)
        self.api.dispatch.assert_not_called()

    def test_source_path_attempt_actor_repository_and_terminal_are_bound(self):
        for change in ({'path':'.github/workflows/other.yml'},{'run_attempt':2}, {'actor':{'login':'me'}},
                       {'event':'schedule'},{'conclusion':'failure'}, {'head_repository':{'full_name':'fork/repo'}}):
            with self.subTest(change=change):
                self.api.get_run.return_value=RUN|change
                with self.assertRaises(ValueError):h.observe(self.api,RID,SHA,READER)
        self.api.report_artifact.assert_not_called()

    def test_from_an_incomplete_or_unverified_sync_never_accepts(self):
        for s in self.jobs[0]['steps']:
            s['conclusion']='skipped'
            with self.subTest(step=s['name']),self.assertRaisesRegex(ValueError,'publish and verify'):
                h.observe(self.api,RID,SHA,READER)
            s['conclusion']='success'
        self.jobs=[]
        with self.assertRaisesRegex(ValueError,'full Search sync'):
            h.observe(self.api,RID,SHA,READER)
        self.api.report_artifact.assert_not_called()

    def test_wrong_receipt_or_multiple_handoffs_fail(self):
        self.api.report_artifact.return_value=(archive(self.v|{'run':RID+1}),{},'')
        with self.assertRaisesRegex(ValueError,'Unbound'):h.observe(self.api,RID,SHA,READER)
        self.api.report_artifact.return_value=(archive(self.v),{},'')
        self.api.runs.return_value.append({'id':READER+1,'display_title':f'reader-search-handoff/{RID}'})
        with self.assertRaisesRegex(ValueError,'unique'):h.observe(self.api,RID,SHA,READER)

    def test_live_guard_exact_journal_even_for_same_generation(self):
        h.check_live(self.v,JOURNAL,AFTER);h.check_backlog(self.v,{'search_generation':GEN})
        for change in ({'state':'rolled-back'},{'head_sha256':'9'*64},{'pointer_transition':'9'*32}):
            with self.assertRaises(ValueError):h.check_live(self.v,JOURNAL|change,AFTER)
        with self.assertRaises(ValueError):h.check_backlog(self.v,{'search_generation':OLD})
        with self.assertRaises(ValueError):h.receipt(RID,SHA,AFTER,AFTER,JOURNAL)

    def test_single_send_and_ambiguous_denial(self):
        self.api.get_run.return_value=RUN|{'status':'in_progress','conclusion':None}
        self.api.runs.return_value=[];self.api.dispatch.side_effect=TimeoutError('unknown send')
        with self.assertRaises(TimeoutError):h.send(self.api,RID,SHA)
        self.api.dispatch.assert_called_once_with(h.DAILY,{'search_source_run':str(RID)})
        self.api.runs.return_value=[{'display_title':f'reader-search-handoff/{RID}'}]
        with self.assertRaisesRegex(ValueError,'do not resend'):h.send(self.api,RID,SHA)
        self.assertEqual(self.api.dispatch.call_count,1)

    def test_new_bot_title_can_be_a_continuation_parent(self):
        row=RUN|{'name':DAILY_NAME,'path':'.github/workflows/'+h.DAILY,'display_title':f'reader-search-handoff/{RID}'}
        validate_daily_run(row,SHA,completed=True)
        with self.assertRaises(RuntimeError):validate_daily_run(row|{'actor':{'login':'me'}},SHA)

    def test_workflow_keeps_trigger_paths_separate(self):
        import yaml
        root=Path(__file__).resolve().parents[1]/'.github'/'workflows'
        s=yaml.load((root/'search-daily-sync.yml').read_text(),Loader=yaml.BaseLoader)
        r=yaml.load((root/h.DAILY).read_text(),Loader=yaml.BaseLoader)
        job=s['jobs']['sync'];steps={x['name']:x for x in job['steps'] if 'name' in x}; names=list(steps)
        opts=steps['Prepare exact bot Search handoff for Reader']['if'];self.assertIn("github.actor == 'github-actions[bot]'",opts);self.assertIn("steps.publish.outcome == 'success'",opts);self.assertIn('READER_V3_AUTO_PAUSED',opts)
        self.assertGreater(names.index('Start Reader once for the bot Search handoff'),names.index('Verify fresh publication receipt and terminal journal'))
        self.assertEqual(job['permissions']['actions'],'write');self.assertNotIn('actions',s['permissions'])
        self.assertEqual(r['concurrency']['group'],'reader-format-daily');self.assertIn('workflow_run',r['on'])
        self.assertIn("github.event.workflow_run.path == '.github/workflows/search-daily-sync.yml'",r['jobs']['formats']['if'])
        names=[x.get('name') for x in r['jobs']['formats']['steps']]
        self.assertLess(names.index('Verify explicit bot Search handoff'),names.index('Observe exact published format backlog'))
