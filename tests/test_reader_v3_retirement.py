from datetime import datetime, timezone
import unittest

from scripts import publish_reader_v3 as publication, reader_v3_retirement as retirement
from tests.test_publish_reader_v3 import V3PublicationTests


class RetirementTests(unittest.TestCase):
    def test_only_acknowledged_old_generations_retire_and_current_survives(self):
        fixture = V3PublicationTests()
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
            self.assertNotIn(current_path, result["retired"])
            self.assertEqual(publication.current(store)[0]["generation"], latest["generation"])
        finally:
            fixture.temp.cleanup()
