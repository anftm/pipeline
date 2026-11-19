import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).parents[1]


class DailyFormatWorkflowTests(unittest.TestCase):
    def test_daily_success_triggers_independent_controller_with_exact_source_job(self):
        self.assertIn('requests==2.32.5', (ROOT / 'scripts/requirements.txt').read_text().splitlines())
        raw = (ROOT / '.github/workflows/reader-format-daily.yml').read_text()
        w = yaml.load(raw, Loader=yaml.BaseLoader)
        self.assertEqual(w['on']['workflow_run'], {'workflows': ['搜索 · Daily Sync', '搜索 · Resume Daily Publication'], 'types': ['completed']})
        self.assertEqual(w['on']['schedule'], [{'cron': '43 6 * * *'}])
        self.assertIn("vars.READER_FORMAT_RECHECK_ENABLED == 'true'", w['jobs']['formats']['if'])
        self.assertIn("vars.READER_V3_AUTO_PAUSED != 'true'", w['jobs']['formats']['if'])
        self.assertEqual(w['concurrency']['group'], 'reader-format-daily')
        self.assertNotIn('concurrency', w['jobs']['formats'])
        self.assertIn("github.event.workflow_run.conclusion == 'success'", w['jobs']['formats']['if'])
        steps = w['jobs']['formats']['steps']
        guard = next(s for s in steps if s.get('name') == 'Verify current code and successful source sync')
        for text in ['attempts/$SOURCE_ATTEMPT/jobs', "job.get('name') == expected_job", "sync[0].get('conclusion') != 'success'", 'git/ref/heads/main']:
            self.assertIn(text, guard['run'])
        inspect = next(s for s in steps if s.get('name') == 'Observe exact published format backlog')
        self.assertIn('from scripts.reader_format_daily import THRESHOLD', inspect['run'])
        self.assertIn('--threshold "$threshold"', inspect['run'])
        apply = next(s for s in steps if s.get('name') == 'Convert and publish triggered formats to completion')
        self.assertIn("env.READER_AUTOMATION_ENABLED == 'true'", apply['if'])
        self.assertIn("env.READER_PAUSED != 'true'", apply['if'])
        self.assertIn('--enabled --known-writers-verified', apply['run'])
        self.assertIn('--max-batches 20', apply['run'])
        self.assertIn('--continuation-run-id', apply['run'])
        self.assertIn('--expected-sha "$GITHUB_SHA"', apply['run'])
        self.assertLess(steps.index(inspect), steps.index(apply))
        evidence = next(s for s in steps if s.get('name') == 'Preserve exact decisions and operation evidence')
        continuation = next(s for s in steps if s.get('name') == 'Queue verified continuation after publishing its evidence')
        self.assertEqual(evidence['if'], 'always()')
        self.assertLess(steps.index(evidence), steps.index(continuation))
        self.assertIn('continue-dispatch', continuation['run'])
        self.assertEqual(continuation['if'], "steps.convert.outputs.status == 'continuation_required'")

    def test_automatic_unify_requires_exact_generation_verification_without_gc(self):
        raw = (ROOT / '.github/workflows/reader-v3-publish.yml').read_text()
        w = yaml.load(raw, Loader=yaml.BaseLoader)
        self.assertEqual(w['concurrency']['group'], 'reader-sidecar')
        self.assertEqual(w['jobs']['publish']['concurrency']['group'], 'daily-sync')
        self.assertIn("format('reader-auto/{0}', inputs.automation_id)", w['run-name'])
        steps = w['jobs']['publish']['steps']
        guard = next(s for s in steps if s.get('name') == 'Guard automatic format publication')
        self.assertIn('"$OPERATION" == unify', guard['run'])
        self.assertIn('READER_FORMAT_AUTOMATION_ENABLED', guard['env']['ENABLED'])
        confirm = next(s for s in steps if s.get('name') == 'Confirm both deployed consumer projections')
        branch = confirm['run'].split('if [[ -n "$AUTOMATION_ID" ]]; then', 1)[1].split('\nfi\n', 1)[0]
        self.assertIn('verify-generation --expected-parent "$generation"', branch)
        self.assertIn('exit 0', branch)
        self.assertNotIn('ack-all', branch)
        self.assertNotIn('retire', branch)


if __name__ == '__main__':
    unittest.main()
