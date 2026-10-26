import unittest
import numpy as np
from scripts import ocr_formula_scripts as scripts


class FormulaScriptTests(unittest.TestCase):
    def crop(self):
        pixels=np.full((30,60,3),255,np.uint8)
        pixels[5:25,3:7]=0;pixels[5:25,15:19]=0
        pixels[10:13,28:37]=0;pixels[7:17,31:34]=0
        pixels[6:17,45:48]=0
        return pixels

    def test_charge_requires_raised_geometry_and_complete_term_agreement(self):
        calls=[]
        def read(image):
            calls.append(image.shape)
            return ('Cu+2',.999) if len(calls)<=4 else ('+2',.999)
        value,proof=scripts.recognize(self.crop(),read)
        self.assertEqual(value,'Cu⁺²')
        self.assertTrue(proof['accepted'])
        self.assertEqual(proof['original'],'Cu+2')
        self.assertEqual(len(proof['attempts']),4)
        pixels=self.crop();pixels[6:17,45:48]=255;pixels[18:29,45:48]=0
        self.assertIsNone(scripts.recognize(pixels,read)[0])

    def test_both_reading_scales_required_and_failures_remain_visible(self):
        attempts=iter([('S-2',.999)]*4+[('-2',.5)]*4)
        value,proof=scripts.recognize(self.crop(),lambda image:next(attempts))
        self.assertIsNone(value);self.assertFalse(proof['accepted'])
        self.assertEqual(len(proof['script_attempts']),4)

    def test_shape_does_not_invent_or_reverse_a_charge(self):
        value,proof=scripts.recognize(self.crop(),lambda image:('Cu2+',.999))
        self.assertIsNone(value);self.assertFalse(proof['accepted'])
        self.assertIsNotNone(scripts.cross_sign(np.array([[255,0,255],[0,0,0],[255,0,255]],np.uint8)))
        self.assertIsNone(scripts.cross_sign(np.array([[255,255,255],[0,0,0],[255,255,255]],np.uint8)))

    def test_low_line_score_needs_independent_base_and_charge_readings(self):
        results=iter([('2H+1',.84)]*4+[('2H',.999)]*4+[('+1',.999)]*4)
        value,proof=scripts.recognize(self.crop(),lambda image:next(results))
        self.assertEqual(value,'2H⁺¹')
        self.assertEqual([a['confidence'] for a in proof['attempts']],[.84]*4)
        self.assertTrue(proof['body_attempts'])
        failed=iter([('2H+1',.84)]*4+[('H',.999)]*4)
        value,proof=scripts.recognize(self.crop(),lambda image:next(failed))
        self.assertIsNone(value)
        self.assertFalse(proof['accepted'])

    def test_independent_minus_and_digit_can_resolve_uncertain_script_without_lowering_scores(self):
        pixels=self.crop();pixels[7:17,31:34]=255;pixels[10:13,28:37]=0
        readings=iter([('OH-1',.84)]*4+[('OH',.999)]*4+[('-1',.89)]*4+[('1',.999)]*4)
        value,proof=scripts.recognize(pixels,lambda image:next(readings))
        self.assertEqual(value,'OH⁻¹')
        self.assertTrue(proof['isolated_sign_geometry'])
        self.assertEqual([a['confidence'] for a in proof['script_attempts']],[.89]*4)
        self.assertTrue(proof['isolated_digit_attempts'])
        self.assertIsNone(scripts.minus_sign(np.array([[255,0,255],[0,0,0],[255,0,255]],np.uint8)))

    def subscript_crop(self):
        pixels=np.full((32,60,3),255,np.uint8)
        pixels[5:25,3:15]=0;pixels[5:25,38:52]=0
        pixels[21:30,25:31]=0
        return pixels

    def test_lowered_digit_is_not_a_coefficient_or_raised_charge(self):
        reads=iter([('2H2O',.999)]*4+[('2',.999)]*4)
        value,proof=scripts.recognize_subscript(self.subscript_crop(),lambda image:next(reads))
        self.assertEqual(value,'2H₂O')
        self.assertTrue(proof['accepted'])
        pixels=self.subscript_crop();pixels[21:30,25:31]=255;pixels[5:14,25:31]=0
        self.assertIsNone(scripts.recognize_subscript(pixels,lambda image:('H2O',.999))[0])

    def test_subscript_needs_all_complete_and_digit_readings_without_brace_cleanup(self):
        for whole in ('H2}O','H20','H2O2'):
            with self.subTest(whole=whole):
                reads=iter([(whole,.999)]*4+[('2',.999)]*4)
                value,proof=scripts.recognize_subscript(self.subscript_crop(),lambda image:next(reads))
                self.assertIsNone(value)

    def test_broken_lower_digit_keeps_its_original_upper_curl(self):
        pixels=self.subscript_crop();pixels[21:30,25:31]=255
        pixels[19:22,25:31]=0;pixels[25:30,25:31]=0
        evidence=scripts.lowered_ink(pixels[:,:,0])
        self.assertEqual(evidence['script_box'],[25,19,31,30])
