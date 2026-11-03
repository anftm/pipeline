from __future__ import annotations

import base64
from contextlib import nullcontext
import copy
import gzip
import importlib.util
import json
import subprocess
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

    def test_rebase_failure_never_retries_push(self):
        for module in (self.space, self.pages):
            with self.subTest(module=module.__name__), \
                    patch.object(module, 'run', side_effect=[(1, '', 'push rejected'),
                                                            (1, '', 'rebase conflict')]) as run, \
                    patch.object(module.time, 'sleep') as sleep:
                self.assertFalse(module.push_with_retry('repo', {'AUTH': 'value'}))
                self.assertEqual([call.args[0] for call in run.call_args_list],
                                 [['git', 'push'], ['git', 'pull', '--rebase']])
                sleep.assert_not_called()

    def test_staging_status_and_commit_errors_fail_without_push(self):
        records = [{'Repo': 'VoiceOfML/A', 'File': 'book', 'Extension': 'pdf', 'Folder': [], 'Size': 1}]
        browser = {'VoiceOfML/A': {'': {'d': [], 'f': []}}}
        for module in (self.space, self.pages):
            for failure in ('missing_output', 'add', 'status', 'commit'):
                with self.subTest(module=module.__name__, failure=failure), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source, tree, folder = root / 'search_data.json', root / 'folder_tree.json', root / 'folder_browser.json'
                    source.write_text(json.dumps(records))
                    tree.write_text('{}')
                    tree.with_suffix('.json.gz').write_bytes(gzip.compress(b'{}'))
                    folder.write_text(json.dumps(browser))
                    clone = root / 'clone'
                    clone.mkdir()
                    calls = []

                    def run(cmd, **_kwargs):
                        calls.append(cmd)
                        if cmd[:2] == ['git', failure]:
                            return 1, '', 'simulated failure'
                        if cmd[:2] == ['git', 'status']:
                            return 0, 'M  data/search_data.json.gz', ''
                        return 0, '', ''

                    def write_index(path, *_args):
                        path.write_bytes(b'fixture')

                    with patch.dict('os.environ', {'HF_TOKEN': 'test', 'PAGES_TOKEN': 'test', 'PAGES_REPO': 'owner/search'}), \
                            patch.object(module, 'SOURCE_JSON', source), patch.object(module, 'FOLDER_TREE_JSON', tree), \
                            patch.object(module, 'FOLDER_BROWSER_JSON', folder), \
                            patch.object(module.tempfile, 'mkdtemp', return_value=str(clone)), \
                            patch.object(module.shutil, 'rmtree'), patch.object(module, 'run', side_effect=run), \
                            patch.object(module, 'push_with_retry') as push:
                        if failure == 'missing_output':
                            write_sidebar = patch.object(module, 'write_sidebar_payloads', return_value=[])
                        else:
                            write_sidebar = nullcontext()
                        if module is self.space:
                            with patch.object(module, 'clone_space_repo', return_value=(0, '', '')), \
                                    patch.object(module, 'write_word_index', side_effect=write_index), \
                                    patch.object(module, 'write_ngram_index', side_effect=write_index), write_sidebar:
                                self.assertEqual(module.main(), 1)
                        else:
                            with write_sidebar:
                                self.assertEqual(module.main(), 1)
                    push.assert_not_called()
                    self.assertNotIn(['git', 'push'], calls)

    def test_scoped_git_commit_keeps_unrelated_files_and_tracks_generated_deletions(self):
        def git(cwd, *args):
            return subprocess.run(['git', *map(str, args)], cwd=cwd, capture_output=True,
                                  text=True, timeout=30, check=True).stdout.strip()

        for module in (self.space, self.pages):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                git(root, 'init', '--initial-branch=main')
                git(root, 'config', 'user.name', 'test')
                git(root, 'config', 'user.email', 'test@example.invalid')
                for name in module.SEARCH_OUTPUT_PATHS:
                    path = root / name
                    if name in ('data/initial', 'data/sidebar'):
                        path.mkdir(parents=True)
                        (path / 'old.json').write_text('old')
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b'old')
                unrelated = root / 'data/reader_assets.json.gz'
                unrelated.write_bytes(b'protect Reader data')
                git(root, 'add', '.')
                git(root, 'commit', '-m', 'original')
                (root / 'data/search_data.json.gz').write_bytes(b'new')
                (root / 'data/initial/old.json').unlink()
                (root / 'data/initial/new.json').write_text('new')
                unrelated.write_bytes(b'foreign staged change')
                git(root, 'add', 'data/reader_assets.json.gz')
                paths = module.SEARCH_OUTPUT_PATHS
                git(root, 'add', '-A', '--', *paths)
                git(root, 'commit', '--only', '-m', 'search update', '--', *paths)
                changes = git(root, 'show', '--format=', '--name-status', 'HEAD')
                self.assertIn('D\tdata/initial/old.json', changes)
                self.assertIn('A\tdata/initial/new.json', changes)
                self.assertIn('M\tdata/search_data.json.gz', changes)
                self.assertNotIn('reader_assets', changes)
                self.assertEqual(git(root, 'diff', '--cached', '--name-only'), 'data/reader_assets.json.gz')
                self.assertEqual(git(root, 'show', 'HEAD:data/reader_assets.json.gz'), 'protect Reader data')

    def test_full_sync_pushes_only_search_paths_in_real_local_git(self):
        def git(cwd, *args):
            return subprocess.run(['git', *map(str, args)], cwd=cwd, capture_output=True,
                                  text=True, timeout=30, check=True).stdout.strip()

        records = [{'Repo': 'VoiceOfML/A', 'File': 'book', 'Extension': 'pdf', 'Folder': [], 'Size': 1}]
        browser = {'VoiceOfML/A': {'': {'d': [], 'f': []}}}
        for module in (self.space, self.pages):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                remote, seed, clone = root / 'remote.git', root / 'seed', root / 'clone'
                git(root, 'init', '--bare', '--initial-branch=main', remote)
                git(root, 'clone', remote, seed)
                git(seed, 'config', 'user.name', 'fixture')
                git(seed, 'config', 'user.email', 'fixture@example.invalid')
                for name in module.SEARCH_OUTPUT_PATHS:
                    path = seed / name
                    if name in ('data/initial', 'data/sidebar'):
                        path.mkdir(parents=True)
                        (path / 'stale.json').write_text('stale')
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b'previous')
                (seed / 'data/reader_assets.json.gz').write_bytes(b'preserve Reader projection')
                (seed / 'data/reader_v3_receipt.json').write_text('preserve receipt')
                (seed / 'app.py').write_text('preserve external application')
                git(seed, 'add', '.')
                git(seed, 'commit', '-m', 'seed')
                git(seed, 'push', 'origin', 'main')
                # Build a genuine local worktree without exercising a clone:
                # this environment's upload-pack adds a 28-second shutdown
                # delay per nonempty clone. Production clone/auth remains
                # covered by separate tests and the successful prior Actions run.
                subprocess.run(['cp', '-a', str(seed), str(clone)], check=True, timeout=30)
                source, tree, folder = root / 'search_data.json', root / 'folder_tree.json', root / 'folder_browser.json'
                source.write_text(json.dumps(records))
                tree.write_text('{}')
                tree.with_suffix('.json.gz').write_bytes(gzip.compress(b'{}'))
                folder.write_text(json.dumps(browser))
                original_run = module.run

                def run(cmd, cwd=None, env=None):
                    if cmd[:2] == ['git', 'clone']:
                        return 0, '', ''
                    return original_run(cmd, cwd=cwd, env=env)

                def clone_space(_url, target, _env):
                    self.assertEqual(target, str(clone))
                    return 0, '', ''

                def write_index(path, *_args):
                    path.write_bytes(b'fixture index')

                with patch.dict('os.environ', {'HF_TOKEN': 'test', 'PAGES_TOKEN': 'test', 'PAGES_REPO': 'owner/search'}), \
                        patch.object(module, 'SOURCE_JSON', source), patch.object(module, 'FOLDER_TREE_JSON', tree), \
                        patch.object(module, 'FOLDER_BROWSER_JSON', folder), \
                        patch.object(module.tempfile, 'mkdtemp', return_value=str(clone)), \
                        patch.object(module, 'run', side_effect=run), \
                        patch.object(module, 'git_auth_env', return_value={}):
                    if module is self.space:
                        with patch.object(module, 'clone_space_repo', side_effect=clone_space), \
                                patch.object(module, 'write_word_index', side_effect=write_index), \
                                patch.object(module, 'write_ngram_index', side_effect=write_index):
                            self.assertEqual(module.main(), 0)
                    else:
                        self.assertEqual(module.main(), 0)
                changed = git(remote, 'show', '--format=', '--name-only', 'main').splitlines()
                self.assertTrue(changed)
                self.assertTrue(all(path in module.SEARCH_OUTPUT_PATHS or
                                    path.startswith(('data/initial/', 'data/sidebar/')) for path in changed))
                self.assertNotIn('data/reader_assets.json.gz', changed)
                self.assertNotIn('data/reader_v3_receipt.json', changed)
                self.assertNotIn('app.py', changed)
                self.assertIn('data/initial/stale.json', changed)
                self.assertIn('data/sidebar/stale.json', changed)
                self.assertEqual(git(remote, 'show', 'main:data/reader_assets.json.gz'), 'preserve Reader projection')
                self.assertEqual(git(remote, 'show', 'main:data/reader_v3_receipt.json'), 'preserve receipt')
                self.assertEqual(git(remote, 'show', 'main:app.py'), 'preserve external application')

    def test_real_git_concurrent_change_rebases_or_stops_on_conflict(self):
        def git(cwd, *args):
            return subprocess.run(['git', *map(str, args)], cwd=cwd, capture_output=True,
                                  text=True, timeout=90, check=True).stdout.strip()

        for module in (self.space, self.pages):
            for conflict in (False, True):
                with self.subTest(module=module.__name__, conflict=conflict), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    remote, seed, ours, other = root / 'remote.git', root / 'seed', root / 'ours', root / 'other'
                    git(root, 'init', '--bare', '--initial-branch=main', remote)
                    git(root, 'clone', remote, seed)
                    git(seed, 'config', 'user.name', 'fixture')
                    git(seed, 'config', 'user.email', 'fixture@example.invalid')
                    (seed / 'data').mkdir()
                    (seed / 'data/search_data.json.gz').write_text('original')
                    (seed / 'data/reader_assets.json.gz').write_text('preserve reader')
                    (seed / 'app.py').write_text('original application')
                    git(seed, 'add', '.')
                    git(seed, 'commit', '-m', 'seed')
                    git(seed, 'push', 'origin', 'main')
                    for target in (ours, other):
                        subprocess.run(['cp', '-a', str(seed), str(target)], check=True, timeout=30)
                    (ours / 'data/search_data.json.gz').write_text('our search update')
                    git(ours, 'add', 'data/search_data.json.gz')
                    git(ours, 'commit', '-m', 'our search update')
                    external_path = 'data/search_data.json.gz' if conflict else 'app.py'
                    (other / external_path).write_text('concurrent external update')
                    git(other, 'add', external_path)
                    git(other, 'commit', '-m', 'external update')
                    git(other, 'push', 'origin', 'main')
                    commands = []
                    original = module.run

                    def run(command, cwd=None, env=None):
                        commands.append(command)
                        return original(command, cwd=cwd, env=env)

                    with patch.object(module, 'run', side_effect=run), patch.object(module.time, 'sleep'):
                        self.assertEqual(module.push_with_retry(str(ours), {}), not conflict)
                    pushes = [command for command in commands if command == ['git', 'push']]
                    self.assertEqual(len(pushes), 1 if conflict else 2)
                    self.assertEqual(git(remote, 'show', 'main:data/reader_assets.json.gz'), 'preserve reader')
                    if conflict:
                        self.assertEqual(git(remote, 'show', 'main:data/search_data.json.gz'), 'concurrent external update')
                    else:
                        self.assertEqual(git(remote, 'show', 'main:data/search_data.json.gz'), 'our search update')
                        self.assertEqual(git(remote, 'show', 'main:app.py'), 'concurrent external update')

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
