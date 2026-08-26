from __future__ import annotations

import base64
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SyncAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.space = load_script("sync_to_space")
        cls.pages = load_script("sync_to_pages")
        cls.directories = load_script("update_dirs")

    def test_git_auth_uses_host_scoped_basic_credentials(self):
        cases = (
            (self.space, "huggingface.co", "VoiceOfML", "hf-secret"),
            (self.pages, "github.com", "x-access-token", "gh-secret"),
        )
        for module, host, username, token in cases:
            with self.subTest(host=host):
                env = module.git_auth_env(host, username, token)
                self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
                self.assertEqual(env["GIT_CONFIG_KEY_0"], f"http.https://{host}/.extraheader")
                scheme, encoded = env["GIT_CONFIG_VALUE_0"].split(" ", 2)[1:]
                self.assertEqual(scheme, "Basic")
                self.assertEqual(base64.b64decode(encoded).decode(), f"{username}:{token}")

    def test_directory_git_auth_uses_host_scoped_basic_credentials(self):
        with patch.object(self.directories, "HF_TOKEN", "hf-secret"):
            env = self.directories.git_auth_env()
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://huggingface.co/.extraheader")
        scheme, encoded = env["GIT_CONFIG_VALUE_0"].split(" ", 2)[1:]
        self.assertEqual(scheme, "Basic")
        self.assertEqual(base64.b64decode(encoded).decode(), "VoiceOfML:hf-secret")

    def test_push_retry_reports_success_after_retry(self):
        for module in (self.space, self.pages):
            with self.subTest(module=module.__name__), \
                 patch.object(module, "run", side_effect=[(1, "", "denied"), (0, "", ""), (0, "", "")]) as run, \
                 patch.object(module.time, "sleep"):
                self.assertTrue(module.push_with_retry("repo", {"AUTH": "value"}))
                self.assertEqual([call.args[0] for call in run.call_args_list], [
                    ["git", "push"], ["git", "pull", "--rebase"], ["git", "push"],
                ])

    def test_push_retry_returns_false_after_exhaustion(self):
        for module in (self.space, self.pages):
            with self.subTest(module=module.__name__), \
                 patch.object(module, "run", side_effect=[(1, "", "denied"), (0, "", ""), (1, "", "denied")]), \
                 patch.object(module.time, "sleep"):
                self.assertFalse(module.push_with_retry("repo", {"AUTH": "value"}))


if __name__ == "__main__":
    unittest.main()
