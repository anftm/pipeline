import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from PIL import Image

from scripts import ocr_regions as regions,ocr_layout,pdf_text_layer


class RegionRecognitionTests(unittest.TestCase):
    def options(self):
        return [{'box':[0,0,.5,1],'rotation':90,'writing_mode':'horizontal-ltr'},
                {'box':[.5,0,1,1],'writing_mode':'horizontal-ltr'}]

    def test_crop_rotation_and_all_corner_coordinates_return_to_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'page.png';Image.new('RGB',(400,300),'white').save(path)
            sizes=[]
            def ocr_page(path,w,h,lang,backend):
                with Image.open(path) as image:self.assertEqual(image.size,(w,h))
                sizes.append((w,h))
                return [{'t':'MAP' if len(sizes)==1 else 'Right text','b':[.1,.2,.8,.4],
                         'q':[[.1,.2],[.8,.2],[.8,.4],[.1,.4]],'c':.999,'s':'ocr'}]
            ocr=SimpleNamespace(ocr_page=ocr_page,actual_ocr_models=lambda *args:{'backend':'fixture',
                'models':{'det':{'sha256':'a'*64},'rec':{'sha256':'b'*64}},
                'diagnostics':{'detected_regions':1,'empty_regions':[]}})
            blocks,models=regions.recognize(path,self.options(),'en','fixture',ocr)
            self.assertEqual(sizes,[(300,200),(200,300)])
            for actual,expected in zip(blocks[0]['b'],[.1,.2,.2,.9]):self.assertAlmostEqual(actual,expected)
            for actual,expected in zip(blocks[1]['b'],[.55,.2,.9,.4]):self.assertAlmostEqual(actual,expected)
            self.assertEqual(models['diagnostics']['detected_regions'],2)
            before=copy.deepcopy(blocks)
            result=regions.arrange(blocks,400,300,{'regions':self.options()})
            self.assertEqual(result['text'],'MAP\n\nRight text')
            self.assertEqual(blocks,before)
            payload={'kind':'pdf-ocr-page','page':18,'source':'ocr','width':400,'height':300,**result}
            layer=pdf_text_layer.from_page(payload,'c'*64,'en');pdf_text_layer.validate(layer)
            self.assertEqual(layer['regions'][0]['quad'],blocks[0]['q'])

    def test_missing_coverage_overlap_and_unsupported_rotation_fail(self):
        for options in [[self.options()[0]],self.options()+[self.options()[0]],
                        [{'box':[0,0,1,1],'rotation':45}]]:
            with self.subTest(options=options),self.assertRaises(ValueError):
                ocr_layout.validate_options({'regions':options})

    def test_upright_reading_order_is_not_source_vertical_sort_order(self):
        bs=[{'t':'second','b':[.1,.2,.2,.4],'c':.99,'s':'ocr','recognition_region':0},
            {'t':'first','b':[.1,.5,.2,.8],'c':.99,'s':'ocr','recognition_region':0}]
        result=regions.arrange(bs,400,300,{'regions':self.options()})
        self.assertEqual([b['t'] for b in result['blocks']],['first','second'])
        self.assertEqual(sorted(s['block'] for s in result['text_spans']),[0,1])
        for span in result['text_spans']:
            self.assertEqual(result['text'][span['start']:span['end']],bs[span['block']]['t'])

    def test_regional_failures_keep_their_original_and_restored_positions(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'page.png';Image.new('RGB',(400,300),'white').save(path)
            issue={'code':'mixed-script-recognition-unresolved','severity':'review','evidence':{'region':0}}
            ocr=SimpleNamespace(ocr_page=lambda *args:[],actual_ocr_models=lambda *args:{
                'models':{'det':{'sha256':'a'*64},'rec':{'sha256':'b'*64}},
                'diagnostics':{'detected_regions':1,'empty_regions':[0],
                    'detection_regions':[{'region':0,'text':'','b':[.1,.2,.8,.4]}],
                    'unresolved_regions':[{'box':[.1,.2,.8,.4],'reason':'split needs review','crop_sha256':'c'*64}],
                    'mixed_script':{'issues':[issue]}}})
            blocks,models=regions.recognize(path,self.options(),'en','fixture',ocr)
            self.assertEqual(models['diagnostics']['empty_regions'],['0:0','1:0'])
            for actual,expected in zip(models['diagnostics']['unresolved_regions'][0]['box'],[.1,.2,.2,.9]):
                self.assertAlmostEqual(actual,expected)
            self.assertEqual(models['diagnostics']['mixed_script']['issues'],[issue,issue])
            self.assertEqual(models['diagnostics']['regional_recognition']['regions'][0]['models']['diagnostics']['unresolved_regions'][0]['box'],[.1,.2,.8,.4])

    def test_symbolic_matrix_keeps_original_page_cell_coordinates_and_no_repeated_text(self):
        from tests.test_ocr_symbolic_table import SymbolicTableTests
        from scripts import ocr_symbolic_table
        image,local=SymbolicTableTests().fixture();grid=ocr_symbolic_table.detect(image,local)
        self.assertIsNotNone(grid)
        source=[{'t':'Left printed prose','b':[.05,.1,.4,.2],'s':'ocr','c':1,'recognition_region':0}]
        for block in local:
            mapped=ocr_layout.map_box_back(block['b'],90)
            source.append({**block,'b':[.5+mapped[0]*.5,mapped[1],.5+mapped[2]*.5,mapped[3]],
                           'recognition_region':1})
        options={'regions':[{'box':[0,0,.5,1],'writing_mode':'horizontal-ltr'},
                            {'box':[.5,0,1,1],'rotation':90,'writing_mode':'horizontal-ltr'}]}
        models={'regions':[{'region':1,'local_tables':[grid]}]}
        result=regions.arrange(source,2400,1000,options,models)
        self.assertEqual(sorted(s['block'] for s in result['text_spans']),list(range(len(source))))
        self.assertEqual(result['text'].count('Note: 0 means no flow.'),1)
        layer=pdf_text_layer.from_page({'kind':'pdf-ocr-page','page':84,'source':'ocr',
            'width':2400,'height':1000,**result},'a'*64,'en')
        pdf_text_layer.validate(layer)
        table=layer['tables'][0]
        self.assertEqual(table['grid_rotation_clockwise'],90)
        self.assertEqual(table['grid_region_box'],[.5,0,1,1])
        self.assertTrue(all(cell['box'][0]>=.5 for cell in table['cells']))
        self.assertTrue(all('b0' not in cell['region_ids'] for cell in table['cells']))
