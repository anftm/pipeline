"""The daily guard never equates a desired pointer with loaded consumers."""
import json
import unittest

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
        result = search_daily.inspect_published(get=lambda url: urls[url])
        self.assertEqual(result["generation"], self.generation)
        self.assertTrue(result["both_consumers_observed"])
        self.assertEqual(result["record_count"], 1)
        for url in (search_daily.PAGES_MANIFEST, search_daily.SPACE_RECEIPT):
            saved = urls[url]
            altered = json.loads(saved)
            altered["generation"] = "b" * 64
            urls[url] = compact(altered)
            with self.subTest(url=url), self.assertRaises(ValueError):
                search_daily.inspect_published(get=lambda value: urls[value])
            urls[url] = saved

    def test_pointer_or_manifest_unavailable_never_reports_synced(self):
        urls = self.urls_for_published()
        with self.assertRaises(KeyError):
            search_daily.inspect_published(get=lambda _url: (_ for _ in ()).throw(KeyError("not found")))
        key = search_daily.BUCKET_RESOLVE + f"generations/{self.generation}/manifest.json"
        value = json.loads(urls[key])
        value["record_count"] = 123
        urls[key] = compact(value)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            search_daily.inspect_published(get=lambda url: urls[url])

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
