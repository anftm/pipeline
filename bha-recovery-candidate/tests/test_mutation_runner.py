"""Check that the scoped mutation runner rejects weak or incomplete evidence."""
import sys
import unittest
from unittest.mock import patch

from tests.run_mutation import evaluate, main


class MutationRunnerTests(unittest.TestCase):
    def test_scores_all_mutants_and_rejects_incomplete_runs(self):
        self.assertEqual(evaluate({"killed": 8, "total": 10, "no_tests": 1, "survived": 1,
                                   "timeout": 0, "suspicious": 0, "skipped": 0,
                                   "check_was_interrupted_by_user": 0, "segfault": 0}, 80), 80)
        stats = {"killed": 8, "total": 10, "no_tests": 1, "survived": 1,
                 "timeout": 0, "suspicious": 0, "skipped": 0, "check_was_interrupted_by_user": 0, "segfault": 0}
        with self.assertRaises(SystemExit):
            evaluate(stats, 81)
        for key in ("timeout", "suspicious", "skipped", "check_was_interrupted_by_user", "segfault"):
            with self.subTest(key=key), self.assertRaises(SystemExit):
                evaluate({**stats, key: 1}, 0)

    def test_unexpected_tool_version_stops_before_running(self):
        with patch.object(sys, "argv", ["run_mutation.py"]), patch("tests.run_mutation.version", return_value="bad"), \
                patch("tests.run_mutation.subprocess.run") as run:
            with self.assertRaisesRegex(SystemExit, "unexpected mutation tool versions"):
                main()
        run.assert_not_called()
