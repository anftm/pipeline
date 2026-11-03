from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class WorkflowContractTests(unittest.TestCase):
    def workflow(self, name):
        # PyYAML treats YAML 1.1 `on` as True; GitHub treats it as the key `on`.
        value = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
        return value, value.get('on', value.get(True))

    def test_ocr_schedule_removed_but_legacy_schedule_kept(self):
        _, triggers = self.workflow('reader-gc-ccrd.yml')
        self.assertEqual(triggers['schedule'], [{'cron': '23 10 * * *'}])
        self.assertIn('workflow_dispatch', triggers)
        source = (ROOT / '.github/workflows/reader-gc-ccrd.yml').read_text()
        self.assertIn("github.event_name == 'schedule' && 'legacy-generations'", source)

    def test_withdraw_workflow_is_manual_and_uses_ccrd_lock(self):
        workflow, triggers = self.workflow('ccrd-ocr-withdraw-consumer.yml')
        self.assertEqual(set(triggers), {'workflow_dispatch'})
        self.assertEqual(workflow['concurrency']['group'], 'publish-ccrd-index')
        self.assertEqual(workflow['jobs']['withdraw']['concurrency']['group'], 'reader-global-maintenance')
        inputs = triggers['workflow_dispatch']['inputs']
        self.assertFalse(inputs['apply']['default'])
        self.assertTrue(all(inputs[key]['required'] for key in
                            ('space_revision', 'ocr_generation', 'ocr_release_sha256',
                             'base_activation_sha256')))


if __name__ == '__main__':
    unittest.main()
