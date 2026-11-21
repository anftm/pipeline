"""Execute producer YAML guards/branches and media filtering without remote writes."""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts import pilot_media_stream as media
from scripts import plan_reader_stream_matrix as planner

ROOT = Path(__file__).resolve().parents[1]
NAMES = ('image-stream', 'media-stream', 'ebook-stream', 'office-chapters',
         'web-stream', 'spreadsheet-stream', 'static-pdf-stream', 'chm-repair')
SHA = 'a' * 40
AUTO_ID = '0123456789abcdef' * 2
GENERATION = 'c' * 64
GATE = 'Validate automation and current main'


def workflow(name):
    return yaml.safe_load((ROOT / f'.github/workflows/reader-{name}.yml').read_text())


def inputs_for(config, **overrides):
    values = {key: value.get('default', '') for key, value in
              config.get('on', config.get(True))['workflow_dispatch']['inputs'].items()}
    return {**values, **overrides}


class Context(dict):
    def __getattr__(self, key):
        return self.get(key, '')


class Array(list):
    def __getitem__(self, key):
        return super().__getitem__(key) if key < len(self) else None


def context(value):
    if isinstance(value, dict):
        return Context({key: context(item) for key, item in value.items()})
    if isinstance(value, list):
        return Array(context(item) for item in value)
    return value


def expression(text, **values):
    """Evaluate the small Actions expression subset used by these producer YAMLs.

    Split quoted strings first so operators/literals inside run names stay literal.
    Job dependency success is applied separately, as by Actions.
    """
    text = text.strip()
    if text.startswith('${{'):
        text = text[3:-2].strip()
    parts = re.split(r"('(?:[^']|'')*')", text)
    for index in range(0, len(parts), 2):
        part = parts[index].replace('&&', ' and ').replace('||', ' or ')
        for old, new in (('true', 'True'), ('false', 'False'), ('null', 'None')):
            part = re.sub(r'\b' + old + r'\b', new, part)
        # Actions permits hyphens in job identifiers; Python attributes do not.
        part = re.sub(r'needs\.([\w-]+)', r'needs["\1"]', part)
        parts[index] = part
    return eval(''.join(parts), {'__builtins__': {}}, {
        **{key: context(value) for key, value in values.items()},
        'always': lambda: True,
        'fromJSON': lambda value: context(json.loads(value)),
        'format': lambda template, *args: template.format(*args),
    })


def needs_for(job):
    needs = job.get('needs', [])
    return [needs] if isinstance(needs, str) else needs


def job_runs(job, inputs, needs):
    condition = job.get('if', 'true')
    if 'always()' not in condition and any(needs[key]['result'] != 'success' for key in needs_for(job)):
        return False
    return bool(expression(condition, inputs=inputs, needs=needs,
                           github={'repository': 'anftm/pipeline'}))


class ProducerAutomationTests(unittest.TestCase):
    def shell(self, script, name, **env):
        values = dict(os.environ, AUTOMATION_ID=AUTO_ID, AUTOMATION_ENABLED='true',
                      SEARCH_GENERATION=GENERATION, EXPECTED_SHA=SHA, DEFER_UNIFY='true',
                      GITHUB_REF='refs/heads/main', GITHUB_SHA=SHA, GITHUB_WORKFLOW_SHA=SHA,
                      GITHUB_REPOSITORY='anftm/pipeline', LOCAL_SHA=SHA, REMOTE_SHA=SHA,
                      REMOTE_STATUS='0', FINALIZE_ONLY='false',
                      GITHUB_WORKFLOW_REF=f'anftm/pipeline/.github/workflows/reader-{name}.yml@refs/heads/main')
        values.update(env)
        stubs = '''git() { [[ "$*" == 'rev-parse HEAD' ]] || return 99; echo "$LOCAL_SHA"; }
gh() {
  [[ "$*" == 'api repos/anftm/pipeline/git/ref/heads/main --jq .object.sha' ]] || return 99
  echo "$REMOTE_SHA"
  return "$REMOTE_STATUS"
}
'''
        return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', stubs + script],
                              cwd=ROOT, env=values, capture_output=True, text=True, timeout=5)

    def test_input_shape_and_actual_manual_automatic_run_names(self):
        labels = {'image-stream': 'Reader/桶 · Build Image WebP Assets',
                  'media-stream': 'Media streams {mode} (all)',
                  'ebook-stream': 'Ebook chapters {mode} (all)',
                  'office-chapters': 'Office chapters {mode}',
                  'web-stream': 'Web streams {mode} (all)',
                  'spreadsheet-stream': 'Spreadsheet streams {mode} (all)',
                  'static-pdf-stream': 'Static PDF {mode} (all)',
                  'chm-repair': 'CHM source-text repair / {mode}'}
        for name in NAMES:
            config = workflow(name)
            triggers = config.get('on', config.get(True))
            self.assertEqual(set(triggers), {'workflow_dispatch'})
            schema = triggers['workflow_dispatch']['inputs']
            self.assertLessEqual(len(schema), 11 if name == 'ebook-stream' else 10)
            for key, kind, default in (('automation_id', 'string', ''), ('defer_unify', 'boolean', False),
                                       ('search_generation', 'string', ''), ('expected_sha', 'string', '')):
                self.assertEqual(schema[key]['type'], kind)
                self.assertIs(schema[key]['required'], False)
                self.assertEqual(schema[key]['default'], default)
            for apply in (False, True):
                with self.subTest(name=name, apply=apply):
                    values = inputs_for(config, apply=apply)
                    self.assertEqual(expression(config['run-name'], inputs=values),
                                     labels[name].format(mode='apply' if apply else 'plan'))
                    values['automation_id'] = AUTO_ID
                    self.assertEqual(expression(config['run-name'], inputs=values), f'reader-auto/{AUTO_ID}')

    def test_execute_every_gate_for_manual_auto_disabled_bad_id_and_stale_main(self):
        invalid = [{'AUTOMATION_ENABLED': value} for value in ('', 'false', 'TRUE')]
        invalid += [{'AUTOMATION_ID': value} for value in
                    ('a' * 31, 'a' * 33, 'A' * 32, 'g' * 32, ' ' + AUTO_ID, AUTO_ID + '\n', '$(exit 0)')]
        invalid += [{'SEARCH_GENERATION': value} for value in ('c' * 63, 'c' * 65, 'C' * 64, GENERATION + '\n')]
        invalid += [{'EXPECTED_SHA': value} for value in ('a' * 39, 'a' * 41, 'A' * 40, 'b' * 40, SHA + '\n')]
        # Any partial automatic tuple must fail, even if automation_id is omitted.
        for present in itertools.product((False, True), repeat=4):
            if all(present) or not any(present):
                continue
            invalid.append({key: value if enabled else ('false' if key == 'DEFER_UNIFY' else '')
                            for (key, value), enabled in zip((('AUTOMATION_ID', AUTO_ID),
                                ('SEARCH_GENERATION', GENERATION), ('EXPECTED_SHA', SHA),
                                ('DEFER_UNIFY', 'true')), present)})
        invalid += [{key: value} for key, value in (
            ('GITHUB_REF', 'refs/heads/feature'), ('GITHUB_REF', 'refs/tags/main'),
            ('GITHUB_WORKFLOW_REF', 'anftm/pipeline/.github/workflows/other.yml@refs/heads/main'),
            ('GITHUB_WORKFLOW_SHA', 'b' * 40), ('LOCAL_SHA', 'b' * 40),
            ('REMOTE_SHA', 'b' * 40), ('REMOTE_SHA', ''), ('REMOTE_STATUS', '1'))]
        for name in NAMES:
            for job_name, job in workflow(name)['jobs'].items():
                for step in job['steps']:
                    if step.get('name') != GATE:
                        continue
                    with self.subTest(name=name, job=job_name):
                        self.assertNotIn('if', step)
                        self.assertNotIn('continue-on-error', step)
                        self.assertEqual(step['env']['AUTOMATION_ID'], '${{ inputs.automation_id }}')
                        self.assertEqual(step['env']['AUTOMATION_ENABLED'], '${{ vars.READER_FORMAT_AUTOMATION_ENABLED }}')
                        self.assertEqual(step['env']['GH_TOKEN'], '${{ github.token }}')
                        for key, value in (('SEARCH_GENERATION', 'search_generation'),
                                           ('EXPECTED_SHA', 'expected_sha'), ('DEFER_UNIFY', 'defer_unify')):
                            self.assertEqual(step['env'][key], '${{ inputs.' + value + ' }}')
                        self.assertEqual(self.shell(step['run'], name).returncode, 0)
                        self.assertEqual(self.shell(step['run'], name, AUTOMATION_ID='',
                                                    SEARCH_GENERATION='', EXPECTED_SHA='', DEFER_UNIFY='false',
                                                    AUTOMATION_ENABLED='', GITHUB_REF='refs/heads/manual',
                                                    REMOTE_STATUS='99').returncode, 0)
                        for env in invalid:
                            with self.subTest(env=env):
                                self.assertNotEqual(self.shell(step['run'], name, **env).returncode, 0)

    def test_canary_gate_executes_in_plan_worker_and_finalizer(self):
        for name, worker, extension, flag, maximum in (
                ('image-stream', 'build', 'jpg', 'READER_JPG_BATCH_BOOST_ENABLED', 12),
                ('media-stream', 'media', 'mp3', 'READER_MP3_BATCH_BOOST_ENABLED', 8)):
            jobs = workflow(name)['jobs']
            gates = [step for job in jobs.values() for step in job['steps'] if step.get('name') == GATE]
            self.assertEqual(len(gates), 3)
            self.assertTrue(all(gate == gates[0] for gate in gates))
            gate = gates[0]
            self.assertEqual(gate['env'][flag], '${{ vars.' + flag + ' }}')
            matrix_guard = next(step['run'] for step in jobs['plan']['steps'] if step.get('id') == 'matrix')
            build_guard = next(step['run'] for step in jobs[worker]['steps'] if '--apply --strict' in step.get('run', ''))
            script = 'python() { ' + sys.executable + ' -B "$@"; };\n' + gate['run'] + '\n'
            script += matrix_guard.split('args=(', 1)[0] + '\nSHARD=0\n'
            script += build_guard.split('args=(', 1)[0] + '\necho writes-allowed\n'
            valid = {'BOOST_CANARY': 'true', flag: 'true', 'LIMIT': str(maximum),
                     'EXTENSION': extension, 'SHARD': 'all', 'KIND': 'audio', 'APPLY': 'true',
                     'RETRY_FAILURES': 'false', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                     'GITHUB_ACTOR': 'github-actions[bot]', 'GITHUB_RUN_ATTEMPT': '1'}
            for overrides, accepted in (({}, True), ({flag: ''}, False),
                    ({'LIMIT': str(maximum + 1)}, False), ({'LIMIT': '3'}, False),
                    ({'BOOST_CANARY': 'false'}, False), ({'BOOST_CANARY': 'false', 'LIMIT': '3'}, True),
                    ({'AUTOMATION_ID': ''}, False), ({'GITHUB_ACTOR': 'operator'}, False),
                    ({'GITHUB_REF': 'refs/heads/feature'}, False), ({'REMOTE_SHA': 'e' * 40}, False),
                    ({'GITHUB_RUN_ATTEMPT': '2'}, False), ({'RETRY_FAILURES': 'true'}, False),
                    ({'EXTENSION': 'all'}, False), ({'APPLY': 'false'}, False),
                    ({'SHARD': '1'} if extension == 'jpg' else {'KIND': 'all'}, False)):
                with self.subTest(name=name, overrides=overrides):
                    result = self.shell(script, name, **{**valid, **overrides})
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)
                    self.assertEqual('writes-allowed' in result.stdout, accepted)

    def test_source_guard_runs_after_fetch_before_planning_and_snapshot_and_fails_closed(self):
        for name in NAMES:
            config = workflow(name)
            steps = config['jobs']['plan']['steps']
            fetch = next(i for i, step in enumerate(steps) if 'fetch_and_parse.py' in step.get('run', ''))
            guard = steps[fetch + 1]
            self.assertEqual(guard['name'], 'Bind automatic batch to published search sources')
            self.assertFalse(expression(guard['if'], inputs=inputs_for(config)))
            self.assertTrue(expression(guard['if'], inputs=inputs_for(config, automation_id=AUTO_ID)))
            self.assertEqual(guard['env'], {'GH_TOKEN': '${{ github.token }}',
                                           'HF_TOKEN': '${{ secrets.HF_TOKEN }}',
                                           'SEARCH_GENERATION': '${{ inputs.search_generation }}',
                                           'EXPECTED_SHA': '${{ inputs.expected_sha }}'})
            self.assertNotIn('continue-on-error', guard)
            for step in steps[:fetch + 1]:
                self.assertNotIn('tar -czf', step.get('run', ''))
                self.assertNotIn('plan_reader_stream_matrix.py', step.get('run', ''))
                self.assertNotIn('pilot_epub_chapters.py', step.get('run', ''))
            script = ('python() { printf "%s\\n" "$@"; return "$GUARD_STATUS"; };\n' +
                      guard['run'] + '\necho snapshot-allowed\n')
            for status in ('0', '1'):
                result = self.shell(script, name, GUARD_STATUS=status)
                self.assertEqual(result.returncode, int(status), result.stderr)
                expected = ['scripts/reader_format_daily.py', 'guard-source', '--generation', GENERATION,
                            '--expected-sha', SHA, '--revisions', 'state/commits.json']
                self.assertEqual(result.stdout.splitlines(), expected + (['snapshot-allowed'] if status == '0' else []))

    def test_actionable_filter_order_failure_and_artifact_handoff(self):
        extensions = dict(zip(NAMES, ('png', 'mp3', 'epub', 'docx', 'html', 'xlsx', 'pptx', 'chm')))
        for name in NAMES:
            config = workflow(name)
            jobs = config['jobs']
            steps = jobs['plan']['steps']
            fetch = next(i for i, step in enumerate(steps) if 'fetch_and_parse.py' in step.get('run', ''))
            guard, filtering, planning = steps[fetch + 1:fetch + 4]
            self.assertIn('guard-source', guard['run'])
            self.assertEqual(filtering['name'], 'Select actionable sources for automatic batch')
            self.assertIn('plan_reader_stream_matrix.py' if name != 'chm-repair' else 'pilot_epub_chapters.py',
                          planning['run'])
            self.assertNotIn('continue-on-error', filtering)
            expected_env = {'HF_TOKEN': '${{ secrets.HF_TOKEN }}',
                'EXTENSION': 'chm' if name == 'chm-repair' else '${{ inputs.extension }}',
                'SEARCH_GENERATION': '${{ inputs.search_generation }}'}
            if name == 'chm-repair':
                expected_env['READER_CHM_PROFILE_REBUILD_ENABLED'] = '${{ vars.READER_CHM_PROFILE_REBUILD_ENABLED }}'
            if name in {'image-stream', 'media-stream'}:
                expected_env.update(BOOST_CANARY='${{ inputs.boost_canary }}', LIMIT="${{ inputs.limit || '1' }}")
            self.assertEqual(filtering['env'], expected_env)
            archive = next(step for step in steps if 'tar -czf' in step.get('run', ''))
            self.assertGreater(steps.index(archive), steps.index(planning))
            build = next(job for job in jobs.values() if any(
                'tar -xzf' in step.get('run', '') for step in job['steps']))
            unpack = next(step for step in build['steps'] if 'tar -xzf' in step.get('run', ''))
            self.assertFalse(any('fetch_and_parse.py' in step.get('run', '') for step in build['steps']))
            # The real YAML shell and tar commands run locally; only the helper is stubbed.
            stub = '''python() {
  printf '%s\\n' "$@" > helper-args
  if [[ "$FILTER_STATUS" != missing ]]; then
    printf '%s' "$FILTERED" > output/reader-actionable.json
    if [[ "$*" == *'--profile-proof output/chm-profile-proof.json' ]]; then
      printf '{}' > output/chm-profile-proof.json
    fi
  fi
  if [[ "$FILTER_STATUS" == fail ]]; then return 1; fi
}
'''
            cases = [(True, 'ok', extensions[name], False), (True, 'fail', extensions[name], False),
                     (True, 'missing', extensions[name], False), (False, 'fail', extensions[name], False),
                     (True, 'ok', 'all', False), (True, 'ok', '', False)]
            if name in {'image-stream', 'media-stream'}:
                cases.append((True, 'ok', 'jpg' if name == 'image-stream' else 'mp3', True))
            for automatic, status, extension, canary in cases:
                if name == 'chm-repair' and extension in ('', 'all'):
                    continue
                with self.subTest(name=name, auto=automatic, status=status, extension=extension), \
                        tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'output').mkdir()
                    (root / 'state').mkdir()
                    original = '[{"File":"unchanged"},{"File":"actionable"}]'
                    filtered = '[{"File":"actionable"}]'
                    revisions = json.dumps({'VoiceOfML/Test': SHA})
                    (root / 'output/search_data.json').write_text(original)
                    (root / 'state/commits.json').write_text(revisions)
                    # Supply planner reports for the actual YAML archive command.
                    if name == 'chm-repair':
                        (root / 'output/chm-plan.json').write_text('{}')
                        artifact = 'output/chm-source-snapshot.tgz'
                    else:
                        for filename in archive['run'].split()[3:]:
                            if filename not in ('output/search_data.json', 'state/commits.json'):
                                (root / filename).write_text('{}')
                        artifact = archive['run'].split()[2]
                    values = inputs_for(config, automation_id=AUTO_ID if automatic else '', extension=extension)
                    enabled = expression(filtering['if'], inputs=values)
                    self.assertEqual(bool(enabled), automatic)
                    script = stub + (filtering['run'] if enabled else '')
                    script += '\nprintf planned > planned\n' + archive['run']
                    env = dict(os.environ, EXTENSION=extension, SEARCH_GENERATION=GENERATION,
                               FILTER_STATUS=status, FILTERED=filtered, AUTOMATION_ID=AUTO_ID if automatic else '',
                               BOOST_CANARY='true' if canary else 'false', LIMIT='4' if canary else '3')
                    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script],
                                            cwd=root, env=env, capture_output=True, text=True, timeout=5)
                    allowed = not automatic or (status == 'ok' and extension not in ('', 'all'))
                    self.assertEqual(result.returncode == 0, allowed, result.stderr)
                    self.assertEqual((root / 'planned').exists(), allowed)
                    self.assertEqual((root / 'output/search_data.json').read_text(),
                                     filtered if automatic and allowed else original)
                    self.assertEqual((root / 'state/commits.json').read_text(), revisions)
                    helper_called = automatic and extension not in ('', 'all')
                    self.assertEqual((root / 'helper-args').exists(), helper_called)
                    if helper_called:
                        expected = ['scripts/reader_format_sources.py', '--extension', extension,
                                    '--generation', GENERATION, '--search-data', 'output/search_data.json',
                                    '--revisions', 'state/commits.json', '--output', 'output/reader-actionable.json']
                        if name == 'chm-repair':
                            expected += ['--profile-proof', 'output/chm-profile-proof.json']
                        if canary:
                            expected += ['--boost-limit', '4']
                        self.assertEqual((root / 'helper-args').read_text().splitlines(), expected)
                    if allowed:
                        restored = root / 'restored'
                        restored.mkdir()
                        # Use the same artifact basename/path the build job downloads.
                        (restored / 'output').mkdir()
                        (restored / artifact).write_bytes((root / artifact).read_bytes())
                        extracted = subprocess.run(['bash', '-e', '-c', unpack['run']], cwd=restored,
                                                   capture_output=True, text=True, timeout=5)
                        self.assertEqual(extracted.returncode, 0, extracted.stderr)
                        self.assertEqual((restored / 'output/search_data.json').read_text(),
                                         filtered if automatic else original)
                        self.assertEqual((restored / 'state/commits.json').read_text(), revisions)
                        if name == 'chm-repair':
                            self.assertEqual((restored / 'output/chm-full-source.json').exists(), automatic)
                            self.assertEqual((restored / 'output/chm-profile-proof.json').exists(), automatic)
                    else:
                        self.assertFalse((root / artifact).exists())
                        needs = {key: {'result': 'failure' if key == 'plan' else 'success',
                                      'outputs': {'matrix': '{"include":[{}]}', 'count': '1'}} for key in jobs}
                        for job_name, job in jobs.items():
                            if job_name != 'plan':
                                self.assertFalse(job_runs(job, inputs_for(config, apply=True), needs))

    def test_build_rechecks_published_sources_after_artifact_before_first_write(self):
        for name in NAMES:
            config = workflow(name)
            jobs = config['jobs']
            plan_guard = next(step for step in jobs['plan']['steps'] if
                              step.get('name') == 'Bind automatic batch to published search sources')
            build = next(job for job in jobs.values() if any(
                'tar -xzf' in step.get('run', '') for step in job['steps']))
            steps = build['steps']
            unpack = next(i for i, step in enumerate(steps) if 'tar -xzf' in step.get('run', ''))
            first_write = next(i for i, step in enumerate(steps) if '--apply' in step.get('run', ''))
            self.assertEqual(steps[unpack + 1]['name'], GATE)
            guard = steps[unpack + 2]
            self.assertEqual(guard, plan_guard)
            self.assertEqual(first_write, unpack + (4 if name == 'chm-repair' else 3))
            if name == 'chm-repair':
                recheck = steps[first_write - 1]
                self.assertIn('cmp output/chm-profile-proof.json', recheck['run'])
                self.assertIn('cmp output/search_data.json', recheck['run'])
                self.assertEqual(recheck['if'], guard['if'])
            self.assertEqual(build['concurrency']['group'], 'reader-sidecar')
            if 'finalize' in jobs:
                self.assertFalse(any('guard-source' in step.get('run', '')
                                     for step in jobs['finalize']['steps']))
            for automatic in (False, True):
                values = inputs_for(config, automation_id=AUTO_ID if automatic else '')
                for status in ('0', '1'):
                    script = 'python() { printf "%s\\n" "$@"; return "$GUARD_STATUS"; };\n'
                    if expression(guard['if'], inputs=values):
                        script += guard['run']
                    script += '\necho conversion-started\n'
                    result = self.shell(script, name, GUARD_STATUS=status)
                    allowed = not automatic or status == '0'
                    self.assertEqual(result.returncode == 0, allowed, result.stderr)
                    self.assertEqual('conversion-started' in result.stdout, allowed)
                    if automatic:
                        self.assertEqual(result.stdout.splitlines()[:8], [
                            'scripts/reader_format_daily.py', 'guard-source', '--generation', GENERATION,
                            '--expected-sha', SHA, '--revisions', 'state/commits.json'])

    def test_all_inline_shell_is_valid_bash(self):
        for name in NAMES:
            for job in workflow(name)['jobs'].values():
                for step in job['steps']:
                    if 'run' in step:
                        result = subprocess.run(['bash', '-n'], input=step['run'], capture_output=True,
                                                text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, f'{name}: {result.stderr}')

    def test_dependency_and_write_guards_cannot_bypass_plan(self):
        for name in NAMES:
            config = workflow(name)
            jobs = config['jobs']
            plan_steps = jobs['plan']['steps']
            gate_index = next(i for i, step in enumerate(plan_steps) if step.get('name') == GATE)
            fetch_index = next(i for i, step in enumerate(plan_steps) if 'fetch_and_parse.py' in step.get('run', ''))
            self.assertLess(gate_index, fetch_index)
            for job_name, job in jobs.items():
                self.assertNotIn('continue-on-error', job)
                if job_name == 'plan':
                    continue
                with self.subTest(name=name, job=job_name):
                    self.assertIn('plan', needs_for(job))
                    needs = {key: {'result': 'success', 'outputs': {
                        'matrix': '{"include":[{"extension":"mp3","shard":0}]}', 'count': '1'}} for key in jobs}
                    self.assertTrue(job_runs(job, inputs_for(config, apply=True), needs))
                    self.assertFalse(job_runs(job, inputs_for(config, apply=False), needs))
                    for dependency in needs_for(job):
                        for result in ('failure', 'cancelled', 'skipped'):
                            needs[dependency]['result'] = result
                            self.assertFalse(job_runs(job, inputs_for(config, apply=True), needs))
                        needs[dependency]['result'] = 'success'
                    if 'strategy' in job or job_name == 'rebuild':
                        needs['plan']['outputs'] = {'matrix': '{"include":[]}', 'count': '0'}
                        self.assertFalse(job_runs(job, inputs_for(config, apply=True), needs))
                    writes = [i for i, step in enumerate(job['steps']) if any(
                        marker in step.get('run', '') for marker in ('--apply', 'scripts/finalize_'))]
                    if not writes:
                        continue
                    self.assertEqual(job['concurrency'], {'group': 'reader-sidecar',
                                                         'cancel-in-progress': False, 'queue': 'max'})
                    gate = next(i for i, step in enumerate(job['steps']) if step.get('name') == GATE)
                    self.assertLess(gate, min(writes))
                    self.assertTrue(any(step.get('uses', '').startswith('actions/checkout@')
                                        for step in job['steps'][:gate]))

    def test_defer_only_skips_dispatch_after_canonical_and_sidecar(self):
        for name in NAMES:
            config = workflow(name)
            job = config['jobs'].get('finalize', config['jobs'].get('rebuild'))
            steps = job['steps']
            dispatch = [step for step in steps if 'gh workflow run' in step.get('run', '')]
            self.assertEqual(len(dispatch), 1)
            self.assertIs(steps[-1], dispatch[0])
            for automation_id in ('', AUTO_ID):
                for defer in (False, True):
                    values = inputs_for(config, apply=True, automation_id=automation_id, defer_unify=defer)
                    active = [step for step in steps if expression(step.get('if', 'true'), inputs=values)]
                    with self.subTest(name=name, auto=automation_id, defer=defer):
                        self.assertEqual(dispatch[0] in active, not defer)
                        self.assertTrue(any('build_reader_assets_v2_sidecar.py --apply' in step.get('run', '')
                                            for step in active))
                        skipped = list(dispatch) if defer else []
                        if not automation_id:
                            skipped += [step for step in steps if 'guard-source' in step.get('run', '')
                                        or step.get('name') == 'Revalidate CHM profile inputs under the Reader writer lock']
                        self.assertCountEqual([step for step in steps if step not in active], skipped)
                        for step in active:
                            if 'scripts/finalize_' in step.get('run', ''):
                                self.assertNotIn('if', step)

    def test_ebook_finalize_only_skips_producers_and_requires_apply(self):
        config = workflow('ebook-stream')
        schema = config.get('on', config.get(True))['workflow_dispatch']['inputs']
        self.assertEqual(schema['finalize_only']['type'], 'boolean')
        self.assertIs(schema['finalize_only']['default'], False)
        self.assertIs(schema['finalize_only']['required'], False)
        jobs = config['jobs']
        self.assertEqual(set(jobs), {'plan', 'ebook-stream', 'finalize'})
        self.assertEqual(config['concurrency'], {'group': 'reader-ebooks',
                                                'cancel-in-progress': False, 'queue': 'max'})
        for finalize_only, apply, plan, producer in itertools.product(
                (False, True), (False, True), ('success', 'skipped', 'failure', 'cancelled'),
                ('success', 'skipped', 'failure', 'cancelled')):
            values = inputs_for(config, finalize_only=finalize_only, apply=apply)
            # No matrix is produced when planning is skipped; it must never be parsed.
            needs = {'plan': {'result': plan, 'outputs': {
                'matrix': '' if finalize_only else '{"include":[{}]}'}},
                'ebook-stream': {'result': producer}}
            with self.subTest(finalize_only=finalize_only, apply=apply, plan=plan, producer=producer):
                self.assertEqual(job_runs(jobs['plan'], values, needs), not finalize_only)
                self.assertEqual(job_runs(jobs['ebook-stream'], values, needs),
                                 not finalize_only and apply and plan == 'success')
                expected = apply and (plan == producer == ('skipped' if finalize_only else 'success'))
                self.assertEqual(job_runs(jobs['finalize'], values, needs), expected)

    def test_ebook_finalize_only_gate_rejects_automation_invalid_inputs_and_stale_main(self):
        config = workflow('ebook-stream')
        gate = next(step for step in config['jobs']['finalize']['steps'] if step.get('name') == GATE)
        values = inputs_for(config, finalize_only=True, apply=True, extension='epub')
        env = {key: str(expression(value, inputs=values, vars={}, github={
            'event_name': 'workflow_dispatch', 'token': ''})).lower()
               for key, value in gate['env'].items()}
        for extension in ('all', 'epub', 'mobi', 'azw3', 'fb2'):
            result = self.shell(gate['run'] + '\necho writes-allowed', 'ebook-stream',
                                **{**env, 'EXTENSION': extension})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('writes-allowed', result.stdout)
        invalid = [{key: value} for key, value in (
            ('APPLY', 'false'), ('EVENT_NAME', 'workflow_run'),
            ('AUTOMATION_ID', AUTO_ID), ('SEARCH_GENERATION', GENERATION),
            ('EXPECTED_SHA', SHA), ('DEFER_UNIFY', 'true'),
            ('GITHUB_REF', 'refs/heads/feature'), ('GITHUB_REF', 'refs/tags/main'),
            ('GITHUB_WORKFLOW_REF', 'anftm/pipeline/.github/workflows/other.yml@refs/heads/main'),
            ('GITHUB_WORKFLOW_SHA', 'b' * 40), ('LOCAL_SHA', 'b' * 40),
            ('REMOTE_SHA', 'b' * 40), ('REMOTE_SHA', ''), ('REMOTE_STATUS', '1'))]
        invalid += [{'EXTENSION': value} for value in ('', 'chm', 'EPUB', 'epub,mobi', 'epub\n', '$(exit 0)')]
        invalid += [{'AUTOMATION_ID': AUTO_ID, 'SEARCH_GENERATION': GENERATION,
                     'EXPECTED_SHA': SHA, 'DEFER_UNIFY': 'true', 'AUTOMATION_ENABLED': 'true'}]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                result = self.shell(gate['run'] + '\necho writes-allowed', 'ebook-stream',
                                    **{**env, **overrides})
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('writes-allowed', result.stdout)

    def test_ebook_finalize_only_reuses_canonical_sidecar_and_unify_in_order(self):
        config = workflow('ebook-stream')
        job = config['jobs']['finalize']
        self.assertEqual(job['concurrency'], {'group': 'reader-sidecar',
                                            'cancel-in-progress': False, 'queue': 'max'})
        steps = job['steps']
        gate_index = next(i for i, step in enumerate(steps) if step.get('name') == GATE)
        operations = steps[gate_index + 1:]
        self.assertEqual(len(operations), 3)
        self.assertFalse(any('download-artifact' in step.get('uses', '') for step in steps))
        for extension, failed in itertools.product(('epub', 'all'), ('', 'merge', 'sidecar')):
            values = inputs_for(config, finalize_only=True, apply=True, extension=extension)
            script = '''python() {
  printf '%s\\n' "$*"
  case "$1:$FAILED" in
    scripts/finalize_ebook_indexes.py:merge|scripts/build_reader_assets_v2_sidecar.py:sidecar) return 1 ;;
  esac
}
gh() { printf '%s\\n' "$*"; }
'''
            for step in operations:
                self.assertNotIn('continue-on-error', step)
                self.assertTrue(expression(step.get('if', 'true'), inputs=values))
                script += step['run'] + '\n'
            result = self.shell(script, 'ebook-stream', EXTENSION=extension, FAILED=failed)
            expected = ['scripts/finalize_ebook_indexes.py ' + (
                '--extension epub' if extension == 'epub' else
                '--extension epub --extension mobi --extension azw3 --extension fb2')]
            if failed != 'merge':
                expected.append('scripts/build_reader_assets_v2_sidecar.py --apply')
            if not failed:
                expected += ['workflow run reader-v3-publish.yml --repo anftm/pipeline --ref main '
                             '-f operation=unify -f apply=true',
                             'central v3 unify dispatched; current reader-sidecar lock will be released when this producer exits']
            self.assertEqual(result.returncode == 0, not failed, result.stderr)
            self.assertEqual(result.stdout.splitlines(), expected)

    def test_execute_media_workflow_arguments_with_precise_extension(self):
        config = workflow('media-stream')
        schema = config.get('on', config.get(True))['workflow_dispatch']['inputs']
        self.assertEqual(set(schema['extension']['options']), media.MEDIA_EXTENSIONS | {'all'})
        self.assertFalse(schema['extension']['required'])
        self.assertEqual(schema['extension']['default'], 'all')
        for extension in ('all', 'mp3', 'asx', 'swf'):
            for job_name, script_name, expected_kind, option in (
                ('plan', 'plan_reader_stream_matrix.py', 'media', '--media-extension'),
                ('media', 'pilot_media_stream.py', 'audio', '--extension')):
                step = next(step for step in config['jobs'][job_name]['steps'] if
                            f'python scripts/{script_name}' in step.get('run', ''))
                values = inputs_for(config, apply=True, extension=extension)
                env = {key: str(expression(value, inputs=values, matrix={'extension': 'audio', 'shard': 0},
                                           secrets={})).lower() for key, value in step['env'].items()}
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'output').mkdir()
                    (root / 'output/media-matrix.json').write_text('{"include":[]}')
                    env = {**os.environ, **env}
                    env['GITHUB_OUTPUT'] = str(root / 'github-output')
                    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c',
                        'python() { printf "%s\\n" "$@"; };\n' + step['run']], cwd=root,
                        env=env, capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    args = result.stdout.splitlines()
                    self.assertEqual(args[0], f'scripts/{script_name}')
                    self.assertEqual(args[args.index('--kind') + 1], expected_kind)
                    self.assertEqual(args[args.index(option) + 1], extension)
                    if job_name == 'media':
                        self.assertIn('--strict', args)
                        self.assertIn('--apply', args)


class MediaExtensionTests(unittest.TestCase):
    def fixture(self, root):
        source = root / 'search.json'
        source.write_text(json.dumps([{'Repo': 'VoiceOfML/Test', 'Folder': [], 'File': f'clip-{ext}',
                                      'Extension': ext, 'Size': 100}
                                     for ext in sorted(media.MEDIA_EXTENSIONS | {'unknown'})]))
        revisions = root / 'revisions.json'
        revisions.write_text(json.dumps({'VoiceOfML/Test': SHA}))
        return source, revisions

    def test_planner_and_pilot_filter_each_supported_source_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            source, revisions = self.fixture(Path(directory))
            revision_map = json.loads(revisions.read_text())
            for extension in sorted(media.MEDIA_EXTENSIONS):
                planned = planner.selected_records(source, revision_map, 'media', {'audio', 'video', 'swf'}, extension)
                self.assertEqual(len(planned), 1, extension)
                selected = []
                for kind in ('audio', 'video', 'swf'):
                    selected.extend(media.select_records(source, revision_map, kind, extension))
                self.assertEqual([item['extension'] for item in selected], [extension])
                self.assertEqual(planned[0]['key'], f"{selected[0]['repo']}\0{selected[0]['path']}")
            self.assertEqual(media.select_records(source, revision_map, 'video', 'mp3'), [])
            self.assertEqual(planner.selected_records(source, revision_map, 'media', {'video'}, 'mp3'), [])
            self.assertGreater(len(media.select_records(source, revision_map, 'audio')), 1)
            self.assertEqual(len(planner.selected_records(source, revision_map, 'media', {'audio', 'video', 'swf'})),
                             len(media.MEDIA_EXTENSIONS))

    def test_unknown_extensions_fail_cli_and_selection_before_io(self):
        for extension in ('unknown', 'audio', 'MP3', '.mp3', 'mp3,wav'):
            with self.subTest(extension=extension), contextlib.redirect_stderr(io.StringIO()):
                with patch.object(sys, 'argv', ['pilot', '--kind', 'audio', '--extension', extension]):
                    with self.assertRaises(SystemExit) as raised:
                        media.parse_args()
                    self.assertEqual(raised.exception.code, 2)
                with patch.object(sys, 'argv', ['planner', '--kind', 'media', '--output', 'unused',
                                              '--media-extension', extension]):
                    with self.assertRaises(SystemExit) as raised:
                        planner.args()
                    self.assertEqual(raised.exception.code, 2)
                with self.assertRaises(ValueError):
                    media.select_records(Path('missing'), {}, 'audio', extension)
                with self.assertRaises(ValueError):
                    planner.selected_records(Path('missing'), {}, 'media', {'audio'}, extension)

    def test_real_cli_main_filters_before_limit_reuse_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            source, revisions = self.fixture(root)
            output = root / 'matrix.json'
            with patch.object(sys, 'argv', ['planner', '--kind', 'media', '--extension', 'all',
                    '--media-extension', 'mp3', '--search-data', str(source), '--revisions', str(revisions),
                    '--shard-count', '1', '--output', str(output)]), \
                    patch.object(planner, 'HfFileSystem'), patch.object(planner, 'read_json', return_value={}):
                self.assertEqual(planner.main(), 0)
            self.assertEqual(json.loads(output.read_text()), {'include': [{'extension': 'audio', 'shard': 0}]})
            other = {'key': 'VoiceOfML/Test\0old.wav', 'source_revision': 'old', 'extension': 'wav'}
            previous = {'files': [other], 'failures': []}
            with patch.object(sys, 'argv', ['pilot', '--kind', 'audio', '--extension', 'mp3', '--limit', '1',
                    '--search-data', str(source), '--revisions', str(revisions), '--apply', '--strict']), \
                    patch.object(media, 'existing_index', return_value=previous), \
                    patch.object(media, 'unchanged_sources', return_value=set()) as reuse, \
                    patch.object(media, 'build_one', return_value=({}, {})) as build, \
                    patch.object(media, 'publish_checkpoint', return_value=previous) as checkpoint:
                self.assertEqual(media.main(), 0)
            self.assertEqual([item['extension'] for item in reuse.call_args.args[0]], ['mp3'])
            self.assertEqual(build.call_args.args[0]['extension'], 'mp3')
            self.assertEqual(checkpoint.call_count, 1)
            self.assertIn(other, checkpoint.call_args.args[2]['files'])
            self.assertTrue(checkpoint.call_args.kwargs['apply'])


if __name__ == '__main__':
    unittest.main()
