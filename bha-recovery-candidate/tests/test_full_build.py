"""Exercise full index activation with temporary sidecars and a simulated ES."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import indexer


class FullBuildTests(unittest.TestCase):
    def run_build(self, *, progressive=True, failures=(), docs=None, create_error=None, merge_error=None):
        documents = docs if docs is not None else [
            {"doc_id": "1:1:a:p", "title": "文章甲", "archive_id": 1,
             "publication_name": "来源", "publication_type": "期刊", "authors": ["甲"],
             "tag_names": ["标签"], "date_display": ["1966/5/16"], "date_years": [1966],
             "content": "正文甲", "article": {"parts": [{"text": "正文甲"}]}},
            {"doc_id": "1:1:b:p", "title": "文章乙", "archive_id": 1,
             "publication_name": "来源", "publication_type": "期刊", "authors": ["乙"],
             "tag_names": ["标签"], "date_display": ["1967/1/1"], "date_years": [1967],
             "content": "正文乙", "article": {"parts": [{"text": "正文乙"}]}},
        ]
        root = Path(self.temp.name)
        docs_db, facets_db, status = root / "docs.sqlite", root / "facets.sqlite", root / "status"
        es = MagicMock()
        es.indices.exists.return_value = False
        es.indices.create.side_effect = create_error
        es.options.return_value = es
        es.indices.forcemerge.side_effect = merge_error
        es.get.return_value = {"_source": {}}
        emitted = []

        def bulk(_client, actions, **kwargs):
            self.assertEqual(kwargs["retry_on_status"], (429, 502, 503, 504))
            for index, action in enumerate(actions):
                emitted.append(action)
                yield index not in failures, {"index": {"_id": action["_id"]}}

        observed = {}

        def activate(_es, build_index, temp_docs, temp_facets):
            with sqlite3.connect(temp_docs) as connection:
                observed["doc_ids"] = {row[0] for row in connection.execute("SELECT doc_id FROM docs")}
                observed["years"] = {row[0] for row in connection.execute("SELECT year FROM years")}
            with sqlite3.connect(temp_facets) as connection:
                observed["sources"] = list(connection.execute("SELECT name,count FROM facets WHERE kind='source'"))
            observed["build_index"] = build_index

        with ExitStack() as stack:
            for name, value in (("DOC_DB", docs_db), ("FACET_DB", facets_db),
                                ("INDEX_STATUS_PATH", status), ("SWITCH_STATE_PATH", root / "switch.json"),
                                ("RESET_INDEX", False)):
                stack.enter_context(patch.object(indexer, name, value))
            for name, value in (("client", es), ("recover_interrupted_switch", None),
                                ("ensure_parsed_data", None), ("parsed_archive_revisions", {1: "a" * 40}),
                                ("parsed_corpus_fingerprint", "f" * 64), ("serving_data_available", not progressive)):
                stack.enter_context(patch.object(indexer, name, return_value=value))
            progress = stack.enter_context(patch.object(indexer, "write_progress"))
            start = stack.enter_context(patch.object(indexer, "start_progressive_build"))
            discard = stack.enter_context(patch.object(indexer, "discard_progressive_build"))
            activation = stack.enter_context(patch.object(indexer, "activate_build", side_effect=activate))
            stack.enter_context(patch.object(indexer, "iter_documents", return_value=iter(documents)))
            stack.enter_context(patch.object(indexer.helpers, "streaming_bulk", side_effect=bulk))
            try:
                value = indexer._ensure_index(reset=not progressive)
                error = None
            except Exception as exc:
                value, error = None, exc
        return {"root": root, "value": value, "error": error, "es": es, "emitted": emitted,
                "start": start, "discard": discard, "activation": activation, "status": status,
                "progress": progress, "observed": observed, "docs_db": docs_db, "facets_db": facets_db}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bha-full-build-")
        self.addCleanup(self.temp.cleanup)

    def test_progressive_full_build_indexes_original_text_and_metadata(self):
        result = self.run_build()
        self.assertIsNone(result["error"])
        self.assertTrue(result["value"])
        result["start"].assert_called_once()
        result["discard"].assert_not_called()
        result["activation"].assert_not_called()
        self.assertEqual(result["status"].read_text(), "ready")
        self.assertEqual([action["_id"] for action in result["emitted"]], ["1:1:a:p", "1:1:b:p"])
        self.assertTrue(all("article" not in action["_source"] for action in result["emitted"]))
        with sqlite3.connect(result["docs_db"]) as connection:
            rows = connection.execute("SELECT doc_id,content FROM docs ORDER BY doc_id").fetchall()
            self.assertEqual(rows, [("1:1:a:p", "正文甲"), ("1:1:b:p", "正文乙")])
            self.assertEqual({row[0] for row in connection.execute("SELECT year FROM years")}, {1966, 1967})
        with sqlite3.connect(result["facets_db"]) as connection:
            self.assertEqual(connection.execute("SELECT count FROM facets WHERE kind='source' AND name='来源'").fetchone()[0], 2)

    def test_full_rebuild_activates_complete_sidecars_even_if_forcemerge_fails(self):
        result = self.run_build(progressive=False, merge_error=RuntimeError("merge unavailable"))
        self.assertIsNone(result["error"])
        self.assertTrue(result["value"])
        result["activation"].assert_called_once()
        result["discard"].assert_not_called()
        self.assertEqual(result["status"].read_text(), "ready")
        self.assertEqual(result["observed"]["doc_ids"], {"1:1:a:p", "1:1:b:p"})
        self.assertEqual(result["observed"]["years"], {1966, 1967})
        self.assertEqual(result["observed"]["sources"], [("来源", 2)])

    def test_bulk_failure_discards_progressive_generation(self):
        result = self.run_build(failures=(1,))
        self.assertIsInstance(result["error"], RuntimeError)
        self.assertIn("bulk indexing failed with 1 errors", str(result["error"]))
        result["discard"].assert_called_once()
        result["activation"].assert_not_called()
        self.assertFalse(result["status"].exists())
        self.assertIn("failed", [call.kwargs.get("status") for call in result["progress"].call_args_list])

    def test_duplicate_document_id_fails_before_generation_activation(self):
        docs = [{"doc_id": "duplicate", "title": "甲"}, {"doc_id": "duplicate", "title": "乙"}]
        result = self.run_build(progressive=False, docs=docs)
        self.assertIsInstance(result["error"], RuntimeError)
        self.assertIn("duplicate doc_id likely", str(result["error"]))
        result["activation"].assert_not_called()
        result["es"].indices.delete.assert_called_once()
        self.assertFalse(result["status"].exists())

    def test_index_creation_failure_cleans_provisional_build(self):
        result = self.run_build(progressive=False, create_error=RuntimeError("ES unavailable"))
        self.assertIsInstance(result["error"], RuntimeError)
        self.assertEqual(str(result["error"]), "ES unavailable")
        result["es"].indices.delete.assert_called_once()
        result["activation"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
