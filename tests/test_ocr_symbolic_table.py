import copy
import unittest
from PIL import Image,ImageDraw

from scripts import ocr_symbolic_table as tables,pdf_text_layer


class SymbolicTableTests(unittest.TestCase):
    def fixture(self):
        image=Image.new('RGB',(1000,1200),'white');draw=ImageDraw.Draw(image)
        draw.line((50,160,950,160),fill='black',width=4)
        blocks=[]
        def add(text,box):blocks.append({'t':text,'b':box,'s':'ocr','c':.999})
        add('TABLE 8.3',[.05,.06,.2,.08]);add('Source symbolic matrix',[.05,.09,.3,.11])
        add('Recipient of the Money Flow',[.4,.145,.8,.17]);add('Issuer of Money Flow',[.06,.19,.18,.24])
        for index,x in enumerate([.3,.45,.6,.75,.9],1):
            add(str(index),[x-.01,.19,x+.01,.21]);add('Header '+str(index),[x-.04,.22,x+.04,.24])
        for row in range(6):
            y=.3+row*.075;add(str(row+1)+'. Holder',[.06,y,.16,y+.02])
            for col,x in enumerate([.3,.45,.6,.75,.9]):
                if row==col:continue
                content='0' if col%2==0 else 'C, R'
                add(content,[x-.02,y,x+.02,y+.02])
        add('Note: 0 means no flow.',[.05,.8,.9,.83])
        return image,blocks

    def test_numbered_axes_keep_zeroes_combined_symbols_and_blank_cells(self):
        image,blocks=self.fixture();before=copy.deepcopy(blocks)
        table=tables.detect(image,blocks)
        self.assertIsNotNone(table)
        self.assertEqual(len(table['columns'])-1,6)
        self.assertEqual(len(table['rows'])-1,8)
        self.assertTrue(any(c['row']>=2 and not c['blocks'] for c in table['cells']))
        result=tables.arrange(blocks,*image.size,table)
        self.assertEqual(sorted(r['block'] for r in result['text_spans']),list(range(len(blocks))))
        self.assertIn('\t0\t',result['text']);self.assertIn('C, R',result['text'])
        layer=pdf_text_layer.from_page({'kind':'pdf-ocr-page','source':'ocr','page':84,
            'width':image.width,'height':image.height,**result},'a'*64,'en')
        pdf_text_layer.validate(layer);self.assertEqual(blocks,before)

    def test_prose_multidigit_numbers_or_missing_numbered_axis_are_not_symbol_tables(self):
        image,blocks=self.fixture()
        for transform in ('prose','numbers','header'):
            damaged=copy.deepcopy(blocks)
            if transform=='prose':next(b for b in damaged if b['t']=='0')['t']='unrelated prose'
            elif transform=='numbers':next(b for b in damaged if b['t']=='0')['t']='1024.3'
            else:damaged=[b for b in damaged if b['t']!='3']
            with self.subTest(transform=transform):self.assertIsNone(tables.detect(image,damaged))
