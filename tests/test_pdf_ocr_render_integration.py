"""Explicit local Poppler acceptance: no model download and no network."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from scripts import pdf_ocr, pdf_ocr_stages as stages


class RealPdfRenderingTests(unittest.TestCase):
    def test_small_scanned_pdf_renders_lossless_ocr_input_before_recognition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "small.pdf"
            with Image.new("RGB", (300, 400), "white") as image:
                ImageDraw.Draw(image).text((30, 50), "OCR input acceptance", fill="black")
                image.save(source, "PDF", resolution=150)
            self.assertLess(source.stat().st_size, 100 * 1024 ** 2)
            bundle = root / "bundle"
            with patch.object(pdf_ocr, "ocr_page", side_effect=AssertionError("rendering must not OCR")):
                result = stages.render_book({"key": "test\0small.pdf", "source_revision": "test"}, source, bundle)
            manifest = json.loads((bundle / result["render_manifest"]["path"]).read_text())
            stages.validate_render(result, manifest)
            self.assertEqual(len(manifest["pages"]), 1)
            page = manifest["pages"][0]
            self.assertEqual(page["source"], "ocr")
            with Image.open(bundle / page["i"]) as png:
                self.assertEqual(png.format, "PNG")
                self.assertEqual(png.size, (600, 800))
            with Image.open(bundle / page["w"]) as webp:
                self.assertEqual(webp.format, "WEBP")
            self.assertNotIn("o", page)


if __name__ == "__main__":
    unittest.main()
