import unittest
import numpy as np
from PIL import Image
from unittest.mock import patch
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

    def test_hybrid_charge_encoding_is_equivalent_only_after_raised_source_ink(self):
        values=['OH-1','OH⁻1','OH-¹','OH⁻¹']
        reads=iter([(value,.99) for value in values]+[(value,.99) for value in ['-1','⁻1','-¹','⁻¹']])
        value,proof=scripts.recognize(self.crop(),lambda image:next(reads))
        self.assertEqual(value,'OH⁻¹')
        self.assertEqual([a['text'] for a in proof['attempts']],values)
        self.assertEqual(proof['equivalent_charge_encodings']['base_unchanged'],'OH')
        self.assertIsNone(scripts.charge_parts('OH1-'))
        self.assertNotEqual(scripts.charge_parts('C1-1'),scripts.charge_parts('Cl⁻¹'))
        for bad in ['OH -1','OH- 1','OH-1}','OH₋₁']:
            self.assertIsNone(scripts.charge_parts(bad))

    def test_charge_encoding_equivalence_never_fixes_base_letter_or_missing_sign(self):
        for changed in ['0H-1','OH1','OH1-','OH-2']:
            reads=iter([('OH-1',.99),(changed,.99),('OH⁻¹',.99),('OH-1',.99)])
            value,proof=scripts.recognize(self.crop(),lambda image:next(reads))
            self.assertIsNone(value);self.assertFalse(proof['accepted'])

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

    def test_independent_complete_and_charge_confirmation_preserves_all_primary_failures(self):
        pixels=self.crop();pixels[7:17,31:34]=255;pixels[10:13,28:37]=0
        primary=iter([('OH-1',.84)]*4+[('OH',.999)]*4+[('-1',.89)]*4+[('1',.91)]*4)
        secondary=iter([('OH-1',.95)]*4+[('-1',.97)]*4)
        value,proof=scripts.recognize(pixels,lambda image:next(primary),read_secondary=lambda image:next(secondary))
        self.assertEqual(value,'OH⁻¹')
        self.assertTrue(proof['secondary_confirmation_accepted'])
        self.assertEqual([a['confidence'] for a in proof['isolated_digit_attempts']],[.91]*4)
        self.assertEqual([a['confidence'] for a in proof['attempts']],[.84]*4)

    def test_secondary_model_cannot_guess_different_base_or_omit_charge_or_lower_threshold(self):
        pixels=self.crop();pixels[7:17,31:34]=255;pixels[10:13,28:37]=0
        for whole,suffix,score in [('NH-1','-1',.99),('OH1','-1',.99),('OH-1','1',.99),('OH-1','-1',.94)]:
            primary=iter([('OH-1',.84)]*4+[('OH',.999)]*4+[('-1',.89)]*4+[('1',.91)]*4)
            secondary=iter([(whole,.99)]*4+[(suffix,score)]*4)
            value,proof=scripts.recognize(pixels,lambda image:next(primary),read_secondary=lambda image:next(secondary))
            with self.subTest(whole=whole,suffix=suffix,score=score):
                self.assertIsNone(value);self.assertFalse(proof['accepted'])

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

    def test_large_printed_subscript_uses_baseline_offset_not_only_small_height(self):
        pixels=np.full((40,65,3),255,np.uint8)
        pixels[5:25,5:20]=0;pixels[10:28,30:42]=0
        proof=scripts.lowered_ink(pixels[:,:,0])
        self.assertIsNotNone(proof)
        self.assertEqual(proof['script_box'],[30,10,42,28])
        reads=iter([('H2',.999)]*4+[('2',.999)]*4)
        value,_=scripts.recognize_subscript(pixels,lambda image:next(reads))
        self.assertEqual(value,'H₂')
        pixels[10:28,30:42]=255;pixels[5:25,30:42]=0
        self.assertIsNone(scripts.lowered_ink(pixels[:,:,0]))

    def test_refined_token_keeps_primary_confidence_and_geometry_without_chemical_replacement(self):
        image=Image.new('RGB',(200,100),'white')
        blocks=[{'t':'H2','b':[.1,.1,.4,.4],'q':[[.1,.1],[.4,.1],[.4,.4],[.1,.4]],'c':.83,'s':'ocr'}]
        proof={'geometry':{'script_box':[10,20,15,28]},'accepted':True}
        with patch.object(scripts,'recognize_subscript',return_value=('H₂',proof)):
            output,report=scripts.refine_tokens(image,blocks,lambda image:None)
        self.assertEqual(output[0]['t'],'H₂')
        self.assertEqual(blocks[0]['t'],'H2')
        for key in ['b','q','c','s']:self.assertEqual(output[0][key],blocks[0][key])
        self.assertTrue(report['tokens'][0]['accepted'])
        with patch.object(scripts,'recognize_subscript',return_value=('N₂',proof)):
            output,report=scripts.refine_tokens(image,blocks,lambda image:None)
        self.assertEqual(output,blocks)
        self.assertEqual(report['issues'][0]['code'],'formula-script-token-unresolved')

    def test_full_reactions_prose_and_neighbouring_text_are_not_token_crop_shortcuts(self):
        image=Image.new('RGB',(200,100),'white')
        blocks=[{'t':'Cu+2+2H2O','b':[.1,.1,.9,.3],'c':1,'s':'ocr'},
                {'t':'中文H2','b':[.1,.4,.9,.6],'c':1,'s':'ocr'}]
        with patch.object(scripts,'recognize_subscript') as recognize:
            self.assertEqual(scripts.refine_tokens(image,blocks,lambda image:None),(blocks,None))
            recognize.assert_not_called()
