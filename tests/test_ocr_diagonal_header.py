"""Synthetic pixels test header ownership and fallback, not OCR model accuracy."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from scripts import ocr_diagonal_header as header, ocr_table_geometry


class DiagonalHeaderTests(unittest.TestCase):
    def fixture(self):
        image = Image.new('RGB', (200, 240), 'white')
        draw = ImageDraw.Draw(image)
        for y in [30, 75, 120]:
            draw.line((35+.64*(y-25), y, 35+.64*(y+35-25), y+35), fill='black', width=2)
        for side in ('left', 'right'):
            for i in range(4):
                y = 55+i*25 if side == 'left' else 35+i*25
                x = round(35+.64*(y-25)+(-25 if side == 'left' else 18))
                draw.rectangle((x, y, x+9, y+13), outline='black', width=2)
        # The fitted edge passes through the last glyph. The crop must retain
        # its visible pixels beyond that mean line position.
        cell = {'row': 0, 'column': 0, 'rowspan': 3, 'colspan': 1,
                'box': [.1, .1, .575, .8], 'blocks': [0]}
        table = {'cells': [cell], 'review': [{'code': 'diagonal-table-header'}]}
        blocks = [{'t': '旧识别', 'b': [.1, .1, .575, .8]}]
        return image, table, blocks

    def recognize(self, values, *, changed_grid=False):
        image, table, blocks = self.fixture()
        before = copy.deepcopy((table, blocks))
        engine, ocr = Mock(), Mock()
        engine.recognize_txt.side_effect = [SimpleNamespace(txts=[text], scores=[score]) for text, score in values]
        ocr.get_ocr_engine.return_value = engine
        ocr.actual_ocr_models.return_value = {'models': {'rec': {'sha256': 'b'*64}}}
        detected = None if changed_grid else {'cells': [{**table['cells'][0], 'blocks': list(range(8))}],
                                              'review': [{'code': 'diagonal-table-header'}]}
        with patch.object(ocr_table_geometry, 'detect', return_value=detected):
            result = header.recognize(image, table, blocks, ocr, 'ch', 'rapidocr_onnxruntime')
        self.assertEqual((table, blocks), before)
        return result, table, blocks

    def test_both_sides_and_edge_glyph_keep_original_page_coordinates(self):
        (blocks, table, evidence), original_table, original = self.recognize(
            [(value, .999) for value in ('操作步骤', '操作步骤', '标准色阶', '标准色阶')])
        self.assertEqual(''.join(b['t'] for b in blocks), '操作步骤标准色阶')
        self.assertEqual([b['table_header_side'] for b in blocks], ['left']*4+['right']*4)
        self.assertEqual(table['review'], [])
        region = evidence['regions'][0]
        self.assertTrue(region['accepted'])
        self.assertEqual(region['original_blocks'], original)
        self.assertGreater(blocks[-1]['b'][2], original_table['cells'][0]['box'][2])
        x, y, _, _ = region['crop_pixels']
        for block, glyph in zip(blocks, [g for side in region['sides'] for g in side['glyphs']]):
            a, b, c, d = glyph
            self.assertEqual(block['b'], [(x+a)/200, (y+b)/240, (x+c)/200, (y+d)/240])

    def test_disagreement_low_confidence_or_missing_glyph_preserves_original(self):
        for text, confidence in [('标准色', .999), ('标准色价', .999), ('标准色阶', .8)]:
            values = [('操作步骤', .999), ('操作步骤', .999), ('标准色阶', .999), (text, confidence)]
            with self.subTest(text=text, confidence=confidence):
                (blocks, table, evidence), original_table, original = self.recognize(values)
                self.assertEqual(blocks, original)
                self.assertEqual(table, original_table)
                self.assertFalse(evidence['regions'][0]['sides'][1]['accepted'])
                self.assertEqual(len(evidence['regions'][0]['sides'][1]['attempts']), 2)

    def test_changed_grid_cannot_accept_replacement(self):
        (blocks, table, evidence), _, original = self.recognize(
            [(v, .999) for v in ('操作步骤', '操作步骤', '标准色阶', '标准色阶')], changed_grid=True)
        self.assertEqual(blocks, original)
        self.assertIsNone(table)
        self.assertFalse(evidence['accepted'])
        self.assertEqual(evidence['reason'], 'grid-changed-after-header-recognition')
