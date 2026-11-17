from pathlib import Path
import unittest
import yaml


class CcrdNormalAutomationTests(unittest.TestCase):
    def test_staged_artifact_precedes_gated_publication_same_job(self):
        root=Path(__file__).parents[1]
        flow=yaml.load((root/'.github/workflows/publish-ccrd-index.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(flow['concurrency']['group'],'publish-ccrd-index')
        steps=flow['jobs']['stage']['steps'];names=[s.get('name') for s in steps]
        publish=next(s for s in steps if s.get('name')=='Publish verified staged update when automation is enabled')
        self.assertIn("steps.stage.outputs.status == 'staged'",publish['if'])
        self.assertIn("vars.CCRD_AUTOMATION_ENABLED == 'true'",publish['if'])
        self.assertLess(names.index('Preserve exact staged candidate for recovery'),names.index(publish['name']))
        self.assertIn('ccrd_release_driver.py',publish['run'])
        self.assertIn('--known-writers-coordinated',publish['run'])
        self.assertIn('ccrd-stage/bundle',publish['run'])
        self.assertNotIn('--resume',publish['run'])
        self.assertEqual(next(s for s in steps if s.get('name')=='Preserve stage report')['if'],'always()')
