"""The daily guard never equates a desired pointer with loaded consumers."""
import json
import unittest
from unittest.mock import patch

from scripts import search_daily
from scripts.search_publication_pointer import make_pointer
from tests.test_search_adopt import SearchAdoptionTests
from tests.test_search_artifact_store import MemoryStore


class SearchDailyTests(SearchAdoptionTests):
    def urls_for_published(self):
        urls = dict(self.urls)
        urls[search_daily.BUCKET_RESOLVE + "published/current.json"] = make_pointer(self.generation, None, "1" * 32)
        urls[search_daily.BUCKET_RESOLVE + f"generations/{self.generation}/manifest.json"] = self.hub.objects[
            f"generations/{self.generation}/manifest.json"]
        return urls

    def test_both_consumers_must_acknowledge_pointer(self):
        urls = self.urls_for_published()
        result = search_daily.inspect_published(get=lambda url, _limit: urls[url])
        self.assertEqual(result["generation"], self.generation)
        self.assertTrue(result["both_consumers_observed"])
        self.assertEqual(result["record_count"], 1)
        for url in (search_daily.PAGES_MANIFEST, search_daily.SPACE_RECEIPT):
            saved = urls[url]
            altered = json.loads(saved)
            altered["generation"] = "b" * 64
            urls[url] = compact(altered)
            with self.subTest(url=url), self.assertRaises(ValueError):
                search_daily.inspect_published(get=lambda value, _limit: urls[value])
            urls[url] = saved

    def test_pointer_or_manifest_unavailable_never_reports_synced(self):
        urls = self.urls_for_published()
        with self.assertRaises(KeyError):
            search_daily.inspect_published(get=lambda _url, _limit: (_ for _ in ()).throw(KeyError("not found")))
        key = search_daily.BUCKET_RESOLVE + f"generations/{self.generation}/manifest.json"
        value = json.loads(urls[key])
        value["record_count"] = 123
        urls[key] = compact(value)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            search_daily.inspect_published(get=lambda url, _limit: urls[url])

    def test_bad_pages_corpus_or_initial_payload_never_counts_as_observed(self):
        urls = self.urls_for_published()
        for suffix in ("/data/search_data.json.gz", "/data/initial/global.json"):
            url = search_daily.PAGES_MANIFEST.removesuffix("/data/generation-manifest.json") + suffix
            before = urls[url]
            urls[url] = b"{}"
            with self.subTest(url=url), self.assertRaises(ValueError):
                search_daily.inspect_published(get=lambda value, _limit: urls[value])
            urls[url] = before

    def test_pointer_changing_during_observation_is_rejected(self):
        urls = self.urls_for_published()
        pointer_url = search_daily.BUCKET_RESOLVE + "published/current.json"
        reads = []

        def get(url, _limit):
            if url == pointer_url:
                reads.append(url)
                if len(reads) == 2:
                    return make_pointer(self.generation, None, "2" * 32)
            return urls[url]

        with self.assertRaisesRegex(ValueError, "pointer changed"):
            search_daily.inspect_published(get=get)
        self.assertEqual(len(reads), 2)

    def test_receipt_and_baseline_are_written_exclusively_after_verification(self):
        urls = self.urls_for_published()
        baseline = self.root / "baseline.json"
        receipt = self.root / "receipt.json"
        expected = search_daily.inspect_published(get=lambda url, _limit: urls[url])
        with patch.object(search_daily, "inspect_published", return_value=expected):
            self.assertEqual(search_daily.main(["inspect", "--baseline-output", str(baseline),
                                               "--receipt-output", str(receipt)]), 0)
            self.assertEqual(json.loads(baseline.read_text()), self.manifest["source_revisions"])
            self.assertEqual(json.loads(receipt.read_text()), expected)
            self.assertEqual(search_daily.main(["inspect", "--expected-receipt", str(receipt)]), 0)
            with self.assertRaises(FileExistsError):
                search_daily.main(["inspect", "--baseline-output", str(baseline)])
        changed = {**expected, "transition": "2" * 32}
        with patch.object(search_daily, "inspect_published", return_value=changed), \
                self.assertRaisesRegex(ValueError, "changed during the run"):
            search_daily.main(["inspect", "--expected-receipt", str(receipt)])

    def test_failed_consumer_verification_never_writes_baseline(self):
        baseline = self.root / "baseline.json"
        with patch.object(search_daily, "inspect_published", side_effect=ValueError("no consumer")), \
                self.assertRaisesRegex(ValueError, "no consumer"):
            search_daily.main(["inspect", "--baseline-output", str(baseline)])
        self.assertFalse(baseline.exists())

    def test_stage_does_not_write_mutable_pointer(self):
        store = MemoryStore()
        self.root.joinpath("second").mkdir()
        source, tree, browser, revisions = [self.root / n for n in ("source", "tree", "browser", "revisions")]
        result = search_daily.stage(source, tree, browser, revisions, self.root / "second/output", store)
        self.assertTrue(result["staged"])
        self.assertFalse(result["pointer_written"])
        self.assertFalse(result["consumers_verified"])
        self.assertNotIn("published/current.json", store.files)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


if __name__ == "__main__":
    unittest.main()
