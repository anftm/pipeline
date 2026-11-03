import math
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

from scripts import ocr_graphic_rules as rules


class GraphicRuleTests(unittest.TestCase):
    def test_circular_dashes_excluded_but_center_cross_and_tangent_text_retained(self):
        image = Image.new('RGB', (400, 400), (220, 230, 245))
        draw = ImageDraw.Draw(image)
        for angle in range(0, 360, 15):
            draw.arc((50, 50, 350, 350), angle, angle+5, fill='black', width=2)
        draw.line((190, 190, 210, 210), fill='black', width=3)
        draw.line((190, 210, 210, 190), fill='black', width=3)
        original = image.tobytes()
        mask, proof = rules.detect(image)
        self.assertTrue(proof['circles'])
        self.assertFalse(mask[200, 200])
        blocks = [{'t': '1', 'b': [.86, .49, .89, .54]},
                  {'t': 'X', 'b': [.46, .46, .54, .54]}]
        # A real glyph adjacent to a dash cannot be erased with the dash.
        draw.text((340, 215), 'a', fill='black')
        blocks.append({'t': 'a', 'b': [.84, .49, .91, .58]})
        models = {'diagnostics': {'empty_regions': [7], 'detected_regions': 4,
                  'detection_regions': [{'region': 7, 'b': blocks[0]['b']}]}}
        with patch.object(rules, 'detect', return_value=(mask, proof)):
            kept, actual, evidence = rules.filter_detections(image, blocks, models)
        self.assertEqual([b['t'] for b in kept], ['X', 'a'])
        self.assertEqual(actual['diagnostics']['empty_regions'], [])
        self.assertEqual(models['diagnostics']['empty_regions'], [7])
        self.assertEqual(evidence['original_blocks'], blocks)
        self.assertNotEqual(original, image.tobytes())  # Only our explicit added glyph changed it.
        current = image.tobytes()
        rules.filter_detections(image, blocks, models)
        self.assertEqual(image.tobytes(), current)

    def test_missing_empty_detection_geometry_stays_unresolved(self):
        image = Image.new('RGB', (40, 40), 'white')
        with patch.object(rules, 'detect', return_value=(np.zeros((40, 40), dtype=bool), {})):
            _, actual, _ = rules.filter_detections(image, [], {'diagnostics': {'empty_regions': [4]}})
        self.assertEqual(actual['diagnostics']['empty_regions'], [4])
