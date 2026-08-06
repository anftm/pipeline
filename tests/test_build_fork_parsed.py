import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_fork_parsed


class BuildForkParsedTests(unittest.TestCase):
    def revisions(self, **changes):
        values = {
            "main": "main-sha", "config": "config-sha", "ocr_cache": "cache-sha",
            "ocr_patch": "patch-sha", "parsed": "parsed-sha",
        }
        values.update(changes)
        return values

    def test_needs_local_build_only_when_source_branch_differs(self):
        upstream = self.revisions()
        self.assertFalse(build_fork_parsed.needs_local_build(self.revisions(), upstream))
        self.assertTrue(build_fork_parsed.needs_local_build(self.revisions(ocr_patch="fork-patch"), upstream))
        self.assertFalse(build_fork_parsed.needs_local_build(self.revisions(parsed="fork-parsed"), upstream))

    def test_run_in_container_writes_as_host_user(self):
        root = Path("/tmp/build")
        cwd = root / "helper"
        with patch.object(build_fork_parsed.os, "getuid", return_value=1001), \
                patch.object(build_fork_parsed.os, "getgid", return_value=1002), \
                patch.object(build_fork_parsed, "run") as run:
            build_fork_parsed.run_in_container(root, cwd, ["npm", "install"])
        self.assertEqual(run.call_args.args[0], [
            "docker", "run", "--rm", "--user", "1001:1002", "--env", "HOME=/tmp",
            "--volume", "/tmp/build:/tmp/build", "--workdir", "/tmp/build/helper",
            "node:24", "npm", "install",
        ])

    def test_parsed_tree_change_ignores_commit_identity(self):
        parsed = Path("/tmp/parsed")
        environment = {"HOME": "/tmp"}
        with patch.object(build_fork_parsed, "run_output", side_effect=["a" * 40, "a" * 40]) as output:
            self.assertFalse(build_fork_parsed.parsed_tree_changed(parsed, "b" * 40, environment))
        self.assertEqual(output.call_args_list[1].args[0], ["git", "rev-parse", f"{'b' * 40}^{{tree}}"])

        with patch.object(build_fork_parsed, "run_output", side_effect=["a" * 40, "c" * 40]):
            self.assertTrue(build_fork_parsed.parsed_tree_changed(parsed, "b" * 40, environment))

    def test_branch_revisions_reads_later_pages(self):
        first_page = [
            {"name": f"feat/{index:03d}", "commit": {"sha": f"feat-{index}"}}
            for index in range(100)
        ]
        second_page = [
            {"name": branch, "commit": {"sha": revision}}
            for branch, revision in self.revisions().items()
        ]
        with patch.object(build_fork_parsed, "api_request", side_effect=[
                (200, first_page), (200, second_page),
        ]) as request:
            revisions = build_fork_parsed.branch_revisions("token", "owner", "repo")
        self.assertEqual(revisions, self.revisions())
        self.assertEqual(request.call_args_list[0].args[2], "/repos/owner/repo/branches?per_page=100&page=1")
        self.assertEqual(request.call_args_list[1].args[2], "/repos/owner/repo/branches?per_page=100&page=2")

    def test_prepare_patch_input_maps_legacy_archive_directory_to_root(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            nested = source / "archives0"
            nested.mkdir(parents=True)
            (nested / "[article][book].ts").write_text("export default [];", encoding="utf-8")
            (source / "README.md").write_text("readme", encoding="utf-8")
            target = build_fork_parsed.prepare_patch_input(source, Path(directory) / "target", 0)
            self.assertEqual((target / "[article][book].ts").read_text(encoding="utf-8"), "export default [];")
            self.assertFalse((target / "archives0").exists())
            self.assertTrue((target / "README.md").exists())

    def test_archive_20_cleanup_removes_image_and_font_markup(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = Path(directory)
            article = parsed / "article.json"
            article.write_text(__import__("json").dumps({
                "title": "题<IMG alt=请插入3号盘！ src=\"D:\\inset\\x.JPG\">目",
                "parts": [{"text": "<FONT class=imgsm>图片说明</FONT>正文"}],
            }), encoding="utf-8")
            build_fork_parsed.clean_selected_archive_parsed(parsed, 20)
            cleaned = __import__("json").loads(article.read_text(encoding="utf-8"))
            self.assertEqual(cleaned["title"], "题目")
            self.assertEqual(cleaned["parts"][0]["text"], "图片说明正文")

    def test_cleanup_decodes_epub_markup_and_normalizes_author_wrappers(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = Path(directory)
            article = parsed / "article.json"
            article.write_text(__import__("json").dumps({
                "authors": [
                    "(张文藻)", "（艾青）", "&#8203;小鹰", "[中共中央]办公室",
                    "[绝密]", "img=504n0101aa>", "(孙作宾", "新华社记者］",
                ],
                "parts": [{"text": "&lt;span class=\"calibre10\"&gt;正文&lt;/span&gt;"}],
            }), encoding="utf-8")
            build_fork_parsed.clean_selected_archive_parsed(parsed, 31)
            cleaned = __import__("json").loads(article.read_text(encoding="utf-8"))
            self.assertEqual(cleaned["authors"], [
                "张文藻", "艾青", "小鹰", "中共中央办公室", "孙作宾", "新华社记者",
            ])
            self.assertEqual(cleaned["parts"][0]["text"], "正文")

    def test_cleanup_truncates_incomplete_latin_author_annotation(self):
        cleaned = build_fork_parsed.clean_legacy_image_markup({
            "authors": ["魏格林（Susanne", "艾恺（Guy", "万一(万家骏)"],
        })
        self.assertEqual(cleaned["authors"], ["魏格林", "艾恺", "万一(万家骏)"])

    def test_archive_12_cleanup_removes_document_markup(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = Path(directory)
            article = parsed / "article.json"
            article.write_text(__import__("json").dumps({
                "parts": [{"text": "&lt;html&gt;&lt;pre&gt;正文&amp;注释&lt;/pre&gt;&lt;/html&gt;"}],
            }), encoding="utf-8")
            build_fork_parsed.clean_selected_archive_parsed(parsed, 12)
            cleaned = __import__("json").loads(article.read_text(encoding="utf-8"))
            self.assertEqual(cleaned["parts"][0]["text"], "正文&注释")

    def test_archive_14_cleanup_removes_invisible_and_control_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = Path(directory)
            article = parsed / "article.json"
            article.write_text(__import__("json").dumps({
                "parts": [{"text": "准\u200b备\u0001正文"}],
            }), encoding="utf-8")
            build_fork_parsed.clean_selected_archive_parsed(parsed, 14)
            cleaned = __import__("json").loads(article.read_text(encoding="utf-8"))
            self.assertEqual(cleaned["parts"][0]["text"], "准备正文")

    def test_archive_cleanup_does_not_change_other_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            article = Path(directory) / "article.json"
            original = {"text": "<IMG alt=请插入3号盘！>"}
            article.write_text(__import__("json").dumps(original), encoding="utf-8")
            build_fork_parsed.clean_selected_archive_parsed(Path(directory), 19)
            self.assertEqual(__import__("json").loads(article.read_text(encoding="utf-8")), original)

    def test_main_builds_diverged_archives_and_records_input_state(self):
        mirror = self.revisions(ocr_patch="fork-patch")
        upstream = self.revisions()
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.json"
            with patch.object(build_fork_parsed, "ARCHIVE_ID", "0"), \
                    patch.object(build_fork_parsed, "STATE_PATH", Path(directory) / "missing.json"), \
                    patch.object(build_fork_parsed, "CANDIDATE_PATH", candidate), \
                    patch.object(build_fork_parsed, "ref_revision", return_value="helper-sha"), \
                    patch.object(build_fork_parsed, "branch_revisions", side_effect=[mirror, upstream]), \
                    patch.object(build_fork_parsed, "prepare_helper", return_value=Path(directory) / "helper"), \
                    patch.object(build_fork_parsed, "build_archive") as build, \
                    patch.dict(os.environ, {"GH_PAT": "token"}, clear=False):
                build_fork_parsed.main()
            build.assert_called_once()
            state = __import__("json").loads(candidate.read_text(encoding="utf-8"))
            self.assertEqual(state["archives"]["0"]["helper"], "helper-sha")
            self.assertEqual(state["archives"]["0"]["ocr_patch"], "fork-patch")

    def test_main_syncs_parsed_with_lease_when_sources_match_upstream(self):
        mirror = self.revisions(parsed="fork-parsed")
        upstream = self.revisions()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(build_fork_parsed, "ARCHIVE_ID", "0"), \
                    patch.object(build_fork_parsed, "STATE_PATH", Path(directory) / "missing.json"), \
                    patch.object(build_fork_parsed, "CANDIDATE_PATH", Path(directory) / "candidate.json"), \
                    patch.object(build_fork_parsed, "ref_revision", return_value="helper-sha"), \
                    patch.object(build_fork_parsed, "branch_revisions", side_effect=[mirror, upstream, mirror]), \
                    patch.object(build_fork_parsed, "sync_parsed") as sync, \
                    patch.object(build_fork_parsed, "build_archive") as build, \
                    patch.dict(os.environ, {"GH_PAT": "token"}, clear=False):
                build_fork_parsed.main()
            sync.assert_called_once()
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
