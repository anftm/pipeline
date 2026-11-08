"""Behavior of the offline two-site search-generation preparer."""
import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts import search_generation, search_snapshot, sync_to_space


def compact_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class SearchGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.json"
        self.tree = self.root / "tree.json"
        self.browser = self.root / "browser.json"
        self.revisions = self.root / "revisions.json"
        self.output = self.root / "output"
        self.payload = {"v": 2, "rp": ["VoiceOfML/Test"], "fd": [[], ["Docs"]], "rc": [
            [0, "Book", "pdf", 0, 20, 1], [0, "中文", "txt", 1, "", 0],
        ]}
        self.source.write_bytes(compact_bytes(self.payload))
        self.tree.write_bytes(compact_bytes({"VoiceOfML/Test": []}))
        self.browser.write_bytes(compact_bytes({"VoiceOfML/Test": {
            "": {"d": [], "f": [{"n": "Book", "e": "pdf", "t": True, "s": 20}]},
        }}))
        self.revisions.write_text(json.dumps({"VoiceOfML/Test": "a" * 40}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, output=None):
        return search_generation.build_generation(self.source, self.tree, self.browser, self.revisions,
                                                  output or self.output)

    def test_both_surfaces_are_reproducible_and_bind_normalized_corpus(self):
        first = self.build()
        second = self.build(self.root / "other-output")
        self.assertEqual(first, second)
        self.assertEqual(first["record_count"], 2)
        self.assertEqual(first["remote_writes"], 0)
        self.assertIs(first["publication_ready"], False)
        self.assertEqual(json.loads((self.output / "snapshot-report.json").read_bytes()), first)
        normalized = json.loads((self.output / "normalized-source.json").read_bytes())
        self.assertEqual([row[5] for row in normalized["rc"]], [0, 0])
        for surface in ("pages", "space"):
            path = self.output / surface / "search_data.json.gz"
            self.assertEqual(json.loads(gzip.decompress(path.read_bytes())), normalized)
            browser = json.loads(gzip.decompress((self.output / surface / "folder_browser.json.gz").read_bytes()))
            self.assertFalse(browser["VoiceOfML/Test"][""]["f"][0]["t"])
        self.assertEqual(search_snapshot.verify_manifest(first, self.output / "pages", self.output / "space",
                                                         self.output / "normalized-source.json"), first)
        self.assertEqual(set(first["files"]["space"]) - set(first["files"]["pages"]),
                         set(search_snapshot.SPACE_INDEXES))
        self.assertEqual(json.loads(self.source.read_bytes()), self.payload, "input must not be rewritten")

    def test_missing_or_malformed_rows_are_not_silently_discarded(self):
        for bad in ([], [1], [[True, "Book", "pdf", 0, 20, 0]],
                    [[0, "Book", "pdf", 9, 20, 0]], [[0, "Book", "pdf", 0, 20, 2]],
                    [[0, "Book", "pdf", 0, 20, False]]):
            with self.subTest(rows=bad):
                payload = copy.deepcopy(self.payload)
                payload["rc"] = bad
                self.source.write_bytes(compact_bytes(payload))
                with self.assertRaises(ValueError):
                    self.build()
                self.assertFalse(self.output.exists())

    def test_revisions_and_directory_metadata_require_exact_coverage(self):
        self.revisions.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'revisions'):
            self.build()
        self.revisions.write_text(json.dumps({"VoiceOfML/Test": "not-a-revision"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, 'revisions'):
            self.build()
        self.assertFalse(self.output.exists())
        self.revisions.write_text(json.dumps({"VoiceOfML/Test": "a" * 40}), encoding="utf-8")
        self.tree.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'exactly cover'):
            self.build()
        self.tree.write_text('[]', encoding='utf-8')
        with self.assertRaises(TypeError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_existing_output_and_input_symlink_are_rejected(self):
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, 'already exist'):
            self.build()
        self.output.rmdir()
        real = self.root / "real.json"
        real.write_bytes(self.source.read_bytes())
        self.source.unlink()
        self.source.symlink_to(real)
        with self.assertRaisesRegex(ValueError, 'ordinary file'):
            self.build()
        self.assertFalse(self.output.exists())

    def test_corruption_in_generated_index_is_detected_by_existing_snapshot_checks(self):
        self.build()
        target = self.output / "space/search_ngrams_2.bin.gz"
        target.write_bytes(gzip.compress(b"invalid", mtime=0))
        manifest = json.loads((self.output / "snapshot-report.json").read_bytes())
        with self.assertRaisesRegex(ValueError, 'does not bind'):
            search_snapshot.verify_manifest(manifest, self.output / "pages", self.output / "space",
                                            self.output / "normalized-source.json")

    def test_extracted_corpus_equivalence_is_independent_of_site_decoder(self):
        self.build()
        expected = copy.deepcopy(self.payload)
        for row in expected["rc"]:
            row[5] = 0
        self.assertEqual(json.loads((self.output / "normalized-source.json").read_bytes()), expected)
        self.assertEqual(len(sync_to_space.decode_search_payload(expected)), 2)


if __name__ == '__main__':
    unittest.main()
