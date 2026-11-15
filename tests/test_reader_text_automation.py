"""Execute the native-text workflow's guards and bounded publication branches."""
import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import test_reader_format_workflow_automation as common
from scripts import pilot_text_stream as text


class TextAutomationTests(unittest.TestCase):
    def setUp(self):
        self.config = common.workflow('text-stream')
        self.jobs = self.config['jobs']

    def test_shared_guards_execute_for_manual_automatic_and_invalid_inputs(self):
        # Reuse the executed shell contract, including every partial automation
        # tuple, stale main, disabled flag, failed source guard and failed needs.
        checks = common.ProducerAutomationTests()
        with patch.object(common, 'NAMES', ('text-stream',)):
            for method in (
                'test_execute_every_gate_for_manual_auto_disabled_bad_id_and_stale_main',
                'test_source_guard_runs_after_fetch_before_planning_and_snapshot_and_fails_closed',
                'test_build_rechecks_published_sources_after_artifact_before_first_write',
                'test_all_inline_shell_is_valid_bash',
                'test_dependency_and_write_guards_cannot_bypass_plan',
                'test_defer_only_skips_dispatch_after_canonical_and_sidecar',
            ):
                with self.subTest(contract=method):
                    getattr(checks, method)()

    def test_inputs_names_and_empty_plan(self):
        triggers = self.config.get('on', self.config.get(True))
        self.assertEqual(set(triggers), {'workflow_dispatch'})
        schema = triggers['workflow_dispatch']['inputs']
        self.assertLessEqual(len(schema), 10)
        self.assertEqual(set(schema['extension']['options']), text.SUPPORTED_EXTENSIONS | {'all'})
        for key in ('apply', 'retry_failures', 'defer_unify'):
            self.assertIs(schema[key]['default'], False)
        for key in ('automation_id', 'search_generation', 'expected_sha'):
            self.assertEqual(schema[key]['default'], '')
            self.assertFalse(schema[key]['required'])
        for apply in (False, True):
            values = common.inputs_for(self.config, apply=apply)
            self.assertEqual(common.expression(self.config['run-name'], inputs=values),
                             f"Text streams {'apply' if apply else 'plan'} (all)")
            values['automation_id'] = common.AUTO_ID
            self.assertEqual(common.expression(self.config['run-name'], inputs=values),
                             'reader-auto/' + common.AUTO_ID)
        self.assertFalse(common.job_runs(self.jobs['text-stream'], common.inputs_for(self.config, apply=True),
                                        {'plan': {'result': 'success', 'outputs': {'count': '0'}}}))

    def test_actual_plan_and_build_arguments_and_limit_boundaries(self):
        for job, apply, limit in itertools.product(('plan', 'text-stream'), (False, True),
                                                   ('0', '1', '10', '11', '-1', '01', 'x', '1; exit 0')):
            step = next(step for step in self.jobs[job]['steps'] if 'scripts/pilot_text_stream.py' in step.get('run', ''))
            with self.subTest(job=job, apply=apply, limit=limit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = dict(os.environ, EXTENSION='markdown', LIMIT=limit, APPLY=str(apply).lower(),
                           RETRY_FAILURES='true', GITHUB_OUTPUT=str(root / 'outputs'))
                # Run the actual pilot command assembled by YAML, recording argv;
                # leave the small report-reading Python command real.
                stub = '''python() {
  if [[ "$1" == scripts/pilot_text_stream.py ]]; then
    printf '%s\\n' "$@" > args
    mkdir -p output
    printf '{"selected":1}' > output/text-stream-report.json
  else
    command "$TEST_PYTHON" "$@"
  fi
}
'''
                import sys
                env['TEST_PYTHON'] = sys.executable
                result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', stub + step['run']],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=5)
                allowed = limit in ('1', '10') or (job == 'plan' and not apply and limit in ('0', '11', '01'))
                self.assertEqual(result.returncode == 0, allowed, result.stderr)
                self.assertEqual((root / 'args').exists(), allowed)
                if allowed:
                    self.assertEqual((root / 'args').read_text().splitlines(), [
                        'scripts/pilot_text_stream.py', '--extension', 'markdown', '--limit', limit,
                        '--all-markdown-repos', '--plan-only' if job == 'plan' else '--apply', '--retry-failures'])
                    if job == 'plan':
                        self.assertEqual((root / 'outputs').read_text(), 'count=1\n')

    def test_filter_snapshot_handoff_and_failure_stop_before_conversion(self):
        steps = self.jobs['plan']['steps']
        fetch = next(i for i, step in enumerate(steps) if 'fetch_and_parse.py' in step.get('run', ''))
        guard, filtering, planning = steps[fetch + 1:fetch + 4]
        self.assertIn('guard-source', guard['run'])
        self.assertIn('reader_format_sources.py', filtering['run'])
        self.assertIn('pilot_text_stream.py', planning['run'])
        self.assertNotIn('continue-on-error', filtering)
        archive = next(step['run'] for step in steps if 'tar -czf' in step.get('run', ''))
        build = self.jobs['text-stream']['steps']
        unpack = next(step['run'] for step in build if 'tar -xzf' in step.get('run', ''))
        self.assertFalse(any('fetch_and_parse.py' in step.get('run', '') for step in build))
        for auto, status, extension in itertools.product((False, True), ('ok', 'fail', 'missing'), ('txt', 'md', 'all', '')):
            with self.subTest(auto=auto, status=status, extension=extension), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'output').mkdir()
                (root / 'state').mkdir()
                original, filtered = '[{"File":"old"},{"File":"new"}]', '[{"File":"new"}]'
                revisions = json.dumps({'Other/Books': common.SHA})
                (root / 'output/search_data.json').write_text(original)
                (root / 'state/commits.json').write_text(revisions)
                (root / 'output/text-stream-report.json').write_text('{"selected":1}')
                enabled = common.expression(filtering['if'], inputs=common.inputs_for(
                    self.config, automation_id=common.AUTO_ID if auto else ''))
                self.assertEqual(bool(enabled), auto)
                stub = '''python() {
  printf '%s\\n' "$@" > helper-args
  if [[ "$FILTER_STATUS" != missing ]]; then printf '%s' "$FILTERED" > output/reader-actionable.json; fi
  if [[ "$FILTER_STATUS" == fail ]]; then return 1; fi
}
'''
                env = dict(os.environ, EXTENSION=extension, SEARCH_GENERATION=common.GENERATION,
                           FILTER_STATUS=status, FILTERED=filtered)
                result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c',
                                         stub + (filtering['run'] if enabled else '') + '\n' + archive],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=5)
                allowed = not auto or (status == 'ok' and extension in ('txt', 'md'))
                self.assertEqual(result.returncode == 0, allowed, result.stderr)
                self.assertEqual((root / 'output/text-plan.tgz').exists(), allowed)
                self.assertEqual((root / 'state/commits.json').read_text(), revisions)
                helper_called = auto and extension in ('txt', 'md')
                self.assertEqual((root / 'helper-args').exists(), helper_called)
                if helper_called:
                    self.assertEqual((root / 'helper-args').read_text().splitlines(), [
                        'scripts/reader_format_sources.py', '--extension', extension,
                        '--generation', common.GENERATION, '--search-data', 'output/search_data.json',
                        '--revisions', 'state/commits.json', '--output', 'output/reader-actionable.json'])
                if allowed:
                    restored = root / 'restored'
                    (restored / 'output').mkdir(parents=True)
                    (restored / 'output/text-plan.tgz').write_bytes((root / 'output/text-plan.tgz').read_bytes())
                    result = subprocess.run(['bash', '-e', '-c', unpack], cwd=restored,
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((restored / 'output/search_data.json').read_text(), filtered if auto else original)
                    self.assertEqual((restored / 'state/commits.json').read_text(), revisions)


if __name__ == '__main__':
    unittest.main()
