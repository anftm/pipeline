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
        self.assertEqual(result['upstream_inputs']['config'], 'f' * 40)

    def test_upstream_commit_drift_is_detected_even_when_build_flag_stays_true(self):
        source = {'main': 'b' * 40, 'config': 'c' * 40, 'ocr_cache': 'd' * 40,
                  'ocr_patch': 'e' * 40, 'parsed': '1' * 40}
        first = {**source, 'config': 'f' * 40}
        second = {**source, 'config': '9' * 40}
        with patch.object(monitor.identity, 'identities', return_value=('repo', {'id': 1}, {'id': 2})), \
                patch.object(monitor.parsed, 'branch_revisions', side_effect=[source, first, source, second]):
            with self.assertRaisesRegex(RuntimeError, 'changed during'):
                monitor.inspect('token', self.path, ids=[0], helper_lookup=lambda *_: HELPER)

    def test_cli_writes_report_before_returning_drift_failure(self):
        report = {'changed_archives': [0], 'remote_writes': 0}
        target = self.path.parent / 'report.json'
        with patch.object(monitor, 'inspect', return_value=report), patch.dict('os.environ', {'GH_PAT': 'test'}):
            self.assertEqual(monitor.main(['--baseline', str(self.path), '--output', str(target), '--fail-on-change']), 1)
        self.assertEqual(json.loads(target.read_text()), report)

    def test_runtime_baseline_monitor_reads_verified_overlay_without_writes(self):
        from scripts import bha_upstream_auto as upstream, bha_runtime_baseline as overlays
        trees={str(i):'f'*40 for i in range(32)}
        self.path.with_name('bha-parsed-revisions.json').write_text(json.dumps(trees))
        newer={**baseline()['archives']['0'],'main':'1'*40}
        overlay=overlays.advance(overlays.empty(),baseline(),trees,archive=0,after_inputs=newer,
                                after_tree='2'*40,acceptance={'verified':True})
        target=self.path.parent/'report.json'
        with patch.object(upstream,'S3Store') as store, patch.object(upstream,'Receipts') as receipts:
            receipts.return_value.state={'baseline_overlay':{'fixture':True}}
            receipts.return_value.read.return_value=overlay
            def inspect(token,path):
                self.assertEqual(json.loads(path.read_text())['archives']['0'],newer)
                return {'changed_archives':[],'remote_writes':0}
            with patch.object(monitor,'inspect',side_effect=inspect):
                self.assertEqual(monitor.main(['--baseline',str(self.path),'--output',str(target),'--runtime-baseline']),0)
            store.return_value.put.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()),baseline())

    def test_workflow_is_manual_or_scheduled_readonly_and_serialized_with_forks(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-input-monitor.yml'
        value = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(value['on']), {'schedule', 'workflow_dispatch'})
        self.assertEqual(value['permissions'], {'contents': 'read'})
        self.assertEqual(value['concurrency']['group'], 'maintain-archive-forks')
        commands = '\n'.join(step.get('run', '') for step in value['jobs']['inspect']['steps'])
        self.assertIn('-m scripts.bha_input_monitor', commands)
        self.assertIn('--runtime-baseline', commands)
        self.assertIn('--fail-on-change', commands)
        artifact = next(step for step in value['jobs']['inspect']['steps'] if step.get('uses') == 'actions/upload-artifact@v7')
        self.assertEqual(artifact['if'], '${{ always() }}')
        for forbidden in ('build_fork_parsed.py', 'git push', '/api/reindex', 'sync_parsed'):
            self.assertNotIn(forbidden, commands)


if __name__ == '__main__': unittest.main()
