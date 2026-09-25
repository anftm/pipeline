import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts import pdf_ocr
from scripts.plan_pdf_ocr import pdf_ocr_shards, recommended_ocr_shard_count


class PdfOcrContractTests(unittest.TestCase):
    def test_normalize_ocr_result_preserves_order_and_normalizes_boxes(self):
        blocks = pdf_ocr.normalize_ocr_result({
            "rec_texts": [" 第二 ", "第一"],
            "rec_scores": [0.91, 0.99],
            "dt_polys": [
                [[100, 200], [300, 200], [300, 250], [100, 250]],
                [[10, 20], [80, 20], [80, 60], [10, 60]],
            ],
        }, 1000, 1000)
        self.assertEqual([block["t"] for block in blocks], ["第一", "第二"])
        self.assertEqual(blocks[0]["b"], [0.01, 0.02, 0.08, 0.06])
        self.assertEqual(blocks[1]["s"], "ocr")

    def test_gzip_page_payload_is_deterministic(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "page.json.gz"
            first = pdf_ocr.write_gzip_json(path, pdf_ocr.page_payload(
                1, 100, 200, [{"t": "正文", "b": [0, 0, 1, 1], "c": 1, "s": "ocr"}], "ocr"))
            bytes_one = path.read_bytes()
            second = pdf_ocr.write_gzip_json(path, pdf_ocr.page_payload(
                1, 100, 200, [{"t": "正文", "b": [0, 0, 1, 1], "c": 1, "s": "ocr"}], "ocr"))
            self.assertEqual(first, second)
            self.assertEqual(bytes_one, path.read_bytes())
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["page"], 1)

    def test_shards_are_page_weighted_and_deterministic(self):
        records = [{"key": f"book-{index}", "page_count": pages} for index, pages in enumerate((100, 90, 80, 70, 60))]
        first = pdf_ocr_shards(records, 3)
        second = pdf_ocr_shards(records, 3)
        self.assertEqual(first, second)
        self.assertEqual(sorted(item["key"] for shard in first for item in shard),
                         sorted(item["key"] for item in records))
        self.assertLessEqual(max(sum(item["page_count"] for item in shard) for shard in first)
                             - min(sum(item["page_count"] for item in shard) for shard in first), 100)

    def test_recommended_shards_amortize_startup_for_page_weighted_queue(self):
        records = [{"key": f"book-{index}", "page_count": pages}
                   for index, pages in enumerate((557, 313, 302, 295, 280, 163, 107, 101, 89, 80,
                                                  79, 70, 40, 40, 33, 27, 25, 24, 22, 17))]
        self.assertEqual(recommended_ocr_shard_count(records), 6)

    def test_recommended_shards_keep_large_book_as_one_record(self):
        records = [{"key": "large", "page_count": 557}, {"key": "small", "page_count": 10}]
        self.assertEqual(recommended_ocr_shard_count(records), 2)

    def test_manifest_rejects_non_object_paths(self):
        manifest = {"version": 1, "files": {"x": {
            "status": "ready", "profile": pdf_ocr.asset_profile(),
            "source_sha256": "a" * 64, "page_count": 1, "ocr_manifest": "../bad"
        }}}
        with self.assertRaises(ValueError):
            pdf_ocr.validate_manifest(manifest)

    def test_manifest_accepts_persisted_ocr_input_png(self):
        path = "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/ocr-input/page-000001.png"
        self.assertEqual(pdf_ocr.validate_ocr_object_path(path, ".png"), path)

    def test_jxl_is_part_of_the_profile_identity(self):
        self.assertIn("-jxl-", pdf_ocr.asset_profile())


if __name__ == "__main__":
    unittest.main()
