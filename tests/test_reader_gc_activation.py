"""Maintenance activation must allow read-only credential verification first."""
from pathlib import Path
import unittest
import yaml


class MaintenanceActivationTests(unittest.TestCase):
    def test_preflight_cannot_pause_or_restore_producers(self):
        workflow = yaml.safe_load(Path('.github/workflows/reader-maintenance.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        self.assertIs(triggers['workflow_dispatch']['inputs']['preflight_only']['default'], False)
        job = workflow['jobs']['maintain']
        self.assertIn("vars.READER_GC_AUTOMATION_ENABLED == 'true'", job['if'])
        self.assertIn("github.event_name == 'workflow_dispatch'", job['if'])
        steps = {s.get('name'): s for s in job['steps'] if s.get('name')}
        self.assertNotIn('if', steps['Verify every producer credential before isolation'])
        self.assertEqual(steps['Pause producers, collect and restore']['if'], 'inputs.preflight_only != true')
        self.assertEqual(steps['Recover recorded producer state after an interrupted orchestration']['if'],
                         'always() && inputs.preflight_only != true')
