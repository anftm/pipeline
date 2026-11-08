"""Separate site receipts require independently observed deployed bytes."""
import hashlib
import json
import unittest

from scripts import search_consumer_receipts as receipts
from tests.test_search_adopt import SearchAdoptionTests
from tests.test_search_generation import compact_bytes


class ConsumerReceiptTests(SearchAdoptionTests):
    def setUp(self):
        super().setUp()
        self.space["files"] = len(self.manifest["objects"]["space"])
        self.urls[receipts.SPACE + "/api/search-data-generation"] = compact_bytes(self.space)

    def test_individual_receipts_bind_generation_and_measured_values(self):
        for site in ("pages", "space"):
            result = json.loads(receipts.observe(site, self.generation, self.manifest, get=self.get,
                                                 timestamp="2026-01-01T00:00:00Z"))
            self.assertEqual(result["site"], site)
            self.assertEqual(result["generation"], self.generation)
            self.assertEqual(result["records"], 1)
            self.assertEqual(result["observed_at"], "2026-01-01T00:00:00Z")
            if site == "pages":
                self.assertEqual(result["details"]["corpus_sha256"], hashlib.sha256(self.pages_corpus).hexdigest())
            else:
                self.assertEqual(result["details"]["files"], self.space["files"])
                self.assertEqual(result["details"]["api_generation"], "api:current")

    def test_pages_rejects_missing_or_corrupt_actual_bytes_and_count(self):
        for suffix, value in (("/data/generation-manifest.json", b"{}"),
                              ("/data/search_data.json.gz", b"corrupt"),
                              ("/data/initial/global.json", b'{"total":2}')):
            url = receipts.PAGES + suffix
            before = self.urls[url]
            self.urls[url] = value
            with self.subTest(url=url), self.assertRaises(ValueError):
                receipts.observe("pages", self.generation, self.manifest, get=self.get)
            self.urls[url] = before

    def test_space_rejects_bad_generation_count_members_or_unverified(self):
        url = receipts.SPACE + "/api/search-data-generation"
        for field, value in (("generation", "a" * 64), ("records", 2), ("files", 0),
                             ("verified", False), ("api_generation", "")):
            self.urls[url] = compact_bytes({**self.space, field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                receipts.observe("space", self.generation, self.manifest, get=self.get)

    def test_invalid_input_or_json_never_generates_receipt(self):
        with self.assertRaises(ValueError):
            receipts.observe("bad", self.generation, self.manifest, get=self.get)
        with self.assertRaises(ValueError):
            receipts.observe("pages", "bad", self.manifest, get=self.get)
        with self.assertRaises(TypeError):
            receipts.observe("pages", self.generation, self.manifest, get=lambda _u, _n: b"[]")


if __name__ == "__main__":
    unittest.main()
