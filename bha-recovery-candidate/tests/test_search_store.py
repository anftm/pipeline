import tempfile
import unittest
import inspect
from pathlib import Path
from unittest.mock import patch

from app import doc_store, search_store
from app.search_logic import local_snippets


class SearchStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "docs.sqlite"
        self.original_connect = doc_store.connect

    def tearDown(self):
        self.directory.cleanup()

    def write(self, doc_id: str, content: str) -> None:
        doc = {
            "doc_id": doc_id,
            "publication_name": "测试刊物",
            "publication_type": "期刊",
            "title": "测试标题",
            "authors": ["作者"],
            "tag_names": ["标签"],
            "date_display": ["1966/5/16"],
            "content": content,
        }
        with self.original_connect(self.db_path) as connection:
            doc_store.init_db(connection)
            doc_store.write_docs_to_conn(connection, [doc])
            connection.commit()

    def read(self, query: str, fulltext: bool = True, exact: bool = True):
        with patch.object(doc_store, "connect_readonly", side_effect=lambda: self.original_connect(self.db_path)):
            return search_store.get_search_docs(["doc-1"], query, fulltext, exact)["doc-1"]

    def test_exact_query_returns_only_late_match_window(self):
        content = "前" * 500 + "文化革命" + "后" * 500
        self.write("doc-1", content)
        result = self.read("文化革命")
        self.assertLessEqual(len(result["content"]), 80 + len("文化革命") + 140)
        self.assertIn("文化革命", result["content"])
        self.assertGreater(result["content_window_start"], 1)
        self.assertEqual(result["content_length"], len(content))
        snippet = local_snippets(
            result["content"], "文化革命", True,
            result["content_window_start"], result["content_length"],
        )[0]
        self.assertTrue(snippet.startswith("..."))
        self.assertTrue(snippet.endswith("..."))
        self.assertIn("<mark>文化革命</mark>", snippet)

    def test_normal_query_uses_earliest_token_window(self):
        content = "前" * 400 + "革命" + "中" * 30 + "文化" + "后" * 400
        self.write("doc-1", content)
        result = self.read("文化 革命", exact=False)
        self.assertIn("革命", result["content"])
        self.assertLess(len(result["content"]), len(content))

    def test_metadata_only_and_missing_match_return_prefix(self):
        content = "开" * 300 + "正文后部"
        self.write("doc-1", content)
        metadata_only = self.read("任意", fulltext=False)
        missing = self.read("不存在")
        self.assertEqual(metadata_only["content"], content[:220])
        self.assertEqual(missing["content"], content[:220])
        self.assertEqual(metadata_only["content_window_start"], 1)

    def test_sqlite_failure_is_explicit(self):
        with patch.object(doc_store, "connect_readonly", side_effect=OSError("unavailable")):
            with self.assertRaises(search_store.SearchStoreError):
                search_store.get_search_docs(["doc-1"], "手机", True, True)

    def test_literal_verification_distinguishes_punctuation(self):
        self.write("doc-1", "正文包含手。机字样")
        self.write("doc-2", "正文只包含手机字样")
        with patch.object(doc_store, "connect_readonly", side_effect=lambda: self.original_connect(self.db_path)):
            matches = search_store.literal_match_ids(["doc-1", "doc-2"], "手。机")
        self.assertEqual(matches, {"doc-1"})

    def test_literal_verification_keeps_latin_case_insensitive(self):
        self.write("doc-1", "正文包含ABC字样")
        with patch.object(doc_store, "connect_readonly", side_effect=lambda: self.original_connect(self.db_path)):
            matches = search_store.literal_match_ids(["doc-1"], "abc")
        self.assertEqual(matches, {"doc-1"})

    def test_literal_row_order_preserves_unicode_nul_and_all_metadata_fields(self):
        docs = [
            {"doc_id": "content", "content": "前\0革命。𠀀！后"},
            {"doc_id": "title", "title": "革命。"},
            {"doc_id": "source", "publication_name": "革命。"},
            {"doc_id": "author", "authors": ["革命。"]},
            {"doc_id": "tag", "tag_names": ["革命。"]},
            {"doc_id": "boundary", "content": "革命 。"},
        ]
        with self.original_connect(self.db_path) as conn:
            doc_store.init_db(conn)
            doc_store.write_docs_to_conn(conn, docs)
            conn.commit()
        ids = [doc["doc_id"] for doc in docs] + ["missing", "content"]
        with patch.object(doc_store, "connect_readonly", side_effect=lambda: self.original_connect(self.db_path)):
            for row_order in (False, True):
                self.assertEqual(search_store.literal_match_ids(ids, "革命。", batch_size=2, row_order=row_order), {"content", "title", "source", "author", "tag"})
                self.assertEqual(search_store.literal_match_ids(ids, "𠀀！", row_order=row_order), {"content"})
                self.assertEqual(search_store.literal_match_ids(ids, "\0革命。", row_order=row_order), {"content"})

    def test_literal_row_order_supports_utf16_and_unicode_casefold(self):
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA encoding='UTF-16le'")
            doc_store.init_db(conn)
            doc_store.write_docs_to_conn(conn, [{"doc_id": "utf16", "content": "前革命。后 Straße!"}])
            conn.commit()
        with patch.object(doc_store, "connect_readonly", side_effect=lambda: self.original_connect(self.db_path)):
            for row_order in (False, True):
                self.assertEqual(search_store.literal_match_ids(["utf16"], "革命。", row_order=row_order), {"utf16"})
                self.assertEqual(search_store.literal_match_ids(["utf16"], "STRASSE!", row_order=row_order), {"utf16"})

    def test_literal_verification_reads_bodies_in_row_order_without_duplicate_reads(self):
        docs = [{"doc_id": doc_id, "content": f"body-{doc_id}目标。"} for doc_id in ("B", "C", "A")]
        with self.original_connect(self.db_path) as conn:
            doc_store.init_db(conn)
            doc_store.write_docs_to_conn(conn, docs)
            conn.commit()
        calls = []
        def connect():
            conn = self.original_connect(self.db_path)
            def instr(value, needle):
                text = str(value or "")
                if text.startswith("body-"):
                    calls.append(text)
                return text.find(needle) + 1
            conn.create_function("instr", 2, instr)
            return conn
        with patch.object(doc_store, "connect_readonly", side_effect=connect):
            matches = search_store.literal_match_ids(["C", "A", "B", "A", "missing"], "目标。", batch_size=2, row_order=True)
        self.assertEqual(matches, {"A", "B", "C"})
        self.assertEqual(calls, ["body-B目标。", "body-C目标。", "body-A目标。"])

    def test_literal_verification_uses_candidate_sized_batches(self):
        default = inspect.signature(search_store.literal_match_ids).parameters["batch_size"].default
        self.assertEqual(default, 500)

    def test_prefix_first_location_preserves_early_match_window(self):
        content = "前" * 100 + "文化革命" + "后" * 500
        self.write("doc-1", content)
        result = self.read("文化革命")
        self.assertEqual(result["content_window_start"], 21)
        self.assertIn("文化革命", result["content"])

    def test_match_location_is_evaluated_once_per_document(self):
        self.write("doc-1", "前" * 500 + "文化革命" + "后" * 500)
        self.write("doc-2", "没有匹配" * 250)
        calls = []

        def connect():
            conn = self.original_connect(self.db_path)
            def counted_instr(content, needle):
                calls.append(needle)
                return content.find(needle) + 1
            conn.create_function("instr", 2, counted_instr)
            return conn

        with patch.object(doc_store, "connect_readonly", side_effect=connect):
            results = search_store.get_search_docs(["doc-2", "missing", "doc-1"], "文化革命", True, True)
        self.assertEqual(list(results), ["doc-2", "doc-1"])
        self.assertEqual(calls, ["文化革命", "文化革命"])
        self.assertEqual(results["doc-1"]["content_window_start"], 421)
        self.assertEqual(results["doc-1"]["content_length"], 1004)
        self.assertIn("文化革命", results["doc-1"]["content"])
        self.assertEqual(results["doc-2"]["content_window_start"], 1)
        self.assertEqual(results["doc-2"]["content"], ("没有匹配" * 250)[:220])


if __name__ == "__main__":
    unittest.main()
