from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import numpy as np
from PIL import Image
import pymupdf

from scripts import pdf_mixed_text as mixed, pdf_ocr, pdf_ocr_stages as stages


class MixedTextTests(unittest.TestCase):
    def test_text_shaped_alpha_mask_is_not_a_fill(self):
        rgba = np.zeros((60, 30, 4), dtype=np.uint8)
        edge = np.minimum(np.arange(60), np.arange(59, -1, -1)).clip(0, 5)/5
        rgba[:, :, 3] = np.rint(edge[:, None]*255)
        self.assertTrue(mixed.simple_fill(rgba))
        rgba[10:30, 10:20, 3] = 0
        self.assertFalse(mixed.simple_fill(rgba))

    def test_short_native_text_and_raster_diagram_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fill = root/'fill.png'
            Image.new('RGB', (20, 200), 'gray').save(fill)
            drawing = root/'drawing.png'
            pixels = np.full((50, 80), 255, dtype=np.uint8)
            pixels[10:15, 15:60] = 0
            pixels[20:40, 30:35] = 0
            Image.fromarray(pixels).save(drawing)
            source = root/'native.pdf'
            with pymupdf.open() as document:
                for n in range(2):
                    page = document.new_page(width=200, height=300)
                    page.insert_text((30, 40), 'Title')
                    page.insert_image(pymupdf.Rect(0, 0, 15, 300), filename=str(fill))
                    if n:
                        page.insert_image(pymupdf.Rect(30, 80, 180, 200), filename=str(drawing))
                document.save(source)
            pages = pdf_ocr.native_pages(source, [1, 2])
            self.assertEqual(pages[1]['raster_evidence']['regions'], [])
            self.assertEqual(len(pages[1]['raster_evidence']['fills']), 1)
            self.assertEqual(len(pages[2]['raster_evidence']['regions']), 1)
            item = {'key': 'repo\0native.pdf', 'repo': 'repo', 'path': 'native.pdf',
                    'source_revision': 'revision', 'reader_presentation': {'strategy': 'preserve-pdf'},
                    'probe': pdf_ocr.probe_pdf(source)}
            result = stages.render_book(item, source, root/'render')
            import json
            manifest = json.loads((root/'render'/result['render_manifest']['path']).read_text())
            self.assertEqual(manifest['pages'][0]['source'], 'native')
            self.assertEqual(manifest['pages'][1]['source'], 'ocr')
            self.assertEqual(manifest['pages'][1]['native_components']['text'], 'Title')
            self.assertEqual(result['ocr_pages'], 1)

    def test_crop_coordinates_and_native_characters_survive_merging(self):
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp)/'page.png'
            Image.new('RGB', (100, 200), 'white').save(image)
            native = {'t': '标题 →', 's': 'native', 'c': 1., 'b': [.1, .1, .9, .2]}
            components = {'blocks': [native], 'raster': {'regions': [{'box': [.2, .4, .8, .8]}]}}
            engine = SimpleNamespace(
                ocr_page=lambda _p, w, h, _l, _b: [{'t': '企业', 's': 'ocr', 'c': .99, 'b': [0., 0., 1., 1.]}],
                actual_ocr_models=lambda _l, _b: {'models': {}, 'diagnostics': {'detected_regions': 1}})
            blocks, models = mixed.recognize(image, components, 'ch', 'fixture', engine)
            self.assertEqual(blocks[0], native)
            self.assertEqual(blocks[1]['b'], [.2, .4, .8, .8])
            self.assertEqual(blocks[1]['t'], '企业')
            self.assertEqual(len(models['diagnostics']['mixed_native']['regions']), 1)
