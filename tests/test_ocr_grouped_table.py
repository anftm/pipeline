"""Source underlines establish group spans; numbers and case never get inferred."""
import copy
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image,ImageDraw

from scripts import ocr_aligned_table as aligned,ocr_grouped_table as grouped,pdf_text_layer


def fixture(split=3):
    image=Image.new('RGB',(1200,1000),'white');draw=ImageDraw.Draw(image)
    draw.line((60,150,1140,150),fill='black',width=3)
    anchors=[.08,.21,.34,.47,.60,.73,.86]
    cut=(anchors[split]+anchors[split+1])/2
    groups=[(.16,cut-.025), (cut+.025,.90)]
    for left,right in groups:
        draw.line((round(left*1200),208,round(right*1200),204),fill='black',width=3)
    blocks=[]
    def add(text,box):
        blocks.append({'t':text,'b':box,'s':'ocr','c':.999})
    add('TABLE 1.2',[.05,.05,.16,.07]);add('Original grouped statistics',[.05,.09,.5,.12])
    for name,(left,right) in zip(['Primary Products','Manufactured Products'],groups):
        center=(left+right)/2
        add(name,[center-.06,.16,center+.06,.19])
    for x in anchors[1:]:
        add('Socialist' if x<.3 else 'Capitalist',[x-.025,.24,x+.025,.26])
        add('Countries',[x-.03,.27,x+.03,.29])
    data_rows=[]
    for group in range(3):
        top=.35+group*.18;add('Category '+chr(65+group),[.055,top,.18,top+.02])
        for offset,year in [(0,1955),(1,1977)]:
            y=top+.05+offset*.055
            add(str(year),[.065,y,.095,y+.02])
            values=[]
            for column,x in enumerate(anchors[1:]):
                value=f'{group+offset}.{column+1:02d}';values.append(value)
                add(value,[x-.02,y,x+.02,y+.02])
            data_rows.append(values)
    add('Source: original footnote',[.05,.94,.5,.96])
    grids=aligned.detect(image,blocks)
    assert len(grids)==1
    return image,blocks,grids[0],data_rows


class GroupedTableTests(unittest.TestCase):
    def test_source_rules_establish_shared_heads_category_rows_and_verbatim_data(self):
        image,blocks,base,expected=fixture();before=copy.deepcopy((blocks,base))
        table=grouped.detect(image,blocks,base)
        self.assertIsNotNone(table)
        self.assertEqual([r['columns'] for r in table['evidence']['shared_header_underlines']],[[1,4],[4,7]])
        actual=[[c['text'] for c in table['cells'] if c['row']==row and c['column']>0]
                for row in table['evidence']['numeric_row_indices']]
        self.assertEqual(actual,expected)
        category_cells=[c for c in table['cells'] if c['colspan']==7]
        self.assertEqual([c['text'] for c in category_cells],['Category A','Category B','Category C'])
        arranged=aligned.arrange(blocks,*image.size,[table])
        self.assertEqual(sorted(s['block'] for s in arranged['text_spans']),list(range(len(blocks))))
        layer=pdf_text_layer.from_page({'kind':'pdf-ocr-page','source':'ocr','page':183,
            'width':image.width,'height':image.height,**arranged},'a'*64,'en')
        pdf_text_layer.validate(layer)
        self.assertTrue(all('text' not in c for c in layer['tables'][0]['cells']))
        self.assertEqual((blocks,base),before)

    def test_group_sizes_follow_original_underlines_not_equal_column_counts(self):
        image,blocks,base,expected=fixture(split=2)
        table=grouped.detect(image,blocks,base)
        self.assertIsNotNone(table)
        self.assertEqual([r['columns'] for r in table['evidence']['shared_header_underlines']],[[1,3],[3,7]])

    def test_detector_fragments_merge_only_when_intervening_original_pixels_prove_line(self):
        image=Image.new('L',(1000,400),'white');draw=ImageDraw.Draw(image)
        draw.line((100,200,900,192),fill='black',width=3)
        fragments=np.asarray([[[100,200,450,196.5]],[[400,197,650,194.5]],[[620,194.8,900,192]]],np.float32)
        detector=type('Detector',(),{'detect':lambda self,gray:(fragments,None,None,None)})()
        with patch('cv2.createLineSegmentDetector',return_value=detector):
            rules=grouped.source_underlines(np.asarray(image))
            self.assertEqual(len(rules),1)
            self.assertEqual(len(rules[0]['segments_pixels']),3)
            self.assertEqual(rules[0]['line_pixels'][::2],[100,900])
            self.assertTrue(all(r['column_support']>=.9 for r in rules[0]['threshold_evidence']))
            draw.rectangle((460,180,540,215),fill='white')
            self.assertEqual(grouped.source_underlines(np.asarray(image)),[])

    def test_missing_underlines_or_missing_group_cannot_justify_merged_headers(self):
        image,blocks,base,expected=fixture()
        self.assertIsNone(grouped.detect(Image.new('RGB',image.size,'white'),blocks,base))
        self.assertIsNone(grouped.detect(image,[b for b in blocks if b['t']!='Primary Products'],base))

    def test_unrelated_reviews_and_ink_in_unrecognized_data_cells_remain_visible(self):
        image,blocks,base,expected=fixture()
        target=next(i for i,b in enumerate(blocks) if b['t']=='0.01')
        b=blocks[target]['b'];draw=ImageDraw.Draw(image)
        draw.rectangle(tuple(round(v*(image.width if axis%2==0 else image.height)) for axis,v in enumerate(b)),fill='black')
        blocks.pop(target)
        base=aligned.detect(image,blocks)[0]
        base['review'].append({'code':'custom-source-failure','block':0})
        table=grouped.detect(image,blocks,base)
        self.assertIsNotNone(table)
        self.assertIn({'code':'custom-source-failure','block':0},table['review'])
        self.assertTrue(any(r['code']=='aligned-table-empty-cell-with-ink' for r in table['review']))
