import unittest
import numpy as np
from scripts import ocr_long_dash


class LongDashTests(unittest.TestCase):
    def fixture(self):
        pixels=np.full((300,30,3),255,np.uint8)
        pixels[20:40,8:22]=0
        pixels[240:260,8:22]=0
        pixels[100:190,14:16]=0
        pixels[100,14]=255
        return pixels

    def test_recovers_only_the_long_printed_separator_with_both_adjacent_readings(self):
        def recognize(crop,model,vertical):
            return ('かな' if crop.shape[0]<100 else 'かつゑ',.999,True)
        original='かな-かつゑ'
        result,proof=ocr_long_dash.recognize(self.fixture(),original,recognize)
        self.assertEqual(result,'かな―かつゑ')
        self.assertEqual(proof['original'],original)
        self.assertTrue(proof['accepted'])
        self.assertEqual(len(proof['attempts']),4)
        self.assertEqual(proof['line_box'],[14,100,16,190])

    def test_model_disagreement_retains_original_and_failed_evidence(self):
        result,proof=ocr_long_dash.recognize(self.fixture(),'かな-かつゑ',lambda *a,**k:('違う',.999,True))
        self.assertEqual(result,'かな-かつゑ')
        self.assertFalse(proof['accepted'])

    def test_short_hyphen_and_multiple_rules_cannot_be_relabelled(self):
        pixels=self.fixture()
        pixels[120:190]=255
        result,proof=ocr_long_dash.recognize(pixels,'かな-かつゑ',lambda *a,**k:self.fail('should not recognize'))
        self.assertEqual(result,'かな-かつゑ')
        self.assertIsNone(proof)

    def test_faint_continuous_line_with_fragmented_dark_ink(self):
        pixels=self.fixture()
        pixels[100:190,14:16]=150
        pixels[110:135,14]=100
        pixels[150:180,15]=100
        def recognize(crop,model,vertical):
            return ('かな' if crop.shape[0]<100 else 'かつゑ',.999,True)
        result,proof=ocr_long_dash.recognize(pixels,'かな-かつゑ',recognize)
        self.assertEqual(result,'かな―かつゑ')
        self.assertLess(proof['dark_row_share'],1)

    def test_two_long_rules_or_faint_background_retain_original(self):
        for duplicate in (True,False):
            pixels=self.fixture()
            if duplicate:
                pixels[100:190,22:24]=0
            else:
                pixels[100:190,14:16]=205
            with self.subTest(duplicate=duplicate):
                result,proof=ocr_long_dash.recognize(pixels,'かな-かつゑ',lambda *a,**k:self.fail('should not recognize'))
                self.assertEqual(result,'かな-かつゑ')
                self.assertIsNone(proof)

    def test_one_printed_line_can_replace_a_run_of_two_ocr_hyphens(self):
        def recognize(crop,model,vertical):
            return ('かな' if crop.shape[0]<100 else 'かつゑ',.999,True)
        result,proof=ocr_long_dash.recognize(self.fixture(),'かな--かつゑ',recognize)
        self.assertEqual(result,'かな―かつゑ')
        self.assertEqual(proof['original_separator'],'--')

    def test_leading_dash_requires_the_entire_remaining_line(self):
        pixels=np.full((280,30,3),255,np.uint8)
        pixels[:70,13:17]=0
        pixels[90:120,8:22]=0
        result,proof=ocr_long_dash.recognize(pixels,'-おやじ',lambda *a,**k:('おやじ',.999,True))
        self.assertEqual(result,'―おやじ')
        self.assertEqual([p['side'] for p in proof['attempts']],['after','after'])
        result,proof=ocr_long_dash.recognize(pixels,'-おやじ',lambda *a,**k:('やじ',.999,True))
        self.assertEqual(result,'-おやじ')
        self.assertFalse(proof['accepted'])

    def test_single_kana_uses_full_pixel_context_without_dropping_the_side_failure(self):
        def recognize(crop,model,vertical):
            if len(crop)>200:return ('本文ル',.999,True)
            return ('本文' if len(crop)<100 else '不明',.999,True)
        result,proof=ocr_long_dash.recognize(self.fixture(),'本文-ル',recognize)
        self.assertEqual(result,'本文―ル')
        self.assertTrue(any(not a['matches_original'] for a in proof['attempts']))
        self.assertTrue(all(a['matches_original'] for a in proof['context_attempts']))

    def test_blank_sections_require_complete_both_model_text_agreement(self):
        pixels=np.full((500,30,3),255,np.uint8)
        pixels[10:210,8:22]=0
        pixels[350:460,8:22]=0
        def recognize(crop,model,vertical):
            return ('かな' if len(crop)>150 else '刊記',.999,True)
        parts,proof=ocr_long_dash.split_sections(pixels,'かな刊記',recognize)
        self.assertTrue(proof['accepted'])
        self.assertEqual(len(parts),2)
        self.assertLess(parts[0]['crop_box'][3],parts[1]['crop_box'][1])
        parts,proof=ocr_long_dash.split_sections(pixels,'かな別の文字',recognize)
        self.assertIsNone(parts)
        self.assertFalse(proof['accepted'])

    def test_title_space_preserves_two_separate_source_boxes(self):
        from scripts import ocr_layout
        pixels=np.full((500,40,3),255,np.uint8)
        pixels[5:165,8:32]=0
        pixels[213:495,8:32]=0
        def recognize(crop,model,vertical):
            return ('伊藤野枝' if len(crop)<200 else 'はるかなる存在のひと',.999,True)
        parts,proof=ocr_long_dash.split_sections(pixels,'伊藤野枝はるかなる存在のひと',recognize)
        self.assertTrue(proof['accepted'])
        blocks=[]
        for i,part in enumerate(parts):
            x,y,u,v=part['crop_box']
            blocks.append({'t':part['attempts'][0]['text'],'b':[.5,y/500,.6,v/500],
                           'c':.999,'s':'ocr','segmentation':proof,'segmentation_part':i})
        arranged=ocr_layout.arrange(blocks,400,500,{'writing_mode':'vertical-rl'})
        self.assertEqual(arranged['text'],'伊藤野枝　はるかなる存在のひと')
        self.assertEqual(len(arranged['text_spans']),2)
        for span,b in zip(arranged['text_spans'],blocks):
            self.assertEqual(arranged['text'][span['start']:span['end']],b['t'])
        blocks[1]['segmentation_part']=3
        self.assertNotIn('　',ocr_layout.arrange(blocks,400,500,{'writing_mode':'vertical-rl'})['text'])

    def test_punctuation_whitespace_cannot_manufacture_a_space(self):
        pixels=np.full((500,40,3),255,np.uint8)
        pixels[5:165,8:32]=0
        pixels[213:495,8:32]=0
        parts,proof=ocr_long_dash.split_sections(pixels,'本文、一通の手紙',
            lambda crop,*a,**k:('本文、' if len(crop)<200 else '一通の手紙',.999,True))
        self.assertIsNone(parts)
        self.assertFalse(proof['accepted'])

    def test_failed_small_split_keeps_verified_large_gap_and_failed_evidence(self):
        pixels=np.full((600,40,3),255,np.uint8)
        pixels[5:155,8:32]=0
        pixels[300:340,8:32]=0
        pixels[390:590,8:32]=0
        def recognize(crop,model,vertical):
            if len(crop)<100:return ('不明' if model=='rec' else '異読',.999,True)
            return ('前の段' if len(crop)<180 else '→後の段' if len(crop)>250 else '後の段',.999,True)
        parts,proof=ocr_long_dash.split_sections(pixels,'前の段→後の段',recognize)
        self.assertEqual(len(parts),2)
        self.assertTrue(proof['accepted'])
        self.assertFalse(proof['rejected_finer_split']['accepted'])
