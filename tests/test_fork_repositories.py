import unittest
from unittest.mock import call, patch

from scripts import fork_repositories


class ForkRepositoriesTests(unittest.TestCase):
    def test_inspect_reports_one_existing_fork_using_only_get_requests(self):
        repo = 'banned-historical-archives0'
        upstream = {'full_name': f'{fork_repositories.UPSTREAM_OWNER}/{repo}', 'default_branch': 'main'}
        mirror = {'full_name': f'{fork_repositories.MIRROR_OWNER}/{repo}', 'fork': True,
                  'parent': {'full_name': upstream['full_name']}}
        methods = []
        def request(_token, method, path, payload=None):
            methods.append(method)
            if path == fork_repositories.repo_path(fork_repositories.UPSTREAM_OWNER, repo): return 200, upstream
            if path == fork_repositories.repo_path(fork_repositories.MIRROR_OWNER, repo): return 200, mirror
            if '/compare/' in path: return 200, {'status': 'ahead'}
            raise AssertionError(path)
        def sha(_token, owner, _repo, branch):
            return ('a' if owner == fork_repositories.UPSTREAM_OWNER else 'b') * 40
        with patch.object(fork_repositories, 'api_request', side_effect=request), \
                patch.object(fork_repositories, 'list_branches', side_effect=[['main', 'ocr_cache', 'extra'], ['main', 'parsed']]), \
                patch.object(fork_repositories, 'branch_sha', side_effect=sha), \
                patch.object(fork_repositories, 'ensure_fork') as ensure, \
                patch.object(fork_repositories, 'delete_branch') as delete, \
                patch.object(fork_repositories, 'sync_branch') as sync:
            report = fork_repositories.inspect_fork('token', 0)
        self.assertEqual(methods, ['GET', 'GET', 'GET'])
        self.assertEqual([row['branch'] for row in report['managed_branches']], ['main', 'ocr_cache', 'parsed'])
        self.assertEqual([row['mirror_relation'] for row in report['managed_branches']],
                         ['ahead', 'upstream-only', 'mirror-only'])
        self.assertEqual(report['unmanaged_upstream_count'], 1)
        self.assertEqual(report['remote_writes'], 0)
        self.assertFalse(report['synchronization_applied'])
        ensure.assert_not_called(); delete.assert_not_called(); sync.assert_not_called()

    def test_inspect_fails_closed_for_unexpected_fork_or_disappearing_branch(self):
        repo = 'banned-historical-archives0'
        upstream = {'full_name': f'{fork_repositories.UPSTREAM_OWNER}/{repo}', 'default_branch': 'main'}
        mirror = {'full_name': f'{fork_repositories.MIRROR_OWNER}/{repo}', 'fork': True,
                  'parent': {'full_name': upstream['full_name']}}
        for modified in ({**mirror, 'fork': False}, {**mirror, 'parent': {'full_name': 'other/repo'}}):
            with patch.object(fork_repositories, 'api_request', side_effect=[(200, upstream), (200, modified)]):
                with self.assertRaises(ValueError): fork_repositories.inspect_fork('token', 0)
        with patch.object(fork_repositories, 'api_request', side_effect=[(200, upstream), (200, mirror)]), \
                patch.object(fork_repositories, 'list_branches', side_effect=[['main'], ['main']]), \
                patch.object(fork_repositories, 'branch_sha', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'changed'): fork_repositories.inspect_fork('token', 0)
        with self.assertRaises(ValueError): fork_repositories.inspect_fork('token', 32)

    def test_inspection_compare_404_is_unknown_not_isolation_or_divergence(self):
        repo = 'banned-historical-archives0'
        upstream = {'full_name': f'{fork_repositories.UPSTREAM_OWNER}/{repo}', 'default_branch': 'main'}
        mirror = {'full_name': f'{fork_repositories.MIRROR_OWNER}/{repo}', 'fork': True,
                  'parent': {'full_name': upstream['full_name']}}
        with patch.object(fork_repositories, 'api_request', side_effect=[(200, upstream), (200, mirror), (404, {})]), \
                patch.object(fork_repositories, 'list_branches', side_effect=[['main'], ['main']]), \
                patch.object(fork_repositories, 'branch_sha', side_effect=['a' * 40, 'b' * 40]):
            result = fork_repositories.inspect_fork('token', 0)
        self.assertEqual(result['managed_branches'][0]['mirror_relation'], 'unknown-not-found')
        self.assertEqual(result['remote_writes'], 0)

    def test_restored_fork_workflow_can_only_run_manual_readonly_inspection(self):
        from pathlib import Path
        import yaml
        flow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/bha-fork-archives.yml').read_text())
        self.assertEqual(set(flow.get('on', flow.get(True))), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        commands = '\n'.join(step.get('run', '') for job in flow['jobs'].values() for step in job['steps'])
        self.assertIn('scripts/fork_repositories.py --inspect', commands)
        self.assertNotIn('python3 scripts/fork_repositories.py\n', commands)

    def test_main_syncs_only_managed_upstream_data_branches(self):
        with patch.dict(fork_repositories.os.environ, {"GH_PAT": "token"}), \
                patch.object(fork_repositories, "REPO_START", 0), \
                patch.object(fork_repositories, "REPO_END", 0), \
                patch.object(fork_repositories, "ensure_fork", return_value="main"), \
                patch.object(
                    fork_repositories, "list_branches",
                    side_effect=[["main", "parsed", "ocr_patch", "feat/old"], ["main", "parsed", "ocr_patch", "feat/old"]],
                ), \
                patch.object(fork_repositories, "sync_branch") as sync_branch, \
                patch.object(fork_repositories, "clean_unmanaged_inherited_branches") as inherited, \
                patch.object(fork_repositories, "clean_resolved_temporary_branches") as temporary:
            result = fork_repositories.main()

        self.assertEqual(result, 0)
        self.assertEqual(sync_branch.call_args_list, [
            call("token", "banned-historical-archives0", "main"),
            call("token", "banned-historical-archives0", "ocr_patch"),
        ])
        inherited.assert_called_once()
        temporary.assert_called_once()

    def test_main_preserves_legacy_data_and_nonstandard_default_branches(self):
        upstream = ["main", "origin", "selected", "tags", "feat/old"]
        with patch.dict(fork_repositories.os.environ, {"GH_PAT": "token"}), \
                patch.object(fork_repositories, "REPO_START", 10), \
                patch.object(fork_repositories, "REPO_END", 10), \
                patch.object(fork_repositories, "ensure_fork", return_value="origin"), \
                patch.object(fork_repositories, "list_branches", side_effect=[upstream, upstream]), \
                patch.object(fork_repositories, "sync_branch") as sync_branch, \
                patch.object(fork_repositories, "clean_unmanaged_inherited_branches") as inherited, \
                patch.object(fork_repositories, "clean_resolved_temporary_branches"):
            self.assertEqual(fork_repositories.main(), 0)
        self.assertEqual([item.args[2] for item in sync_branch.call_args_list], ["main", "origin", "selected", "tags"])
        self.assertIn("origin", inherited.call_args.args[4])
        self.assertIn("selected", inherited.call_args.args[4])

    def test_unmanaged_inherited_branch_is_deleted_only_when_identical(self):
        upstream = {"main", "feat/old", "feat/diverged"}
        mirror = set(upstream)
        shas = {
            (fork_repositories.UPSTREAM_OWNER, "feat/old"): "a",
            (fork_repositories.MIRROR_OWNER, "feat/old"): "a",
            (fork_repositories.UPSTREAM_OWNER, "feat/diverged"): "a",
            (fork_repositories.MIRROR_OWNER, "feat/diverged"): "b",
        }
        with patch.object(fork_repositories, "branch_sha", side_effect=lambda _t, owner, _r, branch: shas[(owner, branch)]), \
                patch.object(fork_repositories, "branch_pulls", return_value=[{"state": "closed"}]), \
                patch.object(fork_repositories, "delete_branch") as delete:
            fork_repositories.clean_unmanaged_inherited_branches(
                "token", "banned-historical-archives0", upstream, mirror,
            )
        delete.assert_called_once_with("token", "banned-historical-archives0", "feat/old")

    def test_open_or_untracked_upstream_branch_is_preserved(self):
        upstream = {"main", "feat/open", "manual-data"}
        with patch.object(
            fork_repositories, "branch_pulls",
            side_effect=[[{"state": "open"}], []],
        ), patch.object(fork_repositories, "branch_sha") as sha, \
                patch.object(fork_repositories, "delete_branch") as delete:
            fork_repositories.clean_unmanaged_inherited_branches(
                "token", "banned-historical-archives0", upstream, set(upstream),
            )
        sha.assert_not_called()
        delete.assert_not_called()

    def test_closed_proofreading_branch_is_deleted_but_open_branch_is_kept(self):
        mirror = {"main", "proofread/111111111111-config", "proofread/222222222222-ocr_patch", "proofread/333333333333-ocr_config", "proofread/444444444444-origin", "proofread/555555555555-main"}
        def pulls(_token, _owner, _repo, branch):
            return [{"state": "open" if branch.endswith("ocr_patch") else "closed"}]

        with patch.object(fork_repositories, "branch_pulls", side_effect=pulls), \
                patch.object(fork_repositories, "delete_branch") as delete:
            fork_repositories.clean_resolved_temporary_branches(
                "token", "banned-historical-archives0", {"main"}, mirror,
            )
        self.assertEqual({call.args[2] for call in delete.call_args_list}, {
            "proofread/111111111111-config", "proofread/333333333333-ocr_config",
            "proofread/444444444444-origin", "proofread/555555555555-main",
        })


    def test_optional_branch_cleanup_setting_does_not_fail_archive_sync(self):
        """A refused cosmetic repo setting must not block fork data syncing."""
        repo = "banned-historical-archives0"
        mirror = {"full_name": f"{fork_repositories.MIRROR_OWNER}/{repo}", "fork": True,
                  "parent": {"full_name": f"{fork_repositories.UPSTREAM_OWNER}/{repo}"}}
        responses = {
            ("GET", f"/repos/{fork_repositories.MIRROR_OWNER}/{repo}"): (200, mirror),
            ("PATCH", f"/repos/{fork_repositories.MIRROR_OWNER}/{repo}"): (404, {"message": "Not Found"}),
            ("GET", f"/repos/{fork_repositories.UPSTREAM_OWNER}/{repo}"): (200, {"default_branch": "main"}),
        }
        with patch.object(fork_repositories, "api_request",
                          side_effect=lambda _t, method, path, payload=None: responses[(method, path)]):
            self.assertEqual(fork_repositories.ensure_fork("token", repo), "main")
        responses[("PATCH", f"/repos/{fork_repositories.MIRROR_OWNER}/{repo}")] = (500, {"message": "boom"})
        with patch.object(fork_repositories, "api_request",
                          side_effect=lambda _t, method, path, payload=None: responses[(method, path)]):
            with self.assertRaisesRegex(RuntimeError, "branch cleanup setting failed"):
                fork_repositories.ensure_fork("token", repo)


if __name__ == "__main__":
    unittest.main()
