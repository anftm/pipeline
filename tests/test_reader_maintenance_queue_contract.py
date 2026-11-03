"""Protect queued maintenance and writer work from single-slot replacement.

These checks cover current workflow definitions only. GitHub limits `max` to
100 waiting entries and historical runs keep their own workflow definitions.
"""
from pathlib import Path
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / '.github/workflows'
PROTECTED = ('publish-ccrd-index', 'reader-global-maintenance', 'reader-sidecar')


class MaintenanceQueueContractTests(unittest.TestCase):
    def test_shared_lock_users_do_not_replace_a_single_pending_run(self):
        checked = []
        for path in sorted(WORKFLOWS.glob('*.y*ml')):
            workflow = yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
            if not isinstance(workflow, dict):
                continue
            candidates = [('workflow', workflow.get('concurrency'))]
            for name, job in (workflow.get('jobs') or {}).items():
                if isinstance(job, dict):
                    candidates.append((name, job.get('concurrency')))
            for location, concurrency in candidates:
                if not isinstance(concurrency, dict):
                    continue
                group = concurrency.get('group', '').lower()
                if not any(name in group for name in PROTECTED):
                    continue
                checked.append((path.name, location))
                with self.subTest(workflow=path.name, location=location, group=group):
                    self.assertEqual(concurrency.get('cancel-in-progress'), 'false')
                    self.assertEqual(concurrency.get('queue'), 'max')
        self.assertGreater(len(checked), 50)

    def test_retired_ocr_audit_is_still_disabled(self):
        workflow = yaml.load((WORKFLOWS / 'reader-ocr-audit.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['jobs']['audit']['if'], '${{ false }}')
        triggers = workflow['on']
        self.assertEqual(set(triggers), {'workflow_dispatch'})


if __name__ == '__main__':
    unittest.main()
