from pathlib import Path
import unittest
import yaml

class ActivationWorkflowTests(unittest.TestCase):
    def test_proofreading_entry_uses_durable_wrapper_and_reports(self):
        root=Path(__file__).parents[1]/'.github/workflows'
        for name in ['proofread-submit.yml','proofread-submit-parse.yml']:
            flow=yaml.load((root/name).read_text(),Loader=yaml.BaseLoader)
            job=flow['jobs']['submit'];self.assertIn('PROOFREAD_DELIVERY_ENABLED',job['if'])
            commands='\n'.join(s.get('run','') for s in job['steps'])
            self.assertIn('-m scripts.proofread_delivery',commands)
            self.assertNotIn('python3 scripts/submit_proofread.py',commands)
            self.assertIn('tests.test_proofread_delivery',commands)
    def test_publisher_cannot_commit_pipeline_main(self):
        raw=(Path(__file__).parents[1]/'.github/workflows/proofread-publish-upstream.yml').read_text()
        self.assertIn('contents: read',raw);self.assertIn('scripts.proofread_upstream_delivery',raw)
        self.assertNotIn('git push',raw);self.assertNotIn('github-actions[bot]',raw)
    def test_snapshot_gc_serializes_index_and_gates_schedule(self):
        raw=(Path(__file__).parents[1]/'.github/workflows/bha-snapshot-gc.yml').read_text()
        self.assertIn('group: update-bha-index',raw);self.assertIn("vars.BHA_GC_ENABLED == 'true'",raw)
        self.assertIn('tests.test_bha_snapshot_gc',raw)
