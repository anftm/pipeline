"""Exercise workflow guards and inline verification offline, without remote writes."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'
CHANGED = "steps.fetch.outputs.data_changed == 'true' && steps.stage.outputs.generation != steps.published.outputs.generation"
UNCHANGED = "steps.fetch.outputs.data_changed != 'true' || steps.stage.outputs.generation == steps.published.outputs.generation"
SHA = 'a' * 40
OTHER = 'b' * 40


def workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def named_steps(config, job):
    return {step['name']: step for step in config['jobs'][job]['steps'] if 'name' in step}


def inline_python(step):
    return step['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]


class AutomationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.daily = workflow('search-daily-sync.yml')
        self.steps = named_steps(self.daily, 'sync')
        self.gc = workflow('search-bucket-gc.yml')
        self.gc_steps = named_steps(self.gc, 'gc')

    def shell(self, script, **env):
        # Fake only the remote main lookup and local checkout identity.
        stubs = 'git() { echo "$LOCAL_SHA"; }; gh() { echo "$REMOTE_SHA"; };\n'
        values = dict(os.environ, GITHUB_REF='refs/heads/main', GITHUB_SHA=SHA,
                      GITHUB_WORKFLOW_SHA=SHA, GITHUB_REPOSITORY='anftm/pipeline',
                      GITHUB_WORKFLOW_REF='anftm/pipeline/.github/workflows/search-daily-sync.yml@refs/heads/main',
                      LOCAL_SHA=SHA, REMOTE_SHA=SHA)
        values.update(env)
        return subprocess.run(['bash', '-c', stubs + script], env=values,
                              capture_output=True, text=True, timeout=5)

    def test_all_search_writers_share_full_run_serialization(self):
        for name in ('search-daily-sync.yml', 'search-bucket-gc.yml', 'search-bucket-probe.yml',
                     'search-bucket-stage.yml', 'search-adopt-current.yml',
                     'search-promotion-intent.yml', 'search-promotion-space.yml',
                     'search-promotion-pages.yml', 'search-promotion-finalize.yml'):
            with self.subTest(name=name):
                self.assertEqual(workflow(name)['concurrency'],
                                 {'group': 'daily-sync', 'cancel-in-progress': 'false', 'queue': 'max'})
        for config in (self.daily, self.gc):
            for job in config['jobs'].values():
                for step in job['steps']:
                    self.assertNotIn('continue-on-error', step)
                    self.assertNotIn('workflow run', step.get('run', ''))
                    self.assertNotIn('/dispatches', step.get('run', ''))

    def test_daily_freshness_guards_reject_old_checkout_ref_and_rerun(self):
        for name in ('Require current main workflow and checkout',
                     'Recheck current main immediately before publication'):
            script = self.steps[name]['run']
            self.assertEqual(self.shell(script).returncode, 0)
            for env in ({'GITHUB_REF': 'refs/heads/old'}, {'REMOTE_SHA': OTHER},
                        {'LOCAL_SHA': OTHER}, {'GITHUB_WORKFLOW_SHA': OTHER},
                        {'GITHUB_WORKFLOW_REF': 'anftm/pipeline/.github/workflows/search-daily-sync.yml@refs/tags/old'}):
                with self.subTest(name=name, env=env):
                    self.assertNotEqual(self.shell(script, **env).returncode, 0)
        jobs = self.daily['jobs']
        self.assertEqual(jobs['directories']['steps'][2], self.steps['Require current main workflow and checkout'])
        order = list(self.steps)
        self.assertLess(order.index('Require current main workflow and checkout'),
                        order.index('Generate and verify source directories'))
        self.assertLess(order.index('Recheck current main immediately before publication'),
                        order.index('Publish staged generation in this job'))

    def test_daily_authorization_fails_staging_only_and_calls_exact_cli(self):
        job = self.daily['jobs']['sync']
        self.assertEqual(job['env']['PUBLICATION_AUTHORIZED'],
                         "${{ vars.SEARCH_AUTOMATION_ENABLED == 'true' || (github.event_name == 'workflow_dispatch' && inputs.known_writers_coordinated) }}")
        self.assertEqual(self.daily['on']['workflow_dispatch']['inputs']['known_writers_coordinated']['default'], 'false')
        gate = self.steps['Fail closed when publication is not authorized']
        for value in ('false', '', 'TRUE'):
            result = self.shell(gate['run'], PUBLICATION_AUTHORIZED=value)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Staging only', result.stderr)
        self.assertEqual(self.shell(gate['run'], PUBLICATION_AUTHORIZED='true').returncode, 0)
        publish = self.steps['Publish staged generation in this job']
        self.assertEqual(publish['if'], CHANGED)
        self.assertEqual(gate['if'], CHANGED)
        self.assertEqual(job['timeout-minutes'], '120')
        self.assertEqual(publish['timeout-minutes'], '75')
        result = self.shell('python() { printf "%s\\n" "$@"; };\n' + publish['run'],
                            PUBLICATION_AUTHORIZED='true', GENERATION='candidate', EXPECTED_PARENT='parent',
                            PAGES_REVISION=SHA, SPACE_REVISION=OTHER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['-B', 'scripts/search_daily_publish.py',
                         '--generation', 'candidate', '--expected-parent', 'parent',
                         '--pages-revision', SHA, '--space-revision', OTHER,
                         '--known-writers-coordinated', '--timeout-seconds', '3600'])
        self.assertIn('requests==2.32.5', self.steps['Install dependencies']['run'])
        for name in ('Verify journal did not change during the run',
                     'Verify publication did not change during the run',
                     'Verify source revisions are fully published'):
            self.assertEqual(self.steps[name]['if'], UNCHANGED)
            self.assertTrue(self.steps[name]['run'])

    def test_exact_revision_lookup_rejects_building_wrong_runtime_and_invalid_sha(self):
        step = self.steps['Resolve exact Pages main and running Space revisions']
        self.assertEqual(step['if'], CHANGED)
        code = inline_python(step)
        for stage, runtime_sha, repo_sha, pages_sha, okay in (
                ('RUNNING', OTHER, OTHER, SHA, True),
                ('BUILDING', OTHER, OTHER, SHA, False),
                ('RUNNING', SHA, OTHER, SHA, False),
                ('RUNNING', None, OTHER, SHA, False),
                ('RUNNING', OTHER, OTHER, 'main', False),
                ('RUNNING', 'main', 'main', SHA, False)):
            with self.subTest(stage=stage, runtime_sha=runtime_sha, pages_sha=pages_sha), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / 'output'
                response = Mock()
                response.json.return_value = {'object': {'sha': pages_sha}}
                get = Mock(return_value=response)
                api = Mock()
                api.space_info.return_value.sha = repo_sha
                api.get_space_runtime.return_value = SimpleNamespace(stage=stage, raw={'sha': runtime_sha})
                factory = Mock(return_value=api)
                with patch.dict(sys.modules, {'requests': SimpleNamespace(get=get),
                                              'huggingface_hub': SimpleNamespace(HfApi=factory)}), \
                        patch.dict(os.environ, {'GITHUB_OUTPUT': str(output), 'HF_TOKEN': 'fake', 'PAGES_TOKEN': 'fake'}):
                    if okay:
                        exec(compile(code, '<revision-step>', 'exec'), {})
                        self.assertEqual(output.read_text(), f'pages_revision={SHA}\nspace_revision={OTHER}\n')
                    else:
                        with self.assertRaises(SystemExit):
                            exec(compile(code, '<revision-step>', 'exec'), {})
                        self.assertFalse(output.exists())
                response.raise_for_status.assert_called_once()
                self.assertTrue(get.call_args.args[0].endswith('/repos/vomebook/search/git/ref/heads/main'))
                api.space_info.assert_called_once_with('VoiceOfML/Search', timeout=30)

    def test_fresh_receipt_requires_complete_matching_journal_and_source_snapshot(self):
        step = self.steps['Verify fresh publication receipt and terminal journal']
        self.assertEqual(step['if'], CHANGED)
        self.assertIn('search_daily.py inspect --receipt-output', step['run'])
        self.assertIn('search_daily_journal.py --output', step['run'])
        code = inline_python(step)
        base_receipt = dict(generation='candidate', transition='tx', both_consumers_observed=True,
                            source_revisions={'repo': SHA})
        base_journal = dict(generation='candidate', state='complete', transition='tx', pointer_transition='tx')
        variants = [(None, None, None)]
        variants += [('receipt', key, value) for key, value in (
            ('generation', 'parent'), ('transition', 'other'), ('both_consumers_observed', False),
            ('source_revisions', {'repo': OTHER}))]
        variants += [('journal', key, value) for key, value in (
            ('generation', 'parent'), ('state', 'publishing'), ('state', 'rolled-back'),
            ('transition', 'other'), ('pointer_transition', 'other'))]
        for target, key, value in variants:
            with self.subTest(target=target, key=key, value=value), tempfile.TemporaryDirectory() as tmp:
                receipt, journal = dict(base_receipt), dict(base_journal)
                if target:
                    (receipt if target == 'receipt' else journal)[key] = value
                for name, data in [('search-final-receipt.json', receipt), ('search-final-journal.json', journal),
                                   ('search-directory-revisions.json', {'repo': SHA})]:
                    (Path(tmp) / name).write_text(json.dumps(data))
                with patch.dict(os.environ, {'RUNNER_TEMP': tmp, 'GENERATION': 'candidate'}), patch('sys.stdout', new_callable=io.StringIO):
                    if target:
                        with self.assertRaises(SystemExit):
                            exec(compile(code, '<fresh-verification>', 'exec'), {})
                    else:
                        exec(compile(code, '<fresh-verification>', 'exec'), {})

    def test_gc_schedule_manual_preview_and_write_gates(self):
        self.assertEqual(self.gc['on']['schedule'], [{'cron': '0 3 * * *'}])
        inputs = self.gc['on']['workflow_dispatch']['inputs']
        self.assertEqual(inputs['mode']['default'], 'preview')
        self.assertEqual(inputs['mode']['options'], ['preview', 'auto', 'mark', 'sweep'])
        self.assertEqual(inputs['known_writers_coordinated']['default'], 'false')
        job = self.gc['jobs']['gc']
        self.assertEqual(job['if'], "github.repository == 'anftm/pipeline' && (github.event_name == 'workflow_dispatch' || vars.SEARCH_AUTOMATION_ENABLED == 'true')")
        self.assertEqual(job['env']['KNOWN_WRITERS_COORDINATED'],
                         "${{ (github.event_name == 'schedule' && vars.SEARCH_AUTOMATION_ENABLED == 'true') || inputs.known_writers_coordinated }}")
        self.assertEqual(job['env']['PLAN_KEY'], "${{ github.event_name == 'workflow_dispatch' && inputs.plan_key || '' }}")
        gate = self.gc_steps['Require opt-in and current main before GC writes']
        self.assertEqual(gate['if'], "env.MODE != 'preview'")
        env = dict(SEARCH_AUTOMATION_ENABLED='true', KNOWN_WRITERS_COORDINATED='true',
                   GITHUB_WORKFLOW_REF='anftm/pipeline/.github/workflows/search-bucket-gc.yml@refs/heads/main')
        self.assertEqual(self.shell(gate['run'], **env).returncode, 0)
        for changed in ({'SEARCH_AUTOMATION_ENABLED': 'false'}, {'KNOWN_WRITERS_COORDINATED': 'false'},
                        {'GITHUB_REF': 'refs/heads/old'}, {'REMOTE_SHA': OTHER}, {'LOCAL_SHA': OTHER},
                        {'GITHUB_WORKFLOW_SHA': OTHER}, {'GITHUB_WORKFLOW_REF': 'old'}):
            with self.subTest(changed=changed):
                self.assertNotEqual(self.shell(gate['run'], **(env | changed)).returncode, 0)

    def test_gc_cli_for_preview_auto_and_manual_plan(self):
        script = 'python() { printf "%s\\n" "$@"; };\n' + self.gc_steps['Run bounded search bucket GC']['run']
        base = dict(GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2', RUNNER_TEMP='/tmp',
                    PLAN_KEY='', SEARCH_AUTOMATION_ENABLED='false', KNOWN_WRITERS_COORDINATED='false',
                    GITHUB_EVENT_NAME='workflow_dispatch')
        for mode in ('preview', 'auto', 'mark', 'sweep'):
            with self.subTest(mode=mode):
                env = base | {'MODE': mode}
                result = self.shell(script, **env)
                if mode != 'preview':
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, '')
                    env.update(SEARCH_AUTOMATION_ENABLED='true', KNOWN_WRITERS_COORDINATED='true')
                    result = self.shell(script, **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = ['-B', 'scripts/search_gc.py', '--mode', mode, '--run-id', '123',
                            '--run-attempt', '2', '--output', '/tmp/search-bucket-gc.json']
                if mode != 'preview':
                    expected += ['--known-writers-coordinated']
                self.assertEqual(result.stdout.splitlines(), expected)
        sweep = base | {'MODE': 'sweep', 'PLAN_KEY': 'plans/exact.json',
                        'SEARCH_AUTOMATION_ENABLED': 'true', 'KNOWN_WRITERS_COORDINATED': 'true'}
        result = self.shell(script, **sweep)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], ['--plan-key', 'plans/exact.json'])
        for invalid in ({'GITHUB_EVENT_NAME': 'schedule'}, {'MODE': 'preview'}, {'MODE': 'auto'}, {'MODE': 'mark'}):
            result = self.shell(script, **(sweep | invalid))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')

    def test_each_writing_job_runs_relevant_safety_tests_before_writes(self):
        for config, job_name, safety_name, modules, first_write in (
                (self.daily, 'sync', 'Verify automation and transition safety before writes',
                 ('tests.test_search_automation_workflows', 'tests.test_search_daily_publish',
                  'tests.test_search_transition'), 'Generate and verify source directories'),
                (self.gc, 'gc', 'Verify GC and automation safety before writes',
                 ('tests.test_search_gc', 'tests.test_search_gc_s3', 'tests.test_search_automation_workflows'),
                 'Require opt-in and current main before GC writes')):
            steps = named_steps(config, job_name)
            safety = steps[safety_name]
            self.assertNotIn('if', safety)
            self.assertNotIn('continue-on-error', safety)
            for module in modules:
                self.assertIn(module, safety['run'])
            self.assertLess(list(steps).index(safety_name), list(steps).index(first_write))
            install = next(step for name, step in steps.items() if name.startswith('Install '))
            self.assertIn('PyYAML==6.0.3', install['run'])
            self.assertLess(list(steps.values()).index(install), list(steps).index(safety_name))
        gc_install = self.gc_steps['Install GC dependencies']['run']
        self.assertNotIn('scripts/requirements.txt', gc_install)
        for dependency in ('huggingface_hub==1.33.0', 'boto3==1.43.123', 'requests==2.32.5'):
            self.assertIn(dependency, gc_install)

    def test_automation_tests_are_in_ci_paths_and_command(self):
        ci = workflow('search-directory-tests.yml')
        paths = ci['on']['push']['paths']
        for path in ('.github/workflows/search-daily-sync.yml', '.github/workflows/search-bucket-gc.yml',
                     '.github/workflows/search-bucket-probe.yml', 'scripts/search_daily_publish.py',
                     'scripts/search_gc.py', 'scripts/search_gc_s3.py', 'tests/test_search_automation_workflows.py',
                     'tests/test_search_daily_publish.py', 'tests/test_search_gc.py', 'tests/test_search_gc_s3.py'):
            self.assertIn(path, paths)
        runs = '\n'.join(step.get('run', '') for step in ci['jobs']['tests']['steps'])
        self.assertIn('tests.test_search_automation_workflows', runs)
        self.assertIn('tests.test_search_directory_sync', runs)
        self.assertIn('tests.test_search_daily_publish', runs)
        self.assertIn('tests.test_search_gc', runs)
        self.assertIn('tests.test_search_gc_s3', runs)
        self.assertIn('boto3==1.43.123', runs)
        self.assertIn('requests==2.32.5', runs)
        install = named_steps(ci, 'tests')['Install search and workflow-test dependencies']['run']
        self.assertIn('huggingface_hub==1.33.0', install.split())


if __name__ == '__main__':
    unittest.main()
