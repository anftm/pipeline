import copy
import unittest
from PIL import Image,ImageDraw

from scripts import ocr_chronology_table as chronology,pdf_text_layer


def fixture():
    image=Image.new('RGB',(1000,1500),'white');draw=ImageDraw.Draw(image)
    for bounds in [(50,180,950,180),(410,270,660,270),(50,360,950,360)]:draw.line(bounds,fill='black',width=3)
    blocks=[]
    def add(text,box):blocks.append({'t':text,'b':box,'c':.999,'s':'ocr'})
    add('TABLE 16.1',[.05,.05,.2,.07]);add('Chronological survey',[.05,.08,.5,.1])
    add('Revolution and Postsocialist Transition',[.42,.14,.65,.17])
    for literal,x in [('Country',.07),('Reform',.22),('Continuation',.41),('Bypassing',.59),('Events with Dates',.74)]:
        add(literal,[x,.20,x+.12,.22])
    for number in range(6):
        y=.28+number*.10
        add('Country '+chr(65+number),[.06,y,.17,y+.015])
        for sub in range(2):
            top=y+sub*.04
            add(str(1900+number*2+sub)+'-',[.23,top,.28,top+.015])
            if number%2==0:add(str(1950+sub),[.43,top,.48,top+.015])
            if number%2==1:add(str(1980+sub),[.60,top,.65,top+.015])
            add(str(1900+number*2+sub)+': Original event',[.72,top,.94,top+.015])
            add('continuation without normalization',[.72,top+.016,.94,top+.031])
    add('Note: original incomplete ending',[.05,.94,.94,.96])
    return image,blocks


class ChronologyTableTests(unittest.TestCase):
    def test_shared_header_country_rowspan_and_every_wrapped_event_survive(self):
        image,blocks=fixture();before=copy.deepcopy(blocks)
        table=chronology.detect(image,blocks)
        self.assertIsNotNone(table)
        self.assertEqual(len(table['columns'])-1,5)
        self.assertEqual(table['evidence']['shared_header']['columns'],[2,4])
        self.assertEqual(sum(c['column']==0 and c['rowspan']==2 and c['row']>=2 for c in table['cells']),6)
        arranged=chronology.arrange(blocks,*image.size,table)
        self.assertIn('1900: Original event\ncontinuation without normalization',arranged['text'])
        self.assertEqual(sorted(s['block'] for s in arranged['text_spans']),list(range(len(blocks))))
        layer=pdf_text_layer.from_page({'kind':'pdf-ocr-page','source':'ocr','page':210,
            'width':image.width,'height':image.height,**arranged},'a'*64,'en')
        pdf_text_layer.validate(layer);self.assertEqual(blocks,before)
        self.assertTrue(all('text' not in c for c in layer['tables'][0]['cells']))

    def test_no_source_rules_or_missing_dates_cannot_establish_table(self):
        image,blocks=fixture()
        self.assertIsNone(chronology.detect(Image.new('RGB',image.size,'white'),blocks))
        self.assertIsNone(chronology.detect(image,[b for b in blocks if not chronology.DATE.fullmatch(b['t'])]))

    def test_indented_parts_of_country_and_unrelated_left_prose_are_never_lost(self):
        image,blocks=fixture()
        first=next(b for b in blocks if b['t']=='Country B')
        first.update(t='Parts of',b=[.075,.38,.17,.395])
        blocks.append({'t':'country','b':[.075,.396,.17,.411],'c':1,'s':'ocr'})
        table=chronology.detect(image,blocks)
        self.assertIsNotNone(table)
        self.assertTrue(any(c['text']=='Parts of\ncountry' for c in table['cells']))
        mapped=[]
        for block in blocks:
            a,b,c,d=block['b'];mapped.append({**block,'b':[.5+a*.5,b,.5+c*.5,d]})
        mapped.extend([{'t':'Left paragraph one.','b':[.05,.4,.43,.43],'c':1,'s':'ocr'},
                       {'t':'Left paragraph two.','b':[.05,.46,.43,.49],'c':1,'s':'ocr'}])
        page=Image.new('RGB',(2000,1500),'white');page.paste(image,(1000,0))
        result=chronology.detect(page,mapped)
        self.assertIsNotNone(result)
        arranged=chronology.arrange(mapped,*page.size,result)
        self.assertLess(arranged['text'].index('Left paragraph two.'),arranged['text'].index('TABLE 16.1'))
        self.assertEqual(arranged['text'].count('Parts of\ncountry'),1)
        self.assertTrue(any(c['column']==2 and not c['blocks'] for c in result['cells']))

    def test_ink_in_apparently_empty_date_cell_is_not_erased_or_filled(self):
        image,blocks=fixture();grid=chronology.detect(image,blocks)
        cell=next(c for c in grid['cells'] if c['row']>=2 and c['column']==3 and not c['blocks'])
        a,b,c,d=cell['box'];x=round((a+c)/2*image.width);y=round((b+d)/2*image.height)
        ImageDraw.Draw(image).rectangle((x,y,x+4,y+5),fill='black')
        actual=chronology.detect(image,blocks)
        self.assertIsNotNone(actual)
        self.assertTrue(any(r['code']=='chronology-empty-cell-with-ink' for r in actual['review']))
        self.assertEqual(next(c for c in actual['cells'] if c['row']==cell['row'] and c['column']==3)['blocks'],[])
