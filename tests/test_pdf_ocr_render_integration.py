"""Explicit local Poppler acceptance: no model download and no network."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from scripts import pdf_ocr, pdf_ocr_stages as stages, plan_pdf_ocr, ocr_quality, pdf_assets


class RealPdfRenderingTests(unittest.TestCase):
    def test_native_text_and_highlight_offsets_use_the_same_order(self):
        import gzip
        import pymupdf
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'native.pdf'
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((50, 60), 'First native line with several words and punctuation.')
            page.insert_text((50, 90), 'Second native line is searchable with aligned coordinates.')
            document.save(source)
            document.close()
            result = stages.render_book({'key': 'test\0native.pdf', 'source_revision': 'test'}, source, root / 'bundle')
            manifest = json.loads((root / 'bundle' / result['render_manifest']['path']).read_text())
            payload = json.loads(gzip.decompress((root / 'bundle' / manifest['pages'][0]['o']).read_bytes()))
            self.assertEqual(ocr_quality.check_page(payload)['status'], 'passed')
            for span in payload['text_spans']:
                self.assertEqual(payload['text'][span['start']:span['end']], payload['raw_blocks'][span['block']]['t'])
            self.assertIn('First native line', payload['extracted_text'])

    def test_legacy_document_entrypoint_cannot_recreate_pdf_images_even_for_large_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, bundle = root / 'scan.pdf', root / 'bundle'
            bundle.mkdir()
            with Image.new('RGB', (300, 400), 'white') as image:
                ImageDraw.Draw(image).text((25, 80), 'Page image', fill='black')
                image.save(source, 'PDF', resolution=150)
            with patch.object(pdf_assets, 'LARGE_BYTES', 0):
                result = pdf_assets.build_item({'key': 'test\0scan.pdf', 'extension': 'pdf'}, source, bundle)
            self.assertEqual(result['status'], 'skipped')
            self.assertEqual(result['strategy'], 'preserve-pdf')
            self.assertNotIn('page_manifest', result)
            self.assertEqual(list(bundle.rglob('*')), [])

    def test_time_estimator_samples_real_pages_locally_and_builds_a_compact_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "sample.pdf"
            with Image.new("RGB", (100, 150), "white") as image:
                image.save(source, "PDF", resolution=100, save_all=True, append_images=[image, image])
            record = {"key": "test\0sample.pdf", "source_revision": "test", "source_kind": "upstream"}
            with patch.object(plan_pdf_ocr, "download_source", return_value=source):
                queue = plan_pdf_ocr.plan([record], workers=1,
                    render_estimator=lambda item, path: stages.estimate_render_cost(item, path, {}))
            self.assertEqual(queue["failed"], [])
            cost = queue["shards"][0]["records"][0]["_render_cost"]
            self.assertEqual((cost["source"], cost["samples"]), ("sample", 3))
            self.assertGreater(cost["seconds_per_page"], 0)
            timed = stages.plan_render_ranges(queue, {}, 1500)
            tasks = [task for shard in timed["shards"] for task in shard["records"]]
            self.assertNotIn("probe", tasks[0])
            self.assertEqual(stages.expand_render_tasks(timed, tasks)[0]["probe"]["page_count"], 3)

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
            self.assertNotIn('w', page)
            self.assertNotIn('j', page)
            self.assertIsNone(result['page_manifest'])
            self.assertNotIn("o", page)

    def test_low_resolution_full_page_scan_preserves_ocr_pixels_but_not_reader_upscale(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "low-res.pdf"
            with Image.new("RGB", (300, 400), "white") as image:
                ImageDraw.Draw(image).text((30, 50), "Source pixels", fill="black")
                image.save(source, "PDF", resolution=100)
            self.assertEqual(pdf_ocr.scan_reader_images(source, 1, 1), {1: (300, 400)})
            def encode(reader_png, destination):
                with Image.open(reader_png) as image:
                    self.assertLessEqual(image.width, 300)
                    self.assertLessEqual(image.height, 400)
                destination.write_bytes(b"jxl")
            with patch.object(pdf_ocr, "JXL_ENABLED", True), patch.object(pdf_ocr, "encode_jxl", side_effect=encode):
                bundle = root / "bundle"
                result = stages.render_book({"key": "test\0low-res.pdf", "source_revision": "test"}, source, bundle)
            page = json.loads((bundle / result["render_manifest"]["path"]).read_text())["pages"][0]
            with Image.open(bundle / page["i"]) as png:
                self.assertGreater(png.width, 300)
                self.assertGreater(png.height, 400)
            self.assertNotIn('w', page)
            self.assertNotIn('j', page)

    def test_small_embedded_image_is_not_treated_as_full_page_scan(self):
        listing = ("page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio\n"
                   "----\n 1 0 image 300 400 rgb 3 8 jpeg no 1 0 100 100 3000B 1%")
        def inspect(command):
            return listing if command[0] == "pdfimages" else "Page 1 size: 612 x 792 pts\n"
        with patch.object(pdf_ocr, "_run", side_effect=inspect):
            self.assertEqual(pdf_ocr.scan_reader_images(Path("sample.pdf"), 1, 1), {})

    def test_obsolete_pdf_reader_flags_only_render_lossless_ocr_png(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "large-page.pdf"
            with Image.new("RGB", (800, 1100), "white") as image:
                image.save(source, "PDF", resolution=150)
            png, width, height = pdf_ocr.render_page(source, 1, root, reader_jxl=True)
            reader_png = png.with_name(png.stem + "-reader.ppm")
            with Image.open(png) as full:
                self.assertEqual(full.size, (width, height))
                self.assertGreater(full.height, 1800)
            self.assertFalse(reader_png.exists())
            self.assertFalse(png.with_suffix('.webp').exists())

    def test_unresolved_font_warning_refuses_an_unreliable_ocr_page(self):
        """A missing CMap/CJK font must fail the render, not OCR a blank page."""
        from PIL import Image as PILImage
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "cjk-cid.pdf"
            with Image.new("RGB", (200, 300), "white") as image:
                image.save(source, "PDF", resolution=100)
            warning = ("Syntax Error: Couldn't find a font for '<cb><ce><cc><e5>'\n"
                       "some font thing failed\n")
            with patch.object(pdf_ocr, "_run", return_value=("", warning)):
                with self.assertRaisesRegex(RuntimeError, "poppler-data"):
                    pdf_ocr.render_page(source, 1, root)
            png, width, height = pdf_ocr.render_page(source, 1, root)
            with PILImage.open(png) as image:
                self.assertEqual(image.size, (width, height))

    def test_reader_quality_uses_clean_scan_only_with_or_without_jxl(self):
        with Image.new("RGB", (128, 128), "white") as clean, \
                Image.new("RGB", (128, 128), (196, 166, 115)) as yellowed, \
                Image.new("RGB", (128, 128), "black") as damaged:
            self.assertEqual(pdf_ocr.reader_webp_quality(clean, True), 80)
            self.assertEqual(pdf_ocr.reader_webp_quality(clean, False), 85)
            self.assertEqual(pdf_ocr.reader_webp_quality(yellowed, True), 85)
            self.assertEqual(pdf_ocr.reader_webp_quality(damaged, True), 85)
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "clean.pdf"
                clean.save(source, "PDF", resolution=100)
                for jxl in (False, True):
                    with self.subTest(jxl=jxl), patch.object(pdf_ocr, "reader_webp_quality",
                                                           wraps=pdf_ocr.reader_webp_quality) as select:
                        pdf_ocr.render_page(source, 1, root, (128, 128), reader_jxl=jxl)
                        select.assert_not_called()


if __name__ == "__main__":
    unittest.main()
