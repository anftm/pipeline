import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import reader_gc_maintenance as maintenance


class MaintenanceTests(unittest.TestCase):
    def test_failure_after_disabling_producers_restores_prior_permissions(self):
        permissions = {"enabled": True, "allowed_actions": "all"}
        events = []
        def api(repo, path, *, method="GET", body=None):
            events.append((path, method, body))
            if path == "actions/permissions":
                return permissions
            if path.endswith("/enable"):
                return {}
            if path == "actions/workflows/1":
                return {"state": "active"}
            raise RuntimeError("failed mutation")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, "producer_repositories", return_value=["central/repo"]), \
                patch.object(maintenance, "load_config", return_value={"publisher_repository": "central/repo"}), \
                patch.object(maintenance, "pages", return_value=[{
                    "id": 1, "state": "active", "path": ".github/workflows/pdf-account-worker.yml"}]), \
                patch.object(maintenance, "api", side_effect=api):
            path = Path(directory) / "journal.json"
            with self.assertRaises(RuntimeError):
                maintenance.run(path)
            state = json.loads(path.read_text())
            self.assertTrue(state["restored"])
            self.assertIn(("actions/workflows/1/enable", "PUT", None), events)

    def test_ambiguous_rerun_is_never_blindly_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            maintenance.journal(path, {"repositories": {"owner/repo": {"workflows": [],
                "cancelled": [{"id": 42, "restoration_requested": True, "restoration_attempt": 1}]}}})
            with patch.object(maintenance, "api", return_value={
                    "status": "completed", "conclusion": "cancelled", "run_attempt": 1}) as api:
                with self.assertRaises(RuntimeError):
                    maintenance.restore(path)
                api.assert_called_once()
            self.assertFalse(json.loads(path.read_text())["restored"])
