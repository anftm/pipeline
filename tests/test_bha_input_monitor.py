"""Parser inputs are inspected twice without rebuilding or mutating branches."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import bha_input_monitor as monitor

HELPER = 'a' * 40


def baseline():
    return {'version': 1, 'archives': {str(i): {'main': 'b' * 40, 'config': 'c' * 40, 'ocr_cache': 'd' * 40,
                                               'ocr_patch': 'e' * 40, 'helper': HELPER, 'patch_layout': '2'}
                                         for i in range(32)}}


class BhaInputMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'baseline.json'; self.path.write_text(json.dumps(baseline()))

    def test_all_rows_are_compared_twice_and_no_write_is_reported(self):
        calls = []
        def lookup(_token, archive, _helper, state):
            calls.append(archive)
            return {'archive': archive, 'input_changed': archive == 1, 'local_build_needed': archive == 2,
                    'baseline': state[str(archive)]}
        result = monitor.inspect('token', self.path, lookup=lookup, helper_lookup=lambda *_: HELPER)
        self.assertEqual(len(calls), 64); self.assertEqual(result['changed_archives'], [1])
        self.assertEqual(result['local_build_required'], [2]); self.assertEqual(result['remote_writes'], 0)
        self.assertFalse(result['build_requested']); self.assertFalse(result['publication_ready'])
        self.assertTrue(result['inputs_observed_twice'])

    def test_head_drift_missing_baseline_and_unknown_inputs_fail_closed(self):
        values = iter([HELPER, 'f' * 40])
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            monitor.inspect('token', self.path, ids=[0], lookup=lambda *_: {'archive': 0},
                            helper_lookup=lambda *_: next(values))
        self.path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'all 32'):
            monitor.inspect('token', self.path)
        with self.assertRaises(ValueError): monitor.inspect('', self.path)

    def test_row_reads_exact_forks_and_preserves_nonidentical_upstream_inputs(self):
        source = {'main': 'b' * 40, 'config': 'c' * 40, 'ocr_cache': 'd' * 40,
                  'ocr_patch': 'e' * 40, 'parsed': '1' * 40}
        upstream = {**source, 'config': 'f' * 40, 'parsed': '2' * 40}
        info = ('repo', {'id': 1}, {'id': 2})
        with patch.object(monitor.identity, 'identities', return_value=info), \
                patch.object(monitor.parsed, 'branch_revisions', side_effect=[source, upstream]):
            result = monitor._row('token', 0, HELPER, baseline()['archives'])
        self.assertFalse(result['input_changed']); self.assertTrue(result['local_build_needed'])
        self.assertFalse(result['upstream_parsed_equal'])

    def test_cli_writes_report_before_returning_drift_failure(self):
        report = {'changed_archives': [0], 'remote_writes': 0}
        target = self.path.parent / 'report.json'
        with patch.object(monitor, 'inspect', return_value=report), patch.dict('os.environ', {'GH_PAT': 'test'}):
            self.assertEqual(monitor.main(['--baseline', str(self.path), '--output', str(target), '--fail-on-change']), 1)
        self.assertEqual(json.loads(target.read_text()), report)

    def test_workflow_is_manual_or_scheduled_readonly_and_serialized_with_forks(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-input-monitor.yml'
        value = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(value['on']), {'schedule', 'workflow_dispatch'})
        self.assertEqual(value['permissions'], {'contents': 'read'})
        self.assertEqual(value['concurrency']['group'], 'maintain-archive-forks')
        commands = '\n'.join(step.get('run', '') for step in value['jobs']['inspect']['steps'])
        self.assertIn('scripts/bha_input_monitor.py', commands)
        self.assertIn('--fail-on-change', commands)
        artifact = next(step for step in value['jobs']['inspect']['steps'] if step.get('uses') == 'actions/upload-artifact@v7')
        self.assertEqual(artifact['if'], '${{ always() }}')
        for forbidden in ('build_fork_parsed.py', 'git push', '/api/reindex', 'sync_parsed'):
            self.assertNotIn(forbidden, commands)


if __name__ == '__main__': unittest.main()
