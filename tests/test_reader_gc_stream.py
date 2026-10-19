import gzip
import json
import unittest

from scripts.reader_gc_stream import catalog_identity, compact_catalog_stream

NEW = "1" * 32
OLD = "2" * 32
RECENT = "3" * 32
ANCIENT = "4" * 32


def snapshot(generation, digest):
    return {"bucket": "vomebook/reader-assets-v2",
            "path": f"reader-index/generations/{generation}/reader_assets.json.gz",
            "sha256": digest, "bytes": 10}


def catalog():
    return {
        "version": 1,
        "kind": "reader-catalog",
        "current": {"generation": NEW, "published_at": "2026-10-19T00:00:00+00:00"},
        "generations": {
            NEW: {"generation": NEW, "status": "current", "sidecar_sha256": "a" * 64,
                  "published_at": "2026-10-19T00:00:00+00:00",
                  "snapshot": snapshot(NEW, "c" * 64),
                  "references": [{"bucket": "vomebook/pdf-pages-v2",
                                  "path": "objects/live/document.pdf"}]},
            OLD: {"generation": OLD, "status": "superseded",
                  "superseded_at": "2026-10-10T00:00:00+00:00", "sidecar_sha256": "b" * 64,
                  "snapshot": snapshot(OLD, "d" * 64),
                  "references": [{"bucket": "vomebook/pdf-pages-v2",
                                  "path": "objects/dead/one.pdf"},
                                 {"bucket": "vomebook/pdf-pages-v2",
                                  "path": "objects/dead/two.pdf"}]},
            RECENT: {"generation": RECENT, "status": "superseded",
                     "superseded_at": "2026-10-18T23:00:00+00:00", "sidecar_sha256": "f" * 64,
                     "references": [{"bucket": "vomebook/pdf-pages-v2",
                                     "path": "objects/keep/three.pdf"}]},
        },
        "retired_generations": {
            ANCIENT: {"replacement_generation": OLD,
                      "replacement_acks": {"hf": True, "pages": True},
                      "snapshot": snapshot(ANCIENT, "e" * 64)},
        },
    }


def chunks(payload, size=7):
    return [payload[n:n + size] for n in range(0, len(payload), size)]


class CatalogCompactionStreamTests(unittest.TestCase):
    def test_compaction_keeps_current_references_and_grace_history(self):
        wire = json.dumps(catalog(), sort_keys=True).encode()
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                payload = gzip.compress(wire, mtime=0) if compressed else wire
                encoded, digest, stats = compact_catalog_stream(
                    chunks(payload), compressed, current=NEW, acknowledged=True,
                    grace_days=2, now="2026-10-19T01:00:00+00:00")
                result = json.loads(encoded)
                # "recent" is inside the grace window; "old" is outside it.
                self.assertEqual(set(result["generations"]), {NEW, RECENT})
                # A dropped reference here would silently lose live objects.
                self.assertEqual(result["generations"][NEW]["references"],
                                 [{"bucket": "vomebook/pdf-pages-v2",
                                   "path": "objects/live/document.pdf"}])
                self.assertEqual(result["generations"][NEW]["sidecar_sha256"], "a" * 64)
                tombstone = result["retired_generations"][OLD]
                self.assertEqual(tombstone["snapshot"]["path"],
                                 f"reader-index/generations/{OLD}/reader_assets.json.gz")
                self.assertNotIn("references", tombstone)
                self.assertEqual(tombstone["replacement_acks"], {"hf": True, "pages": True})
                # Pre-existing tombstones are never discarded by compaction.
                self.assertIn(ANCIENT, result["retired_generations"])
                self.assertEqual(stats["kept"], 2)
                self.assertEqual(stats["retired_generations"], 2)

    def test_compaction_is_idempotent_on_the_wire(self):
        first, _, stats = compact_catalog_stream(
            chunks(json.dumps(catalog(), sort_keys=True).encode()),
            False, current=NEW, acknowledged=True, grace_days=0,
            now="2026-10-19T01:00:00+00:00")
        second, _, again = compact_catalog_stream(
            chunks(first), False, current=NEW, acknowledged=True, grace_days=0,
            now="2026-10-19T01:00:00+00:00")
        self.assertEqual(first, second)
        self.assertEqual(again["dropped"], 0)

    def test_tombstone_never_names_a_foreign_snapshot(self):
        source = catalog()
        source["generations"][OLD]["generation"] = OLD
        source["generations"][OLD]["snapshot"] = {
            "bucket": "vomebook/reader-assets-v2",
            "path": "reader-index/snapshots/" + "0" * 64 + "/reader_assets.json.gz",
            "sha256": "d" * 64, "bytes": 10}
        encoded, _, _ = compact_catalog_stream(
            chunks(json.dumps(source, sort_keys=True).encode()),
            False, current=NEW, acknowledged=True, grace_days=0,
            now="2026-10-19T01:00:00+00:00")
        tombstone = json.loads(encoded)["retired_generations"][OLD]
        # The collector only deletes snapshots at the generation path; a
        # tombstone naming anything else must not be created.
        self.assertIsNone(tombstone["snapshot"])

    def test_unacknowledged_catalog_drops_no_generation(self):
        wire = json.dumps(catalog(), sort_keys=True).encode()
        encoded, _, _ = compact_catalog_stream(
            chunks(wire), False, current=NEW, acknowledged=False, grace_days=3650)
        self.assertEqual(set(json.loads(encoded)["generations"]), {NEW, OLD, RECENT})

    def test_stream_reports_the_exact_wire_digest(self):
        import hashlib
        wire = json.dumps(catalog(), sort_keys=True).encode()
        identity, digest = catalog_identity(chunks(wire))
        self.assertEqual(digest, hashlib.sha256(wire).hexdigest())
        self.assertEqual(identity["current"], NEW)
        self.assertEqual(identity["bound_sidecar_sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
