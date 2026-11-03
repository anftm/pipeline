import unittest
from scripts import ndl_ocr


def candidate(text, score, model='rec', margin=0, terminated=True):
    return {'text': text, 'confidence': score, 'model': model, 'margin': margin, 'terminated': terminated}


class RubyRecognitionTests(unittest.TestCase):
    def test_capacity_segmentation_requires_both_models_and_keeps_all_pixels(self):
        import numpy as np
        engine = object.__new__(ndl_ocr.Engine)
        image = np.full((160, 20, 3), 255, dtype=np.uint8)
        image[10:55, 4:16] = 0
        image[100:150, 4:16] = 0
        engine.read_line = lambda crop, model, vertical: ('本文', .99, True)
        segments, agreed = engine.read_segments(image)
        self.assertTrue(agreed)
        self.assertEqual(segments[0]['crop_box'][1], 0)
        self.assertEqual(segments[-1]['crop_box'][3], 160)
        for a, b in zip(segments, segments[1:]):
            self.assertEqual(a['crop_box'][3], b['crop_box'][1])
        engine.read_line = lambda crop, model, vertical: ('本文' if model == 'rec' else '不明', .99, True)
        segments, agreed = engine.read_segments(image)
        self.assertFalse(agreed)
        self.assertEqual(len(segments[0]['attempts']), 2)

    def test_slanted_printed_rule_separates_sidebar_from_two_tier_body(self):
        import cv2
        import numpy as np
        image = np.full((1000, 700), 255, np.uint8)
        cv2.line(image, (550, 30), (560, 950), 40, 2)
        boxes = [[40, 80, 500, 430], [40, 520, 510, 940], [590, 100, 660, 800]]
        cuts, proof = ndl_ocr.vertical_separators(image, boxes)
        self.assertEqual(len(cuts), 1)
        self.assertTrue(550 < cuts[0] < 560)
        self.assertGreater(proof[0]['coverage'], .8)
        self.assertEqual(ndl_ocr.vertical_separators(image, boxes + [[540, 30, 570, 950]])[0], [])

    def test_clipped_ruby_recovers_with_two_models_and_two_margins(self):
        original = candidate('かつざふよ', .7268)
        values = [original, candidate('かつぎよう', .9675, margin=2),
                  candidate('かつぎよう', .9588, 'rec_long', 2),
                  candidate('かつぎよう', .9834, margin=4),
                  candidate('かつぎよう', .9841, 'rec_long', 4)]
        self.assertEqual(ndl_ocr.select_ruby(values)['text'], 'かつぎよう')
        self.assertEqual(values[0], original)

    def test_single_model_or_single_margin_cannot_override_text(self):
        original = candidate('かな', .72)
        for extra in ([candidate('がな', .99, margin=2), candidate('がな', .98, margin=4)],
                      [candidate('がな', .99, margin=2), candidate('がな', .98, 'rec_long', 2)]):
            self.assertEqual(ndl_ocr.select_ruby([original, *extra]), original)

    def test_high_confidence_symbols_and_truncated_candidates_are_rejected(self):
        original = candidate('ま', .9107)
        for text, ended in [('**', True), ('かな', False)]:
            values = [candidate(text, .999, model, margin, ended)
                      for model in ('rec', 'rec_long') for margin in (2, 4)]
            self.assertEqual(ndl_ocr.select_ruby([original, *values]), original)

    def test_conflicting_model_agreement_preserves_original_evidence(self):
        original = candidate('かな', .75)
        values = [candidate(text, .99, model, margin) for text in ('かつぎよう', 'かつきよう')
                  for model in ('rec', 'rec_long') for margin in (2, 4)]
        self.assertEqual(ndl_ocr.select_ruby([original, *values]), original)
