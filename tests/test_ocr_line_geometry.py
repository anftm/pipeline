import unittest
from types import SimpleNamespace

import numpy as np
from scripts import ocr_line_geometry as geometry, pdf_ocr


class HorizontalLineGeometryTests(unittest.TestCase):
    def test_tall_number_column_keeps_orientation_and_every_source_row(self):
        image = np.full((42, 16, 3), 225, dtype=np.uint8)
        image[4:14, 4:10] = 20
        image[24:34, 4:10] = 20
        polygon = np.array([[0, 0], [16, 0], [16, 42], [0, 42]], dtype=np.float32)
        pixels = geometry.crop(image, polygon)
        self.assertEqual(pixels.shape, image.shape)
        detection = SimpleNamespace(boxes=np.array([polygon]), scores=[.9])
        recognition = SimpleNamespace(txts=['3'], scores=[.4])
        crops, evidence = geometry.split([pixels], detection, recognition)
        self.assertEqual(len(crops), 2)
        np.testing.assert_array_equal(np.concatenate(crops), pixels)
        self.assertEqual(evidence[0]['primary'], '3')
        self.assertEqual(detection.boxes[0][2][1], detection.boxes[1][0][1])
        self.assertEqual(detection.boxes[-1][2][1], 42)

    def test_detached_accent_is_not_split_into_a_text_line(self):
        image = np.full((35, 80, 3), 255, dtype=np.uint8)
        image[4:6, 10:13] = 0
        image[13:30, 10:65] = 0
        self.assertEqual(geometry.bands(image), [(0, 35)])

    def test_new_crop_recipe_changes_recognition_identity(self):
        profile = pdf_ocr.asset_profile('ru', 'rapidocr_onnxruntime')
        self.assertIn('-horizontal-crops-v1', profile)
        self.assertTrue(pdf_ocr.valid_published_profile(profile))
