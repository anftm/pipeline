import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import config, data_loader


class DataLoaderTests(unittest.TestCase):
    def test_legacy_repo_prefix_cannot_override_anftm_parsed_source(self):
        self.assertEqual(
            config.parsed_repo_prefix({"REPO_PREFIX": "https://github.com/banned-historical-archives"}),
            "https://github.com/anftm",
        )
        self.assertEqual(
            config.parsed_repo_prefix({"BHA_PARSED_REPO_PREFIX": "https://example.com/mirror/"}),
            "https://example.com/mirror",
        )

    def test_search_tokenizer_initialization_does_not_require_a_query(self):
        with patch.object(data_loader.jieba, "initialize") as initialize:
            data_loader.initialize_search_tokenizer()
        initialize.assert_called_once_with()

    def test_search_tokenization_reuses_normalized_queries(self):
        data_loader._search_words.cache_clear()
        with patch.object(data_loader.jieba, "lcut", return_value=["手机"]) as tokenize:
            self.assertEqual(data_loader.search_words("手机"), "手机")
            self.assertEqual(data_loader.search_words("手机"), "手机")
        tokenize.assert_called_once_with("手机")
        data_loader._search_words.cache_clear()

    def test_read_json_fails_with_path_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, str(path)):
                data_loader.read_json(path)

    def test_doc_ids_include_archive_and_publication_name_is_tokenized_once(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed_root = Path(directory)
            book_root = parsed_root / "archives3" / "group" / "book"
            article_root = book_root / "articles"
            article_root.mkdir(parents=True)
            (book_root / "publication.metadata").write_text(
                json.dumps({"name": "重复刊名"}, ensure_ascii=False), encoding="utf-8"
            )
            for article_id in ("one", "two"):
                (article_root / f"{article_id}.json").write_text(
                    json.dumps({"title": article_id}, ensure_ascii=False), encoding="utf-8"
                )
            calls = []

            def tokenize(value):
                calls.append(value)
                return str(value)

            with patch.object(data_loader, "PARSED_ROOT", parsed_root), \
                    patch.object(data_loader, "ARCHIVE_START", 3), \
                    patch.object(data_loader, "ARCHIVE_END", 3), \
                    patch.object(data_loader, "search_words", side_effect=tokenize):
                docs = list(data_loader.iter_documents())

        self.assertEqual(len(docs), 2)
        self.assertTrue(all(doc["doc_id"].startswith("3:") for doc in docs))
        self.assertEqual(calls.count("重复刊名"), 1)

    def test_parsed_data_reports_archive_progress(self):
        progress = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(data_loader, "PARSED_ROOT", Path(directory)), \
                patch.object(data_loader, "ARCHIVE_START", 2), \
                patch.object(data_loader, "ARCHIVE_END", 3), \
                patch.object(data_loader, "run"):
            data_loader.ensure_parsed_data(lambda current, total: progress.append((current, total)))
        self.assertEqual(progress, [(1, 2), (2, 2)])

    def test_remote_archive_commits_use_bounded_timeout(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(kwargs)
            archive_id = int(command[2].split("archives", 1)[1].split(".", 1)[0])
            return SimpleNamespace(stdout=f"{'a' * 39}{archive_id % 10} refs/heads/parsed\n")

        with patch.object(data_loader.subprocess, "run", side_effect=fake_run):
            revisions = data_loader.parsed_archive_commits(remote=True, archive_ids=[0, 1, 2])
        self.assertEqual(set(revisions), {0, 1, 2})
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call["timeout"] == data_loader.REMOTE_GIT_TIMEOUT_SECONDS for call in calls))

    def test_article_text_preserves_description_parts_and_comments_order(self):
        article = {
            "description": "说明",
            "parts": [{"text": "第一段"}, {"ignored": "x"}, {"text": "第二段"}],
            "comments": ["评论一", "", "评论二"],
        }
        self.assertEqual(data_loader.text_from_article(article), "说明\n第一段\n第二段\n评论一\n评论二")

    def test_date_and_tag_normalization_preserves_all_filter_shapes(self):
        article = {"dates": [
            {"year": 1966, "month": 5, "day": 16},
            {"year": 1967},
            {"month": 6, "day": 1},
            "invalid",
        ]}
        values = data_loader.normalize_dates(article)
        self.assertEqual(values[0], 19660516)
        self.assertEqual(values[1], 19670101)
        self.assertEqual(values[4], [1966, 1967])
        self.assertIn("05-16", values[7])
        self.assertIn("1966-05", values[8])
        self.assertIn("1966-16", values[9])
        self.assertIn("1966-05-16", values[10])
        names, types = data_loader.normalize_tags([
            {"name": " 标签 ", "type": " 人物 "}, {}, "invalid",
        ])
        self.assertEqual(names, ["标签"])
        self.assertEqual(types, ["人物"])

    def test_malformed_nested_metadata_does_not_break_normalization(self):
        self.assertEqual(data_loader.text_from_article({"parts": [None, {"text": "正文"}], "comments": [None, "注"]}), "正文\n注")
        dates = data_loader.normalize_dates({"dates": [None, [], {"year": 1966, "month": "5"}]})
        self.assertEqual(dates[0], 19660101)
        self.assertEqual(data_loader.normalize_tags([None, [], {"name": " 标签 ", "type": None}]), (["标签"], []))

    def test_timeout_environment_has_safe_default(self):
        with patch.dict("os.environ", {"BHA_REMOTE_GIT_TIMEOUT_SECONDS": "invalid"}):
            self.assertEqual(data_loader.remote_git_timeout(), 30)
        with patch.dict("os.environ", {"BHA_REMOTE_GIT_TIMEOUT_SECONDS": "0"}):
            self.assertEqual(data_loader.remote_git_timeout(), 1)

    def test_iter_documents_builds_complete_search_and_download_record(self):
        with tempfile.TemporaryDirectory() as directory:
            parsed_root = Path(directory)
            book_root = parsed_root / "archives7" / "group" / "book"
            article_root = book_root / "articles"
            article_root.mkdir(parents=True)
            (book_root / "publication.metadata").write_text(json.dumps({
                "name": "测试刊物", "author": "出版者", "type": "期刊",
                "files": ["https://example.com/a.pdf"],
            }, ensure_ascii=False), encoding="utf-8")
            article = {
                "title": "手机资料", "authors": ["作者甲"], "description": "说明",
                "parts": [{"text": "正文"}], "dates": [{"year": 1966, "month": 5, "day": 16}],
            }
            (article_root / "abc.json").write_text(json.dumps(article, ensure_ascii=False), encoding="utf-8")
            (article_root / "abc.tags").write_text(json.dumps([{"name": "标签甲", "type": "主题"}], ensure_ascii=False), encoding="utf-8")
            with patch.object(data_loader, "PARSED_ROOT", parsed_root), \
                    patch.object(data_loader, "ARCHIVE_START", 7), patch.object(data_loader, "ARCHIVE_END", 7):
                docs = list(data_loader.iter_documents())
                document_ids = set(data_loader.iter_document_ids([7]))
        self.assertEqual(len(docs), 1)
        self.assertEqual(document_ids, {"7:3:abc:publication"})
        doc = docs[0]
        self.assertEqual(doc["doc_id"], "7:3:abc:publication")
        self.assertEqual(doc["content"], "说明\n正文")
        self.assertEqual(doc["source_files"], ["https://example.com/a.pdf"])
        self.assertEqual(doc["tag_names"], ["标签甲"])
        self.assertEqual(doc["date_year_month_days"], ["1966-05-16"])


if __name__ == "__main__":
    unittest.main()
