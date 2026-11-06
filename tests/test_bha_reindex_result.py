"""Synthetic tests for the minimal workflow completion check."""

import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

from scripts import bha_reindex_result as result
from scripts import check_bha_revisions as revisions


class ReindexResultTests(unittest.TestCase):
    def setUp(self):
        self.commits = {str(i): format(i + 1, "040x") for i in range(32)}
        self.revisions = {str(i): format(i + 101, "040x") for i in range(32)}
        self.acceptance = {"ok": True, "status": "accepted", "mode": "incremental", "job_id": "a" * 32}
        self.observed = {"index_status": "ready", "switch_pending": False, "build_fingerprint": "synthetic",
                         "archive_revisions": copy.deepcopy(self.revisions),
                         "bucket_snapshot_configured": True, "bucket_snapshot_marker_present": False,
                         "bucket_snapshot": {"state": "published", "snapshot": "bha-1-abc"},
                         "snapshot_manifest": {"snapshot": "bha-1-abc", "generation": "bha-1-abc",
                                               "archive_revisions": copy.deepcopy(self.revisions),
                                               "build_fingerprint": "synthetic"},
                         "job": {"job_id": "a" * 32, "state": "succeeded", "mode": "incremental",
                                 "target_archive_commits": copy.deepcopy(self.commits),
                                 "target_archive_revisions": copy.deepcopy(self.revisions)}}

    def evaluate(self, observation=None):
        return result.evaluate(self.acceptance, self.commits, self.revisions,
                               self.observed if observation is None else observation)

    def test_exact_job_and_targets_are_complete(self):
        self.assertEqual(self.evaluate(), "complete")
        self.assertEqual(result.evaluate_accepted_pair(self.acceptance, self.commits, self.revisions,
                                                       self.observed, copy.deepcopy(self.observed)), "complete")

    def test_two_complete_observations_must_keep_same_snapshot_and_build(self):
        after = copy.deepcopy(self.observed)
        after["snapshot_manifest"]["snapshot"] = "bha-2-def"
        after["snapshot_manifest"]["generation"] = "bha-2-def"
        after["bucket_snapshot"]["snapshot"] = "bha-2-def"
        with self.assertRaises(result.InvalidResult):
            result.evaluate_accepted_pair(self.acceptance, self.commits, self.revisions, self.observed, after)
        after["bucket_snapshot"]["state"] = "publishing"
        self.assertEqual(result.evaluate_accepted_pair(self.acceptance, self.commits, self.revisions,
                                                       self.observed, after), "pending")

    def test_running_is_pending_even_if_old_revisions_match(self):
        self.observed["job"]["state"] = "running"
        self.assertEqual(self.evaluate(), "pending")

    def test_failed_job_never_reports_success_even_if_revisions_match(self):
        self.observed["job"]["state"] = "failed"
        with self.assertRaises(result.JobFailed):
            self.evaluate()

    def test_matching_index_waits_for_snapshot_publication(self):
        for status, marker in (("publishing", True), ("pending_publish", False)):
            with self.subTest(status=status):
                observation = copy.deepcopy(self.observed)
                observation["bucket_snapshot"]["state"] = status
                observation["bucket_snapshot_marker_present"] = marker
                self.assertEqual(self.evaluate(observation), "pending")
        observation = copy.deepcopy(self.observed)
        observation["snapshot_manifest"]["archive_revisions"]["0"] = "f" * 40
        self.assertEqual(self.evaluate(observation), "pending")

    def test_failed_snapshot_and_unconfigured_bucket_are_not_completion(self):
        observation = copy.deepcopy(self.observed)
        observation["bucket_snapshot"] = {"state": "publish_failed"}
        observation["snapshot_manifest"] = None
        with self.assertRaises(result.JobFailed):
            self.evaluate(observation)
        observation = copy.deepcopy(self.observed)
        observation["bucket_snapshot_configured"] = False
        with self.assertRaises(result.InvalidResult):
            self.evaluate(observation)

    def test_wrong_job_or_targets_fail_closed(self):
        for change in (lambda x: x["job"].update(job_id="b" * 32),
                       lambda x: x["job"]["target_archive_commits"].update({"0": "f" * 40}),
                       lambda x: x["job"]["target_archive_revisions"].pop("0"),
                       lambda x: x.update(switch_pending=True),
                       lambda x: x["archive_revisions"].update({"0": "f" * 40}),
                       lambda x: x.update(build_fingerprint=None)):
            observation = copy.deepcopy(self.observed)
            change(observation)
            with self.assertRaises(result.InvalidResult):
                self.evaluate(observation)

    def test_missing_or_malformed_acceptance_cannot_adopt_old_job(self):
        for value in ({}, {**self.acceptance, "job_id": "invalid"},
                      {**self.acceptance, "mode": "full"}, {**self.acceptance, "ok": 1}):
            with self.subTest(value=value), self.assertRaises(result.InvalidResult):
                result.evaluate(value, self.commits, self.revisions, self.observed)

    def test_cli_exit_codes_distinguish_pending_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / str(i) for i in range(4)]
            for path, value in zip(paths, (self.acceptance, self.commits, self.revisions, self.observed)):
                path.write_text(json.dumps(value), encoding="utf-8")
            command = [sys.executable, "-B", result.__file__, *(str(path) for path in paths)]
            self.assertEqual(subprocess.run(command, capture_output=True, check=False).returncode, 0)
            self.observed["job"]["state"] = "running"
            paths[3].write_text(json.dumps(self.observed), encoding="utf-8")
            self.assertEqual(subprocess.run(command, capture_output=True, check=False).returncode, 1)
            self.observed["job"]["state"] = "failed"
            paths[3].write_text(json.dumps(self.observed), encoding="utf-8")
            self.assertEqual(subprocess.run(command, capture_output=True, check=False).returncode, 2)

    def test_workflow_requires_job_check_and_stops_on_uncertain_post(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.load((root / ".github/workflows/bha-update-index.yml").read_text(), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["update"]["steps"]
        trigger = next(step for step in steps if step.get("name") == "Trigger incremental BHA index update")["run"]
        wait = next(step for step in steps if step.get("name") == "Wait for accepted revisions")["run"]
        self.assertIn("accepted_job", trigger)
        self.assertIn("409|429) sleep 10 ;;", trigger)
        self.assertIn("*) exit 1 ;;", trigger)
        self.assertIn("bha_reindex_result.py", wait)
        self.assertIn("/tmp/bha-reindex-response.json", wait)
        self.assertNotIn('actual.get("index_status") == "ready" and revisions == expected', wait)

    def test_workflow_does_not_record_target_before_snapshot_confirmation(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.load((root / ".github/workflows/bha-update-index.yml").read_text(), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["update"]["steps"]
        trigger_index = next(i for i, step in enumerate(steps) if step.get("name") == "Trigger incremental BHA index update")
        wait_index = next(i for i, step in enumerate(steps) if step.get("name") == "Wait for accepted revisions")
        record_index = next(i for i, step in enumerate(steps) if step.get("name") == "Record accepted revisions")
        self.assertLess(trigger_index, wait_index)
        self.assertLess(wait_index, record_index)
        self.assertEqual(steps[trigger_index]["if"], "steps.revisions.outputs.changed == 'true'")
        self.assertEqual(steps[wait_index]["if"], "steps.revisions.outputs.changed == 'true'")
        self.assertEqual(steps[record_index]["if"],
                         "steps.revisions.outputs.baseline == 'true' || steps.revisions.outputs.changed == 'true'")
        wait = steps[wait_index]["run"]
        self.assertIn("/tmp/bha-reindex-status-second.json", wait)
        self.assertIn("/tmp/bha-reindex-status-first.json", wait)
        self.assertIn("if test \"$result\" = \"2\"; then exit 1; fi", wait)

    def test_next_run_still_detects_changes_if_accepted_job_was_not_recorded(self):
        # Characterization, not permission to run the workflow: if the first
        # accepted job times out during confirmation, its 202 response exists
        # only under /tmp. The next run sees the same prior committed state and
        # can POST a second job. This test performs no network or index work.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            old = {str(i): "a" * 40 for i in range(32)}
            state.write_text(json.dumps(old), encoding="utf-8")
            output = io.StringIO()
            with patch.object(revisions, "STATE_PATH", state), \
                    patch.object(revisions, "CANDIDATE_PATH", root / "candidate.json"), \
                    patch.object(revisions, "COMMIT_CANDIDATE_PATH", root / "commits.json"), \
                    patch.object(revisions, "GITHUB_OUTPUT", None), \
                    patch.object(revisions, "archive_identity", side_effect=lambda _token, i: (str(i), "c" * 40, "b" * 40)), \
                    patch.object(revisions, "wait_for_git_revisions"), redirect_stdout(output):
                revisions.main()
                revisions.main()
            self.assertEqual(output.getvalue().splitlines().count("changed=true"), 2)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), old)
            self.assertEqual(json.loads((root / "candidate.json").read_text(encoding="utf-8")),
                             {str(i): "b" * 40 for i in range(32)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
