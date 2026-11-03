from __future__ import annotations

import base64
import copy
import gzip
import importlib.util
import json
import tempfile
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

    def test_retired_ocr_flags_are_cleared_without_changing_sources(self):
        records = [{"Repo": "VoiceOfML/A", "File": "book", "Extension": "pdf", "Folder": ["docs"], "HasTxt": True},
                   {"Repo": "VoiceOfML/B", "File": "original", "Extension": "txt", "Folder": [], "HasTxt": True}]
        browser = {"VoiceOfML/A": {"docs": {"f": [{"n": "book", "e": "pdf", "t": True},
                                                    {"n": "unmatched", "e": "pdf", "t": True}]}}}
        original_records, original_browser = copy.deepcopy(records), copy.deepcopy(browser)
        for module in (self.space, self.pages):
            with self.subTest(module=module.__name__):
                actual_records, actual_browser = copy.deepcopy(records), copy.deepcopy(browser)
                module.clear_legacy_ocr_flags(actual_records, actual_browser)
                self.assertEqual([record["HasTxt"] for record in actual_records], [False, False])
                self.assertEqual([item["t"] for item in actual_browser["VoiceOfML/A"]["docs"]["f"]], [False, False])
                self.assertEqual(actual_records[0]["Folder"], ["docs"])
                self.assertEqual(actual_records[1]["File"], "original")
                packed = module.encode_search_payload(actual_records)
                self.assertEqual([row[5] for row in packed["rc"]], [0, 0])
                self.assertEqual(len(packed["rc"][0]), 6)
        self.assertEqual(records, original_records)
        self.assertEqual(browser, original_browser)

    def test_full_sync_does_not_scan_legacy_txt_or_republish_flags(self):
        records = [{"Repo": "VoiceOfML/A", "File": "book", "Extension": "pdf", "Folder": ["docs"], "Size": 10, "HasTxt": True},
                   {"Repo": "VoiceOfML/A", "File": "original", "Extension": "txt", "Folder": [], "Size": 11, "HasTxt": True}]
        browser = {"VoiceOfML/A": {"docs": {"d": [], "f": [{"n": "book", "e": "pdf", "t": True}]},
                                    "": {"d": [], "f": [{"n": "original", "e": "txt", "t": True}]}}}
        for module in (self.space, self.pages):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, tree, folder = root / "search_data.json", root / "folder_tree.json", root / "folder_browser.json"
                source.write_text(json.dumps(records), encoding="utf-8")
                tree.write_text("{}", encoding="utf-8")
                tree.with_suffix(".json.gz").write_bytes(gzip.compress(b"{}"))
                folder.write_text(json.dumps(browser), encoding="utf-8")
                clone = root / "clone"
                clone.mkdir()
                legacy_file = clone / "txt" / "docs" / "book.txt"
                legacy_file.parent.mkdir(parents=True)
                legacy_file.write_text("do not inspect", encoding="utf-8")
                calls = []

                def run(cmd, **_kwargs):
                    calls.append(cmd)
                    return 0, "", ""

                def write_index(path, *_args):
                    path.write_bytes(b"index")

                with patch.dict("os.environ", {"HF_TOKEN": "test", "PAGES_TOKEN": "test", "PAGES_REPO": "owner/search"}), \
                     patch.object(module, "SOURCE_JSON", source), patch.object(module, "FOLDER_TREE_JSON", tree), \
                     patch.object(module, "FOLDER_BROWSER_JSON", folder), patch.object(module.tempfile, "mkdtemp", return_value=str(clone)), \
                     patch.object(module.shutil, "rmtree"), patch.object(module, "run", side_effect=run), \
                     patch.object(Path, "rglob", side_effect=AssertionError("legacy TXT scan is forbidden")):
                    if module is self.space:
                        with patch.object(module, "clone_space_repo", return_value=(0, "", "")) as clone_space, \
                             patch.object(module, "write_word_index", side_effect=write_index), \
                             patch.object(module, "write_ngram_index", side_effect=write_index):
                            self.assertEqual(module.main(), 0)
                            clone_space.assert_called_once()
                    else:
                        self.assertEqual(module.main(), 0)
                        clones = [cmd for cmd in calls if cmd[:2] == ["git", "clone"]]
                        self.assertEqual(len(clones), 1)
                        self.assertIn("https://github.com/owner/search.git", clones[0])
                result = json.loads(gzip.decompress((clone / "data/search_data.json.gz").read_bytes()))
                self.assertEqual([row[5] for row in result["rc"]], [0, 0])
                self.assertEqual([row[2] for row in result["rc"]], ["pdf", "txt"])
                result_browser = json.loads(gzip.decompress((clone / "data/folder_browser.json.gz").read_bytes()))
                self.assertFalse(result_browser["VoiceOfML/A"]["docs"]["f"][0]["t"])
                self.assertFalse(result_browser["VoiceOfML/A"][""]["f"][0]["t"])
                self.assertEqual(legacy_file.read_text(encoding="utf-8"), "do not inspect")


if __name__ == "__main__":
    unittest.main()
