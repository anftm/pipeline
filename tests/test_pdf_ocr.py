import gzip
import json
import os
import subprocess
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

    def test_language_selects_supported_model_without_changing_render_profile(self):
        root = Path(__file__).resolve().parents[1]
        script = "from scripts import pdf_ocr; print(pdf_ocr.OCR_VERSION); print(pdf_ocr.asset_profile()); print(pdf_ocr.OCR_ENGINE)"
        for language, version in (("en", "PP-OCRv6"), ("japan", "PP-OCRv6"),
                                  ("ar", "PP-OCRv5"), ("fa", "PP-OCRv5"), ("korean", "PP-OCRv5")):
            with self.subTest(language=language):
                env = {**os.environ, "PDF_OCR_LANG": language}
                output = subprocess.check_output(["python3", "-c", script], cwd=root, env=env, text=True)
                lines = output.splitlines()
                self.assertEqual(lines[0], version)
                self.assertIn(f"-lang-{language}-", lines[1])
                self.assertIn(language, lines[2])

        for backend in ("paddle_onnxruntime", "rapidocr_onnxruntime"):
            env = {**os.environ, "PDF_OCR_BACKEND": backend}
            output = subprocess.check_output(["python3", "-c", script], cwd=root, env=env, text=True)
            self.assertIn(f"-backend-{backend.replace('_', '-')}", output.splitlines()[1])

        invalid = {**os.environ, "PDF_OCR_LANG": "fa", "PDF_OCR_VERSION": "PP-OCRv6"}
        result = subprocess.run(["python3", "-c", "from scripts import pdf_ocr"], cwd=root,
                                env=invalid, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        invalid_backend = {**os.environ, "PDF_OCR_LANG": "fa", "PDF_OCR_BACKEND": "rapidocr_onnxruntime"}
        result = subprocess.run(["python3", "-c", "from scripts import pdf_ocr"], cwd=root,
                                env=invalid_backend, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_rapidocr_result_uses_shared_block_contract(self):
        result = type("RapidResult", (), {
            "boxes": [[[0, 0], [50, 0], [50, 20], [0, 20]]],
            "txts": ("English",), "scores": (0.95,),
        })()
        blocks = pdf_ocr.normalize_rapid_result(result, 100, 100)
        self.assertEqual(blocks[0]["t"], "English")
        self.assertEqual(blocks[0]["b"], [0.0, 0.0, 0.5, 0.2])

    def test_manifest_accepts_published_jxl_layout_from_separate_ocr_worker(self):
        current = pdf_ocr.asset_profile()
        other = current.replace(f"-jxl-{int(pdf_ocr.JXL_ENABLED)}-",
                                f"-jxl-{int(not pdf_ocr.JXL_ENABLED)}-")
        entry = {"status": "ready", "profile": other + "-layout-v1-" + "a" * 16,
                 "source_sha256": "b" * 64, "page_count": 1,
                 "ocr_manifest": "objects/bb/" + "b" * 64 + "/" + "c" * 16 + "/ocr-manifest.json"}
        manifest = {"version": 1, "files": {"book": entry}}
        self.assertIs(pdf_ocr.validate_manifest(manifest), manifest)
        with self.assertRaises(ValueError):
            pdf_ocr.validate_manifest({"version": 1, "files": {"book": {**entry, "profile": other + "-layout-v1-bad"}}})
        with self.assertRaises(ValueError):
            pdf_ocr.validate_manifest({"version": 1, "files": {"book": {**entry, "profile": other + "-layout-"}}})


if __name__ == "__main__":
    unittest.main()
