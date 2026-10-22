"""CCRD OCR retirement tier: exact closures, retention frontier, blockers."""
import hashlib
import json
import unittest

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
