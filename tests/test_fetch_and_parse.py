from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script():
    path = ROOT / "scripts" / "fetch_and_parse.py"
    spec = importlib.util.spec_from_file_location("fetch_and_parse", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FetchAndParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_script()

    def test_write_action_output_appends_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                self.module.write_action_output("data_changed", "false")
                self.module.write_action_output("other", "value")

            self.assertEqual(output.read_text(encoding="utf-8"), "data_changed=false\nother=value\n")

    def test_write_action_output_is_optional_outside_actions(self):
        with patch.dict(os.environ, {}, clear=True):
            self.module.write_action_output("data_changed", "true")

    def test_main_reports_unchanged_without_generating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            state_file = temporary_path / "commits.json"
            action_output = temporary_path / "github-output"
            state_file.write_text(json.dumps({repo: "same-sha" for repo in self.module.REPOS}), encoding="utf-8")

            with patch.object(self.module, "STATE_FILE", state_file), \
                 patch.object(self.module, "OUTPUT_FILE", temporary_path / "missing.json"), \
                 patch.object(self.module, "get_repo_sha", return_value="same-sha"), \
                 patch.dict(os.environ, {"GITHUB_OUTPUT": str(action_output), "FORCE_SYNC": "false"}, clear=True):
                result = self.module.main()

            self.assertEqual(result, 0)
            self.assertEqual(action_output.read_text(encoding="utf-8"), "data_changed=false\n")
            self.assertFalse((temporary_path / "missing.json").exists())

    def test_main_stops_when_a_source_revision_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            state_file = temporary_path / "commits.json"
            action_output = temporary_path / "github-output"
            original_state = {repo: "old-sha" for repo in self.module.REPOS}
            state_file.write_text(json.dumps(original_state), encoding="utf-8")

            def get_sha(repo, _token):
                return "" if repo == self.module.REPOS[0] else "new-sha"

            with patch.object(self.module, "STATE_FILE", state_file), \
                 patch.object(self.module, "get_repo_sha", side_effect=get_sha), \
                 patch.dict(os.environ, {"GITHUB_OUTPUT": str(action_output)}, clear=True):
                result = self.module.main()

            self.assertEqual(result, 1)
            self.assertEqual(action_output.read_text(encoding="utf-8"), "data_changed=false\n")
            self.assertEqual(json.loads(state_file.read_text(encoding="utf-8")), original_state)

    def test_main_stops_when_a_source_catalog_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            state_file = temporary_path / "commits.json"
            output_file = temporary_path / "search.json"
            state_file.write_text(json.dumps({repo: "old-sha" for repo in self.module.REPOS}), encoding="utf-8")

            def get_text(url, _token):
                return "" if self.module.REPOS[0] in url else "A docx\n"

            with patch.object(self.module, "STATE_FILE", state_file), \
                 patch.object(self.module, "OUTPUT_FILE", output_file), \
                 patch.object(self.module, "get_repo_sha", return_value="new-sha"), \
                 patch.object(self.module, "http_get_text", side_effect=get_text), \
                 patch.object(self.module, "batch_get_sizes", return_value={}), \
                 patch.dict(os.environ, {"FORCE_SYNC": "true"}, clear=True):
                result = self.module.main()

            self.assertEqual(result, 1)
            self.assertFalse(output_file.exists())


if __name__ == "__main__":
    unittest.main()
