"""Immutable staging must not turn partial writes into publication."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts import search_artifact_store as artifacts
from scripts import search_generation, search_snapshot
from tests.test_search_generation import compact_bytes


class MemoryStore:
    def __init__(self):
        self.files = {}
        self.writes = []
        self.lost_responses = set()

    def read_optional(self, key):
        return self.files.get(key)

    def create_immutable(self, key, data):
        if key in self.files:
            raise FileExistsError(key)
        self.files[key] = data
        self.writes.append(key)
        if key in self.lost_responses:
            raise TimeoutError("write response lost")


class SearchArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        source, tree, browser, revisions = (root / n for n in ("source.json", "tree.json", "browser.json", "revisions.json"))
        source.write_bytes(compact_bytes({"v": 2, "rp": ["VoiceOfML/Test"], "fd": [[]],
                                          "rc": [[0, "Book", "pdf", 0, 20, 0]]}))
        tree.write_bytes(compact_bytes({"VoiceOfML/Test": []}))
        browser.write_bytes(compact_bytes({"VoiceOfML/Test": {"": {"d": [], "f": []}}}))
        revisions.write_bytes(compact_bytes({"VoiceOfML/Test": "a" * 40}))
        self.directory = root / "output"
        self.report = search_generation.build_generation(source, tree, browser, revisions, self.directory)
        self.store = MemoryStore()

    def tearDown(self):
        self.temporary.cleanup()

    def test_stage_deduplicates_objects_and_is_idempotent(self):
        first = artifacts.stage_generation(self.directory, self.store)
        self.assertFalse(first["publication_ready"])
        self.assertFalse(first["pointer_written"])
        self.assertNotIn("published/current.json", self.store.files)
        self.assertEqual(first["objects"] + 1, len(self.store.writes))
        self.assertGreater(first["logical_files"], first["objects"])
        before = list(self.store.writes)
        self.assertEqual(artifacts.stage_generation(self.directory, self.store), first)
        self.assertEqual(self.store.writes, before, "existing bytes are verified rather than rewritten")
        key = f"generations/{first['generation']}/manifest.json"
        manifest = json.loads(self.store.files[key])
        self.assertEqual(manifest["generation"], first["generation"])
        self.assertFalse(manifest["publication_ready"])

    def test_conflicting_existing_object_prevents_manifest_creation(self):
        manifest = artifacts.make_manifest(self.report)
        key = next(iter(manifest["objects"]["pages"].values()))["object"]
        self.store.files[key] = b"different bytes"
        with self.assertRaisesRegex(ValueError, "differs"):
            artifacts.stage_generation(self.directory, self.store)
        self.assertNotIn(f"generations/{self.report['generation']}/manifest.json", self.store.files)
        self.assertNotIn("published/current.json", self.store.files)

    def test_lost_create_response_is_read_back_not_retried(self):
        manifest = artifacts.make_manifest(self.report)
        key = next(iter(manifest["objects"]["pages"].values()))["object"]
        self.store.lost_responses.add(key)
        result = artifacts.stage_generation(self.directory, self.store)
        self.assertEqual(self.store.writes.count(key), 1)
        self.assertFalse(result["pointer_written"])

    def test_local_data_change_fails_before_remote_write(self):
        (self.directory / "pages/initial/global.json").write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaises(ValueError):
            artifacts.stage_generation(self.directory, self.store)
        self.assertEqual(self.store.writes, [])

    def test_existing_manifest_mismatch_fails_closed_before_writes(self):
        key = f"generations/{self.report['generation']}/manifest.json"
        self.store.files[key] = b"{broken}"
        with self.assertRaisesRegex(ValueError, "invalid existing"):
            artifacts.stage_generation(self.directory, self.store)
        self.assertEqual(self.store.files[key], b"{broken}")
        self.assertEqual(self.store.writes, [])
        self.assertNotIn("published/current.json", self.store.files)

    def test_existing_candidate_observations_are_preserved_not_upgraded(self):
        key = f"generations/{self.report['generation']}/manifest.json"
        original = {**artifacts.make_manifest(self.report), "additional_checks": {"source": "unverified"}}
        self.store.files[key] = search_snapshot.canonical(original)
        result = artifacts.stage_generation(self.directory, self.store)
        self.assertEqual(self.store.files[key], search_snapshot.canonical(original))
        self.assertIs(result["publication_ready"], False)
        self.assertNotIn(key, self.store.writes)
        original["publication_ready"] = True
        self.store.files[key] = search_snapshot.canonical(original)
        before = list(self.store.writes)
        with self.assertRaisesRegex(ValueError, "does not match"):
            artifacts.stage_generation(self.directory, self.store)
        self.assertEqual(self.store.writes, before)


if __name__ == "__main__":
    unittest.main()
