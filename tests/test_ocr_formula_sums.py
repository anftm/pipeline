import copy
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image,ImageDraw

from scripts import ocr_formula_sums as sums,ocr_formula_scripts as scripts


class FormulaSumsTests(unittest.TestCase):
    def fixture(self):
        image=Image.new('RGB',(150,50),'white');draw=ImageDraw.Draw(image)
        for x in [8,18,95,105]:draw.rectangle((x,10,x+3,29),fill='black')
        draw.rectangle((64,20,80,22),fill='black');draw.rectangle((71,13,73,29),fill='black')
        return image,[{'t':'H+1+OH-1','b':[0,0,1,1],'s':'ocr','c':.82}]

    def test_only_complete_two_term_confirmation_changes_script_positions(self):
        image,blocks=self.fixture();before=copy.deepcopy(blocks)
        evidence={'accepted':True,'geometry':{'script_box':[5,3,14,10]}}
        with patch.object(scripts,'recognize',side_effect=[('H⁺¹',evidence),('OH⁻¹',evidence)]):
            output,proof=sums.refine(image,blocks,lambda image:None)
        self.assertEqual(output[0]['t'],'H⁺¹+OH⁻¹')
        self.assertTrue(proof['sums'][0]['accepted']);self.assertEqual(proof['issues'],[])
        for part in proof['sums'][0]['parts']:
            self.assertEqual(output[0]['t'][part['text_start']:part['text_end']],part['text'])
            self.assertEqual(len(part['script_source_box']),4)
            self.assertTrue(all(0<=v<=1 for v in part['script_source_box']))
        self.assertEqual(blocks,before)
        for key in ('b','c','s'):self.assertEqual(blocks[0][key],output[0][key])

    def test_one_missing_or_changed_term_cannot_be_filled_from_original_formula_or_chemistry(self):
        for second in [None,'NH⁻¹','OH¹⁻']:
            image,blocks=self.fixture()
            with patch.object(scripts,'recognize',side_effect=[('H⁺¹',{}),(second,{})]):
                output,proof=sums.refine(image,blocks,lambda image:None)
            self.assertEqual(output,blocks)
            self.assertFalse(proof['sums'][0]['accepted'])
            self.assertEqual(proof['issues'][0]['code'],'binary-formula-sum-unresolved')

    def test_missing_cross_raised_charge_and_body_letter_are_not_operator_shortcuts(self):
        image,blocks=self.fixture()
        draw=ImageDraw.Draw(image);draw.rectangle((60,10,85,32),fill='white')
        self.assertEqual(sums.baseline_crosses(np.asarray(image.convert('L'))),[])
        # H has a horizontal bar, but also two body stems at its edges.
        draw.rectangle((64,13,66,29),fill='black');draw.rectangle((78,13,80,29),fill='black');draw.rectangle((64,20,80,22),fill='black')
        self.assertEqual(sums.baseline_crosses(np.asarray(image.convert('L'))),[])

    def test_full_reactions_and_prose_are_not_handled_as_binary_sums(self):
        image,blocks=self.fixture()
        for value in ['Cu+2+H2O=Cu(OH)2+H+1','正文H+1+OH-1','H+1+OH-1+Cl-1']:
            blocks[0]['t']=value
            with patch.object(scripts,'recognize') as read:
                self.assertEqual(sums.refine(image,blocks,lambda image:None),(blocks,None));read.assert_not_called()

    def test_raised_plus_or_adjacent_text_does_not_establish_body_sum_separator(self):
        image,blocks=self.fixture();draw=ImageDraw.Draw(image)
        draw.rectangle((60,10,85,32),fill='white')
        draw.rectangle((64,8,72,9),fill='black');draw.rectangle((67,5,68,12),fill='black')
        self.assertEqual(sums.baseline_crosses(np.asarray(image.convert('L'))),[])
        image,blocks=self.fixture();blocks.append({'t':'adjacent original','b':[.8,.8,1,1],'c':1,'s':'ocr'})
        with patch.object(scripts,'recognize') as read:
            self.assertEqual(sums.refine(image,blocks,lambda image:None),(blocks,None));read.assert_not_called()

    def test_peripheral_original_region_is_preserved_and_stays_reviewable_after_script_confirmation(self):
        image,blocks=self.fixture()
        blocks[0]['b']=[0,.1,1,1]
        # A prior arrow box extends into the crop but is centered above it.
        blocks.append({'t':'1','b':[.45,0,.55,.14],'c':.4,'s':'ocr'})
        before=copy.deepcopy(blocks)
        with patch.object(scripts,'recognize',side_effect=[('H⁺¹',{'accepted':True}),('OH⁻¹',{'accepted':True})]):
            output,proof=sums.refine(image,blocks,lambda image:None)
        self.assertEqual(output[0]['t'],'H⁺¹+OH⁻¹')
        self.assertEqual(output[1],before[1])
        self.assertEqual(proof['issues'][0]['code'],'binary-formula-adjacent-region-unresolved')
        self.assertEqual(proof['sums'][0]['adjacent_original_regions'][0]['block'],before[1])
