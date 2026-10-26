"""Boxes and complete source routes, not inferred all-to-all causal meaning."""
import copy
import unittest

import numpy as np
from PIL import Image,ImageDraw

from scripts import ocr_flow_diagram as flow,pdf_text_layer,ocr_quality


def fixture():
    image=Image.new('RGB',(1100,800),'white');draw=ImageDraw.Draw(image)
    boxes=[(60,180,280,500),(420,180,640,430),(780,180,1000,550)]
    for box in boxes:draw.rectangle(box,outline='black',width=3)
    def arrow(points,direction):
        draw.line(points,fill='black',width=3)
        x,y=points[-1]
        if direction=='right':arms=[(x-25,y-8),(x,y),(x-25,y+8)]
        elif direction=='down':arms=[(x-8,y-25),(x,y),(x+8,y-25)]
        else:arms=[(x-8,y+25),(x,y),(x+8,y+25)]
        draw.line(arms,fill='black',width=3)
    arrow([(280,310),(420,310)],'right')
    arrow([(120,180),(120,80),(890,80),(890,180)],'down')
    arrow([(530,430),(530,640),(900,640),(900,550)],'up')
    for y in (330,345,360):draw.rectangle((805,y,809,y+5),fill='black')
    blocks=[]
    def add(value,box,confidence=.999):
        blocks.append({'t':value,'b':[a/(image.width if i%2==0 else image.height) for i,a in enumerate(box)],
                       'c':confidence,'s':'ocr'})
    add('Original left prose.',(5,25,250,45))
    for n,(x,y,u,v) in enumerate(boxes,1):
        add('Block '+str(n),(x+15,y+42,x+100,y+62))
        add('Original short-',(x+15,y+90,u-15,y+110))
        add('age and letters.',(x+15,y+115,u-15,y+135))
    add('>',(392,300,419,322),.4)
    add('V',(879,151,901,180),.99)
    add('4',(886,548,914,577),.4)
    add('faint annotation',(12,350,48,380),.3)
    add('Source note, do not omit.',(55,700,900,730))
    return image,blocks


class FlowDiagramTests(unittest.TestCase):
    def test_original_complete_routes_and_box_members_remain_separate(self):
        image,blocks=fixture();before=copy.deepcopy(blocks)
        graph=flow.detect(image,blocks)
        self.assertIsNotNone(graph)
        self.assertEqual(len(graph['nodes']),3)
        self.assertEqual({(e['from'],e['to']) for e in graph['edges']},{('n1','n4'),('n1','n7'),('n4','n7')})
        self.assertEqual({e['arrow_direction'] for e in graph['edges']},{'right','down','up'})
        self.assertEqual(graph['review'],[])
        self.assertEqual(len(graph['nodes'][-1]['marks'][0]['boxes']),3)
        output,models,actual=flow.filter_detections(image,blocks,{'models':{}},graph)
        excluded=models['diagnostics']['flow_graphics']['excluded']
        self.assertIn('>',{r['original_block']['t'] for r in excluded})
        # Any unmatched original arm remains reviewable instead of being
        # erased just because its OCR string resembles an arrow.
        removed={r['index'] for r in excluded}
        for index,block in enumerate(blocks):
            if index not in removed:self.assertTrue(any(b==block for b in output))
        self.assertTrue(any(b['t']=='faint annotation' for b in output))
        self.assertEqual(sum(b['t']=='⋮' for b in output),1)
        self.assertEqual(blocks,before)
        result=flow.arrange(output,*image.size,actual)
        self.assertIn('Original short-\nage and letters.',result['text'])
        self.assertEqual(result['text'].count('faint annotation'),1)
        self.assertEqual(sorted(s['block'] for s in result['text_spans']),list(range(len(output))))
        payload={'kind':'pdf-ocr-page','page':194,'source':'ocr','width':image.width,'height':image.height,**result}
        layer=pdf_text_layer.from_page(payload,'a'*64,'en');pdf_text_layer.validate(layer)
        self.assertTrue(all('blocks' not in node for node in layer['flow_diagrams'][0]['nodes']))
        self.assertTrue(all('text' not in node for node in layer['flow_diagrams'][0]['nodes']))
        self.assertTrue(layer['flow_diagrams'][0]['nodes'][-1]['marks'][0]['operator_region_id'].startswith('b'))

    def test_body_letters_are_not_excluded_even_if_their_string_looks_like_an_arrow(self):
        image,blocks=fixture();blocks[2]['t']='V'
        graph=flow.detect(image,blocks)
        output,models,actual=flow.filter_detections(image,blocks,{},graph)
        self.assertTrue(any(b['t']=='V' and b['b']==blocks[2]['b'] for b in output))
        image=Image.new('RGB',image.size,'white')
        self.assertIsNone(flow.detect(image,blocks))

    def test_missing_arrowhead_cannot_be_accepted_from_a_straight_line_or_box_edge(self):
        image,blocks=fixture();draw=ImageDraw.Draw(image)
        draw.rectangle((385,290,417,330),fill='white');draw.line((280,310,418,310),fill='black',width=3)
        graph=flow.detect(image,blocks)
        self.assertNotIn(('n1','n4'),{(e['from'],e['to']) for e in graph['edges']})
        self.assertTrue(any(r['code']=='flow-route-arrowhead-unresolved' for r in graph['review']))
        self.assertIsNone(flow.arrowhead(np.asarray(image.convert('L')),[418,310],'right',20))

    def test_missing_node_and_changed_route_direction_fail_layer_validation(self):
        image,blocks=fixture();graph=flow.detect(image,blocks)
        result=flow.arrange(blocks,*image.size,graph)
        layer=pdf_text_layer.from_page({'kind':'pdf-ocr-page','page':194,'source':'ocr',
            'width':image.width,'height':image.height,**result},'a'*64,'en')
        for key,value in [('to','unknown'),('arrow_direction','left')]:
            changed=copy.deepcopy(layer);changed['flow_diagrams'][0]['edges'][0][key]=value
            changed['generation']=pdf_text_layer.digest({k:v for k,v in changed.items() if k!='generation'})
            with self.subTest(key=key),self.assertRaises(ValueError):pdf_text_layer.validate(changed)
        changed=copy.deepcopy(layer)
        edge=changed['flow_diagrams'][0]['edges'][0]
        edge['path'][0]=[edge['path'][0][0],edge['path'][0][1]-.01]
        changed['generation']=pdf_text_layer.digest({k:v for k,v in changed.items() if k!='generation'})
        with self.assertRaises(ValueError):pdf_text_layer.validate(changed)
