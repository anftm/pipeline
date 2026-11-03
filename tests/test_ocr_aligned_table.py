import copy
import unittest
from PIL import Image,ImageDraw

from scripts import ocr_aligned_table as tables,ocr_layout,pdf_text_layer


def fixture():
    image=Image.new('RGB',(1000,1400),'white');draw=ImageDraw.Draw(image)
    draw.line((50,180,950,180),fill='black',width=3)
    blocks=[{'t':'TABLE 1.1','b':[.05,.05,.15,.07],'c':.999,'s':'ocr'},
            {'t':'Country statistics, 1987','b':[.05,.08,.4,.10],'c':.999,'s':'ocr'}]
    def add(text,x,y,w=.06,h=.015):
        blocks.append({'t':text,'b':[x,y,x+w,y+h],'c':.999,'s':'ocr'})
    for x,text in [(.06,'No.'),(.17,'Country'),(.39,'Year'),(.57,'Population'),(.77,'Area')]:
        add(text,x,.15,.03 if x==.06 else .1)
    for n in range(1,9):
        y=.22+n*.055
        add(str(n)+'.',.06,y,.03)
        add('Country '+str(n),.17,y,.15)
        add(str(1900+n),.39,y)
        add(str(n*10)+'.5',.57,y)
        add(str(n*11),.77,y)
    add('Source: original statistics',.06,.80,.7)
    return image,blocks


class AlignedTableTests(unittest.TestCase):
    def test_numbered_rows_and_empty_cells_preserve_source_text_and_rotated_coordinates(self):
        image,blocks=fixture();before=copy.deepcopy(blocks)
        grid=tables.detect(image,blocks)
        self.assertEqual(len(grid),1)
        self.assertEqual(len(grid[0]['columns'])-1,5)
        self.assertEqual(len(grid[0]['rows'])-1,9)
        self.assertEqual(grid[0]['review'],[])
        result=tables.arrange(blocks,*image.size,grid)
        self.assertIn('1.\tCountry 1\t1901\t10.5\t11',result['text'])
        self.assertIn('8.\tCountry 8\t1908\t80.5\t88',result['text'])
        self.assertEqual(blocks,before)
        self.assertEqual(sorted(s['block'] for s in result['text_spans']),list(range(len(blocks))))
        restored=ocr_layout.restore_coordinates(result,90)
        payload={'kind':'pdf-ocr-page','source':'ocr','page':17,'width':1400,'height':1000,**restored}
        layer=pdf_text_layer.from_page(payload,'a'*64,'en');pdf_text_layer.validate(layer)
        self.assertEqual(layer['tables'][0]['grid_rotation_clockwise'],90)
        self.assertTrue(all('text' not in cell for cell in layer['tables'][0]['cells']))

    def test_prose_and_missing_source_footer_cannot_become_tables(self):
        image,blocks=fixture()
        self.assertEqual(tables.detect(image,[b for b in blocks if not b['t'].startswith('Source:')]),[])
        blocks[0]['t']='Not a table title'
        self.assertEqual(tables.detect(image,blocks),[])

    def test_overlapping_tables_cannot_duplicate_source_blocks(self):
        image,blocks=fixture()
        grid=tables.detect(image,blocks)
        with self.assertRaisesRegex(ValueError,'ownership'):
            tables.arrange(blocks,*image.size,grid+copy.deepcopy(grid))

    def test_unresolved_wrapped_label_keeps_its_original_cell(self):
        blocks=[{'t':'original label','b':[.1,.1,.25,.13],'c':.99,'s':'ocr'}]
        cells=[{'row':0,'column':0,'rowspan':1,'colspan':1,'blocks':[0]}]
        for row in range(1,6):
            blocks.append({'t':'kg','b':[.5,.1+row*.1,.6,.13+row*.1],'c':.99,'s':'ocr'})
            cells.append({'row':row,'column':1,'rowspan':1,'colspan':1,'blocks':[row]})
        grid={'cells':cells,'body_rows':[0,5],'evidence':{},'review':[]}
        actual=tables.align_wrapped_labels(blocks,[grid])[0]
        self.assertEqual(actual['cells'][0]['blocks'],[0])
        self.assertTrue(any(r['code']=='aligned-table-wrapped-label-unresolved' for r in actual['review']))
        self.assertEqual(sorted(i for c in actual['cells'] for i in c['blocks']),list(range(6)))

    def test_empty_cell_ink_is_flagged_instead_of_inventing_a_dash_or_zero(self):
        image,blocks=fixture();missing=next(b for b in blocks if b['t']=='33')
        blocks.remove(missing)
        draw=ImageDraw.Draw(image);draw.line((785,551,810,551),fill='black',width=2)
        grid=tables.detect(image,blocks)
        self.assertTrue(any(r['code']=='aligned-table-empty-cell-with-ink' for r in grid[0]['review']))
        self.assertTrue(any(c['row']>=1 and c['column']==4 and not c['blocks'] for c in grid[0]['cells']))

    def test_repeated_original_dashes_recover_but_solitary_scratch_does_not(self):
        image,blocks=fixture();draw=ImageDraw.Draw(image)
        for number in [2,3]:
            target=next(b for b in blocks if b['t']==str(number*11));blocks.remove(target)
            y=round((.22+number*.055+.0075)*image.height)
            draw.line((785,y,800,y),fill='black',width=2)
        grids=tables.detect(image,blocks)
        output,actual,proof=tables.recover_dashes(image,blocks,grids)
        self.assertEqual(len(proof['regions']),2)
        self.assertEqual(sum(b['t']=='—' for b in output),2)
        self.assertFalse(any(b['t']=='—' for b in blocks))
        self.assertFalse(any(r['code']=='aligned-table-empty-cell-with-ink' for r in actual[0]['review']))
        self.assertEqual(len(tables.arrange(output,*image.size,actual)['text_spans']),len(output))

    def test_category_split_keeps_cells_sorted_and_full_word_ownership(self):
        blocks=[{'t':'Cigarettes','b':[.1,.2,.2,.23],'c':1,'s':'ocr'},
                {'t':'box','b':[.5,.2,.6,.23],'c':1,'s':'ocr'},
                {'t':'Nonfood','b':[.1,.25,.2,.28],'c':1,'s':'ocr'},
                {'t':"Men's suit",'b':[.1,.3,.2,.33],'c':1,'s':'ocr'},
                {'t':'piece','b':[.5,.3,.6,.33],'c':1,'s':'ocr'}]
        grid={'profile':tables.PROFILE,'source_pixels_sha256':'a'*64,'box':[0,.1,1,.4],
              'rows':[.1,.29,.4],'columns':[0,.4,1],'body_rows':[0,1],
              'review':[{'code':'aligned-table-category-band-review','block':2}], 'evidence':{},
              'cells':[{'row':r,'column':c,'rowspan':1,'colspan':1,'box':[c*.4,[.1,.29][r],.4 if not c else 1,[.29,.4][r]],
                        'blocks':indices} for r,c,indices in [(0,0,[0,2]),(0,1,[1]),(1,0,[3]),(1,1,[4])]]}
        actual=tables.separate_category_rows(blocks,[grid])[0]
        self.assertEqual(actual['review'],[])
        self.assertEqual([(c['row'],c['column']) for c in actual['cells']],sorted((c['row'],c['column']) for c in actual['cells']))
        self.assertEqual(next(c['blocks'] for c in actual['cells'] if c['row']==1 and c['column']==0),[2])
        self.assertEqual(sorted(i for c in actual['cells'] for i in c['blocks']),list(range(5)))
