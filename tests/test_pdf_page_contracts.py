"""Shared page-manifest contracts retained from the retired asset worker suite."""

import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter
from scripts import pdf_assets


class PdfPageContractsTests(unittest.TestCase):
    def test_page_manifest_v2_is_compact_and_paths_are_derived(self):
        pages = [
            {"page": 1, "path": "objects/aa/book/pages/page-000001.webp", "sha256": "a", "bytes": 1},
            {"page": 2, "path": "objects/aa/book/pages/page-000002.webp", "sha256": "b", "bytes": 2},
        ]
        manifest = pdf_assets.compact_page_manifest("c" * 64, "profile", pages, [{"title": "T", "page": 1}])
        self.assertEqual(manifest, {
            "version": 2, "kind": "pdf-pages", "source_sha256": "c" * 64,
            "profile": "profile", "page_count": 2, "toc": [{"title": "T", "page": 1}],
        })
        with self.assertRaisesRegex(ValueError, "derived manifest paths"):
            pdf_assets.compact_page_manifest("c" * 64, "profile", [
                {"page": 1, "path": "objects/aa/book/pages/wrong.webp"},
            ])

    def test_page_manifest_carries_ocr_page_references(self):
        pages = [{"page": 1, "path": "objects/aa/book/pages/page-000001.webp", "sha256": "a", "bytes": 1}]
        ocr = [{"page": 1, "o": "objects/aa/" + "c" * 64 + "/ocr-profile/ocr/page-000001.json.gz",
                "os": "d" * 64, "ob": 20}]
        manifest = pdf_assets.compact_page_manifest("c" * 64, "profile", pages, ocr_pages=ocr)
        self.assertEqual(manifest["ocr"], [{"p": 1, "o": ocr[0]["o"], "os": "d" * 64, "ob": 20}])

    def test_djvu_png_and_each_mixed_codec_carry_independent_integrity(self):
        for codecs in [('png', 'png'), ('png', 'jxl'), ('png', 'webp'), ('webp', 'jxl')]:
            with self.subTest(codecs=codecs):
                pages = [{'page': n, 'path': f'objects/aa/book/pages/page-{n:06d}.{codec}',
                          'sha256': 'a' * 64, 'bytes': n} for n, codec in enumerate(codecs, 1)]
                manifest = pdf_assets.compact_page_manifest('c' * 64, 'profile', pages, source_format='djvu')
                self.assertEqual(manifest['codec'], 'png' if codecs == ('png', 'png') else 'mixed')
                self.assertEqual(manifest['source_format'], 'djvu')
                self.assertEqual([ref['bytes'] for ref in manifest['images']], [1, 2])
                if manifest['codec'] == 'mixed':
                    self.assertEqual([ref['codec'] for ref in manifest['images']], list(codecs))
                for source_format in (None, 'pdf'):
                    with self.assertRaisesRegex(ValueError, 'native DjVu'):
                        pdf_assets.compact_page_manifest('c' * 64, 'profile', pages, source_format=source_format)
                for corrupt in ({'sha256': 'g' * 64}, {'bytes': True}, {'bytes': 0}):
                    with self.assertRaisesRegex(ValueError, 'integrity'):
                        pdf_assets.compact_page_manifest('c' * 64, 'profile', [{**pages[0], **corrupt}, pages[1]],
                                                         source_format='djvu')

    def test_extract_pdf_outline_returns_page_and_depth_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "outlined.pdf"
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=100, height=100)
            first = writer.add_outline_item("Chapter 1", 0)
            writer.add_outline_item("Section 1", 1, parent=first)
            writer.add_outline_item("Chapter 2", 2)
            with path.open("wb") as stream:
                writer.write(stream)
            self.assertEqual(pdf_assets.extract_pdf_outline(path, 3), [
                {"title": "Chapter 1", "page": 1, "depth": 0},
                {"title": "Section 1", "page": 2, "depth": 1},
                {"title": "Chapter 2", "page": 3, "depth": 0},
            ])


if __name__ == "__main__":
    unittest.main()
