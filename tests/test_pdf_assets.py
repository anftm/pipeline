import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import pdf_assets


class PdfAssetsTests(unittest.TestCase):
    def test_compact_records_are_decoded_and_queue_is_stable(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            search = root / "search.json"
            revisions = root / "commits.json"
            search.write_text(json.dumps({"rp": ["VoiceOfML/A"], "fd": [[]],
                "rc": [[0, "book", "pdf", 0, 60000000, False],
                       [0, "other", "txt", 0, 1, False]]}), encoding="utf-8")
            revisions.write_text(json.dumps({"VoiceOfML/A": "rev"}), encoding="utf-8")
            records = pdf_assets.load_records(search, revisions)
            self.assertEqual([x["path"] for x in pdf_assets.queue(records, 1, 0)], ["book.pdf"])
            self.assertEqual(pdf_assets.queue(records, 1, 1), [])

    def test_small_pdf_is_skipped_without_tools(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "x.pdf"
            source.write_bytes(b"x" * (pdf_assets.MIN_BYTES - 1))
            item = {"key": "r\0x.pdf", "repo": "r", "path": "x.pdf", "source_revision": "rev"}
            with patch.object(pdf_assets, "_run") as run:
                result = pdf_assets.build_item(item, source, root / "bundle")
            self.assertEqual(result["status"], "skipped")
            run.assert_not_called()

    def test_weighted_shards_use_descending_page_count_and_stable_ties(self):
        records = [{"key": key, "page_count": pages} for key, pages in (
            ("a", 10), ("b", 9), ("c", 8), ("d", 7), ("e", 6), ("f", 5))]
        shards = pdf_assets.weighted_shards(records, 3)
        self.assertEqual([[item["key"] for item in shard] for shard in shards],
                         [["a", "f"], ["b", "e"], ["c", "d"]])
        self.assertEqual([sum(item["page_count"] for item in shard) for shard in shards], [15, 15, 15])

    def test_weighted_shards_tie_breaks_by_key(self):
        records = [{"key": key, "page_count": 4} for key in ("c", "a", "b")]
        shards = pdf_assets.weighted_shards(records, 2)
        self.assertEqual([[item["key"] for item in shard] for shard in shards], [["a", "c"], ["b"]])

    def test_publish_manifest_is_separate_and_content_addressed(self):
        result = {"key": "r\0x.pdf", "status": "skipped", "reason": "estimated-webp-over-90-percent",
                  "strategy": "sampled-webp", "source_sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as root:
            manifest, operations = pdf_assets.build_publish(pdf_assets.empty_manifest(), [result], Path(root))
        self.assertIn("r\0x.pdf", manifest["files"])
        self.assertEqual(operations[-1].path_in_repo, "pdf_manifest.json")
        self.assertEqual(pdf_assets.MANIFEST_NAME, "pdf_manifest.json")


if __name__ == "__main__":
    unittest.main()
