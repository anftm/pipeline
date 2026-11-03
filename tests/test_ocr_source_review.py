import base64
import copy
import hashlib
import io
import unittest
from PIL import Image

from scripts import ocr_source_review as review


class SourceReviewTests(unittest.TestCase):
    def test_scientific_symbol_coverage_is_exact_page_and_input_not_character_acceptance(self):
        rules=[{'page':74,'input_sha256':'a'*64,'classification':'scientific-symbols',
                'script_coverage':['Han','Latin'],'basis':'Original source chemical equations, not English prose'}]
        proof=review.scientific_coverage(rules,page=74,input_sha256='a'*64,source_sha256='b'*64,language='ch')
        self.assertEqual(proof['character_accuracy'],'unreviewed')
        self.assertEqual(proof['script_coverage'],['Han','Latin'])
        for page,sha,language in [(75,'a'*64,'ch'),(74,'c'*64,'ch'),(74,'a'*64,'en')]:
            self.assertIsNone(review.scientific_coverage(rules,page=page,input_sha256=sha,source_sha256='b'*64,language=language))
    def test_only_exact_page_input_and_reviewed_region_can_be_excluded(self):
        image=Image.new('RGB',(100,200),'white')
        blocks=[{'t':'S','b':[.95,.03,.99,.05],'c':.2,'s':'ocr'},
                {'t':'János Kornai','b':[.5,.3,.8,.4],'c':.999,'s':'ocr'}]
        models={'models':{'rec':{'sha256':'a'*64}}}
        before=copy.deepcopy((blocks,models));source_pixels=image.tobytes()
        rules=[{'page':1,'input_sha256':'a'*64,'box':[.94,.02,1,.06],
                'classification':'scan-artifact','basis':'Full page and enlarged source pixels'}]
        actual,evidence=review.apply(image,blocks,models,rules,page=1,input_sha256='a'*64)
        self.assertEqual(actual,[blocks[1]])
        proof=evidence['diagnostics']['source_nontext_reviews']
        self.assertEqual(proof['original_blocks'],blocks)
        raw=base64.b64decode(proof['excluded'][0]['crop_png_base64'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(),proof['excluded'][0]['crop_sha256'])
        self.assertEqual(Image.open(io.BytesIO(raw)).size,(6,8))
        self.assertEqual((blocks,models),before)
        self.assertEqual(image.tobytes(),source_pixels)
        for page,sha in [(2,'a'*64),(1,'b'*64)]:
            kept,_=review.apply(image,blocks,models,rules,page=page,input_sha256=sha)
            self.assertEqual(kept,blocks)
        rules[0]['box']=[.96,.02,1,.06]
        self.assertEqual(review.apply(image,blocks,models,rules,page=1,input_sha256='a'*64)[0],blocks)

    def test_no_visual_review_cannot_exclude_a_low_confidence_letter(self):
        image=Image.new('RGB',(100,200),'white');blocks=[{'t':'i','b':[.2,.3,.25,.4],'c':.2}]
        rules=[{'page':1,'input_sha256':'a'*64,'box':[0,0,1,1],'classification':'scan-artifact'}]
        self.assertEqual(review.apply(image,blocks,{},rules,page=1,input_sha256='a'*64)[0],blocks)

    def test_empty_detection_requires_exact_original_geometry_and_keeps_failure_evidence(self):
        image=Image.new('RGB',(100,200),'white')
        empty={'region':0,'b':[.96,.03,.99,.05],'q':[[.96,.03],[.99,.03],[.99,.05],[.96,.05]],'text':''}
        models={'diagnostics':{'empty_regions':[0,1,2], 'detection_regions':[empty,
            {'region':1,'text':''},{'region':2,'b':[.3,.3,.4,.4],'text':''}]}}
        before=copy.deepcopy(models)
        rules=[{'page':336,'input_sha256':'a'*64,'box':[.95,.02,1,.06],
                'classification':'scan-artifact','basis':'Full page and user enlarged scan mark'}]
        blocks,actual=review.apply(image,[],models,rules,page=336,input_sha256='a'*64)
        self.assertEqual(blocks,[])
        self.assertEqual(actual['diagnostics']['empty_regions'],[1,2])
        proof=actual['diagnostics']['source_nontext_reviews']
        self.assertEqual(proof['original_empty_regions'],[0,1,2])
        self.assertEqual(proof['excluded_empty_detections'][0]['original_block'],empty)
        self.assertEqual(models,before)
        self.assertEqual(review.apply(image,[],models,rules,page=336,input_sha256='b'*64)[1],models)
