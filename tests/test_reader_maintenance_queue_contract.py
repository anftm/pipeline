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
        checked_names = {name for name, _ in checked}
        self.assertTrue({'reader-maintenance.yml', 'reader-gc-collect.yml',
                         'reader-v3-publish.yml', 'publish-ccrd-index.yml'} <= checked_names)

    def test_retired_ocr_audit_has_no_workflow(self):
        self.assertFalse((WORKFLOWS / 'reader-ocr-audit.yml').exists())


if __name__ == '__main__':
    unittest.main()
