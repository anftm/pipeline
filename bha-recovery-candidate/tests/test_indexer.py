import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from app import indexer
from app.doc_store import connect as connect_doc_db, init_db as init_doc_db, write_docs_to_conn
from app.facet_store import write_facets


class IndexActivationTests(unittest.TestCase):
    def setUp(self):
        self.serving_lock_patch = patch.object(indexer, "serving_lock", return_value=nullcontext())
        self.serving_lock_patch.start()
        self.addCleanup(self.serving_lock_patch.stop)

    def test_typed_document_and_index_mapping_contract_is_stable(self):
        document = {"doc_id": "1:a:p", "title": "标题", "article": {"parts": []}, "authors": ["甲"]}
        es_doc = indexer.elasticsearch_document(document)
        self.assertEqual(es_doc, {"doc_id": "1:a:p", "title": "标题", "authors": ["甲"]})
        self.assertIn("article", document)
        self.assertEqual(indexer.index_mapping()["mappings"]["_source"]["excludes"], ["content", "source_files", "*_words", "*_literal"])

    def test_index_mapping_remains_json_identical_when_copied_for_sdk(self):
        mapping = indexer.index_mapping()
        copied = dict(mapping)
        self.assertEqual(json.dumps(mapping, sort_keys=True), json.dumps(copied, sort_keys=True))
        self.assertEqual(mapping["settings"]["index"]["max_result_window"], indexer.MAX_RESULT_WINDOW)
        self.assertEqual(mapping["settings"]["index"]["number_of_replicas"], 0)

    def test_forcemerge_uses_transport_timeout_not_a_request_field(self):
        from elasticsearch import Elasticsearch

        client = Elasticsearch("http://localhost:9200").options(request_timeout=3600)
        with patch.object(client, "perform_request", return_value={"acknowledged": True}) as request:
            client.indices.forcemerge(index="article-test", max_num_segments=1)
        self.assertEqual(client._request_timeout, 3600)
        self.assertEqual(request.call_args.args[:2], ("POST", "/article-test/_forcemerge"))
        self.assertNotIn("request_timeout", request.call_args.kwargs.get("query", {}))

    def test_alias_switch_keeps_one_new_index_and_removes_old(self):
        es = MagicMock()
        es.indices.get_alias.return_value = {"article-old": {"aliases": {"article": {}}}}
        es.indices.exists.return_value = True
        es.indices.exists_alias.return_value = True
        indexer.activate_index(es, "article-new")
        es.indices.update_aliases.assert_called_once_with(actions=[
            {"remove": {"index": "article-old", "alias": indexer.INDEX_NAME}},
            {"add": {"index": "article-new", "alias": indexer.INDEX_NAME}},
        ])
        es.indices.delete.assert_called_once_with(index=["article-old"], ignore_unavailable=True)
    def test_clone_waits_for_target_primary_before_incremental_writes(self):
        es = MagicMock()
        es.indices.clone.return_value = {"acknowledged": True, "shards_acknowledged": False}
        es.cluster.health.return_value = {"status": "yellow", "timed_out": False}

        indexer.clone_serving_index(es, "article-old", "article-new")

        es.indices.clone.assert_called_once_with(
            index="article-old",
            target="article-new",
            body={"settings": {"index.blocks.write": False}},
            wait_for_active_shards="1",
            timeout="120s",
        )
        es.cluster.health.assert_called_once_with(
            index="article-new",
            wait_for_status="yellow",
            wait_for_no_initializing_shards=True,
            timeout="120s",
        )
        self.assertEqual(
            es.indices.put_settings.call_args_list,
            [
                unittest.mock.call(index="article-old", settings={"index.blocks.write": True}),
                unittest.mock.call(index="article-old", settings={"index.blocks.write": False}),
            ],
        )

    def test_ensure_index_uses_incremental_build_for_changed_revisions(self):
        es = MagicMock()
        es.indices.exists.return_value = True
        es.count.return_value = {"count": 10}
        es.get.return_value = {"_source": {"archive_revisions": {}}}
        revisions = {archive_id: "1" * 40 for archive_id in range(32)}
        with patch.object(indexer, "client", return_value=es), \
                patch.object(indexer, "write_progress"), \
                patch.object(indexer, "recover_interrupted_switch"), \
                patch.object(indexer, "ensure_parsed_data"), \
                patch.object(indexer, "parsed_archive_revisions", return_value=revisions), \
                patch.object(indexer, "parsed_corpus_fingerprint", return_value="a" * 64), \
                patch.object(indexer, "serving_data_available", return_value=True), \
                patch.object(indexer, "metadata_matches", return_value=False), \
                patch.object(indexer, "incremental_build") as incremental:
            changed = indexer._ensure_index()
        self.assertTrue(changed)
        incremental.assert_called_once_with(
            es, "a" * 64, revisions, {"archive_revisions": {}}
        )

    def test_cold_incremental_syncs_only_archives_with_changed_trees(self):
        es = MagicMock()
        es.indices.exists.return_value = True
        es.count.return_value = {"count": 10}
        old_revisions = {archive_id: "0" * 40 for archive_id in range(32)}
        new_revisions = dict(old_revisions)
        new_revisions[7] = "1" * 40
        old_meta = indexer.desired_metadata("a" * 64, old_revisions)
        es.get.return_value = {"_source": old_meta}
        commits = {archive_id: "2" * 40 for archive_id in range(32)}
        with patch.object(indexer, "client", return_value=es), \
                patch.object(indexer, "write_progress"), \
                patch.object(indexer, "recover_interrupted_switch"), \
                patch.object(indexer, "ensure_parsed_data") as ensure_data, \
                patch.object(indexer, "parsed_archive_commits", return_value={7: commits[7]}) as local_commits, \
                patch.object(indexer, "parsed_archive_revisions", return_value={7: new_revisions[7]}) as local_revisions, \
                patch.object(indexer, "parsed_corpus_fingerprint", return_value="b" * 64), \
                patch.object(indexer, "serving_data_available", return_value=True), \
                patch.object(indexer, "metadata_matches", return_value=False), \
                patch.object(indexer, "incremental_build") as incremental:
            changed = indexer._ensure_index(expected_commits=commits, expected_revisions=new_revisions)

        self.assertTrue(changed)
        self.assertEqual(ensure_data.call_args.kwargs["archive_ids"], [7])
        local_commits.assert_called_once_with(archive_ids=[7])
        local_revisions.assert_called_once_with(archive_ids=[7])
        incremental.assert_called_once_with(es, "b" * 64, new_revisions, old_meta)

    def test_incremental_failure_keeps_serving_generation_without_full_fallback(self):
        es = MagicMock()
        es.indices.exists.return_value = True
        es.count.return_value = {"count": 10}
        old_revisions = {archive_id: "0" * 40 for archive_id in range(32)}
        new_revisions = dict(old_revisions)
        new_revisions[7] = "1" * 40
        old_meta = indexer.desired_metadata("a" * 64, old_revisions)
        es.get.return_value = {"_source": old_meta}
        commits = {archive_id: "2" * 40 for archive_id in range(32)}
        with patch.object(indexer, "client", return_value=es), \
                patch.object(indexer, "write_progress"), \
                patch.object(indexer, "recover_interrupted_switch"), \
                patch.object(indexer, "ensure_parsed_data", return_value={7: {"article.json"}}) as ensure_data, \
                patch.object(indexer, "parsed_archive_commits", return_value={7: commits[7]}), \
                patch.object(indexer, "parsed_archive_revisions", return_value={7: new_revisions[7]}), \
                patch.object(indexer, "parsed_corpus_fingerprint", return_value="b" * 64), \
                patch.object(indexer, "serving_data_available", return_value=True), \
                patch.object(indexer, "metadata_matches", return_value=False), \
                patch.object(indexer, "incremental_build", side_effect=indexer.IncrementalUnsafe("clone failed")):
            with self.assertRaisesRegex(indexer.IncrementalUnsafe, "clone failed"):
                indexer._ensure_index(
                    expected_commits=commits,
                    expected_revisions=new_revisions,
                    changed_archives=[7],
                )

        ensure_data.assert_called_once()
        self.assertEqual(ensure_data.call_args.kwargs["archive_ids"], [7])
        es.indices.create.assert_not_called()

    def test_ensure_index_skips_all_writes_when_revisions_match(self):
        es = MagicMock()
        es.indices.exists.return_value = True
        es.count.return_value = {"count": 10}
        revisions = {archive_id: "1" * 40 for archive_id in range(32)}
        with patch.object(indexer, "client", return_value=es), \
                patch.object(indexer, "write_progress"), \
                patch.object(indexer, "recover_interrupted_switch"), \
                patch.object(indexer, "ensure_parsed_data"), \
                patch.object(indexer, "parsed_archive_revisions", return_value=revisions), \
                patch.object(indexer, "parsed_corpus_fingerprint", return_value="a" * 64), \
                patch.object(indexer, "serving_data_available", return_value=True), \
                patch.object(indexer, "sidecars_ready", return_value=True), \
                patch.object(indexer, "metadata_matches", return_value=True), \
                patch.object(indexer, "incremental_build") as incremental:
            changed = indexer._ensure_index()
        self.assertFalse(changed)
        incremental.assert_not_called()

    def test_incremental_build_replaces_only_changed_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs.sqlite"
            facets = root / "facets.sqlite"
            old_changed = {
                "doc_id": "1:3:old:publication", "article_id": "old",
                "publication_id": "publication", "publication_name": "旧来源",
                "publication_type": "书籍", "archive_id": 1, "title": "旧文",
                "authors": ["旧作者"], "tag_names": ["旧标签"],
                "date_display": ["1966/1/1"], "date_years": [1966], "content": "旧正文",
            }
            unchanged = {
                "doc_id": "1:4:keep:publication", "article_id": "keep",
                "publication_id": "publication", "publication_name": "保留来源",
                "publication_type": "书籍", "archive_id": 1, "title": "保留",
                "authors": [], "tag_names": [], "date_display": ["1970/1/1"],
                "date_years": [1970], "content": "保留正文",
            }
            replacement = {
                "doc_id": "1:3:new:publication", "article_id": "new",
                "publication_id": "publication", "publication_name": "新来源",
                "publication_type": "书籍", "archive_id": 1, "title": "新文",
                "authors": ["新作者"], "tag_names": ["新标签"],
                "date_display": ["1971/1/1"], "date_years": [1971], "content": "新正文",
            }
            with connect_doc_db(docs) as conn:
                init_doc_db(conn)
                write_docs_to_conn(conn, [old_changed, unchanged])
                conn.commit()
            counters = {kind: indexer.Counter() for kind in ("source", "author", "tag", "type", "archive")}
            for doc in (old_changed, unchanged):
                indexer.update_facets(counters, doc)
            write_facets(counters, facets)
            old_revisions = {archive_id: "0" * 40 for archive_id in range(32)}
            new_revisions = dict(old_revisions)
            new_revisions[1] = "1" * 40
            old_meta = indexer.desired_metadata("a" * 64, old_revisions)
            with connect_doc_db(docs) as conn:
                indexer.write_sidecar_metadata(conn, old_meta)
                conn.commit()
            indexer.write_facet_sidecar_metadata(facets, old_meta)
            bulk_actions = []

            def streaming_bulk(_client, actions, **_kwargs):
                bulk_actions.extend(actions)
                for action in bulk_actions:
                    yield True, action

            def activate(_es, _index, temp_docs, temp_facets):
                indexer.checkpoint_for_replace(temp_docs)
                with connect_doc_db(temp_docs) as conn:
                    ids = {row[0] for row in conn.execute("SELECT doc_id FROM docs")}
                    years = {row[0] for row in conn.execute("SELECT year FROM years")}
                with indexer.sqlite3.connect(temp_facets) as conn:
                    facet_rows = {row[0]: row[1] for row in conn.execute("SELECT name, count FROM facets WHERE kind = 'source'")}
                self.assertEqual(ids, {unchanged["doc_id"], replacement["doc_id"]})
                self.assertEqual(years, {1970, 1971})
                self.assertEqual(facet_rows["新来源"], 1)
                self.assertEqual(facet_rows["保留来源"], 1)
                self.assertNotIn("旧来源", facet_rows)

            es = MagicMock()
            es.count.return_value = {"count": 2}
            with patch.object(indexer, "DOC_DB", docs), \
                    patch.object(indexer, "FACET_DB", facets), \
                    patch.object(indexer, "INDEX_STATUS_PATH", root / "index-status"), \
                    patch.object(indexer, "SWITCH_STATE_PATH", root / "index-switch.json"), \
                     patch.object(indexer, "serving_physical_index", return_value="article-old"), \
                     patch.object(indexer, "clone_serving_index") as clone, \
                     patch.object(indexer.helpers, "streaming_bulk", side_effect=streaming_bulk) as bulk, \
                     patch.object(indexer, "activate_build", side_effect=activate) as activate_build, \
                     patch.object(indexer, "iter_document_ids", return_value={unchanged["doc_id"], replacement["doc_id"]}), \
                     patch.object(indexer, "iter_documents", return_value=iter([replacement, unchanged])) as iter_docs:
                indexer.incremental_build(
                    es, "b" * 64, new_revisions, old_meta,
                    changed_paths={1: {"group/book/articles/new.json"}},
                )

            clone.assert_called_once_with(es, "article-old", ANY)
            iter_docs.assert_called_once_with([1], changed_paths={1: {"group/book/articles/new.json"}})
            bulk_kwargs = bulk.call_args.kwargs
            self.assertEqual(bulk_kwargs["max_retries"], 8)
            self.assertEqual(bulk_kwargs["initial_backoff"], 2)
            self.assertEqual(bulk_kwargs["max_backoff"], 60)
            self.assertEqual(
                {(action["_op_type"], action["_id"]) for action in bulk_actions},
                {("delete", old_changed["doc_id"]), ("index", replacement["doc_id"])},
            )

    def test_ensure_index_enters_builder_with_supplied_lock(self):
        lock_file = MagicMock()
        with patch.object(indexer, "_ensure_index") as ensure:
            indexer.ensure_index(reset=True, lock_file=lock_file)
        ensure.assert_called_once_with(True)
        lock_file.close.assert_not_called()

    def test_serving_data_requires_es_and_both_sidecars(self):
        es = MagicMock()
        es.indices.exists.return_value = True
        es.count.return_value = {"count": 10}
        with patch.object(indexer, "sidecars_ready", return_value=False):
            self.assertFalse(indexer.serving_data_available(es))
        with patch.object(indexer, "sidecars_ready", return_value=True):
            self.assertTrue(indexer.serving_data_available(es))

    def test_metadata_contract_keeps_deployment_version_and_archive_bounds(self):
        desired = indexer.desired_metadata()
        self.assertEqual(desired["version"], "1.0.0")
        self.assertEqual(set(desired), {"version", "archive_start", "archive_end", "build_fingerprint"})
        es = MagicMock()
        source_fingerprint = "0" * 64
        es.get.return_value = {"_source": indexer.desired_metadata(source_fingerprint)}
        self.assertTrue(indexer.metadata_matches(es, source_fingerprint))
        es.get.return_value = {"_source": {**indexer.desired_metadata(source_fingerprint), "version": "wrong"}}
        self.assertFalse(indexer.metadata_matches(es, source_fingerprint))

    def test_build_fingerprint_changes_with_mapping(self):
        original = indexer.build_fingerprint()
        mapping = indexer.index_mapping()
        mapping["mappings"]["properties"]["content"]["index_phrases"] = False
        with patch.object(indexer, "index_mapping", return_value=mapping):
            self.assertNotEqual(indexer.build_fingerprint(), original)

    def test_build_fingerprint_changes_only_for_explicit_build_contracts(self):
        original = indexer.build_fingerprint()
        with patch.object(indexer, "INDEX_BUILD_SCHEMA_VERSION", "2"):
            self.assertNotEqual(indexer.build_fingerprint(), original)
        with patch.object(indexer, "DOC_STORE_SCHEMA_VERSION", "different"):
            self.assertNotEqual(indexer.build_fingerprint(), original)
        with patch.object(indexer, "__file__", "/unrelated/runtime/location.py"):
            self.assertEqual(indexer.build_fingerprint(), original)

    def test_facet_updates_count_each_composable_dimension(self):
        from collections import Counter
        counters = {kind: Counter() for kind in ("source", "author", "tag", "type", "archive")}
        indexer.update_facets(counters, {
            "publication_name": "刊物", "publication_type": "期刊", "archive_id": 3,
            "authors": ["作者", "作者", ""], "tag_names": ["标签"],
        })
        self.assertEqual(counters["source"]["刊物"], 1)
        self.assertEqual(counters["author"]["作者"], 2)
        self.assertEqual(counters["tag"]["标签"], 1)
        self.assertEqual(counters["type"]["期刊"], 1)
        self.assertEqual(counters["archive"]["3"], 1)

    def test_progressive_build_publishes_alias_before_bulk_indexing(self):
        es = MagicMock()
        with patch.object(indexer, "connect_doc_db") as doc_connect, \
                patch.object(indexer, "connect_facet_db") as facet_connect, \
                patch.object(indexer, "reset_db") as reset_docs, \
                patch.object(indexer, "reset_facet_db") as reset_facets, \
                patch.object(indexer, "activate_index") as activate:
            doc_connect.return_value.__enter__.return_value = MagicMock()
            facet_connect.return_value.__enter__.return_value = MagicMock()
            indexer.start_progressive_build(es, "article-build")
        reset_docs.assert_called_once_with(indexer.DOC_DB)
        reset_facets.assert_called_once_with(indexer.FACET_DB)
        activate.assert_called_once_with(es, "article-build")

    def test_failed_progressive_build_removes_alias_index_and_sidecars(self):
        es = MagicMock()
        with patch.object(indexer, "reset_db") as reset_docs, \
                patch.object(indexer, "reset_facet_db") as reset_facets:
            indexer.discard_progressive_build(es, "article-build")
        es.indices.delete.assert_called_once_with(index="article-build", ignore_unavailable=True)
        reset_docs.assert_called_once_with(indexer.DOC_DB)
        reset_facets.assert_called_once_with(indexer.FACET_DB)

    def test_activate_index_switches_between_alias_generations(self):
        es = MagicMock()
        es.indices.get_alias.return_value = {
            "article-old": {"aliases": {indexer.INDEX_NAME: {}}},
        }

        indexer.activate_index(es, "article-build")

        actions = es.indices.update_aliases.call_args.kwargs["actions"]
        self.assertEqual(actions, [
            {"remove": {"index": "article-old", "alias": indexer.INDEX_NAME}},
            {"add": {"index": "article-build", "alias": indexer.INDEX_NAME}},
        ])
        es.indices.delete.assert_called_once_with(index=["article-old"], ignore_unavailable=True)

    def test_activate_build_restores_sidecars_when_alias_switch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs.sqlite"
            facets = root / "facets.sqlite"
            temp_docs = root / "docs.tmp"
            temp_facets = root / "facets.tmp"
            docs.write_text("old docs", encoding="utf-8")
            facets.write_text("old facets", encoding="utf-8")
            temp_docs.write_text("new docs", encoding="utf-8")
            temp_facets.write_text("new facets", encoding="utf-8")
            Path(f"{docs}-wal").write_bytes(b"")
            Path(f"{docs}-shm").write_bytes(b"")
            Path(f"{facets}-wal").write_bytes(b"")
            Path(f"{facets}-shm").write_bytes(b"")

            with patch.object(indexer, "DOC_DB", docs), \
                    patch.object(indexer, "FACET_DB", facets), \
                    patch.object(indexer, "checkpoint_for_replace"), \
                    patch.object(indexer, "activate_index", side_effect=RuntimeError("switch failed")):
                with self.assertRaisesRegex(RuntimeError, "switch failed"):
                    indexer.activate_build(MagicMock(), "article-build", temp_docs, temp_facets)

            self.assertEqual(docs.read_text(encoding="utf-8"), "old docs")
            self.assertEqual(facets.read_text(encoding="utf-8"), "old facets")
            self.assertFalse(Path(f"{docs}-wal").exists())
            self.assertFalse(Path(f"{docs}-shm").exists())
            self.assertFalse(Path(f"{facets}-wal").exists())
            self.assertFalse(Path(f"{facets}-shm").exists())

    def test_activate_build_keeps_new_sidecars_when_alias_commit_was_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs.sqlite"
            facets = root / "facets.sqlite"
            temp_docs = root / "docs.tmp"
            temp_facets = root / "facets.tmp"
            docs.write_text("old docs", encoding="utf-8")
            facets.write_text("old facets", encoding="utf-8")
            temp_docs.write_text("new docs", encoding="utf-8")
            temp_facets.write_text("new facets", encoding="utf-8")
            Path(f"{docs}-wal").write_bytes(b"")
            Path(f"{docs}-shm").write_bytes(b"")
            Path(f"{facets}-wal").write_bytes(b"")
            Path(f"{facets}-shm").write_bytes(b"")

            with patch.object(indexer, "DOC_DB", docs), \
                    patch.object(indexer, "FACET_DB", facets), \
                    patch.object(indexer, "checkpoint_for_replace"), \
                    patch.object(indexer, "activate_index", side_effect=TimeoutError("response lost")), \
                    patch.object(indexer, "alias_points_to", return_value=True):
                indexer.activate_build(MagicMock(), "article-build", temp_docs, temp_facets)

            self.assertEqual(docs.read_text(encoding="utf-8"), "new docs")
            self.assertEqual(facets.read_text(encoding="utf-8"), "new facets")
            self.assertFalse(docs.with_name("docs.sqlite.previous").exists())
            self.assertFalse(facets.with_name("facets.sqlite.previous").exists())
            self.assertFalse(Path(f"{docs}-wal").exists())
            self.assertFalse(Path(f"{docs}-shm").exists())
            self.assertFalse(Path(f"{facets}-wal").exists())
            self.assertFalse(Path(f"{facets}-shm").exists())

    def test_recover_interrupted_switch_restores_old_sidecars_before_alias_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs.sqlite"
            facets = root / "facets.sqlite"
            docs.write_text("new docs", encoding="utf-8")
            facets.write_text("new facets", encoding="utf-8")
            docs.with_name("docs.sqlite.previous").write_text("old docs", encoding="utf-8")
            facets.with_name("facets.sqlite.previous").write_text("old facets", encoding="utf-8")
            switch_state = root / "index-switch.json"
            switch_state.write_text(json.dumps({
                "build_index": "article-build",
                "had_docs": True,
                "had_facets": True,
            }), encoding="utf-8")
            status = root / "index-status"
            es = MagicMock()

            with patch.object(indexer, "DOC_DB", docs), \
                    patch.object(indexer, "FACET_DB", facets), \
                    patch.object(indexer, "SWITCH_STATE_PATH", switch_state), \
                    patch.object(indexer, "INDEX_STATUS_PATH", status), \
                    patch.object(indexer, "alias_points_to", return_value=False):
                indexer.recover_interrupted_switch(es)

            self.assertEqual(docs.read_text(encoding="utf-8"), "old docs")
            self.assertEqual(facets.read_text(encoding="utf-8"), "old facets")
            es.indices.delete.assert_called_once_with(index="article-build", ignore_unavailable=True)
            self.assertFalse(switch_state.exists())

    def test_recover_interrupted_switch_keeps_committed_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / "docs.sqlite"
            facets = root / "facets.sqlite"
            docs.write_text("new docs", encoding="utf-8")
            facets.write_text("new facets", encoding="utf-8")
            docs.with_name("docs.sqlite.previous").write_text("old docs", encoding="utf-8")
            facets.with_name("facets.sqlite.previous").write_text("old facets", encoding="utf-8")
            switch_state = root / "index-switch.json"
            status = root / "index-status"
            switch_state.write_text(json.dumps({
                "build_index": "article-build",
                "had_docs": True,
                "had_facets": True,
            }), encoding="utf-8")

            with patch.object(indexer, "DOC_DB", docs), \
                    patch.object(indexer, "FACET_DB", facets), \
                    patch.object(indexer, "SWITCH_STATE_PATH", switch_state), \
                    patch.object(indexer, "INDEX_STATUS_PATH", status), \
                    patch.object(indexer, "alias_points_to", return_value=True):
                indexer.recover_interrupted_switch(MagicMock())

            self.assertEqual(docs.read_text(encoding="utf-8"), "new docs")
            self.assertEqual(facets.read_text(encoding="utf-8"), "new facets")
            self.assertEqual(status.read_text(encoding="utf-8"), "ready")
            self.assertFalse(switch_state.exists())


if __name__ == "__main__":
    unittest.main()
