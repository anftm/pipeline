from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'
RETIRED_OCR_MAINTENANCE = {
    'ccrd-ocr-exact-remove.yml', 'ccrd-ocr-withdraw-consumer.yml',
    'expanded-ocr-graph-preview.yml', 'expanded-ocr-retirement.yml',
}
RETIRED_ENTRY_SCRIPTS = {
    'run_ccrd_ocr_exact_maintenance.py', 'ccrd_ocr_exact_retirement.py',
    'withdraw_ccrd_ocr_consumer.py', 'run_expanded_ocr_graph_preview.py',
    'run_expanded_ocr_retirement.py',
}


class WorkflowContractTests(unittest.TestCase):
    def workflow(self, name):
        # PyYAML treats YAML 1.1 `on` as True; GitHub treats it as the key `on`.
        value = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
        return value, value.get('on', value.get(True))

    def test_ocr_schedule_removed_but_legacy_schedule_kept(self):
        workflow, triggers = self.workflow('reader-gc-ccrd.yml')
        self.assertEqual(triggers['schedule'], [{'cron': '23 10 * * *'}])
        self.assertIn('workflow_dispatch', triggers)
        source = (ROOT / '.github/workflows/reader-gc-ccrd.yml').read_text()
        self.assertIn("github.event_name == 'schedule' && 'legacy-generations'", source)
        self.assertFalse(triggers['workflow_dispatch']['inputs']['include_staging']['default'])
        self.assertFalse(triggers['workflow_dispatch']['inputs']['apply']['default'])
        steps = workflow['jobs']['collect']['steps']
        step = next(item for item in steps if item.get('name') == 'Plan or collect retired index and OCR generations')
        self.assertEqual(step['env']['INCLUDE_STAGING'], "${{ inputs.include_staging || 'false' }}")
        self.assertNotIn("23 9 * * *", source)

    def test_retired_ocr_maintenance_has_no_workflow_entrypoints_or_calls(self):
        for name in RETIRED_OCR_MAINTENANCE:
            with self.subTest(workflow=name):
                self.assertFalse((WORKFLOWS / name).exists())
        for path in WORKFLOWS.iterdir():
            if path.suffix not in {'.yml', '.yaml'}:
                continue
            content = path.read_text(encoding='utf-8')
            for name in RETIRED_OCR_MAINTENANCE | RETIRED_ENTRY_SCRIPTS:
                with self.subTest(workflow=path.name, forbidden=name):
                    self.assertNotIn(name, content)

    def test_historical_scripts_and_workflow_identities_are_preserved(self):
        from scripts import ccrd_ocr_exact_retirement as exact, withdraw_ccrd_ocr_consumer as withdraw
        from scripts import run_expanded_ocr_graph_preview as preview, run_expanded_ocr_retirement as expanded

        for name in RETIRED_ENTRY_SCRIPTS:
            with self.subTest(script=name):
                self.assertTrue((ROOT / 'scripts' / name).is_file())
        prefix = 'anftm/pipeline/.github/workflows/'
        suffix = '@refs/heads/main'
        self.assertEqual(exact.WORKFLOW, prefix + 'ccrd-ocr-exact-remove.yml' + suffix)
        self.assertEqual(withdraw.WORKFLOW, prefix + 'ccrd-ocr-withdraw-consumer.yml' + suffix)
        self.assertEqual(preview.WORKFLOW, prefix + 'expanded-ocr-graph-preview.yml' + suffix)
        self.assertEqual(expanded.WORKFLOW, '.github/workflows/expanded-ocr-retirement.yml')

    def test_historical_entry_scripts_reject_other_workflow_contexts(self):
        from scripts import ccrd_ocr_exact_retirement as exact, withdraw_ccrd_ocr_consumer as withdraw
        from scripts import run_expanded_ocr_graph_preview as preview, run_expanded_ocr_retirement as expanded
        from scripts.reader_gc_fence import assert_workflow_context

        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline',
               'GITHUB_WORKFLOW_REF': 'anftm/pipeline/.github/workflows/unrelated.yml@refs/heads/main',
               'GITHUB_RUN_ID': '123'}
        guards = (exact.assert_writer, withdraw.assert_writer, preview.assert_context,
                  lambda: assert_workflow_context(expanded.WORKFLOW))
        with patch.dict('os.environ', env, clear=True):
            for guard in guards:
                with self.subTest(guard=guard), self.assertRaises(RuntimeError):
                    guard()


if __name__ == '__main__':
    unittest.main()
