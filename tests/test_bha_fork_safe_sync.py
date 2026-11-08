"""A BHA fork updater can only apply one exact, proven fast-forward."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import bha_fork_safe_sync as sync

A, B = 'a' * 40, 'b' * 40


def plan():
    return {'version': 1, 'kind': 'bha-fork-branch-plan', 'archive': 0, 'branch': 'main',
            'upstream_repository': 'banned-historical-archives/banned-historical-archives0',
            'upstream_repository_id': 1, 'mirror_repository': 'anftm/banned-historical-archives0',
            'mirror_repository_id': 2, 'upstream': B, 'mirror': A, 'mirror_relation': 'behind',
            'can_fast_forward': True, 'remote_writes': 0, 'external_writers_verified': False}


def identical():
    return {**plan(), 'mirror': B, 'mirror_relation': 'identical', 'can_fast_forward': False}


class BhaForkSafeSyncTests(unittest.TestCase):
    def test_identity_verifies_numeric_parent_and_upstream(self):
        upstream = {'full_name': 'banned-historical-archives/banned-historical-archives0', 'id': 1}
        mirror = {'full_name': 'anftm/banned-historical-archives0', 'id': 2, 'fork': True,
                  'parent': {'id': 1, 'full_name': upstream['full_name']}}
        with patch.object(sync.fork, 'api_request', side_effect=[(200, upstream), (200, mirror)]):
            self.assertEqual(sync.identities('token', 0)[0], 'banned-historical-archives0')
        for bad in ({**mirror, 'id': None}, {**mirror, 'parent': {'id': 3, 'full_name': upstream['full_name']}}):
            with self.subTest(bad=bad), patch.object(sync.fork, 'api_request', side_effect=[(200, upstream), (200, bad)]), \
                    self.assertRaises(ValueError):
                sync.identities('token', 0)
        with self.assertRaises(ValueError): sync.identities('', 0)
        with self.assertRaises(ValueError): sync.identities('token', 32)

    def test_inspection_is_get_only_and_preserves_divergence(self):
        p = plan(); up = {'full_name': p['upstream_repository'], 'id': 1}; mir = {'full_name': p['mirror_repository'], 'id': 2}
        with patch.object(sync, 'identities', return_value=('banned-historical-archives0', up, mir)), \
                patch.object(sync.fork, 'branch_sha', side_effect=[B, A, B, A]), \
                patch.object(sync.fork, 'api_request', return_value=(200, {'status': 'behind'})) as api:
            self.assertEqual(sync.inspect('token', archive=0, branch='main'), p)
            self.assertEqual(api.call_args.args[1], 'GET')
        with self.assertRaisesRegex(ValueError, 'approved'):
            sync.inspect('token', archive=0, branch='parsed')
        with self.assertRaisesRegex(ValueError, 'approved'):
            sync.inspect('token', archive=0, branch='proofread/foreign')

    def test_identical_branch_remains_a_zero_write_plan(self):
        p = plan(); up = {'full_name': p['upstream_repository'], 'id': 1}; mir = {'full_name': p['mirror_repository'], 'id': 2}
        with patch.object(sync, 'identities', return_value=('banned-historical-archives0', up, mir)), \
                patch.object(sync.fork, 'branch_sha', return_value=A), patch.object(sync.fork, 'api_request') as api:
            result = sync.inspect('token', archive=0, branch='main')
        self.assertEqual(result['mirror_relation'], 'identical')
        self.assertFalse(result['can_fast_forward'])
        self.assertEqual(result['remote_writes'], 0)
        api.assert_not_called()

    def test_branch_change_unknown_relation_and_repository_recreation_fail_closed(self):
        p = plan(); up = {'full_name': p['upstream_repository'], 'id': 1}; mir = {'full_name': p['mirror_repository'], 'id': 2}
        with patch.object(sync, 'identities', return_value=('banned-historical-archives0', up, mir)), \
                patch.object(sync.fork, 'branch_sha', side_effect=[B, A]), \
                patch.object(sync.fork, 'api_request', return_value=(404, {})), \
                self.assertRaisesRegex(ValueError, 'relationship is unknown'):
            sync.inspect('token', archive=0, branch='main')
        with patch.object(sync, 'identities', return_value=('banned-historical-archives0', up, mir)), \
                patch.object(sync.fork, 'branch_sha', side_effect=[B, A, 'c' * 40]), \
                patch.object(sync.fork, 'api_request', return_value=(200, {'status': 'behind'})), \
                self.assertRaisesRegex(RuntimeError, 'branch changed'):
            sync.inspect('token', archive=0, branch='main')
        with patch.object(sync, 'identities', side_effect=[('banned-historical-archives0', up, mir),
                                                          ('banned-historical-archives0', up, {**mir, 'id': 3})]), \
                patch.object(sync.fork, 'branch_sha', return_value=A), \
                self.assertRaisesRegex(RuntimeError, 'repository identity changed'):
            sync.inspect('token', archive=0, branch='main')

    def test_apply_rejects_branch_drift_before_push_and_mismatched_readback(self):
        p = plan()
        def run(cmd, **_kwargs):
            if cmd[:2] == ['git', 'rev-parse']:
                return SimpleNamespace(stdout=B if cmd[-1].endswith('upstream') else A)
            return SimpleNamespace(stdout='')
        changed = {**p, 'upstream': 'c' * 40}
        with patch.object(sync, 'inspect', side_effect=[p, changed]), patch.object(sync, '_run', side_effect=run) as calls, \
                patch.object(sync.parsed, 'git_environment', return_value={}):
            with self.assertRaisesRegex(ValueError, 'changed before push'):
                sync.apply('token', p, confirmed=True)
            self.assertFalse(any(c.args[0][:2] == ['git', 'push'] for c in calls.call_args_list))
        with patch.object(sync, 'inspect', side_effect=[p, p, changed]), patch.object(sync, '_run', side_effect=run), \
                patch.object(sync.parsed, 'git_environment', return_value={}), \
                self.assertRaisesRegex(RuntimeError, 'readback differs'):
            sync.apply('token', p, confirmed=True)

    def test_changed_plan_or_no_confirmation_cannot_write(self):
        p = plan()
        with patch.object(sync, 'inspect', return_value={**p, 'mirror': 'c' * 40}), \
                patch.object(sync, '_run') as run:
            with self.assertRaisesRegex(ValueError, 'changed'):
                sync.apply('token', p, confirmed=True)
            run.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            sync.apply('token', p, confirmed=False)
        ahead = {**p, 'mirror_relation': 'ahead', 'can_fast_forward': False}
        with patch.object(sync, 'inspect', return_value=ahead), patch.object(sync, '_run') as run:
            with self.assertRaisesRegex(ValueError, 'fast-forward'):
                sync.apply('token', ahead, confirmed=True)
            run.assert_not_called()

    def test_exact_git_refs_ancestor_and_single_lease_push(self):
        p = plan(); calls = []
        def run(cmd, **_kwargs):
            calls.append(cmd)
            stdout = B if cmd[:2] == ['git', 'rev-parse'] and cmd[-1].endswith('upstream') else (
                A if cmd[:2] == ['git', 'rev-parse'] else '')
            return SimpleNamespace(stdout=stdout)
        with patch.object(sync, 'inspect', side_effect=[p, p, identical()]), \
                patch.object(sync, '_run', side_effect=run), patch.object(sync.parsed, 'git_environment', return_value={}):
            result = sync.apply('token', p, confirmed=True)
        self.assertEqual(result['status'], 'written')
        self.assertFalse(result['external_writers_verified'])
        pushes = [cmd for cmd in calls if cmd[:2] == ['git', 'push']]
        self.assertEqual(len(pushes), 1)
        self.assertIn(f'--force-with-lease=refs/heads/main:{A}', pushes[0])
        self.assertIn(f'{B}:refs/heads/main', pushes[0])
        self.assertIn(['git', 'merge-base', '--is-ancestor', A, B], calls)
        self.assertFalse(any('delete' in part for cmd in calls for part in cmd))

    def test_lost_push_response_is_observed_once_and_never_replayed(self):
        p = plan(); pushes = []
        def run(cmd, **_kwargs):
            if cmd[:2] == ['git', 'rev-parse']:
                return SimpleNamespace(stdout=B if cmd[-1].endswith('upstream') else A)
            if cmd[:2] == ['git', 'push']:
                pushes.append(cmd); raise subprocess.TimeoutExpired(cmd, 1)
            return SimpleNamespace(stdout='')
        with patch.object(sync, 'inspect', side_effect=[p, p, identical()]), \
                patch.object(sync, '_run', side_effect=run), patch.object(sync.parsed, 'git_environment', return_value={}):
            result = sync.apply('token', p, confirmed=True)
        self.assertEqual(result['status'], 'written-response-unknown')
        self.assertEqual(len(pushes), 1)

    def test_unconfirmed_timeout_and_moved_git_refs_do_not_replay(self):
        p = plan(); pushes = []
        def run(cmd, **_kwargs):
            if cmd[:2] == ['git', 'rev-parse']:
                return SimpleNamespace(stdout=B if cmd[-1].endswith('upstream') else A)
            if cmd[:2] == ['git', 'push']:
                pushes.append(cmd); raise subprocess.TimeoutExpired(cmd, 1)
            return SimpleNamespace(stdout='')
        with patch.object(sync, 'inspect', side_effect=[p, p, p]), \
                patch.object(sync, '_run', side_effect=run), patch.object(sync.parsed, 'git_environment', return_value={}), \
                self.assertRaises(subprocess.TimeoutExpired):
            sync.apply('token', p, confirmed=True)
        self.assertEqual(len(pushes), 1)
        with patch.object(sync, 'inspect', return_value=p), patch.object(sync.parsed, 'git_environment', return_value={}), \
                patch.object(sync, '_run', return_value=SimpleNamespace(stdout='d' * 40)) as call:
            with self.assertRaisesRegex(ValueError, 'Git refs changed'): sync.apply('token', p, confirmed=True)
            self.assertFalse(any(c.args[0][:2] == ['git', 'push'] for c in call.call_args_list))

    def test_workflow_is_manual_and_requires_a_reviewed_exact_plan(self):
        import yaml
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-fork-sync.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertEqual(flow['concurrency'], {'group': 'maintain-archive-forks',
                                               'cancel-in-progress': 'false', 'queue': 'max'})
        inputs = flow['on']['workflow_dispatch']['inputs']
        self.assertEqual(inputs['apply']['default'], 'false')
        self.assertEqual(set(inputs['branch']['options']), sync.BRANCHES)
        self.assertNotIn('parsed', inputs['branch']['options'])
        steps = {step.get('name'): step for step in flow['jobs']['synchronize']['steps'] if step.get('name')}
        self.assertIn('PLAN_RUN', steps['Validate request']['env'])
        self.assertIn('PLAN_SHA', steps['Validate request']['env'])
        self.assertIn('sha256(raw)', steps['Verify plan provenance and exact content']['run'])
        self.assertIn("run.get('head_sha') != os.environ['GITHUB_SHA']", steps['Verify plan provenance and exact content']['run'])
        command = steps['Inspect or apply one branch']['run']
        self.assertIn('--apply-plan', command)
        self.assertIn('--plan-sha256 "$PLAN_SHA"', command)
        self.assertIn('--confirm-exact-fast-forward', command)
        self.assertNotIn('fork_repositories.py', command)
        self.assertNotIn('git push', command)
        self.assertEqual(steps['Preserve reviewed plan']['if'], '${{ !inputs.apply }}')

    def test_cli_rechecks_exact_plan_hash_before_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / 'plan.json'; path.write_text(json.dumps(plan()))
            args = ['--archive', '0', '--branch', 'main', '--output', str(root / 'result.json'),
                    '--apply-plan', str(path), '--plan-sha256', '0' * 64, '--confirm-exact-fast-forward']
            with patch.object(sync, 'apply') as apply, self.assertRaisesRegex(ValueError, 'SHA-256 differs'):
                sync.main(args)
            apply.assert_not_called()
            args[args.index('--plan-sha256') + 1] = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(sync, 'apply', return_value={'status': 'synthetic'}) as apply:
                self.assertEqual(sync.main(args), 0)
            apply.assert_called_once()
            self.assertEqual(json.loads((root / 'result.json').read_text()), {'status': 'synthetic'})

    def test_cli_cannot_overwrite_approved_plan_or_choose_arbitrary_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'plan.json'; path.write_text(json.dumps(plan()))
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                sync.main(['--archive', '0', '--branch', 'main', '--output', str(path), '--apply-plan', str(path)])
            self.assertEqual(json.loads(path.read_text()), plan())


if __name__ == '__main__':
    unittest.main()
