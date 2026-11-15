import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).parents[1]


class ResumeWorkflowTests(unittest.TestCase):
    def test_resume_requires_current_main_explicit_identity_and_coordination(self):
        w = yaml.load((ROOT / '.github/workflows/search-daily-resume.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(w['on']), {'workflow_dispatch'})
        self.assertEqual(w['concurrency'], {'group': 'daily-sync', 'cancel-in-progress': 'false', 'queue': 'max'})
        inputs = w['on']['workflow_dispatch']['inputs']
        for field in ('transaction', 'generation', 'expected_parent', 'pages_revision', 'space_revision'):
            self.assertEqual(inputs[field]['required'], 'true')
        self.assertEqual(inputs['known_writers_coordinated']['default'], 'false')
        steps = w['jobs']['resume']['steps']
        gate = next(s for s in steps if s.get('name') == 'Require exact current main and coordinated writers')
        for text in ('"$ENABLED" == true', '"$WRITERS" == true', '"$GITHUB_REF" == refs/heads/main',
                     '"$GITHUB_WORKFLOW_SHA" == "$GITHUB_SHA"', 'git/ref/heads/main'):
            self.assertIn(text, gate['run'])
        safety = next(s for s in steps if s.get('name') == 'Verify publication and recovery contracts')
        apply = steps[-1]
        self.assertLess(steps.index(gate), steps.index(safety))
        self.assertLess(steps.index(safety), steps.index(apply))
        self.assertIn('--resume-transaction "$TRANSACTION"', apply['run'])
        self.assertIn('--known-writers-coordinated', apply['run'])
        for field in ('generation', 'expected-parent', 'pages-revision', 'space-revision'):
            self.assertIn('--' + field, apply['run'])
        self.assertNotIn('continue-on-error', apply)
        self.assertNotIn('if', apply)
        self.assertNotIn('workflow_dispatch', apply['run'])

    def test_gc_verifies_current_consumer_roots_before_apply(self):
        w = yaml.load((ROOT / '.github/workflows/search-bucket-gc.yml').read_text(), Loader=yaml.BaseLoader)
        steps = w['jobs']['gc']['steps']
        apply = next(s for s in steps if s.get('name') == 'Run bounded search bucket GC')
        self.assertEqual(apply['env']['PAGES_TOKEN'], '${{ secrets.PAGES_TOKEN }}')
        self.assertTrue(any('tests.test_search_gc_roots' in s.get('run', '') for s in steps[:steps.index(apply)]))


if __name__ == '__main__':
    unittest.main()
