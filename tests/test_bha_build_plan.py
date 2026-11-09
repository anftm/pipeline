"""BHA build demand depends on changed input, not merely fork divergence."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import bha_build_plan as plan
from scripts import search_snapshot
from tests.test_bha_input_monitor import baseline


class BhaBuildPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / 'inputs.json'; self.inputs.write_text(json.dumps(baseline()))
        self.trees = self.root / 'trees.json'; self.trees.write_text(json.dumps({str(i): 'f' * 40 for i in range(32)}))
        mirror = {'main': 'b' * 40, 'config': 'c' * 40, 'ocr_cache': 'd' * 40,
                  'ocr_patch': 'e' * 40, 'parsed': '1' * 40}
        self.snapshot = {'archive': 0, 'upstream_repository_id': 11, 'mirror_repository_id': 22,
                         'mirror_branches': mirror, 'upstream_branches': {**mirror, 'ocr_patch': '9' * 40},
                         'helper_revision': 'a' * 40, 'parsed_tree': 'f' * 40}

    def inspect(self, snapshot):
        return plan.inspect('fixture', 0, input_path=self.inputs, tree_path=self.trees, snapshot=lambda *_: snapshot)

    def test_fork_difference_alone_does_not_claim_rebuild_is_needed(self):
        value = self.inspect(self.snapshot)
        self.assertTrue(value['fork_requires_local_build'])
        self.assertFalse(value['input_changed'])
        self.assertFalse(value['parsed_tree_changed'])
        self.assertEqual(value['action'], 'no-input-change')
        self.assertFalse(value['build_requested']); self.assertFalse(value['consumer_verified'])
        self.assertFalse(value['publication_ready']); self.assertFalse(value['external_writers_verified'])
        self.assertEqual(value['remote_writes'], 0)
        digest = value.pop('plan_sha256')
        self.assertEqual(digest, hashlib.sha256(search_snapshot.canonical(value)).hexdigest())

    def test_actual_input_change_plans_build_not_deployment(self):
        snapshot = {**self.snapshot, 'mirror_branches': {**self.snapshot['mirror_branches'], 'ocr_patch': '4' * 40}}
        value = self.inspect(snapshot)
        self.assertEqual(value['action'], 'build-candidate')
        self.assertTrue(value['input_changed']); self.assertEqual(value['remote_writes'], 0)

    def test_changed_but_matching_upstream_needs_review_not_automatic_rebuild(self):
        mirror = {**self.snapshot['mirror_branches'], 'ocr_patch': '4' * 40}
        value = self.inspect({**self.snapshot, 'mirror_branches': mirror, 'upstream_branches': dict(mirror)})
        self.assertTrue(value['input_changed'])
        self.assertFalse(value['fork_requires_local_build'])
        self.assertEqual(value['action'], 'inspect-upstream-parsed')
        self.assertFalse(value['publication_ready'])

    def test_unexpected_parsed_tree_takes_precedence_over_build(self):
        snapshot = {**self.snapshot, 'parsed_tree': '4' * 40,
                    'mirror_branches': {**self.snapshot['mirror_branches'], 'ocr_cache': '5' * 40}}
        value = self.inspect(snapshot)
        self.assertEqual(value['action'], 'inspect-drift')
        self.assertTrue(value['input_changed']); self.assertTrue(value['parsed_tree_changed'])

    def test_changes_during_inspection_and_malformed_identity_fail_closed(self):
        next_snapshot = {**self.snapshot, 'parsed_tree': '0' * 40}
        values = iter([self.snapshot, next_snapshot])
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            plan.inspect('fixture', 0, input_path=self.inputs, tree_path=self.trees, snapshot=lambda *_: next(values))
        for change in ({'mirror_repository_id': True}, {'parsed_tree': 'broken'},
                       {'mirror_branches': {**self.snapshot['mirror_branches'], 'extra': '1' * 40}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.inspect({**self.snapshot, **change})
        with self.assertRaises(ValueError): plan.inspect('', 0, input_path=self.inputs, tree_path=self.trees)
        with self.assertRaises(ValueError): plan.inspect('fixture', 32, input_path=self.inputs, tree_path=self.trees)

    def test_bad_baselines_and_existing_output_rejected(self):
        trees = json.loads(self.trees.read_text()); del trees['31']; self.trees.write_text(json.dumps(trees))
        with self.assertRaisesRegex(ValueError, 'all 32'):
            self.inspect(self.snapshot)
        output = self.root / 'existing'; output.write_text('protected')
        with self.assertRaisesRegex(ValueError, 'new path'):
            plan.main(['--archive', '0', '--output', str(output)])
        self.assertEqual(output.read_text(), 'protected')

    def test_workflow_is_manual_readonly_and_never_executes_builder(self):
        import yaml
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-build-plan.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'maintain-archive-forks')
        steps = flow['jobs']['inspect']['steps']
        commands = '\n'.join(step.get('run', '') for step in steps)
        self.assertIn('scripts/bha_build_plan.py', commands)
        for forbidden in ('build_fork_parsed.py', 'git push', '/api/reindex', 'docker run'):
            self.assertNotIn(forbidden, commands)
        artifact = next(step for step in steps if step.get('uses') == 'actions/upload-artifact@v7')
        self.assertEqual(artifact['with']['if-no-files-found'], 'error')

    def test_snapshot_rechecks_parsed_commit_identity(self):
        mirror = self.snapshot['mirror_branches']; upstream = self.snapshot['upstream_branches']
        with patch.object(plan.identity, 'identities', return_value=('repo', {'id': 11}, {'id': 22})), \
                patch.object(plan.parsed, 'branch_revisions', side_effect=[mirror, upstream]), \
                patch.object(plan.parsed, 'ref_revision', return_value='a' * 40), \
                patch.object(plan.revisions, 'archive_identity', return_value=('0', '7' * 40, 'f' * 40)):
            with self.assertRaisesRegex(ValueError, 'changed during'):
                plan._snapshot('fixture', 0)


if __name__ == '__main__':
    unittest.main()
