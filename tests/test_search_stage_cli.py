"""The generation CLI defaults offline and cannot imply consumer acceptance."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import search_stage_cli
from tests.test_search_artifact_store import MemoryStore
from tests.test_search_generation import compact_bytes


class SearchStageCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        payload = {"v": 2, "rp": ["VoiceOfML/Test"], "fd": [[]], "rc": [[0, "Book", "pdf", 0, 5, 0]]}
        values = {"source.json": payload, "tree.json": {"VoiceOfML/Test": []},
                  "browser.json": {"VoiceOfML/Test": {"": {"d": [], "f": []}}},
                  "revisions.json": {"VoiceOfML/Test": "a" * 40}}
        for name, value in values.items():
            (self.root / name).write_bytes(compact_bytes(value))
        self.args = ["--source", str(self.root / "source.json"), "--tree", str(self.root / "tree.json"),
                     "--browser", str(self.root / "browser.json"), "--revisions", str(self.root / "revisions.json"),
                     "--output", str(self.root / "out")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_does_not_create_a_remote_adapter(self):
        with patch.object(search_stage_cli, "S3SearchArtifactStore", side_effect=AssertionError("must be offline")):
            self.assertEqual(search_stage_cli.main(self.args), 0)
        self.assertFalse(json.loads((self.root / "out/snapshot-report.json").read_text())["publication_ready"])

    def test_stage_stores_immutable_objects_without_pointer(self):
        store = MemoryStore()
        with patch.object(search_stage_cli, "S3SearchArtifactStore", return_value=store):
            self.assertEqual(search_stage_cli.main(self.args + ["--stage"]), 0)
        self.assertTrue(any(k.endswith("/manifest.json") for k in store.files))
        self.assertNotIn("published/current.json", store.files)

    def test_invalid_revision_never_creates_remote_adapter(self):
        (self.root / "revisions.json").write_text('{"VoiceOfML/Test":"invalid"}', encoding="utf-8")
        with patch.object(search_stage_cli, "S3SearchArtifactStore", side_effect=AssertionError("must not write")), \
                self.assertRaises(ValueError):
            search_stage_cli.main(self.args + ["--stage"])


if __name__ == "__main__":
    unittest.main()
