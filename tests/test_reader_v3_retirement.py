from datetime import datetime, timezone
import hashlib
import json
import unittest

from scripts import publish_reader_v3 as publication, reader_v3_retirement as retirement
from tests import test_publish_reader_v3 as fixtures


class RetirementTests(unittest.TestCase):
    def test_complete_ocr_input_with_no_open_review_is_released_after_acks(self):
        fixture = fixtures.V3PublicationTests()
        fixture.setUp()
        try:
            store = fixture.store
            store.list_files = lambda bucket, prefixes: {
                path for b, path in store.objects if b == bucket and path.startswith(prefixes)}
            staged = publication.stage(store, fixture.candidate(), fixture.bundle, apply=True)
            promoted = publication.promote(store, staged["candidate"], None, apply=True)
            path = f"reader-index/v3/promotions/{promoted['generation']}.json"
            history = json.loads(store.read_bytes(publication.ASSETS, path))
            history["acks"] = {"hf": True, "pages": True}
            store.put_bytes(publication.ASSETS, path, publication.encode(history))
            source = "a" * 64
            input_path = f"objects/aa/{source}/{'c' * 16}/ocr-input/page-000001.png"
            ocr_path = f"objects/aa/{source}/{'c' * 16}/ocr-manifest.json"
            raw = publication.encode({"kind": "pdf-ocr", "complete": True, "page_count": 1,
                "source_sha256": source, "pages": [{"p": 1, "i": input_path, "is": "d" * 64, "ib": 42}]})
            store.put_bytes(publication.v3.shared.PDF_PAGES_BUCKET, ocr_path, raw)
            store.put_bytes(publication.ASSETS, "reader-index/pdf_ocr_manifest.json", publication.encode({"files": {
                "repo\0book.pdf": {"status": "ready", "source_sha256": source, "ocr_manifest": ocr_path,
                    "ocr_manifest_bytes": len(raw), "ocr_manifest_sha256": hashlib.sha256(raw).hexdigest()}}}))
            result = retirement.run(store, apply=True)
            self.assertEqual(result["released_input_roots"], 1)
            self.assertEqual(result["released_input_pages"], 1)
            journal = json.loads(store.read_bytes(publication.ASSETS, retirement.STATE))
            root = input_path.rsplit("/", 1)[0]
            self.assertEqual(journal["retired_input_roots"][root]["source_sha256"], source)
            # A second run reuses the journal without re-reading the book.
            again = retirement.run(store, apply=True)
            self.assertEqual(again["released_input_roots"], 1)
        finally:
            fixture.temp.cleanup()

    def test_only_acknowledged_old_generations_retire_and_current_survives(self):
        fixture = fixtures.V3PublicationTests()
        fixture.setUp()
        try:
            store = fixture.store
            store.list_files = lambda bucket, prefixes: {
                path for b, path in store.objects if b == bucket and path.startswith(prefixes)}
            staged = publication.stage(store, fixture.candidate(), fixture.bundle, apply=True)
            first = publication.promote(store, staged["candidate"], None, apply=True)
            path = f"reader-index/v3/promotions/{first['generation']}.json"
            history = publication.json.loads(store.read_bytes(publication.ASSETS, path))
            history.update(acks={"hf": True, "pages": True}, created_at="2026-09-01T00:00:00+00:00")
            store.put_bytes(publication.ASSETS, path, publication.encode(history))
            second = publication.stage(store, fixture.candidate("b" * 64, "repo\0other.pdf"), fixture.bundle, apply=True)
            latest = publication.promote(store, second["candidate"], first["generation"], apply=True)
            current_path = f"reader-index/v3/promotions/{latest['generation']}.json"
            current = publication.json.loads(store.read_bytes(publication.ASSETS, current_path))
            self.assertEqual(retirement.run(store)["retired"], [])
            current["acks"] = {"hf": True, "pages": True}
            store.put_bytes(publication.ASSETS, current_path, publication.encode(current))
            result = retirement.run(store, apply=True, now=datetime(2026, 10, 11, tzinfo=timezone.utc))
            self.assertIn(path, result["retired"])
            self.assertIn(second["processing_root"], result["retired"])
            self.assertIn(second["candidate"]["path"], result["retired"])
            self.assertNotIn(current_path, result["retired"])
            self.assertEqual(publication.current(store)[0]["generation"], latest["generation"])
        finally:
            fixture.temp.cleanup()
