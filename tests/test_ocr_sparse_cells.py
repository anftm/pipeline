import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image,ImageDraw

from scripts import ocr_sparse_cells as cells,ocr_table_geometry as tables,ocr_layout,pdf_text_layer


class SparseCellTests(unittest.TestCase):
    def test_division_dots_cannot_be_called_a_broken_plus(self):
        import numpy as np
        pixels=np.full((28,40),255,np.uint8)
        pixels[8:11,19:21]=0
        pixels[13:15,15:25]=0
        pixels[18:20,19:21]=0
        self.assertIsNone(cells.broken_plus(pixels))
        pixels[15:20,19:21]=0
        self.assertIsNotNone(cells.broken_plus(pixels))

    def fixture(self):
        image=Image.new('RGB',(400,300),'white')
        draw=ImageDraw.Draw(image)
        rows=[0,42,78,114,150,186,222,258]
        blocks=[]
        grid={'profile':tables.PROFILE,'source_pixels_sha256':'a'*64,'box':[0,0,1,.86],
              'rows':[v/300 for v in rows],'columns':[0,.5,1],'body_rows':[1,6],
              'inferred_rows':[v/300 for v in rows[2:-1]],'cells':[],'review':[]}
        for row in range(7):
            for col in range(2):
                owned=[]
                if row:
                    y=50+(row-1)*36
                    if col==0:
                        draw.rectangle((20,y,31,y+23),outline='black',width=2)
                        for x in range(50,184,12):draw.rectangle((x,y+19,x+2,y+21),fill='black')
                    owned=[len(blocks)]
                    blocks.append({'t':'旧…' if col==0 else str(row),'c':.99,'s':'ocr',
                                   'b':[(15+col*200)/400,y/300,(185+col*200)/400,(y+24)/300]})
                grid['cells'].append({'row':row,'column':col,'rowspan':1,'colspan':1,'blocks':owned,
                    'box':[col*.5,rows[row]/300,(col+1)*.5,rows[row+1]/300]})
        return image,grid,blocks

    def test_whole_glyph_low_confidence_and_leaders_survive_layer_rotation(self):
        image,grid,blocks=self.fixture()
        before=copy.deepcopy((grid,blocks))
        engine=Mock();ocr=Mock();ocr.get_ocr_engine.return_value=engine
        engine.recognize_txt.return_value=SimpleNamespace(txts=['字'],scores=[.58])
        output,actual,proof=cells.recognize(image,grid,blocks,ocr,'ch','rapidocr_onnxruntime')
        self.assertEqual((grid,blocks),before)
        self.assertEqual([b['t'] for b in output if b.get('table_cell')],['字']*6)
        self.assertTrue(all(b['c']==.58 for b in output if b.get('table_cell')))
        self.assertEqual(len(proof['regions'][0]['original_blocks']),6)
        arranged=tables.arrange(output,400,300,actual)
        restored=ocr_layout.restore_coordinates(arranged,90)
        payload={'kind':'pdf-ocr-page','page':1,'width':300,'height':400,'source':'ocr',**restored}
        layer=pdf_text_layer.from_page(payload,'a'*64,'ch')
        json.dumps(layer)
        owned=next(c for c in layer['tables'][0]['cells'] if c['row']==1 and c['column']==0)
        self.assertEqual(len(owned['dot_leader']['boxes']),12)
        self.assertEqual(owned['dot_leader']['boxes'][0],ocr_layout.map_box_back(actual['cells'][2]['dot_leader']['boxes'][0],90))

    def test_disagreement_retains_original_text_and_failed_attempts(self):
        image,grid,blocks=self.fixture()
        engine=Mock();ocr=Mock();ocr.get_ocr_engine.return_value=engine
        engine.recognize_txt.side_effect=[SimpleNamespace(txts=[t],scores=[.999]) for t in ['甲','乙']*6]
        output,actual,proof=cells.recognize(image,grid,blocks,ocr,'ch','rapidocr_onnxruntime')
        self.assertEqual(output,blocks)
        self.assertFalse(proof['regions'][0]['accepted'])
        self.assertTrue(all(len(p['attempts'])==2 for p in proof['regions'][0]['regions']))

    def test_no_dot_leader_cannot_replace_a_column(self):
        image,grid,blocks=self.fixture()
        ImageDraw.Draw(image).rectangle((45,42,195,258),fill='white')
        engine=Mock();ocr=Mock();ocr.get_ocr_engine.return_value=engine
        output,actual,proof=cells.recognize(image,grid,blocks,ocr,'ch','rapidocr_onnxruntime')
        self.assertEqual(output,blocks)
        self.assertIsNone(proof)
        engine.recognize_txt.assert_not_called()
