import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_fork_parsed


class BuildForkParsedTests(unittest.TestCase):
    def test_selected_archive_ids_supports_a_comma_separated_batch(self):
        with patch.object(build_fork_parsed, "ARCHIVE_ID", "3,9,10,20,24,9"):
            self.assertEqual(build_fork_parsed.selected_archive_ids(), [3, 9, 10, 20, 24])

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

    def test_commit_tree_revision_reads_tree_identity(self):
        with patch.object(build_fork_parsed, "api_request", return_value=(200, {"tree": {"sha": "tree-sha"}})) as request:
            tree = build_fork_parsed.commit_tree_revision("token", "owner", "repo", "commit/sh a")
        self.assertEqual(tree, "tree-sha")
        self.assertEqual(request.call_args.args[2], "/repos/owner/repo/git/commits/commit%2Fsh%20a")

    def test_ocr_rebase_guard_ignores_unchanged_ocr_cache(self):
        current = self.revisions()
        with patch.object(build_fork_parsed, "ocr_patch_files") as files:
            conflict = build_fork_parsed.ocr_patch_rebase_conflict(
                "token", 3, {"ocr_cache": current["ocr_cache"]}, current, self.revisions(ocr_patch="upstream-patch"),
            )
        self.assertIsNone(conflict)
        files.assert_not_called()

    def test_ocr_rebase_guard_allows_identical_patch_trees(self):
        current = self.revisions(ocr_cache="new-cache", ocr_patch="fork-patch")
        upstream = self.revisions(ocr_cache="new-cache", ocr_patch="upstream-patch")
        files = {"[article][publication].ts": "same-blob"}
        with patch.object(build_fork_parsed, "ocr_patch_files", side_effect=[files, files]):
            conflict = build_fork_parsed.ocr_patch_rebase_conflict(
                "token", 3, {"ocr_cache": "old-cache"}, current, upstream,
            )
        self.assertIsNone(conflict)

    def test_ocr_rebase_guard_blocks_local_patches_on_new_ocr(self):
        current = self.revisions(ocr_cache="new-cache", ocr_patch="fork-patch")
        upstream = self.revisions(ocr_cache="new-cache", ocr_patch="upstream-patch")
        with patch.object(build_fork_parsed, "ocr_patch_files", side_effect=[
                    {"[article][publication].ts": "fork-blob"},
                    {"[article][publication].ts": "upstream-blob"},
                ]), \
                patch.object(build_fork_parsed, "ALLOW_OCR_PATCH_REBASE", False):
            conflict = build_fork_parsed.ocr_patch_rebase_conflict(
                "token", 3, {"ocr_cache": "old-cache"}, current, upstream,
            )
        self.assertEqual(conflict["articles"], [{
            "path": "[article][publication].ts",
            "article_id": "article",
            "publication_id": "publication",
            "doc_id": "3:7:article:publication",
        }])

    def test_ocr_rebase_guard_requires_dedicated_override(self):
        current = self.revisions(ocr_cache="new-cache", ocr_patch="fork-patch")
        upstream = self.revisions(ocr_cache="new-cache", ocr_patch="upstream-patch")
        with patch.object(build_fork_parsed, "ocr_patch_files", side_effect=[
                    {"[article][publication].ts": "fork-blob"},
                    {"[article][publication].ts": "upstream-blob"},
                ]), \
                patch.object(build_fork_parsed, "ALLOW_OCR_PATCH_REBASE", True):
            conflict = build_fork_parsed.ocr_patch_rebase_conflict(
                "token", 3, {"ocr_cache": "old-cache"}, current, upstream,
            )
        self.assertIsNone(conflict)

    def test_ocr_patch_files_reads_only_typescript_blobs(self):
        tree = {
            "tree": [
                {"path": "[a][p].ts", "type": "blob", "sha": "patch"},
                {"path": "README.md", "type": "blob", "sha": "readme"},
                {"path": "nested", "type": "tree", "sha": "directory"},
            ],
            "truncated": False,
        }
        with patch.object(build_fork_parsed, "commit_tree_revision", return_value="tree-sha"), \
                patch.object(build_fork_parsed, "api_request", return_value=(200, tree)):
            files = build_fork_parsed.ocr_patch_files("token", "owner", "repo", "revision")
        self.assertEqual(files, {"[a][p].ts": "patch"})

    def test_parsed_article_finds_publication_and_renders_complete_text(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = Path(directory)
            book = parsed / "pub" / "publication"
            article_root = book / "art"
            article_root.mkdir(parents=True)
            (book / "publication.metadata").write_text("{}", encoding="utf-8")
            (article_root / "article.json").write_text(__import__("json").dumps({
                "title": "文章标题", "description": "说明",
                "parts": [{"text": "第一段"}, {"text": "第二段"}],
                "comments": ["注释"],
            }, ensure_ascii=False), encoding="utf-8")
            article = build_fork_parsed.parsed_article(parsed, "article", "publication")
        self.assertEqual(article, {
            "title": "文章标题", "content": "说明\n第一段\n第二段\n注释",
        })

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
            "authors": [
                "魏格林（Susanne", "艾恺（Guy", "鸣不平（王应素",
                "朱裕璧（文汇报记者", "万一(万家骏)",
            ],
        })
        self.assertEqual(cleaned["authors"], [
            "魏格林", "艾恺", "鸣不平", "朱裕璧", "万一(万家骏)",
        ])

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

    def test_archive_24_cleanup_removes_ocr_layout_markers(self):
        cleaned = build_fork_parsed.clean_legacy_image_markup({
            "authors": [
                "孙学贵〖HH/换行〗DW：某部队", "DW：",
                "12968〖-ZQ/总期〗19930713〖-RQ/日期〗标题〖-BT/标题〗",
                "<传达记录要点>", "工代会房修一公司<红旗造反团>", "孙俊<", "/ct>",
                "孙俊<img=522b0201aa>",
            ],
            "parts": [{"text": "正文〖JZ/加重〗后缀〖JZ；加重〗〖-ZI/字符〗〖ZQ/总期"}],
        })
        self.assertEqual(cleaned["authors"], [
            "孙学贵", "工代会房修一公司红旗造反团", "孙俊", "孙俊",
        ])
        self.assertEqual(cleaned["parts"][0]["text"], "正文后缀")

    def test_archive_24_cleanup_splits_slash_separated_authors(self):
        with tempfile.TemporaryDirectory() as directory:
            article = Path(directory) / "article.json"
            article.write_text(__import__("json").dumps({
                "authors": ["甲/乙/某机构", "保留；分号"],
                "parts": [{"text": "正文"}],
            }, ensure_ascii=False), encoding="utf-8")
            build_fork_parsed.clean_selected_archive_parsed(Path(directory), 24)
            cleaned = __import__("json").loads(article.read_text(encoding="utf-8"))
        self.assertEqual(cleaned["authors"], ["甲", "乙", "某机构", "保留；分号"])

    def test_archive_24_cleanup_removes_replacement_character_content_and_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed = Path(directory)
            corrupt = parsed / "corrupt.json"
            corrupt_tags = corrupt.with_suffix(".tags")
            clean = parsed / "clean.json"
            corrupt.write_text(__import__("json").dumps({
                "title": "乱码文章", "parts": [{"text": "正文含�乱码"}],
            }, ensure_ascii=False), encoding="utf-8")
            corrupt_tags.write_text("[]", encoding="utf-8")
            clean.write_text(__import__("json").dumps({
                "title": "正常文章", "parts": [{"text": "正常正文"}],
            }, ensure_ascii=False), encoding="utf-8")

            build_fork_parsed.clean_selected_archive_parsed(parsed, 24)

            self.assertFalse(corrupt.exists())
            self.assertFalse(corrupt_tags.exists())
            self.assertTrue(clean.exists())

    def test_archive_24_cleanup_removes_replacement_character_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            article = Path(directory) / "article.json"
            article.write_text(__import__("json").dumps({
                "title": "乱码�标题", "parts": [{"text": "正常正文"}],
            }, ensure_ascii=False), encoding="utf-8")

            build_fork_parsed.clean_selected_archive_parsed(Path(directory), 24)

            self.assertFalse(article.exists())

    def test_replacement_character_content_is_not_dropped_from_other_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            article = Path(directory) / "article.json"
            article.write_text(__import__("json").dumps({
                "parts": [{"text": "正文含�乱码"}],
            }, ensure_ascii=False), encoding="utf-8")

            build_fork_parsed.clean_selected_archive_parsed(Path(directory), 20)

            self.assertTrue(article.exists())

    def test_selected_archives_remove_empty_content_articles_and_tags(self):
        for archive_id in (10, 20, 24):
            with self.subTest(archive_id=archive_id), tempfile.TemporaryDirectory() as directory:
                article = Path(directory) / "article.json"
                tags = article.with_suffix(".tags")
                article.write_text(__import__("json").dumps({
                    "title": "只有标题",
                    "description": " ",
                    "comments": ["", None],
                    "parts": [{"text": "\n"}],
                }, ensure_ascii=False), encoding="utf-8")
                tags.write_text("[]", encoding="utf-8")
                build_fork_parsed.clean_selected_archive_parsed(Path(directory), archive_id)
                self.assertFalse(article.exists())
                self.assertFalse(tags.exists())

    def test_selected_archives_keep_any_article_content(self):
        variants = [
            {"description": "摘要"},
            {"comments": ["注释"]},
            {"parts": [{"text": "正文"}]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            parsed = Path(directory)
            for index, value in enumerate(variants):
                (parsed / f"article-{index}.json").write_text(
                    __import__("json").dumps(value, ensure_ascii=False), encoding="utf-8",
                )
            build_fork_parsed.clean_selected_archive_parsed(parsed, 24)
            self.assertEqual(
                sorted(path.name for path in parsed.glob("*.json")),
                ["article-0.json", "article-1.json", "article-2.json"],
            )

    def test_cleanup_removes_misparsed_author_metadata_without_guessing_names(self):
        cleaned = build_fork_parsed.clean_legacy_image_markup({
            "authors": [
                "/ct", "1", "《", "？", "□伟", "?夫",
                "1952年3月8日政务院会议批准", "——文章副标题", "《人民日报",
                "人民日报》社论", "解放军报》社论", "军事译文出版社出版）（翟席）",
                "正文" * 41, "合法的长机构署名编辑部", "××",
                "张鹏图、王秉祥；阅办文件；", "《红旗》杂志、", "甲？乙？丙",
                "事由：这不是作者。", "甲/乙简介：甲，干部？简介：乙，干部？",
                "摄ZZ：刘红斌。图为错误正文。刘红斌",
            ],
        })
        self.assertEqual(cleaned["authors"], [
            "《人民日报》社论", "《解放军报》社论", "翟席",
            "合法的长机构署名编辑部", "××", "张鹏图、王秉祥",
            "《红旗》杂志", "甲/乙/丙",
        ])

    def test_archive_9_cleanup_applies_reviewed_author_separators(self):
        with tempfile.TemporaryDirectory() as directory:
            article = Path(directory) / "article.json"
            article.write_text(__import__("json").dumps({"authors": [
                "甲/乙；丙", "贵州省委工作组?", "××", "—毛远新给毛泽东的报告",
                build_fork_parsed.ARCHIVE9_JOINED_CREDIT,
            ]}, ensure_ascii=False), encoding="utf-8")
            build_fork_parsed.clean_selected_archive_parsed(Path(directory), 9)
            cleaned = __import__("json").loads(article.read_text(encoding="utf-8"))
        self.assertEqual(cleaned["authors"], [
            "甲", "乙", "丙", "贵州省委工作组", "毛远新给毛泽东的报告",
            "王性尧、胡子婴、胡厥文、郭棣活、盛丕华、汤蒂因、荣毅仁、刘靖基、魏如代表的联合发言",
        ])

    def test_archive_cleanup_does_not_change_other_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            article = Path(directory) / "article.json"
            original = {"text": "<IMG alt=请插入3号盘！>"}
            article.write_text(__import__("json").dumps(original), encoding="utf-8")
            build_fork_parsed.clean_selected_archive_parsed(Path(directory), 19)
            self.assertEqual(__import__("json").loads(article.read_text(encoding="utf-8")), original)

    def test_metadata_override_updates_and_renames_article_and_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parsed = root / "parsed"
            config = root / "config"
            book = parsed / "pub" / "book"
            articles = book / "art"
            articles.mkdir(parents=True)
            (book / "publication.metadata").write_text("{}", encoding="utf-8")
            article = {
                "title": "旧标题", "authors": ["旧作者"], "dates": [{"year": 1967}],
                "is_range_date": False, "parts": [{"text": "正文"}],
            }
            old_id = build_fork_parsed.article_id(article)
            article_path = articles / f"{old_id}.json"
            tags_path = articles / f"{old_id}.tags"
            article_path.write_text(__import__("json").dumps(article, ensure_ascii=False), encoding="utf-8")
            tags_path.write_text('[]', encoding="utf-8")
            patch = {
                "title": "新标题", "authors": ["新作者"],
                "tags": [{"name": "新标签", "type": "主题/事件"}],
            }
            expected = {**article, **patch}
            new_id = build_fork_parsed.article_id(expected)
            override_dir = config / "metadata_overrides" / "publication"
            override_dir.mkdir(parents=True)
            (override_dir / f"{old_id}.json").write_text(__import__("json").dumps({
                "version": 1, "publication_id": "publication", "article_id": old_id,
                "new_article_id": new_id,
                "article": {key: article[key] for key in ("title", "authors", "dates", "is_range_date")},
                "metadata": patch,
            }, ensure_ascii=False), encoding="utf-8")

            build_fork_parsed.apply_metadata_overrides(parsed, config)

            self.assertFalse(article_path.exists())
            updated_path = book / new_id[:3] / f"{new_id}.json"
            self.assertEqual(__import__("json").loads(updated_path.read_text(encoding="utf-8"))["title"], "新标题")
            self.assertEqual(__import__("json").loads(updated_path.with_suffix(".tags").read_text(encoding="utf-8")), patch["tags"])

    def test_metadata_override_rejects_stale_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parsed = root / "parsed"
            config = root / "config"
            book = parsed / "pub" / "book"
            articles = book / "art"
            articles.mkdir(parents=True)
            (book / "publication.metadata").write_text("{}", encoding="utf-8")
            (articles / "oldid.json").write_text('{"title":"different"}', encoding="utf-8")
            override_dir = config / "metadata_overrides" / "publication"
            override_dir.mkdir(parents=True)
            (override_dir / "oldid.json").write_text(__import__("json").dumps({
                "version": 1, "publication_id": "publication", "article_id": "oldid",
                "new_article_id": "newid",
                "article": {"title": "expected", "authors": [], "dates": [], "is_range_date": False},
                "metadata": {"title": "新标题"},
            }, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "source identity changed"):
                build_fork_parsed.apply_metadata_overrides(parsed, config)

    def test_override_patch_is_mapped_to_parser_source_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patch_input = root / "patch"
            config = root / "config"
            patch_input.mkdir()
            override_dir = config / "metadata_overrides" / "publication"
            override_dir.mkdir(parents=True)
            (override_dir / "oldarticle.json").write_text(__import__("json").dumps({
                "version": 1, "publication_id": "publication", "article_id": "oldarticle",
                "new_article_id": "1234567890", "article": {}, "metadata": {"title": "新"},
            }), encoding="utf-8")
            new_patch = patch_input / "[1234567890][publication].ts"
            old_patch = patch_input / "[oldarticle][publication].ts"
            new_patch.write_text("new patch", encoding="utf-8")
            old_patch.write_text("stale patch", encoding="utf-8")

            build_fork_parsed.map_override_patches(patch_input, config)

            self.assertEqual(old_patch.read_text(encoding="utf-8"), "new patch")

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
