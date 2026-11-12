from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def git(*args, cwd=None):
    return subprocess.run(["git", *map(str, args)], cwd=cwd, check=True,
                          capture_output=True, text=True, timeout=120).stdout.strip()


class DirectorySnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.update = load_script("update_dirs")
        cls.fetch = load_script("fetch_and_parse")

    def make_remote(self, base, *, lfs=False):
        remote, seed = base / "remote.git", base / "seed"
        git("init", "--bare", "--initial-branch=main", remote)
        git("clone", remote, seed)
        git("config", "user.name", "test", cwd=seed)
        git("config", "user.email", "test@example.invalid", cwd=seed)
        if lfs:
            git("lfs", "install", "--local", cwd=seed)
            git("lfs", "track", "*.txt", cwd=seed)
        (seed / "old.pdf").write_bytes(b"preserve old book")
        (seed / "README.md").write_text("preserve readme\n")
        (seed / "mul.py").write_text("preserve original script\n")
        git("add", ".", cwd=seed)
        git("commit", "-m", "source", cwd=seed)
        git("push", "origin", "main", cwd=seed)
        return remote, seed

    def test_real_git_and_lfs_regenerate_then_skip_and_preserve_source(self):
        for lfs in (False, True):
            with self.subTest(lfs=lfs), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                remote, seed = self.make_remote(base, lfs=lfs)
                url = remote.as_uri()
                # Start with no catalogues and an existing unrelated mul.py.
                # A localhost file:// LFS remote must not use the production
                # HF auth header (Git LFS otherwise attempts credential lookup).
                with patch.object(self.update, "HF_TOKEN", ""):
                    revision = self.update.process_repo("datasets/test/book", clone_url=url)
                self.assertTrue(revision)
                self.assertEqual(revision, git("rev-parse", "main", cwd=remote))
                git("pull", "--ff-only", cwd=seed)
                self.assertEqual((seed / "mul.py").read_text(), "preserve original script\n")
                self.assertEqual((seed / "old.pdf").read_bytes(), b"preserve old book")
                self.assertEqual((seed / "直接目录.txt").read_text(), "./old.pdf\n")
                self.assertIn("└── old.pdf", (seed / "树形目录.txt").read_text())
                if lfs:
                    pointer = git("show", f"{revision}:直接目录.txt", cwd=remote)
                    self.assertTrue(pointer.startswith("version https://git-lfs.github.com/spec/v1"))

                # A bot commit does not excuse skipping actual generation.
                with patch.object(self.update, "HF_TOKEN", ""):
                    self.assertEqual(self.update.process_repo("datasets/test/book", clone_url=url), revision)
                # Content-only changes do not change the path catalogue and do
                # not warrant a separate bot commit.
                (seed / "README.md").write_text("updated readme\n")
                (seed / "old.pdf").write_bytes(b"updated contents, same name")
                git("add", "README.md", "old.pdf", cwd=seed)
                git("commit", "-m", "content only", cwd=seed)
                git("push", "origin", "main", cwd=seed)
                content_revision = git("rev-parse", "HEAD", cwd=seed)
                with patch.object(self.update, "HF_TOKEN", ""):
                    self.assertEqual(self.update.process_repo("datasets/test/book", clone_url=url), content_revision)
                git("rm", "old.pdf", cwd=seed)
                (seed / "new.pdf").write_bytes(b"new book")
                git("add", "new.pdf", cwd=seed)
                git("commit", "-m", "source changes", cwd=seed)
                git("push", "origin", "main", cwd=seed)
                with patch.object(self.update, "HF_TOKEN", ""):
                    revision2 = self.update.process_repo("datasets/test/book", clone_url=url)
                self.assertTrue(revision2)
                self.assertNotEqual(revision2, revision)
                git("pull", "--ff-only", cwd=seed)
                self.assertEqual((seed / "直接目录.txt").read_text(), "./new.pdf\n")
                self.assertIn("└── new.pdf", (seed / "树形目录.txt").read_text())
                self.assertNotIn("old.pdf", (seed / "树形目录.txt").read_text())
                self.assertEqual((seed / "mul.py").read_text(), "preserve original script\n")

    def test_bot_head_does_not_hide_deleted_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            remote, seed = self.make_remote(base)
            url = remote.as_uri()
            with patch.object(self.update, "HF_TOKEN", ""):
                first = self.update.process_repo("datasets/test/book", clone_url=url)
            git("pull", "--ff-only", cwd=seed)
            git("rm", "直接目录.txt", cwd=seed)
            git("-c", "user.name=github-actions[bot]", "commit", "-m",
                "自动更新目录 intentionally incomplete", cwd=seed)
            git("push", "origin", "main", cwd=seed)
            with patch.object(self.update, "HF_TOKEN", ""):
                regenerated = self.update.process_repo("datasets/test/book", clone_url=url)
            self.assertNotEqual(first, regenerated)
            git("pull", "--ff-only", cwd=seed)
            self.assertEqual((seed / "直接目录.txt").read_text(), "./old.pdf\n")

    def test_partial_failure_never_emits_old_or_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "versions.json"
            output.write_text('{"outdated":true}')
            with patch.object(self.update, "HF_TOKEN", "test"), \
                    patch.object(self.update, "REPOS", ["datasets/A/one", "datasets/A/two"]), \
                    patch.object(self.update, "process_repo", side_effect=["a" * 40, None]), \
                    patch.dict(os.environ, {"DIRECTORY_REVISIONS_FILE": str(output)}):
                self.assertEqual(self.update.main(), 1)
            self.assertFalse(output.exists())

    def test_success_snapshot_contains_each_exact_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "versions.json"
            with patch.object(self.update, "HF_TOKEN", "test"), \
                    patch.object(self.update, "REPOS", ["datasets/A/one", "datasets/A/two"]), \
                    patch.object(self.update, "process_repo", side_effect=["a" * 40, "b" * 40]), \
                    patch.dict(os.environ, {"DIRECTORY_REVISIONS_FILE": str(output)}):
                self.assertEqual(self.update.main(), 0)
            self.assertEqual(json.loads(output.read_text()), {"A/one": "a" * 40, "A/two": "b" * 40})

    def test_fetch_rejects_changed_revision_without_writing_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state, versions, output = base / "state.json", base / "versions.json", base / "actions.txt"
            state.write_text(json.dumps({"A/one": "c" * 40}))
            versions.write_text(json.dumps({"A/one": "a" * 40}))
            with patch.object(self.fetch, "REPOS", ["A/one"]), \
                    patch.object(self.fetch, "STATE_FILE", state), \
                    patch.object(self.fetch, "get_repo_sha", return_value="b" * 40), \
                    patch.object(self.fetch, "http_get_text") as download, \
                    patch.dict(os.environ, {"DIRECTORY_REVISIONS_FILE": str(versions),
                                            "GITHUB_OUTPUT": str(output)}, clear=True):
                self.assertEqual(self.fetch.main(), 1)
            download.assert_not_called()
            self.assertEqual(json.loads(state.read_text()), {"A/one": "c" * 40})
            self.assertEqual(output.read_text(), "data_changed=false\n")

    def test_fetch_rejects_missing_incomplete_or_malformed_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            versions = Path(temporary) / "versions.json"
            for content in (None, "{}", '{"A/one":"not-a-revision"}', "not json"):
                with self.subTest(content=content):
                    if content is None:
                        if versions.exists():
                            versions.unlink()
                    else:
                        versions.write_text(content)
                    with patch.object(self.fetch, "REPOS", ["A/one"]), \
                            patch.object(self.fetch, "get_repo_sha") as get_sha, \
                            patch.dict(os.environ, {"DIRECTORY_REVISIONS_FILE": str(versions)}, clear=True):
                        self.assertEqual(self.fetch.main(), 1)
                    get_sha.assert_not_called()

    def test_matching_snapshot_allows_normal_change_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state, versions, output = base / "state.json", base / "versions.json", base / "actions.txt"
            revision = "a" * 40
            state.write_text(json.dumps({"A/one": revision}))
            versions.write_text(json.dumps({"A/one": revision}))
            with patch.object(self.fetch, "REPOS", ["A/one"]), \
                    patch.object(self.fetch, "STATE_FILE", state), \
                    patch.object(self.fetch, "get_repo_sha", return_value=revision), \
                    patch.dict(os.environ, {"DIRECTORY_REVISIONS_FILE": str(versions),
                                            "GITHUB_OUTPUT": str(output)}, clear=True):
                self.assertEqual(self.fetch.main(), 0)
            self.assertEqual(output.read_text(), "data_changed=false\n")

    def test_directory_failure_stops_before_commit_or_push(self):
        from types import SimpleNamespace

        responses = iter([
            SimpleNamespace(returncode=0, stdout="a" * 40 + "\tHEAD\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # clone
            SimpleNamespace(returncode=1, stdout="", stderr="LFS unavailable"),
        ])
        with patch.object(self.update, "run_cmd", side_effect=lambda *args, **kwargs: next(responses)) as run:
            self.assertIsNone(self.update.process_repo("datasets/test/book", clone_url="test://remote"))
        self.assertEqual(len(run.call_args_list), 3)
        self.assertNotIn(["git", "push"], [call.args[0] for call in run.call_args_list])

    def test_head_race_after_generation_is_not_a_verified_snapshot(self):
        from types import SimpleNamespace

        okay = lambda stdout="": SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        commands = []

        def run(cmd, cwd=None, extra_env=None):
            commands.append(cmd)
            if cmd[1] == "ls-remote":
                sha = "a" * 40 if sum(c[1] == "ls-remote" for c in commands) == 1 else "b" * 40
                return okay(sha + "\tHEAD\n")
            if cmd[1] == "clone":
                repo_dir = Path(cmd[-1])
                repo_dir.mkdir()
                for name in self.update.DIRECTORY_FILES:
                    (repo_dir / name).write_text("current\n")
            if cmd[1] == "rev-parse":
                return okay("a" * 40 + "\n")
            return okay()

        def generate(cmd, cwd=None, **kwargs):
            for name in self.update.DIRECTORY_FILES:
                (Path(cwd) / name).write_text("current\n")
            return okay()

        with patch.object(self.update, "run_cmd", side_effect=run), \
                patch.object(self.update.subprocess, "run", side_effect=generate):
            self.assertIsNone(self.update.process_repo("datasets/test/book", clone_url="test://remote"))

    def test_daily_workflow_serializes_and_requires_generation_first(self):
        daily = (ROOT / ".github/workflows/search-daily-sync.yml").read_text()
        import yaml
        daily_config = yaml.load(daily, Loader=yaml.BaseLoader)
        self.assertEqual(daily_config.get('permissions'), {'contents': 'write'})
        self.assertEqual(daily_config['jobs']['sync']['steps'][0]['uses'], 'actions/checkout@v7')
        self.assertLess(daily.index("- name: Generate and verify source directories"),
                        daily.index("- name: Fetch and parse"))
        self.assertEqual(daily.count("DIRECTORY_REVISIONS_FILE:"), 2)
        self.assertIn("group: daily-sync", daily)
        self.assertIn("cancel-in-progress: false", daily)
        self.assertIn("queue: max", daily)
        ci = yaml.load((ROOT / ".github/workflows/search-directory-tests.yml").read_text(),
                       Loader=yaml.BaseLoader)
        runs = '\n'.join(step.get('run', '') for step in ci['jobs']['tests']['steps'])
        self.assertIn('jieba==0.42.1', runs)
        self.assertIn('PyYAML==6.0.3', runs)

    def test_manual_directory_only_mode_cannot_publish_search_or_central_state(self):
        """Validate actual YAML steps; this test never runs the directory producer."""
        import yaml
        workflow = yaml.load((ROOT / '.github/workflows/search-daily-sync.yml').read_text(),
                             Loader=yaml.BaseLoader)
        self.assertFalse((ROOT / '.github/workflows/search-update-directories.yml').exists())
        inputs = workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(inputs['target']['default'], 'all')
        self.assertEqual(inputs['target']['options'], ['all', 'hf-space', 'directories'])
        self.assertEqual(workflow['on']['schedule'], [{'cron': '0 0 * * *'}])
        self.assertEqual(workflow['concurrency'], {'group': 'daily-sync', 'cancel-in-progress': 'false',
                                                    'queue': 'max'})
        self.assertEqual(set(workflow['jobs']), {'directories', 'sync'})
        manual = workflow['jobs']['directories']
        self.assertEqual(manual['if'], "github.event_name == 'workflow_dispatch' && inputs.target == 'directories'")
        self.assertEqual(manual['permissions'], {'contents': 'read'})
        self.assertEqual(manual['timeout-minutes'], '60')
        self.assertEqual([step.get('uses') for step in manual['steps'][:2]],
                         ['actions/checkout@v7', 'actions/setup-python@v7'])
        self.assertEqual(manual['steps'][1]['with']['python-version'], '3.11')
        self.assertEqual(manual['steps'][2]['run'], 'python scripts/update_dirs.py')
        self.assertEqual(manual['steps'][2]['env'], {'HF_TOKEN': '${{ secrets.HF_TOKEN }}'})
        self.assertEqual(len(manual['steps']), 3)
        self.assertFalse(any('if' in step or 'continue-on-error' in step for step in manual['steps']))
        job = workflow['jobs']['sync']
        self.assertEqual(job['if'], "inputs.target != 'directories'")
        self.assertEqual(job['env']['TARGET'], "${{ inputs.target || 'all' }}")
        steps = {step.get('name'): step for step in job['steps'] if step.get('name')}
        install = steps['Install dependencies']
        self.assertEqual(install['run'], 'pip install -r scripts/requirements.txt requests==2.32.5')
        self.assertNotIn('if', install)
        self.assertNotIn('continue-on-error', install)
        self.assertLess(job['steps'].index(install),
                        job['steps'].index(steps['Inspect newly staged candidate without publication']))
        directory = steps['Generate and verify source directories']
        self.assertEqual(directory['run'], 'python -u scripts/update_dirs.py')
        self.assertNotIn('if', directory)
        self.assertIn('DIRECTORY_REVISIONS_FILE', directory['env'])
        self.assertNotIn('if', steps['Fetch and parse'])
        self.assertEqual(steps['Stage immutable search generation']['if'], "steps.fetch.outputs.data_changed == 'true'")
        self.assertEqual(steps['Stage immutable search generation']['id'], 'stage')
        self.assertIn('--baseline-output "$RUNNER_TEMP/search-published-revisions.json"',
                      steps['Verify published generation and both consumers']['run'])
        self.assertIn('--receipt-output "$RUNNER_TEMP/search-published-receipt.json"',
                      steps['Verify published generation and both consumers']['run'])
        self.assertEqual(steps['Verify published generation and both consumers']['id'], 'published')
        self.assertIn('SEARCH_BASELINE_FILE', steps['Fetch and parse']['env'])
        self.assertLess(job['steps'].index(steps['Audit terminal publication journal before fetching']),
                        job['steps'].index(steps['Generate and verify source directories']))
        self.assertLess(job['steps'].index(steps['Generate and verify source directories']),
                        job['steps'].index(steps['Verify published generation and both consumers']))
        self.assertLess(job['steps'].index(steps['Verify published generation and both consumers']),
                        job['steps'].index(steps['Fetch and parse']))
        self.assertEqual(steps['Recheck publication journal before staging']['if'], "steps.fetch.outputs.data_changed == 'true'")
        self.assertLess(job['steps'].index(steps['Recheck publication journal before staging']),
                        job['steps'].index(steps['Stage immutable search generation']))
        self.assertIn('search_daily_journal.py --expected', steps['Verify journal did not change during the run']['run'])
        self.assertIn('--expected-receipt "$RUNNER_TEMP/search-published-receipt.json"',
                      steps['Verify publication did not change during the run']['run'])
        handoff = steps['Inspect newly staged candidate without publication']
        artifact = steps['Upload candidate handoff evidence']
        new_candidate = "steps.fetch.outputs.data_changed == 'true' && steps.stage.outputs.generation != steps.published.outputs.generation"
        self.assertEqual(handoff['if'], new_candidate)
        self.assertEqual(artifact['if'], new_candidate)
        self.assertIn('scripts/search_daily_handoff.py', handoff['run'])
        self.assertEqual(artifact['uses'], 'actions/upload-artifact@v7')
        self.assertEqual(artifact['with']['if-no-files-found'], 'error')
        self.assertLess(job['steps'].index(steps['Stage immutable search generation']), job['steps'].index(handoff))
        self.assertLess(job['steps'].index(handoff), job['steps'].index(artifact))
        self.assertLess(job['steps'].index(artifact), job['steps'].index(steps['Fail closed when a new generation awaits promotion']))
        self.assertEqual(steps['Fail closed when a new generation awaits promotion']['if'], "steps.fetch.outputs.data_changed == 'true'")
        self.assertIn('STAGED_GENERATION', steps['Fail closed when a new generation awaits promotion']['env'])
        self.assertIn('PUBLISHED_GENERATION', steps['Fail closed when a new generation awaits promotion']['env'])
        self.assertIn('SOURCE_REVISIONS_FILE', steps['Verify source revisions are fully published']['env'])
        for retired in ('Sync to HF Space', 'Sync to GitHub Pages', 'Deploy GitHub Pages', 'Commit updated state'):
            self.assertNotIn(retired, steps)
        self.assertFalse(any('sync_to_pages.py' in step.get('run', '') or 'sync_to_space.py' in step.get('run', '')
                             or 'git push' in step.get('run', '') for step in job['steps']))
        # The entire sync job is skipped in directory-only mode; no consumer
        # can treat an absent output as proof of synchronization.
        self.assertEqual(steps['Fetch and parse']['id'], 'fetch')
        self.assertEqual(sum('scripts/update_dirs.py' in step.get('run', '') for step in job['steps']), 1)

    def test_pushed_catalog_readback_mismatch_is_not_accepted(self):
        from types import SimpleNamespace

        okay = lambda stdout="": SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        commands = []
        source_revision = "a" * 40
        published_revision = "b" * 40

        def run(cmd, cwd=None, extra_env=None):
            commands.append(cmd)
            if cmd[1] == "ls-remote":
                sha = source_revision if sum(c[1] == "ls-remote" for c in commands) == 1 else published_revision
                return okay(sha + "\tHEAD\n")
            if cmd[1] == "clone":
                repo_dir = Path(cmd[-1])
                repo_dir.mkdir(exist_ok=True)
                content = "old\n" if sum(c[1] == "clone" for c in commands) == 1 else "corrupt\n"
                for name in self.update.DIRECTORY_FILES:
                    (repo_dir / name).write_text(content)
            if cmd[1] == "status":
                return okay(" M catalog\n")
            if cmd[1] == "rev-parse":
                return okay(published_revision + "\n")
            return okay()

        def generate(cmd, cwd=None, **kwargs):
            for name in self.update.DIRECTORY_FILES:
                (Path(cwd) / name).write_text("new\n")
            return okay()

        with patch.object(self.update, "run_cmd", side_effect=run), \
                patch.object(self.update.subprocess, "run", side_effect=generate):
            self.assertIsNone(self.update.process_repo("datasets/test/book", clone_url="test://remote"))
        self.assertIn(["git", "push"], commands)

    def test_add_or_commit_failure_does_not_push(self):
        from types import SimpleNamespace

        okay = lambda stdout="": SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        for failing in ("add", "commit"):
            with self.subTest(failing=failing):
                commands = []

                def run(cmd, cwd=None, extra_env=None):
                    commands.append(cmd)
                    if cmd[1] == "clone":
                        repo_dir = Path(cmd[-1])
                        repo_dir.mkdir()
                        for name in self.update.DIRECTORY_FILES:
                            (repo_dir / name).write_text("old\n")
                    if cmd[1] == "status":
                        return okay(" M catalog\n")
                    if cmd[1] == failing or (failing == "commit" and "commit" in cmd):
                        return SimpleNamespace(returncode=1, stdout="", stderr="expected failure")
                    return okay()

                def generate(cmd, cwd=None, **kwargs):
                    for name in self.update.DIRECTORY_FILES:
                        (Path(cwd) / name).write_text("new\n")
                    return okay()

                with patch.object(self.update, "run_cmd", side_effect=run), \
                        patch.object(self.update.subprocess, "run", side_effect=generate):
                    self.assertIsNone(self.update.process_repo("datasets/test/book", clone_url="test://remote"))
                self.assertNotIn(["git", "push"], commands)

    def test_local_generation_failure_never_commits_or_pushes(self):
        from types import SimpleNamespace

        okay = lambda stdout="": SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        commands = []

        def run(cmd, cwd=None, extra_env=None):
            commands.append(cmd)
            if cmd[1] == "clone":
                Path(cmd[-1]).mkdir()
            return okay()

        failure = SimpleNamespace(returncode=1, stdout="", stderr="generator failed")
        with patch.object(self.update, "run_cmd", side_effect=run), \
                patch.object(self.update.subprocess, "run", return_value=failure):
            self.assertIsNone(self.update.process_repo("datasets/test/book", clone_url="test://remote"))
        self.assertNotIn(["git", "push"], commands)

    def test_matching_verified_snapshot_generates_from_pinned_catalogues(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            revision = "a" * 40
            versions, state, output = base / "versions.json", base / "state.json", base / "actions.txt"
            versions.write_text(json.dumps({"A/one": revision}))
            state.write_text(json.dumps({"A/one": "b" * 40}))
            with patch.object(self.fetch, "REPOS", ["A/one"]), \
                    patch.object(self.fetch, "STATE_FILE", state), \
                    patch.object(self.fetch, "OUTPUT_DIR", base), \
                    patch.object(self.fetch, "OUTPUT_FILE", base / "search.json"), \
                    patch.object(self.fetch, "FOLDER_TREE_FILE", base / "folder_tree.json"), \
                    patch.object(self.fetch, "FOLDER_BROWSER_FILE", base / "folder_browser.json"), \
                    patch.object(self.fetch, "get_repo_sha", return_value=revision), \
                    patch.object(self.fetch, "http_get_text", return_value="./Books/new.pdf\n") as download, \
                    patch.object(self.fetch, "batch_get_sizes", return_value={"Books/new.pdf": 123}), \
                    patch.object(self.fetch.time, "sleep"), \
                    patch.dict(os.environ, {"DIRECTORY_REVISIONS_FILE": str(versions),
                                            "GITHUB_OUTPUT": str(output)}, clear=True):
                self.assertEqual(self.fetch.main(), 0)
            self.assertIn(f"/resolve/{revision}/", download.call_args.args[0])
            payload = json.loads((base / "search.json").read_text())
            self.assertEqual(payload["rc"][0][1:3], ["new", "pdf"])
            self.assertEqual(json.loads(state.read_text()), {"A/one": revision})
            self.assertEqual(output.read_text(), "data_changed=true\n")

    def test_verified_snapshot_with_unreadable_catalog_fails_without_tree_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            revision = "a" * 40
            versions, state = base / "versions.json", base / "state.json"
            versions.write_text(json.dumps({"A/one": revision}))
            state.write_text(json.dumps({"A/one": "b" * 40}))
            with patch.object(self.fetch, "REPOS", ["A/one"]), \
                    patch.object(self.fetch, "STATE_FILE", state), \
                    patch.object(self.fetch, "OUTPUT_FILE", base / "search.json"), \
                    patch.object(self.fetch, "get_repo_sha", return_value=revision), \
                    patch.object(self.fetch, "http_get_text", return_value=""), \
                    patch.object(self.fetch, "tree_records") as fallback, \
                    patch.dict(os.environ, {"DIRECTORY_REVISIONS_FILE": str(versions)}, clear=True):
                self.assertEqual(self.fetch.main(), 1)
            fallback.assert_not_called()
            self.assertFalse((base / "search.json").exists())
            self.assertEqual(json.loads(state.read_text()), {"A/one": "b" * 40})

    def test_verified_snapshot_repo_inventory_matches_producer(self):
        self.assertEqual(set(self.fetch.REPOS),
                         {repo.removeprefix("datasets/") for repo in self.update.REPOS})

    def test_generator_target_argument_does_not_write_beside_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "repo"
            base.mkdir()
            (base / "文档.pdf").write_bytes(b"book")
            for filename in ("直接目录.txt", "树形目录.txt"):
                self.assertFalse((ROOT / "scripts" / filename).exists())
            subprocess.run(["python3", str(ROOT / "scripts/mul.py"), str(base) + "/"], check=True, timeout=10)
            self.assertEqual((base / "直接目录.txt").read_text(), "./文档.pdf\n")
            self.assertTrue((base / "树形目录.txt").read_text().startswith("repo\n"))
            for filename in ("直接目录.txt", "树形目录.txt"):
                self.assertFalse((ROOT / "scripts" / filename).exists())


if __name__ == "__main__":
    unittest.main()
