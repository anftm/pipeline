import tempfile
import unittest
from pathlib import Path

from scripts import review_crops


class ReviewCropTests(unittest.TestCase):
    def test_page_override_controls_conditional_crop(self):
        article = {
            "page_start": 1,
            "page_end": 2,
            "ocr": {"content_thresholds": [0.1, 0.1, 0.1, 0.1]},
            "ocr_exceptions": {"2": {"content_thresholds": [0, 0, 0, 0]}},
        }
        self.assertEqual(review_crops.effective_crop(article, 1), [0.1, 0.1, 0.1, 0.1])
        self.assertIsNone(review_crops.effective_crop(article, 2))

    def test_crop_page_count_is_bounded_before_download(self):
        request = {"articles": [{
            "title": "长文", "page_start": 1, "page_end": review_crops.MAX_ARTICLE_CROP_PAGES + 1,
            "ocr": {"content_thresholds": [0.1, 0.1, 0.1, 0.1]},
        }]}
        with self.assertRaisesRegex(RuntimeError, "too many cropped review pages"):
            review_crops.requested_crops(request)

    def test_source_path_supports_rasters_and_pdf_previews(self):
        files = {
            "one": {"path": "archives25/one.webp"},
            "pdf": {"path": "archives25/file.pdf", "page_previews": {"paths": ["p1.webp", "p2.webp"]}},
        }
        self.assertEqual(review_crops.source_path(["one"], files, 1), "archives25/one.webp")
        self.assertEqual(review_crops.source_path(["pdf"], files, 2), "p2.webp")
        with self.assertRaisesRegex(RuntimeError, "cannot provide page 2"):
            review_crops.source_path(["one"], files, 2)

    def test_montage_is_a_bounded_webp(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            Image.new("RGB", (100, 80), "white").save(source)

            class Api:
                @staticmethod
                def hf_hub_download(**_kwargs):
                    return str(source)

            value = review_crops.montage_bytes(
                Api(), "revision", ["one"], {"one": {"path": "archives25/one.png"}},
                [(1, [0.1, 0.1, 0.1, 0.1])],
            )
            self.assertLessEqual(len(value), review_crops.MAX_OUTPUT_BYTES)
            output = Path(directory) / "crop.webp"
            output.write_bytes(value)
            with Image.open(output) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertGreater(image.width, 0)
                self.assertGreater(image.height, 0)
