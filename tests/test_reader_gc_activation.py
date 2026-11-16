"""Maintenance activation must allow read-only credential verification first."""
from pathlib import Path
import json
import os
import subprocess
import unittest
import yaml


class MaintenanceActivationTests(unittest.TestCase):
    def test_exact_scope_workflow_passes_json_as_one_literal_argument(self):
        workflow = yaml.safe_load(Path('.github/workflows/reader-maintenance.yml').read_text())
        triggers = workflow.get('on', workflow.get(True))
        inputs = triggers['workflow_dispatch']['inputs']
        self.assertEqual(inputs['scope']['options'],
                         ['retired-assets', 'unreferenced', 'page-plan', 'page-apply', 'obsolete-pdf-references'])
        self.assertEqual(inputs['scope']['default'], 'retired-assets')
        self.assertEqual(inputs['resume_run']['type'], 'string')
        self.assertEqual(inputs['resume_run']['default'], '')
        step = next(s for s in workflow['jobs']['maintain']['steps']
                    if s.get('name') == 'Pause producers, collect and restore')
        self.assertEqual(step['env']['RESUME_RUN'], "${{ inputs.resume_run || '' }}")
        self.assertNotIn('${{', step['run'])
        # Capture argv without running maintenance; shell syntax in JSON paths
        # must remain literal, including whitespace, quotes and substitutions.
        configs = [('page-plan', json.dumps({'paths': ['derived/a b', 'objects/$(exit 91)`exit 92`";*']}, indent=2)),
                   ('page-apply', json.dumps({'plan_run': '123', 'plan_sha256': 'a' * 64,
                                             'external_writers_stopped': True})),
                   ('retired-assets', '')]
        for scope, config in configs:
            for plan_only in ('true', 'false'):
                with self.subTest(scope=scope, plan_only=plan_only):
                    result = subprocess.run(['bash', '-eu', '-c',
                        'python() { printf "%s\\0" "$@"; }\n' + step['run']],
                        env={**os.environ, 'LIMIT': '0', 'SCOPE': scope, 'RESUME_RUN': config,
                             'PLAN_ONLY': plan_only, 'EVENT_NAME': 'workflow_dispatch'},
                        capture_output=True, check=True)
                    expected = ['-u', 'scripts/reader_gc_maintenance.py', 'run', '--journal',
                                'output/maintenance/journal.json', '--limit', '0', '--scope', scope,
                                '--resume-run', config]
                    if plan_only == 'true':
                        expected.append('--plan-only')
                    self.assertEqual(result.stdout.decode().split('\0')[:-1], expected)
        collector = yaml.safe_load(Path('.github/workflows/reader-gc-collect.yml').read_text())
        self.assertEqual(collector['concurrency']['group'], 'reader-sidecar')
        self.assertIs(collector['concurrency']['cancel-in-progress'], False)

    def test_obsolete_reference_operation_requires_manual_write_selection(self):
        flow = yaml.safe_load(Path('.github/workflows/reader-maintenance.yml').read_text())
        step = next(s for s in flow['jobs']['maintain']['steps'] if s.get('name') == 'Pause producers, collect and restore')
        for event, plan, limit in [('schedule', 'false', '0'), ('workflow_dispatch', 'true', '0'), ('workflow_dispatch', 'false', '1')]:
            result = subprocess.run(['bash', '-eu', '-c', 'python() { exit 99; }; mkdir() { exit 98; };\n' + step['run']],
                env={**os.environ, 'SCOPE':'obsolete-pdf-references', 'EVENT_NAME':event,
                     'PLAN_ONLY':plan,'LIMIT':limit,'RESUME_RUN':'{}'}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(result.returncode, [98, 99])
        self.assertIn('python -u -m scripts.reader_obsolete_pdf_run', step['run'])

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
