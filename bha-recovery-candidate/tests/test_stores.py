import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from app import doc_store, facet_store


def sample_doc(doc_id="doc-1", publication="刊物甲", title="共同标题", years=None):
    return {
        "doc_id": doc_id,
        "article_id": f"article-{doc_id}",
        "publication_id": f"publication-{publication}",
        "publication_name": publication,
        "publication_type": "期刊",
        "archive_id": 3,
        "title": title,
        "authors": ["作者甲"],
        "tag_names": ["标签甲"],
        "date_display": ["1966/5/16"],
        "date_years": years or [1966],
        "source_files": ["https://example.com/source.pdf"],
        "content": "开头文化革命正文结尾",
        "path": "group/book/article.json",
    }


class DocumentStoreTests(unittest.TestCase):
    def test_readonly_connections_are_query_only_and_use_the_mmap_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "docs.sqlite"
            doc_store.init_db(doc_store.connect(path))
            with patch.object(doc_store, "SQLITE_MMAP_BYTES", 64 * 1024 * 1024):
                conn = doc_store.connect_readonly(path)
            try:
                self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA mmap_size").fetchone()[0], 64 * 1024 * 1024)
            finally:
                conn.close()

    def test_mmap_window_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "docs.sqlite"
            doc_store.init_db(doc_store.connect(path))
            with patch.object(doc_store, "SQLITE_MMAP_BYTES", 0):
                conn = doc_store.connect_readonly(path)
            try:
                self.assertEqual(conn.execute("PRAGMA mmap_size").fetchone()[0], 0)
            finally:
                conn.close()


    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "docs.sqlite"
        self.original_connect = doc_store.connect

    def tearDown(self):
        self.directory.cleanup()

    def patched_connect(self):
        return patch.object(doc_store, "connect_readonly", side_effect=lambda: self.original_connect(self.db_path))

    def write(self, docs):
        with self.original_connect(self.db_path) as connection:
            doc_store.init_db(connection)
            doc_store.write_docs_to_conn(connection, docs)
            connection.commit()

    def test_round_trip_batch_prefix_years_and_json_fields(self):
        docs = [sample_doc(), sample_doc("doc-2", "刊物乙", years=[1967])]
        self.write(docs)
        with self.patched_connect():
            one = doc_store.get_doc("doc-1")
            years = doc_store.available_years()
        self.assertEqual(one["authors"], ["作者甲"])
        self.assertEqual(one["source_file_count"], 1)
        self.assertEqual(years, [1966, 1967])
        with self.original_connect(self.db_path) as connection:
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(docs)")}
        self.assertIn("idx_docs_archive_id", indexes)

    def test_replace_random_warmup_and_missing_document(self):
        self.write([sample_doc()])
        replacement = sample_doc()
        replacement["content"] = "替换后的内容"
        self.write([replacement])
        with self.patched_connect(), patch.object(doc_store.random, "randint", return_value=1):
            self.assertEqual(doc_store.get_doc("doc-1")["content"], "替换后的内容")
            self.assertEqual(doc_store.random_doc()["doc_id"], "doc-1")
            self.assertTrue(doc_store.warmup_db())
            self.assertIsNone(doc_store.get_doc("missing"))
    def test_delete_removes_document(self):
        self.write([sample_doc()])
        with self.original_connect(self.db_path) as connection:
            doc_store.init_db(connection)
            doc_store.delete_docs_to_conn(connection, ["doc-1"])
            connection.commit()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM docs").fetchone()[0], 0)

    def test_variants_are_unique_by_publication_and_exclude_current(self):
        self.write([
            sample_doc("doc-1", "刊物甲"),
            sample_doc("doc-2", "刊物乙"),
            sample_doc("doc-3", "刊物乙"),
            sample_doc("doc-4", "刊物丙"),
            sample_doc("doc-5", "刊物丁", title="其他标题"),
        ])
        with self.patched_connect():
            variants = doc_store.variants_for_title("共同标题", "doc-1", "刊物甲")
        self.assertEqual([item["publication_name"] for item in variants], ["刊物丙", "刊物乙"])
        self.assertNotIn("doc-1", {item["doc_id"] for item in variants})

    def test_serving_connection_is_read_only(self):
        self.write([sample_doc()])
        with doc_store.connect_readonly(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM docs").fetchone()[0], 1)
            with self.assertRaises(Exception):
                connection.execute("DELETE FROM docs")

    def test_read_failures_return_stable_empty_values(self):
        with patch.object(doc_store, "connect_readonly", side_effect=OSError("unavailable")):
            self.assertIsNone(doc_store.get_doc("doc-1"))
            self.assertEqual(doc_store.parse_queue_docs(25, "待拆分", 1, 20), (0, []))
            self.assertIsNone(doc_store.random_doc())
            self.assertEqual(doc_store.variants_for_title("标题", "doc-1", "刊物"), [])
            self.assertEqual(doc_store.available_years(), [])

    def test_empty_year_table_falls_back_to_document_json(self):
        self.write([sample_doc()])
        with self.original_connect(self.db_path) as connection:
            connection.execute("DELETE FROM years")
            connection.commit()
        with self.patched_connect():
            self.assertEqual(doc_store.available_years(), [1966])


class FacetStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "facets.sqlite"
        self.original_connect = facet_store.connect

    def tearDown(self):
        self.directory.cleanup()

    def patched_connect(self):
        return patch.object(facet_store, "connect_readonly", side_effect=lambda *_args, **_kwargs: self.original_connect(self.db_path))

    def test_write_exact_counts_pagination_and_literal_like_search(self):
        counters = {
            "source": Counter({"甲%刊": 3, "甲_刊": 2, "乙刊": 1}),
            "type": Counter({"期刊": 6}),
            "archive": Counter({"3": 6}),
        }
        facet_store.write_facets(counters, self.db_path)
        with self.patched_connect():
            first = facet_store.list_facets("source", page=1, page_size=2)
            second = facet_store.list_facets("source", page=2, page_size=2)
            percent = facet_store.list_facets("source", keyword="%")
            underscore = facet_store.list_facets("source", keyword="_")
            sources = facet_store.sources_payload()
        self.assertEqual(first["total"], 3)
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual([item["name"] for item in percent["items"]], ["甲%刊"])
        self.assertEqual([item["name"] for item in underscore["items"]], ["甲_刊"])
        self.assertEqual(sources["archives"][0]["value"], 3)
        with self.original_connect(self.db_path) as connection:
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(facets)")}
        self.assertIn("idx_facets_kind_name_nocase", indexes)

    def test_deltas_add_remove_skip_zero_and_strip_empty_names(self):
        facet_store.write_facets({"source": Counter({"甲": 2, "乙": 1})}, self.db_path)
        removed = {"source": Counter({"甲": 1, "乙": 1, "unchanged": 1})}
        added = {"source": Counter({"丙": 2, "unchanged": 1, " ": 99})}
        facet_store.apply_facet_deltas(removed, added, self.db_path)
        with self.original_connect(self.db_path) as connection:
            rows = [tuple(row) for row in connection.execute("SELECT name, count FROM facets ORDER BY name")]
        self.assertEqual(rows, [("丙", 2), ("甲", 1)])

    def test_unknown_kind_and_bounds_have_stable_shape(self):
        with self.patched_connect():
            unknown = facet_store.list_facets("missing", page=-1, page_size=999)
        self.assertEqual(unknown["items"], [])
        self.assertEqual(unknown["total"], 0)

    def test_serving_connection_is_read_only(self):
        facet_store.write_facets({"source": Counter({"甲刊": 1})}, self.db_path)
        with facet_store.connect_readonly(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM facets").fetchone()[0], 1)
            with self.assertRaises(Exception):
                connection.execute("DELETE FROM facets")

    def test_read_failure_returns_stable_empty_shape(self):
        with patch.object(facet_store, "connect_readonly", side_effect=OSError("unavailable")):
            result = facet_store.list_facets("source")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)


if __name__ == "__main__":
    unittest.main()
