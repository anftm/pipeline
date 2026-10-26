import copy
import unittest
from PIL import Image,ImageDraw

from scripts import ocr_fraction_layout as fractions,pdf_text_layer,ocr_layout,ocr_quality


class FractionLayoutTests(unittest.TestCase):
    def fixture(self):
        image=Image.new('RGB',(300,200),'white');draw=ImageDraw.Draw(image)
        draw.line((101,99,138,99),fill='black',width=2)
        blocks=[{'t':'10','b':[.34,.30,.46,.45],'c':1,'s':'ocr'},
                {'t':'5','b':[.37,.55,.43,.7],'c':1,'s':'ocr'},
                {'t':'= 2','b':[.5,.42,.65,.58],'c':1,'s':'ocr'}]
        return image,blocks

    def test_original_bar_establishes_fraction_and_every_part_keeps_source_coordinates(self):
        image,blocks=self.fixture();before=copy.deepcopy(blocks)
        found=fractions.detect(image,blocks)
        self.assertEqual(len(found),1)
        result=fractions.arrange(blocks,*image.size,found,{'writing_mode':'horizontal-ltr'})
        self.assertIn('10/5',result['text'])
        self.assertEqual(blocks,before)
        self.assertEqual(sorted(s['block'] for s in result['text_spans']),[0,1,2,3])
        self.assertEqual(result['raw_blocks'][0]['b'],blocks[0]['b'])
        self.assertEqual(result['raw_blocks'][1]['b'],blocks[1]['b'])
        self.assertEqual(result['raw_blocks'][3]['confidence_source'],'original-fraction-bar-geometry')
        payload={'kind':'pdf-ocr-page','source':'ocr','page':20,'width':image.width,'height':image.height,**result}
        layer=pdf_text_layer.from_page(payload,'a'*64,'ch');pdf_text_layer.validate(layer)
        self.assertEqual(layer['fractions'][0]['numerator_region_id'],'b0')
        self.assertEqual(layer['fractions'][0]['denominator_region_id'],'b1')
        self.assertEqual(layer['fractions'][0]['operator_region_id'],'b3')
        self.assertNotIn('text',layer['fractions'][0])

    def test_fraction_bar_overlap_is_exempt_only_for_its_exact_parent_parts(self):
        image,blocks=self.fixture();found=fractions.detect(image,blocks)
        result=fractions.arrange(blocks,*image.size,found)
        # An overgenerous numerator box can encompass the original ink bar.
        result['raw_blocks'][0]['b']=[.33,.3,.47,.52]
        result['text_spans'][next(i for i,s in enumerate(result['text_spans']) if s['block']==0)]['box']=result['raw_blocks'][0]['b']
        report=ocr_quality.check_page({'source':'native','page':20,'width':300,'height':200,**result})
        self.assertFalse(any(i['code']=='overlapping-recognized-regions' for i in report['issues']))
        result['raw_blocks'][3]['fraction_bar_evidence']['bar_pixels_sha256']='unknown'
        report=ocr_quality.check_page({'source':'native','page':20,'width':300,'height':200,**result})
        self.assertTrue(any(i['code']=='overlapping-recognized-regions' for i in report['issues']))

    def test_no_source_bar_no_fraction_and_no_arithmetic_guess(self):
        image,blocks=self.fixture()
        self.assertEqual(fractions.detect(Image.new('RGB',image.size,'white'),blocks),[])
        blocks[1]['t']='0'
        found=fractions.detect(image,blocks)
        self.assertEqual(len(found),1)
        result=fractions.arrange(blocks,*image.size,found)
        self.assertIn('10/0',result['text'])

    def test_adjacent_intruding_glyph_and_duplicate_parts_are_not_silently_merged(self):
        image,blocks=self.fixture();blocks.append({'t':'x','b':[.39,.46,.42,.53],'c':1,'s':'ocr'})
        self.assertEqual(fractions.detect(image,blocks),[])

    def test_quarter_turn_restores_fraction_parts_and_relation_to_original_page(self):
        image,blocks=self.fixture();found=fractions.detect(image,blocks)
        arranged=fractions.arrange(blocks,*image.size,found)
        restored=ocr_layout.restore_coordinates(arranged,90)
        expected=ocr_layout.map_box_back(found[0]['bar_box'],90)
        self.assertEqual(restored['layout']['fractions'][0]['bar_box'],expected)
        self.assertEqual(restored['raw_blocks'][-1]['b'],expected)
        self.assertEqual(restored['raw_blocks'][-1]['fraction_bar_evidence']['bar_box'],expected)
        layer=pdf_text_layer.from_page({'kind':'pdf-ocr-page','source':'ocr','page':20,
            'width':200,'height':300,**restored},'a'*64,'ch')
        pdf_text_layer.validate(layer)
        self.assertEqual(layer['fractions'][0]['bar_box'],expected)
