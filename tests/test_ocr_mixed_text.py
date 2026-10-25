import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from scripts import ocr_mixed_text as mixed, pdf_ocr


def result(text):
    words = [(c, .9, [[i * 10, 5], [(i + 1) * 10, 5], [(i + 1) * 10, 25], [i * 10, 25]])
             for i, c in enumerate(text) if not c.isspace()]
    width = len(text) * 10
    return SimpleNamespace(txts=[text], boxes=[[[0, 5], [width, 5], [width, 25], [0, 25]]],
                           scores=[.9], word_results=[words], img=np.full((30, width, 3), 255, dtype=np.uint8))


class MixedTextTests(unittest.TestCase):
    def setUp(self):
        self.polytonic = patch.object(mixed.ocr_greek, 'recognize', return_value={
            'model': {'sha256': 'b' * 64},
            'regions': [{'core': 'διανοίας', 'returncode': 0} for _ in range(4)]}).start()
        self.addCleanup(patch.stopall)

    def test_space_omitted_by_word_boxes_does_not_shift_greek_coordinates(self):
        raw = result('按照 E. Havelock，δtαvoiaç 在线段中')
        engine = SimpleNamespace(recognize_txt=lambda crops: SimpleNamespace(
            txts=['διανοίας', 'διανοίας'], scores=[.99, .98]))
        models = {'models': {}}
        with patch.object(pdf_ocr, 'get_ocr_engine', return_value=engine), \
                patch.object(pdf_ocr, 'actual_ocr_models', return_value={'models': {'rec': {'sha256': 'a' * 64}}}):
            blocks = mixed.recognize(raw, raw.img.shape[1], 30, models)
        self.assertEqual(blocks[0]['primary_text'], raw.txts[0])
        self.assertEqual(blocks[0]['t'], '按照 E. Havelock，διανοίας 在线段中')
        correction = models['diagnostics']['mixed_script']['regions'][0]
        self.assertEqual(raw.txts[0][slice(*correction['range'])], 'δtαvoiaç')
        self.assertTrue(correction['accepted'])

    def test_crop_disagreement_keeps_the_primary_and_requires_review(self):
        self.polytonic.return_value['regions'][1]['core'] = 'διανοιάς'
        raw = result('事实上μuμnoι')
        engine = SimpleNamespace(recognize_txt=lambda crops: SimpleNamespace(txts=['μιμησι', 'μιμησις'], scores=[.99, .99]))
        models = {'models': {}}
        with patch.object(pdf_ocr, 'get_ocr_engine', return_value=engine), \
                patch.object(pdf_ocr, 'actual_ocr_models', return_value={'models': {'rec': {'sha256': 'a' * 64}}}):
            blocks = mixed.recognize(raw, raw.img.shape[1], 30, models)
        self.assertEqual(blocks[0]['t'], raw.txts[0])
        self.assertEqual(models['diagnostics']['mixed_script']['issues'][0]['code'], 'mixed-script-recognition-unresolved')

    def test_missing_region_coordinates_cannot_silently_skip_secondary_recognition(self):
        raw = result('希腊δtαvoiaç')
        raw.word_results = []
        models = {'models': {}}
        with patch.object(pdf_ocr, 'get_ocr_engine'), \
                patch.object(pdf_ocr, 'actual_ocr_models', return_value={'models': {'rec': {'sha256': 'a' * 64}}}):
            blocks = mixed.recognize(raw, raw.img.shape[1], 30, models)
        self.assertEqual(blocks[0]['t'], raw.txts[0])
        self.assertEqual(models['diagnostics']['mixed_script']['issues'][0]['code'], 'mixed-script-coordinates-unavailable')

    def test_unrecognized_word_tail_is_included_without_adjacent_chinese(self):
        image = np.full((70, 200, 3), 255, np.uint8)
        # Three detected glyphs, two missed tails, then the next Chinese glyph.
        for left, right in [(15, 26), (31, 42), (47, 58), (65, 76), (82, 93), (111, 137)]:
            image[20:50, left:right] = 0
        boxes = [[[left, 15], [right, 15], [right, 55], [left, 55]]
                 for left, right in [(12, 28), (28, 45), (45, 60), (108, 140)]]
        bounds, proof = mixed.crop_bounds(image, boxes, 0, 3)
        for x0, y0, x1, y1 in bounds:
            self.assertLess(x0, 15)
            self.assertGreater(x1, 93)
            self.assertLess(x1, 111)
        self.assertEqual(proof['character_bounds'][2], 60)

    def test_polytonic_marks_and_word_spaces_are_not_normalized_away(self):
        self.assertEqual(mixed.ocr_greek.core('(εὖ ζῆν):')[0], 'εὖ ζῆν')
        self.assertEqual(mixed.ocr_greek.core('Μοῖραι,')[0], 'Μοῖραι')
        self.assertNotEqual(mixed.ocr_greek.core('μοῖρα')[0], mixed.ocr_greek.core('μοιρα')[0])

    def test_word_line_repair_preserves_detached_accent_and_excludes_both_neighbors(self):
        pixels = np.full((76, 120, 3), 255, dtype=np.uint8)
        pixels[0:6, 5:110] = 0
        pixels[20:29, 44:49] = 0
        pixels[32:60, 10:110] = 0
        pixels[71:76, 5:110] = 0
        repaired, evidence = mixed.ocr_greek.line_pixels(pixels)
        self.assertEqual(evidence['bounds'], [17, 63])
        np.testing.assert_array_equal(repaired[3:12, 44:49], pixels[20:29, 44:49])
        self.assertTrue((repaired[:3] == 255).all())
        self.assertTrue((repaired[-3:] == 255).all())

    def test_model_error_retains_primary_text_and_failure_evidence(self):
        raw = result('希腊μῦθος')
        self.polytonic.side_effect = ValueError('Greek model checksum mismatch')
        models = {'models': {}}
        engine = SimpleNamespace(recognize_txt=lambda crops: SimpleNamespace(txts=['μθος'] * 2, scores=[.99] * 2))
        with patch.object(pdf_ocr, 'get_ocr_engine', return_value=engine), \
                patch.object(pdf_ocr, 'actual_ocr_models', return_value={'models': {'rec': {'sha256': 'a' * 64}}}):
            blocks = mixed.recognize(raw, raw.img.shape[1], 30, models)
        self.assertEqual(blocks[0]['t'], raw.txts[0])
        self.assertIn('checksum mismatch', models['diagnostics']['mixed_script']['regions'][0]['polytonic']['error'])
