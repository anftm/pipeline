import unittest
from unittest.mock import call, patch

from scripts import fork_repositories


class ForkRepositoriesTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
