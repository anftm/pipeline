"""Adoption binds independently observed consumers and exact object bytes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import search_adopt, search_artifact_store, search_generation
from tests.test_search_artifact_store import MemoryStore
from tests.test_search_generation import compact_bytes
from tests.test_search_publication_pointer import MemoryPointer


class Hub:
    def __init__(self, objects):
        self.objects = objects

    def read_bytes(self, bucket, path):
        if bucket != search_adopt.BUCKET:
            raise AssertionError("unexpected bucket")
        return self.objects[path]


class SearchAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        source, tree, browser, revisions = [self.root / n for n in ("source", "tree", "browser", "revisions")]
        source.write_bytes(compact_bytes({"v": 2, "rp": ["VoiceOfML/Test"], "fd": [[]],
                                          "rc": [[0, "Book", "pdf", 0, 4, 0]]}))
        tree.write_bytes(compact_bytes({"VoiceOfML/Test": []}))
        browser.write_bytes(compact_bytes({"VoiceOfML/Test": {"": {"d": [], "f": []}}}))
        revisions.write_bytes(compact_bytes({"VoiceOfML/Test": "a" * 40}))
        self.output = self.root / "output"
        self.report = search_generation.build_generation(source, tree, browser, revisions, self.output)
        self.generation = self.report["generation"]
        store = MemoryStore()
        search_artifact_store.stage_generation(self.output, store)
        self.hub = Hub(store.files)
        self.manifest = json.loads(store.files[f"generations/{self.generation}/manifest.json"])
        self.pages_corpus = (self.output / "pages/search_data.json.gz").read_bytes()
        self.pages_initial = (self.output / "pages/initial/global.json").read_bytes()
        self.space = {"generation": self.generation, "verified": True, "surface": "space", "records": 1,
                      "api_generation": "api:current"}
        self.urls = {search_adopt.SPACE + "/api/search-data-generation": compact_bytes(self.space),
                     search_adopt.PAGES + "/data/generation-manifest.json": compact_bytes(self.manifest),
                     search_adopt.PAGES + "/data/search_data.json.gz": self.pages_corpus,
                     search_adopt.PAGES + "/data/initial/global.json": self.pages_initial}

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, url, limit):
        data = self.urls[url]
        self.assertLessEqual(len(data), limit)
        return data

    def test_preview_never_writes_and_requires_exact_consumers(self):
        pointer = MemoryPointer()
        result = search_adopt.adopt(self.generation, apply=False, writers_stopped=False, hub=self.hub,
                                    pointer_store=pointer, get=self.get)
        self.assertFalse(result["pointer_written"])
        self.assertFalse(result["external_writers_verified"])
        self.assertEqual(pointer.calls, 0)
        self.assertEqual(result["observed"]["pages_generation"], self.generation)

    def test_apply_requires_declaration_and_writes_once_after_validation(self):
        pointer = MemoryPointer()
        with self.assertRaisesRegex(ValueError, "coordination"):
            search_adopt.adopt(self.generation, apply=True, writers_stopped=False, hub=self.hub,
                               pointer_store=pointer, get=self.get)
        self.assertEqual(pointer.calls, 0)
        result = search_adopt.adopt(self.generation, apply=True, writers_stopped=True, hub=self.hub,
                                    pointer_store=pointer, get=self.get)
        self.assertTrue(result["pointer_written"])
        self.assertFalse(result["external_writers_verified"])
        self.assertEqual(pointer.calls, 1)
        with self.assertRaisesRegex(ValueError, "already exists"):
            search_adopt.adopt(self.generation, apply=True, writers_stopped=True, hub=self.hub,
                               pointer_store=pointer, get=self.get)
        self.assertEqual(pointer.calls, 1)

    def test_corrupted_object_is_never_accepted(self):
        key = self.manifest["objects"]["pages"]["search_data.json.gz"]["object"]
        self.hub.objects[key] = b"bad"
        with self.assertRaisesRegex(ValueError, "readback mismatch"):
            search_adopt.adopt(self.generation, apply=False, writers_stopped=False, hub=self.hub, get=self.get)

    def test_invalid_space_or_pages_stops_before_pointer(self):
        pointer = MemoryPointer()
        url = search_adopt.SPACE + "/api/search-data-generation"
        self.urls[url] = compact_bytes({**self.space, "verified": False})
        with self.assertRaisesRegex(ValueError, "Space has not loaded"):
            search_adopt.adopt(self.generation, apply=True, writers_stopped=True, hub=self.hub,
                               pointer_store=pointer, get=self.get)
        self.assertEqual(pointer.calls, 0)
        self.urls[url] = compact_bytes(self.space)
        self.urls[search_adopt.PAGES + "/data/search_data.json.gz"] = b"wrong"
        with self.assertRaisesRegex(ValueError, "corpus bytes"):
            search_adopt.adopt(self.generation, apply=True, writers_stopped=True, hub=self.hub,
                               pointer_store=pointer, get=self.get)
        self.assertEqual(pointer.calls, 0)

    def test_generation_and_manifest_identity_must_match(self):
        with self.assertRaises(ValueError):
            search_adopt.validate_generation(self.hub, "x" * 64)
        key = f"generations/{self.generation}/manifest.json"
        altered = json.loads(self.hub.objects[key])
        altered["record_count"] += 1
        self.hub.objects[key] = compact_bytes(altered)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            search_adopt.validate_generation(self.hub, self.generation)

    def test_lost_response_is_read_back_and_does_not_assert_credential_isolation(self):
        pointer = MemoryPointer()
        pointer.lose_response = True
        with patch.object(search_adopt.uuid, "uuid4") as u:
            u.return_value.hex = "f" * 32
            result = search_adopt.adopt(self.generation, apply=True, writers_stopped=True, hub=self.hub,
                                        pointer_store=pointer, get=self.get)
        self.assertEqual(result["pointer"]["status"], "written-response-unknown")
        self.assertEqual(pointer.calls, 1)
        self.assertFalse(result["external_writers_verified"])


if __name__ == "__main__":
    unittest.main()
