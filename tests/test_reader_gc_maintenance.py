import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import reader_gc_maintenance as maintenance


class MaintenanceTests(unittest.TestCase):
    def test_failure_after_disabling_producers_restores_prior_permissions(self):
        permissions = {"enabled": True, "allowed_actions": "all"}
        workflow = {"state": "active"}
        events = []
        mutations = {"disable": 0}

        def api(repo, path, *, method="GET", body=None):
            events.append((path, method, body))
            if path == "actions/permissions":
                return dict(permissions)
            if path == "actions/workflows/1":
                return dict(workflow)
            if path == "actions/runs/9":
                return {"id": 9, "status": "completed", "conclusion": "cancelled", "run_attempt": 1}
            if "/attempts/" in path:
                return {"id": 9, "status": "queued", "run_attempt": 1}
            if path.endswith("/rerun"):
                return {}
            if path.endswith("/disable"):
                workflow["state"] = "disabled_manually"
                return {}
            if path.endswith("/enable"):
                workflow["state"] = "active"
                return {}
            raise AssertionError(path)

        # Producer disabling succeeds, then the queued-run cancellation fails.
        # The journaled workflow state must still be restored.
        def cancel_raises(repo, path, *, method="GET", body=None):
            if path.endswith("/cancel"):
                mutations["disable"] += 1
                raise RuntimeError("failed mutation")
            return api(repo, path, method=method, body=body)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, "producer_repositories", return_value=["central/repo"]), \
                patch.object(maintenance, "load_config", return_value={"publisher_repository": "central/repo"}), \
                patch.object(maintenance, "pages", side_effect=self.pages), \
                patch.object(maintenance, "api", side_effect=cancel_raises):
            path = Path(directory) / "journal.json"
            with self.assertRaises(RuntimeError):
                maintenance.run(path)
            state = json.loads(path.read_text())
            self.assertTrue(state["restored"])
            self.assertIn(("actions/workflows/1/enable", "PUT", None), events)
            self.assertFalse(state["restored"] is None)

    @staticmethod
    def pages(repo, path, field):
        if path == "actions/workflows":
            return [{"id": 1, "state": "active", "path": ".github/workflows/pdf-account-worker.yml"}]
        if path == "actions/runs?status=queued":
            return [{"id": 9, "status": "queued", "path": ".github/workflows/pdf-account-worker.yml"}]
        return []

    def test_restoration_skips_a_workflow_that_is_already_active(self):
        events = []

        def api(repo, path, *, method="GET", body=None):
            events.append((path, method))
            if path == "actions/permissions":
                return {"enabled": True}
            if path == "actions/workflows/1":
                return {"state": "active"}
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            maintenance.journal(path, {"repositories": {"owner/repo": {
                "permission": {"enabled": True},
                "workflows": [{"id": 1, "state": "active"}], "cancelled": []}}})
            with patch.object(maintenance, "api", side_effect=api), \
                    patch.object(maintenance, "load_config",
                                 return_value={"publisher_repository": "central/repo"}):
                maintenance.restore(path)
            self.assertNotIn(("actions/workflows/1/enable", "PUT"), events)
            self.assertTrue(json.loads(path.read_text())["restored"])

    def test_preflight_fails_before_isolation_without_exposing_tokens(self):
        def api(repo, path, *, method="GET", body=None):
            if repo == "owner/denied":
                raise maintenance.MaintenanceAPIError(repo, path, method, 403)
            return {"enabled": True} if path == "actions/permissions" else {}

        with patch.object(maintenance, "producer_repositories",
                          return_value=["owner/ok", "owner/denied"]), \
                patch.object(maintenance, "pages",
                             return_value=[{"id": 1, "state": "active",
                                            "path": ".github/workflows/pdf-account-worker.yml"},
                                           {"id": 2, "state": "active",
                                            "path": ".github/workflows/reader-assets.yml"}]), \
                patch.object(maintenance, "api", side_effect=api):
            with self.assertRaisesRegex(RuntimeError, "preflight failed for 1") as caught:
                maintenance.preflight()
        self.assertIn("403", str(caught.exception))
        # No credential material may appear in a failure intended for logs.
        self.assertNotIn("ghp_", str(caught.exception))

    def test_disabling_repository_actions_omits_allowed_actions(self):
        state = {"enabled": True, "allowed_actions": "all"}
        writes = []

        def api(repo, path, *, method="GET", body=None):
            if path != "actions/permissions":
                raise AssertionError(path)
            if method == "GET":
                return dict(state)
            writes.append(body)
            state.clear()
            state.update(body)
            return {}

        with patch.object(maintenance, "api", side_effect=api):
            maintenance.permissions("owner/repo", {
                "enabled": False, "allowed_actions": "all"})
        self.assertEqual(writes, [{"enabled": False}])
        self.assertFalse(state["enabled"])

    def test_repository_permission_denial_is_best_effort(self):
        record = {"permission": {"enabled": True, "allowed_actions": "all"}}
        denied = maintenance.MaintenanceAPIError(
            "owner/repo", "actions/permissions", "PUT", 403)
        with patch.object(maintenance, "permissions", side_effect=denied):
            self.assertFalse(maintenance.disable_actions("owner/repo", record))
        self.assertEqual(record["permission_denied"], 403)
        self.assertNotIn("permission_changed", record)

    def test_repository_permission_server_error_is_not_ignored(self):
        record = {"permission": {"enabled": True}}
        failed = maintenance.MaintenanceAPIError(
            "owner/repo", "actions/permissions", "PUT", 500)
        with patch.object(maintenance, "permissions", side_effect=failed):
            with self.assertRaises(maintenance.MaintenanceAPIError):
                maintenance.disable_actions("owner/repo", record)

    def test_preflight_skips_central_repository_actions_permission(self):
        calls = []

        def api(repo, path, *, method="GET", body=None):
            calls.append((repo, path))
            raise AssertionError("central Actions policy must not be read")

        with patch.object(maintenance, "producer_repositories",
                          return_value=["central/repo"]), \
                patch.object(maintenance, "load_config",
                             return_value={"publisher_repository": "central/repo"}), \
                patch.object(maintenance, "pages",
                             return_value=[{"id": 1, "state": "active",
                                            "path": ".github/workflows/pdf-account-worker.yml"}]), \
                patch.object(maintenance, "api", side_effect=api):
            result = maintenance.preflight()
        self.assertEqual(list(result), ["central/repo"])
        self.assertEqual(result["central/repo"]["permission"], {})
        self.assertEqual(calls, [])

    def test_cancelled_run_without_jobs_is_not_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            maintenance.journal(path, {"repositories": {"owner/repo": {"workflows": [],
                "cancelled": [{"id": 42}]}}})
            with patch.object(maintenance, "api", return_value={
                    "status": "completed", "conclusion": "cancelled", "run_attempt": 1}) as api, \
                    patch.object(maintenance, "pages", return_value=[]):
                maintenance.restore(path)
            self.assertTrue(json.loads(path.read_text())["restored"])
            self.assertFalse(any(call.args[1].endswith("/rerun") for call in api.call_args_list))

    def test_ambiguous_rerun_is_never_blindly_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            maintenance.journal(path, {"repositories": {"owner/repo": {"workflows": [],
                "cancelled": [{"id": 42, "restoration_requested": True, "restoration_attempt": 1}]}}})
            with patch.object(maintenance, "api", return_value={
                    "status": "completed", "conclusion": "cancelled", "run_attempt": 1}) as api:
                maintenance.restore(path)
            state = json.loads(path.read_text())
            self.assertTrue(state["restored"])
            self.assertTrue(state["repositories"]["owner/repo"]["cancelled"][0]["rerun_skipped"])
            self.assertFalse(any(call.args[1].endswith("/rerun") for call in api.call_args_list))

    def test_not_yet_queued_cancellation_is_deferred_to_the_fence(self):
        queued = {"id": 42, "status": "queued", "path": ".github/workflows/pdf-account-worker.yml"}
        calls = []

        def api(repo, path, *, method="GET", body=None):
            calls.append((repo, path, method))
            if path == "actions/workflows":
                return [{"id": 1, "state": "active",
                         "path": ".github/workflows/pdf-account-worker.yml"}]
            if path.startswith("actions/workflows/1"):
                return {"state": "disabled_manually"} if method == "GET" else {}
            if path.startswith("actions/runs?status="):
                return [queued] if path.endswith("queued") else []
            if path.endswith("/cancel"):
                raise maintenance.MaintenanceAPIError(repo, path, "POST", 409)
            if "/attempts/" in path:
                return {"id": 42, "status": "queued", "run_attempt": 1}
            if path == "actions/runs":
                return []
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            def pages(repo, request, field):
                if request == "actions/workflows":
                    return [{"id": 1, "state": "active",
                             "path": ".github/workflows/pdf-account-worker.yml"}]
                if "status=queued" in request:
                    return [queued]
                return []
            with patch.object(maintenance, "producer_repositories", return_value=["central/repo"]), \
                    patch.object(maintenance, "load_config",
                                 return_value={"publisher_repository": "central/repo"}), \
                    patch.object(maintenance, "ACTIVE", ("queued",)), \
                    patch.object(maintenance, "pages", side_effect=pages), \
                    patch.object(maintenance, "restore") as restore, \
                    patch.object(maintenance, "api", side_effect=api):
                with self.assertRaises(Exception):
                    maintenance.run(path, apply=False)
            state = json.loads(path.read_text())
            self.assertTrue(state["repositories"]["central/repo"]["cancelled"][0].get("cancel_deferred"))
