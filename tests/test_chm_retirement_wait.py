import unittest
from unittest.mock import Mock, patch

from scripts import chm_retirement_wait as wait


class ChmRetirementWaitTests(unittest.TestCase):
    def test_live_producer_waits_and_does_not_cancel(self):
        run={'id':99,'path':'.github/workflows/reader-ebook-stream.yml',
             'status':'queued','updated_at':'2026-01-01T00:00:00Z','check_suite_id':7,'run_attempt':1}
        fence=Mock()
        def api(endpoint):
            if 'actions/runs?status=queued' in endpoint:return [{'workflow_runs':[run]}]
            if 'actions/runs?status=' in endpoint:return [{'workflow_runs':[]}]
            if '/jobs?' in endpoint:return [{'total_count':2,'jobs':[{'id':1},{'id':2}]}]
            if 'check-runs?' in endpoint:return [{'total_count':0,'check_runs':[]}]
            raise AssertionError(endpoint)
        fence.api.side_effect=api;fence.single.return_value=run
        self.assertEqual(wait.waiting_producers(fence),[99])
        self.assertTrue(all('/cancel' not in call.args[0] for call in fence.api.call_args_list))

    def test_only_stable_ghost_without_jobs_or_checks_is_skipped(self):
        run={'id':99,'path':'.github/workflows/reader-ebook-stream.yml',
             'status':'queued','updated_at':'2026-01-01T00:00:00Z','check_suite_id':7,'run_attempt':1}
        fence=Mock()
        def api(endpoint):
            if 'actions/runs?status=queued' in endpoint:return [{'workflow_runs':[run]}]
            if 'actions/runs?status=' in endpoint:return [{'workflow_runs':[]}]
            if '/jobs?' in endpoint:return [{'total_count':0,'jobs':[]}]
            if 'check-runs?' in endpoint:return [{'total_count':0,'check_runs':[]}]
            raise AssertionError(endpoint)
        fence.api.side_effect=api;fence.single.return_value=run
        self.assertEqual(wait.waiting_producers(fence),[])
        fence.single.side_effect=[run,{**run,'run_attempt':2}]
        self.assertEqual(wait.waiting_producers(fence),[99])

    def test_main_waits_then_continues_under_matching_workflow_context(self):
        with patch.dict('os.environ',{'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':wait.REPOSITORY,
                    'GITHUB_WORKFLOW_REF':f'{wait.REPOSITORY}/{wait.WORKFLOW}@refs/heads/main'}), \
             patch.object(wait,'GitHubProducerFence'), \
             patch.object(wait,'waiting_producers',side_effect=[[99],[],[]]), \
             patch.object(wait.time,'monotonic',side_effect=[0,1,2,3,183]), \
             patch.object(wait.time,'sleep') as sleep:
            wait.main()
            self.assertEqual(sleep.call_count,2)
            self.assertTrue(all(call.args==(60,) for call in sleep.call_args_list))
