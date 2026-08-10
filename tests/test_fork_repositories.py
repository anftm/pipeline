import unittest
from unittest.mock import call, patch

from scripts import fork_repositories


class ForkRepositoriesTests(unittest.TestCase):
    def test_main_skips_generated_parsed_branch(self):
        with patch.dict(fork_repositories.os.environ, {"GH_PAT": "token"}), \
                patch.object(fork_repositories, "REPO_START", 0), \
                patch.object(fork_repositories, "REPO_END", 0), \
                patch.object(fork_repositories, "ensure_fork"), \
                patch.object(
                    fork_repositories, "list_branches", return_value=["main", "parsed", "ocr_patch"],
                ), \
                patch.object(fork_repositories, "sync_branch") as sync_branch:
            result = fork_repositories.main()

        self.assertEqual(result, 0)
        self.assertEqual(sync_branch.call_args_list, [
            call("token", "banned-historical-archives0", "main"),
            call("token", "banned-historical-archives0", "ocr_patch"),
        ])


if __name__ == "__main__":
    unittest.main()
