"""CCRD OCR retirement tier: exact closures, retention frontier, blockers."""
import hashlib
import json
import unittest
from unittest.mock import patch

from scripts import ccrd_lifecycle as lifecycle, reader_gc_ccrd as gc


BUCKET = lifecycle.BUCKET
ASSETS = lifecycle.BUCKETS[0]


class Store:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put(self, path, value):
        raw = value if isinstance(value, bytes) else json.dumps(
            value, ensure_ascii=False, sort_keys=True).encode() + b"\n"
        self.objects[path] = raw
        return raw

    def ref(self, path, role="runtime"):
        raw = self.objects[path]
        return {"bucket": BUCKET, "path": path, "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw), "role": role}

    def read_bytes(self, bucket, path):
        if bucket != BUCKET or path not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[path]

    def list_files(self, bucket, prefixes):
        if bucket != BUCKET:
            raise FileNotFoundError(bucket)
        return {path for path in self.objects if path.startswith(tuple(prefixes))}

    def stat(self, bucket, path):
        if bucket != BUCKET or path not in self.objects:
            raise FileNotFoundError(path)
        return len(self.objects[path])

    def put_json(self, bucket, path, value):
        if bucket != BUCKET:
            raise ValueError(bucket)
        self.put(path, value)

    def delete(self, bucket, paths):
        if bucket != BUCKET:
            raise ValueError(bucket)
        for path in paths:
            self.objects.pop(path, None)
        self.deleted.extend(paths)


class Live:
    def __init__(self, generation, sources=None):
        self.generation = generation
        self.sources = sources or ["CCRD", "CW"]

    def get(self, url, **kwargs):
        return Response(self.generation, self.sources)


class Response:
    def __init__(self, generation, sources):
        self.generation = generation
        self.sources = sources

    def raise_for_status(self):
        return None

    def json(self):
        return {"ready": True, "ocr_release": self.generation,
                "loaded_sources": self.sources}


def build_store(*, generations=4, open_upload=False, branch=False):
    store = Store()
    releases = []
    for index in range(generations):
        suffix = f"{index + 1:064x}"
        book = f"{index + 11:064x}"
        export_gen = f"{index + 21:064x}"
        text_path = f"text/objects/{suffix[:2]}/{suffix}.txt"
        store.put(text_path, f"text {index}".encode())
        text_ref = store.ref(text_path)
        reader_ref = {"bucket": ASSETS, "path": f"objects/{suffix[:2]}/reader/page.png",
                      "sha256": "a" * 64, "bytes": 10, "role": "runtime"}
        export = {"version": 1, "kind": "ccrd-ocr-text-export", "complete": True,
                  "book_id": book, "generation": export_gen, "page_count": 1, "pages": [{}],
                  "text": text_ref, "retained_reader_resources": [reader_ref]}
        export_path = f"text/exports/{book}/{export_gen}/manifest.json"
        store.put(export_path, export)
        export_ref = store.ref(export_path, "provenance")
        identity = {"version": 1, "kind": "ccrd-ocr-search-release",
                    "base_metadata_sha256": "b" * 64, "builder_sha256": "c" * 64,
                    "tokenizer_version": lifecycle.TOKENIZER, "exports": [export_ref]}
        generation = lifecycle.digest(lifecycle.canonical(identity))
        database_path = f"indexes/ocr/{generation}/OCR.sqlite3"
        store.put(database_path, f"db {index}".encode())
        manifest = {**identity, "generation": generation, "documents": 1,
                    "database": store.ref(database_path)}
        release_path = f"releases/ocr-{generation}/manifest.json"
        store.put(release_path, manifest)
        release_ref = store.ref(release_path)
        releases.append({"generation": generation, "release": release_ref, "manifest": manifest,
                         "export": export_ref, "text": text_ref, "database": store.ref(database_path),
                         "paths": [release_path, export_path, text_path, database_path]})
    for index, item in enumerate(releases):
        parent = releases[index - 1]["generation"] if index else None
        resource_set = [item["release"], item["database"], item["export"], item["text"]]
        store.put(f"control/intents/{item['generation']}.json", {
            "version": 1, "kind": "ccrd-ocr-intent", "generation": item["generation"],
            "status": "committed", "expected_parent": parent, "resources": resource_set})
        if item["generation"] != releases[-1]["generation"]:
            store.put(f"control/retained/{item['generation']}.json", {
                "version": 1, "kind": "ccrd-ocr-retention", "resources": [item["release"]]})
    current = releases[-1]
    store.put(gc.CURRENT, {"version": 1, "kind": "ccrd-ocr-current",
                           "generation": current["generation"],
                           "release": current["release"], "resources": [current["release"]],
                           "parent": releases[-2]["generation"]})
    store.put(gc.IDS, {"version": 1, "kind": "ccrd-ocr-id-registry",
                       "ids": {"a" * 64: {"doc_id": "OCR:1", "readable_rel_path": "x.txt"}},
                       "resources": [current["release"]]})
    store.put(gc.AUTOMATION, {"version": 1, "kind": "ccrd-ocr-automation",
                              "resources": [], "failures": {}})
    store.put("text/objects/orphan.txt", b"orphan")
    if open_upload:
        pending = f"{'f' * 64}"
        database_path = f"indexes/ocr/{pending}/OCR.sqlite3"
        store.put(database_path, b"pending db")
        release_path = f"releases/ocr-{pending}/manifest.json"
        store.put(release_path, {"version": 1, "kind": "ccrd-ocr-search-release",
                                 "generation": pending})
        store.put(f"control/intents/{pending}.json", {
            "version": 1, "kind": "ccrd-ocr-intent", "generation": pending,
            "status": "uploading", "expected_parent": current["generation"],
            "resources": [store.ref(release_path), store.ref(database_path)]})
    if branch:
        other = f"{'e' * 64}"
        store.put(f"control/intents/{other}.json", {
            "version": 1, "kind": "ccrd-ocr-intent", "generation": other,
            "status": "committed", "expected_parent": releases[-2]["generation"],
            "resources": []})
    store.put("generations/legacy-a/CCRD.sqlite3", b"old")
    store.put("generations/legacy-b/CCRD.sqlite3", b"older")
    store.put("current.json", {"format": 1, "tokenizer_version": lifecycle.TOKENIZER,
                               "databases": {"CCRD": {"path": "generations/legacy-a/CCRD.sqlite3",
                                                      "documents": 1, "fts_rows": 1,
                                                      "sha256": "a" * 64, "bytes": 3},
                                             "CW": {"path": "generations/legacy-a/CW.sqlite3",
                                                    "documents": 1, "fts_rows": 1,
                                                    "sha256": "a" * 64, "bytes": 3}}})
    return store, releases


class CcrdGcTests(unittest.TestCase):
    def test_withdrawn_empty_library_is_a_successful_scheduled_noop(self):
        store = Store()
        store.put(gc.IDS, {'version': 1, 'kind': 'ccrd-ocr-id-registry', 'ids': {}, 'resources': []})
        report = gc.plan(store, client=Live(None))
        gc.apply(store, report, client=Live(None))
        self.assertTrue(report['complete'])
        self.assertEqual(report['deleted']['objects'], 0)
        self.assertIn(gc.IDS, store.objects)

    def test_partial_delete_is_not_counted_until_readback_and_retries_complete(self):
        store, releases = build_store()
        live = Live(releases[-1]['generation'])
        report = gc.plan(store, client=live)
        original = store.delete
        def partial(bucket, paths):
            original(bucket, paths[:1])
            raise OSError('lost delete response')
        saved = []
        with patch.object(store, 'delete', side_effect=partial), self.assertRaises(OSError):
            gc.apply(store, report, client=live, checkpoint=lambda value: saved.append(json.loads(json.dumps(value))))
        self.assertEqual(report['deleted']['objects'], 0)
        self.assertEqual(saved[-1]['batches'][0]['status'], 'submitted')
        retry = gc.plan(store, client=live)
        gc.apply(store, retry, client=live)
        self.assertTrue(retry['complete'])
        for release in releases[-2:]:
            self.assertTrue(set(release['paths']) <= set(store.objects))

    def test_same_named_control_with_new_pin_blocks_deletion(self):
        store, releases = build_store()
        report = gc.plan(store, client=Live(releases[-1]['generation']))
        auto = json.loads(store.objects[gc.AUTOMATION])
        auto['resources'].append(releases[0]['text'])
        store.put(gc.AUTOMATION, auto)
        with self.assertRaisesRegex(gc.Blocker, 'content changed'):
            gc.apply(store, report, client=Live(releases[-1]['generation']))
        self.assertEqual(store.deleted, [])

    def test_plan_keeps_current_and_parent_and_reclaims_only_expired_closures(self):
        store, releases = build_store()
        report = gc.plan(store, keep_generations=2, client=Live(releases[-1]["generation"]))
        self.assertFalse(report["deletion_enabled"])
        self.assertEqual(report["kept"], [releases[-1]["generation"], releases[-2]["generation"]])
        self.assertEqual(report["expiring"], [releases[1]["generation"], releases[0]["generation"]])
        self.assertEqual(report["deferred"], [])
        candidates = set(report["candidates"])
        for item in releases[:2]:
            self.assertTrue(set(item["paths"]) <= candidates, item["generation"])
        for item in releases[2:]:
            self.assertFalse(set(item["paths"]) & candidates, item["generation"])
        self.assertIn("text/objects/orphan.txt", candidates)
        self.assertNotIn("generations/legacy-a/CCRD.sqlite3", candidates)
        self.assertEqual(report["legacy_generations"]["retired_generations"], ["legacy-b"])
        self.assertEqual(report["legacy_generations"]["retired_bytes"], len(b"older"))
        self.assertEqual(report["objects"]["candidates"], len(candidates))

    def test_apply_removes_control_then_data_and_preserves_kept_closure(self):
        store, releases = build_store()
        report = gc.plan(store, keep_generations=2, client=Live(releases[-1]["generation"]))
        gc.apply(store, report, client=Live(releases[-1]["generation"]))
        for item in releases[2:]:
            for path in item["paths"]:
                self.assertIn(path, store.objects)
        for item in releases[:2]:
            for path in item["paths"]:
                self.assertNotIn(path, store.objects)
            retired = json.loads(store.objects[f"control/intents/{item['generation']}.json"])
            self.assertEqual((retired["status"], retired["resources"]), ("retired", []))
            root = json.loads(store.objects[f"control/retained/{item['generation']}.json"])
            self.assertEqual((root["status"], root["resources"]), ("retired", []))
        self.assertNotIn("text/objects/orphan.txt", store.objects)
        self.assertIn(gc.CURRENT, store.objects)
        self.assertIn(gc.IDS, store.objects)
        self.assertTrue(report["applied"])
        self.assertEqual(report["deleted"]["control"], 0)
        self.assertEqual(report["deleted"]["intents_retired"], 2)
        self.assertEqual(report["deleted"]["retention_retired"], 2)

    def test_limit_expires_oldest_first_and_defers_the_rest(self):
        store, releases = build_store()
        report = gc.plan(store, keep_generations=2, limit=1,
                         client=Live(releases[-1]["generation"]))
        self.assertEqual(report["expiring"], [releases[0]["generation"]])
        self.assertEqual(report["deferred"], [releases[1]["generation"]])
        self.assertNotIn(releases[1]["paths"][0], report["candidates"])
        gc.apply(store, report, client=Live(releases[-1]["generation"]))
        self.assertIn(releases[1]["paths"][0], store.objects)
        self.assertNotIn(releases[0]["paths"][0], store.objects)

    def test_open_upload_protects_its_members_without_blocking_old_history(self):
        store, releases = build_store(open_upload=True)
        pending = f"{'f' * 64}"
        report = gc.plan(store, keep_generations=2, client=Live(releases[-1]["generation"]))
        self.assertIn(pending, report["generations"]["open_intents"])
        self.assertNotIn(f"indexes/ocr/{pending}/OCR.sqlite3", report["candidates"])
        self.assertIn(releases[0]["paths"][0], report["candidates"])

    def test_committed_branch_or_live_mismatch_blocks_before_any_write(self):
        store, releases = build_store(branch=True)
        with self.assertRaisesRegex(gc.Blocker, "outside the current parent chain"):
            gc.plan(store, keep_generations=2, client=Live(releases[-1]["generation"]))
        store, releases = build_store()
        with self.assertRaisesRegex(gc.Blocker, "exact current OCR generation"):
            gc.plan(store, keep_generations=2, client=Live("0" * 64))
        self.assertNotIn(gc.CURRENT, store.deleted)

    def test_control_history_changed_between_plan_and_apply_blocks(self):
        store, releases = build_store()
        report = gc.plan(store, keep_generations=2, client=Live(releases[-1]["generation"]))
        store.put("control/intents/" + "d" * 64 + ".json",
                  {"version": 1, "kind": "ccrd-ocr-intent", "generation": "d" * 64,
                   "status": "uploading", "expected_parent": None, "resources": []})
        with self.assertRaisesRegex(gc.Blocker, "control history changed"):
            gc.apply(store, report, client=Live(releases[-1]["generation"]))
        self.assertIn(releases[0]["paths"][0], store.objects)

    def test_live_acceptance_uses_ocr_release_not_a_literal_ocr_source(self):
        store, releases = build_store()
        report = gc.plan(store, keep_generations=2,
                         client=Live(releases[-1]["generation"], ["CCRD", "CW"]))
        self.assertTrue(report["live"]["checked"])
        self.assertTrue(report["live"]["ready"])

    def test_apply_rechecks_live_before_any_write(self):
        store, releases = build_store()
        report = gc.plan(store, keep_generations=2, verify_live=False)
        before = dict(store.objects)
        with self.assertRaisesRegex(gc.Blocker, "exact current OCR generation"):
            gc.apply(store, report, client=Live("0" * 64))
        self.assertEqual(store.objects, before)

    def test_missing_kept_member_blocks_instead_of_shrinking_the_live_release(self):
        store, releases = build_store()
        store.objects.pop(releases[-1]["paths"][2])
        with self.assertRaisesRegex(gc.Blocker, "missing"):
            gc.plan(store, keep_generations=2, client=Live(releases[-1]["generation"]))

    def test_retry_finishes_partial_cleanup_without_expiring_newer_history(self):
        store, releases = build_store()
        report = gc.plan(store, keep_generations=2, limit=1,
                         client=Live(releases[-1]["generation"]))
        self.assertEqual(report["expiring"], [releases[0]["generation"]])
        gc.apply(store, report, client=Live(releases[-1]["generation"]))
        # Simulate an interruption after control retirement but before the
        # expired data objects were removed.
        store.put(releases[0]["paths"][0], releases[0]["manifest"])
        store.put(releases[0]["paths"][3], b"db 0")
        retry = gc.plan(store, keep_generations=2, limit=1,
                        client=Live(releases[-1]["generation"]))
        self.assertEqual(retry["expiring"], [releases[0]["generation"]])
        self.assertEqual(retry["deferred"], [releases[1]["generation"]])
        self.assertIn(releases[0]["generation"], retry["generations"]["already_retired"])
        gc.apply(store, retry, client=Live(releases[-1]["generation"]))
        self.assertNotIn(releases[0]["paths"][0], store.objects)
        self.assertIn(releases[1]["paths"][0], store.objects)

    def test_retention_frontier_must_include_the_rollback_parent(self):
        store, releases = build_store()
        with self.assertRaises(ValueError):
            gc.plan(store, keep_generations=1, client=Live(releases[-1]["generation"]))


if __name__ == "__main__":
    unittest.main()


class LegacyLive:
    def __init__(self, generations, counts):
        self.generations = generations
        self.counts = counts

    def get(self, url, **kwargs):
        if url.endswith('/api/sources'):
            return LegacySources(self.counts)
        return LegacyStatus(self.generations)


class LegacyStatus:
    def __init__(self, generations):
        self.generations = generations

    def raise_for_status(self):
        return None

    def json(self):
        return {"ready": True, "ocr_release": "0" * 64,
                "loaded_sources": ["CCRD", "CW"],
                "bucket": {"state": "ready", "source": "bucket-pointer",
                           "sources": ["CCRD", "CW"]}}


class LegacySources:
    def __init__(self, counts):
        self.counts = counts

    def raise_for_status(self):
        return None

    def json(self):
        return [{"slug": name, "count": self.counts[name]} for name in ("CCRD", "CW")]


def build_legacy(*, cross_ref=False, orphan=False):
    store = Store()
    directories = ["gen-current", "gen-prev", "gen-old"]
    entries = {}
    for index, directory in enumerate(directories):
        cw_docs = 100 - index
        db_paths = {
            "CCRD": f"generations/{directory}/CCRD.sqlite3",
            "CW": f"generations/{directory}/CW.sqlite3",
        }
        store.put(db_paths["CCRD"], f"ccrd {directory}".encode())
        store.put(db_paths["CW"], f"cw {directory}".encode())
        databases = {}
        for name, path in db_paths.items():
            databases[name] = {"path": path, "bytes": len(store.objects[path]),
                               "sha256": hashlib.sha256(store.objects[path]).hexdigest(),
                               "documents": 3 if name == "CCRD" else cw_docs,
                               "fts_rows": 3 if name == "CCRD" else cw_docs}
        manifest = {"format": 1, "tokenizer_version": lifecycle.TOKENIZER,
                    "created_at": f"2026-10-{20 - index:02d}T00:00:00+00:00",
                    "databases": databases}
        if cross_ref and directory == "gen-prev":
            manifest["databases"]["CCRD"]["path"] = "generations/gen-old/CCRD.sqlite3"
            body = b"ccrd gen-old"
            manifest["databases"]["CCRD"]["bytes"] = len(body)
            manifest["databases"]["CCRD"]["sha256"] = hashlib.sha256(body).hexdigest()
        store.put(f"generations/{directory}/manifest.json", manifest)
        entries[directory] = manifest
    active = entries["gen-current"]["databases"]
    store.put("current.json", {"format": 1, "tokenizer_version": lifecycle.TOKENIZER,
                               "databases": active})
    if orphan:
        store.put("generations/gen-orphan/CCRD.sqlite3", b"orphan ccrd")
        store.put("generations/gen-orphan/CW.sqlite3", b"orphan cw")
    counts = {"CCRD": active["CCRD"]["documents"], "CW": active["CW"]["documents"]}
    return store, directories, counts


class LegacyGcTests(unittest.TestCase):
    def four_source(self):
        from tests.test_ccrd_lifecycle import CcrdLifecycleTests
        fixture = CcrdLifecycleTests()
        fixture.setUp()
        pointer = fixture.four_source_pointer()
        store, directories, counts = build_legacy()
        old_prefix = 'generations/' + pointer['generation']
        generation = 'ccrd-four-' + 'f' * 64
        new_prefix = 'generations/' + generation
        for path, raw in fixture.store.objects[BUCKET].items():
            if path != 'current.json':
                store.put(path.replace(old_prefix, new_prefix), raw)
        pointer = json.loads(json.dumps(pointer).replace(old_prefix, new_prefix))
        pointer['generation'] = generation
        pointer['previous_index_manifest_sha256'] = gc.digest(store.objects['generations/gen-current/manifest.json'])
        previous = json.loads(store.objects['current.json'])
        for name in ['CCRD', 'CW']:
            pointer['databases'][name] = previous['databases'][name]
        # The four-source contract permits only canonical ccrd-* database paths.
        for name in ['CCRD', 'CW']:
            original = pointer['databases'][name]['path']
            canonical = original.replace('gen-current', 'ccrd-current')
            store.put(canonical, store.objects[original])
            pointer['databases'][name]['path'] = canonical
        store.put('current.json', pointer)
        store.put(new_prefix + '/manifest.json', pointer)
        counts = {name: item['documents'] for name, item in pointer['databases'].items()}
        class FourLive(LegacyLive):
            def get(self, url, **kwargs):
                if url.endswith('/api/sources'):
                    response = LegacySources(counts)
                    response.json = lambda: [{'slug': name, 'count': count} for name, count in counts.items()]
                    return response
                response = LegacyStatus([])
                response.json = lambda: {'ready': True, 'loaded_sources': list(counts), 'bucket': {
                    'state': 'ready', 'source': 'space-activation', 'sources': list(counts),
                    'generation': generation, 'base_metadata_sha256': pointer['base_metadata_sha256']}}
                return response
        return store, pointer, FourLive([], counts)

    def test_four_source_index_keeps_exact_parent_shared_databases_and_archives(self):
        store, pointer, live = self.four_source()
        store.put('indexes/newspapers/PLA/obsolete.sqlite3', b'old newspaper')
        report = gc.legacy_plan(store, keep_generations=2, client=live)
        self.assertEqual(report['kept'], ['gen-current'])
        protected = {ref['path'] for ref in lifecycle.references(pointer, 'current.json')}
        self.assertFalse(protected & set(report['candidates']))
        self.assertIn('indexes/newspapers/PLA/obsolete.sqlite3', report['candidates'])
        gc.legacy_apply(store, report, client=live)
        self.assertTrue(report['complete'])
        self.assertTrue(protected <= store.objects.keys())
        self.assertNotIn('generations/gen-old/manifest.json', store.objects)

    def test_new_rollout_pin_between_batches_stops_index_deletion(self):
        store, pointer, live = self.four_source()
        report = gc.legacy_plan(store, client=live)
        delete = store.delete
        def race(bucket, paths):
            delete(bucket, paths)
            store.put('control/retention-new.json', {'version': 1, 'kind': 'ccrd-ocr-retention',
                      'resources': [store.ref('generations/gen-old/CCRD.sqlite3')]})
        with patch.object(store, 'delete', side_effect=race), self.assertRaisesRegex(gc.Blocker, 'control references changed'):
            gc.legacy_apply(store, report, client=live)
        self.assertIn('generations/gen-old/CCRD.sqlite3', store.objects)
        self.assertEqual(report['deleted']['objects'], 0)

    def test_existing_control_pin_keeps_the_entire_index_generation(self):
        store, pointer, live = self.four_source()
        manifest = 'generations/gen-old/manifest.json'
        store.put('control/retention-other.json', {'version': 1, 'kind': 'ccrd-ocr-retention',
                  'resources': [store.ref(manifest)]})
        report = gc.legacy_plan(store, client=live)
        self.assertFalse(any(path.startswith('generations/gen-old/') for path in report['candidates']))

    def test_interrupted_manifest_batch_resumes_and_only_counts_verified_deletions(self):
        store, directories, counts = build_legacy(orphan=True)
        live = LegacyLive(directories, counts)
        report = gc.legacy_plan(store, client=live)
        remove = store.delete
        def partial(bucket, paths):
            remove(bucket, paths[:1])
            raise OSError('response lost')
        with patch.object(store, 'delete', side_effect=partial), self.assertRaises(OSError):
            gc.legacy_apply(store, report, client=live)
        self.assertFalse(report['complete'])
        self.assertEqual(report['deleted']['objects'], 0)
        retry = gc.legacy_plan(store, client=live)
        gc.legacy_apply(store, retry, client=live)
        self.assertTrue(retry['complete'])
        self.assertTrue(all(batch['status'] == 'verified-deleted' for batch in retry['batches']))
        self.assertFalse(any(path.startswith(('generations/gen-old/', 'generations/gen-orphan/'))
                             for path in store.objects))
        self.assertIn('generations/gen-prev/manifest.json', store.objects)

    def test_changed_same_name_manifest_with_new_reference_blocks_deletion(self):
        store, directories, counts = build_legacy()
        live = LegacyLive(directories, counts)
        report = gc.legacy_plan(store, client=live)
        path = 'generations/gen-prev/manifest.json'
        value = json.loads(store.objects[path])
        value['databases']['CCRD']['path'] = 'generations/gen-evil/CCRD.sqlite3'
        store.put(path, value)
        with self.assertRaisesRegex(gc.Blocker, 'manifest changed'):
            gc.legacy_apply(store, report, client=live)
        self.assertEqual(store.deleted, [])

    def test_silent_partial_delete_is_not_reported_as_verified(self):
        store, directories, counts = build_legacy()
        live = LegacyLive(directories, counts)
        report = gc.legacy_plan(store, client=live)
        remove = store.delete
        def partial(bucket, paths):
            remove(bucket, paths[:1])
        with patch.object(store, 'delete', side_effect=partial), self.assertRaisesRegex(RuntimeError, 'readback'):
            gc.legacy_apply(store, report, client=live)
        self.assertFalse(report['complete'])
        self.assertEqual(report['deleted']['objects'], 0)

    def test_plan_keeps_current_and_previous_and_reclaims_older(self):
        store, directories, counts = build_legacy()
        report = gc.legacy_plan(store, keep_generations=2,
                                client=LegacyLive(directories, counts))
        self.assertEqual(report["current_dirs"], ["gen-current"])
        self.assertEqual(report["kept"], ["gen-prev"])
        self.assertEqual(report["expiring"], ["gen-old"])
        self.assertTrue(any(path.startswith("generations/gen-old/") for path in report["candidates"]))
        self.assertFalse(any(path.startswith("generations/gen-current/") for path in report["candidates"]))
        self.assertFalse(any(path.startswith("generations/gen-prev/") for path in report["candidates"]))
        self.assertFalse(report["deletion_enabled"])

    def test_apply_deletes_older_generation_and_keeps_pointer(self):
        store, directories, counts = build_legacy()
        report = gc.legacy_plan(store, keep_generations=2,
                                client=LegacyLive(directories, counts))
        gc.legacy_apply(store, report, client=LegacyLive(directories, counts))
        self.assertIn("generations/gen-current/CCRD.sqlite3", store.objects)
        self.assertIn("generations/gen-prev/CCRD.sqlite3", store.objects)
        self.assertNotIn("generations/gen-old/CCRD.sqlite3", store.objects)
        self.assertNotIn("generations/gen-old/manifest.json", store.objects)
        self.assertIn("current.json", store.objects)
        self.assertTrue(report["applied"])

    def test_cross_directory_manifest_reference_is_protected(self):
        store, directories, counts = build_legacy(cross_ref=True)
        report = gc.legacy_plan(store, keep_generations=2,
                                client=LegacyLive(directories, counts))
        self.assertNotIn("generations/gen-old/CCRD.sqlite3", report["candidates"])
        self.assertIn("generations/gen-old/CW.sqlite3", report["candidates"])
        gc.legacy_apply(store, report, client=LegacyLive(directories, counts))
        self.assertIn("generations/gen-old/CCRD.sqlite3", store.objects)

    def test_limit_only_expires_oldest_and_defers_the_rest(self):
        store, directories, counts = build_legacy()
        report = gc.legacy_plan(store, keep_generations=2, limit=1,
                                client=LegacyLive(directories, counts))
        self.assertEqual(report["expiring"], ["gen-old"])
        gc.legacy_apply(store, report, client=LegacyLive(directories, counts))
        self.assertNotIn("generations/gen-old/manifest.json", store.objects)
        self.assertIn("generations/gen-prev/manifest.json", store.objects)

    def test_live_count_mismatch_blocks_before_deletion(self):
        store, directories, counts = build_legacy()
        wrong = {"CCRD": counts["CCRD"], "CW": counts["CW"] + 1}
        with self.assertRaisesRegex(gc.Blocker, "source counts"):
            gc.legacy_plan(store, keep_generations=2,
                           client=LegacyLive(directories, wrong))
        self.assertIn("generations/gen-old/CCRD.sqlite3", store.objects)

    def test_missing_active_database_blocks(self):
        store, directories, counts = build_legacy()
        store.objects.pop("generations/gen-current/CCRD.sqlite3")
        with self.assertRaisesRegex(gc.Blocker, "legacy pointer"):
            gc.legacy_plan(store, keep_generations=2,
                           client=LegacyLive(directories, counts))
