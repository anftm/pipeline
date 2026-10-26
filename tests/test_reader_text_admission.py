import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import reader_text_admission as admission, pdf_text_layer as text, pdf_reading_v3 as v3
from scripts import ocr_quality, shared, export_reader_txt as exporter
from tests.test_pdf_text_layer import raw_page

PROFILE = 'pdf-ocr-fixture-quality-v1-layout-layout-v3-fixture'


def recognized_layers(contents, source, objects, *, profile=PROFILE, mutate=None):
    layers = []
    for page, body in enumerate(contents, 1):
        payload = {**raw_page(body), 'page': page, 'recognition': {
            'policy': ocr_quality.POLICY, 'profile': profile, 'source_sha256': source,
            'input_sha256': 'd' * 64, 'language': 'ch', 'language_evidence': {
                'language': 'ch', 'source': 'override', 'scripts': ['Han'], 'issues': [], 'probe': {'blank': not body}},
            'models': {'models': {'det': {'sha256': 'e' * 64}, 'rec': {'sha256': 'f' * 64}}}}}
        payload['quality_check'] = ocr_quality.check_page(payload)
        if mutate:
            mutate(payload)
        raw = gzip.compress(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(), mtime=0)
        sha = hashlib.sha256(raw).hexdigest()
        path = f'objects/{source[:2]}/{source}/{sha[:16]}/ocr/page-{page:06d}.json.gz'
        ref = {'bucket': shared.PDF_PAGES_BUCKET, 'path': path, 'bytes': len(raw), 'sha256': sha, 'role': 'provenance'}
        objects[ref['bucket'], path] = raw
        layers.append(text.from_page(payload, source, 'ch', ref))
    return layers


class ReaderTextAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.spec = {'source_sha256': 'a' * 64, 'recognition_profile': PROFILE, 'page_count': 2}

    def read(self, ref):
        return self.objects[ref['bucket'], ref['path']]

    def test_cached_pass_cannot_hide_missing_text_or_model_identity(self):
        def damage(payload):
            if payload['page'] == 2:
                payload['recognition']['models']['models']['rec']['sha256'] = 'unknown'
        layers = recognized_layers(['正文', '末页'], 'a' * 64, self.objects, mutate=damage)
        with self.assertRaisesRegex(admission.QualityRejection, 'model-identity'):
            admission.check(self.spec, layers, self.read)

    def test_blank_page_and_complete_coverage_survive_quality_admission(self):
        layers = recognized_layers(['正文', ''], 'a' * 64, self.objects)
        value = admission.check(self.spec, layers, self.read)
        self.assertEqual(value['quality_check']['checked_pages'], 2)
        self.assertEqual(value['quality_check']['status'], 'passed')
        self.assertEqual(value['quality_check']['character_accuracy'], 'unreviewed')
        with self.assertRaisesRegex(admission.QualityRejection, 'every physical page'):
            admission.check(self.spec, layers[:1], self.read)

    def test_wrong_recognition_profile_or_corrupt_raw_resource_blocks_body(self):
        layers = recognized_layers(['正文', '末页'], 'a' * 64, self.objects)
        with self.assertRaisesRegex(admission.QualityRejection, 'recognition version'):
            admission.check({**self.spec, 'recognition_profile': PROFILE + '-changed'}, layers, self.read)
        ref = layers[1]['provenance']['raw_resource']
        self.objects[ref['bucket'], ref['path']] += b'corrupted'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            admission.check(self.spec, layers, self.read)

    def test_positioned_review_requires_the_actual_raw_image_not_just_the_book_sha(self):
        layers=recognized_layers(['正文','末页'],'a'*64,self.objects)
        original=layers[0]
        proposal={'kind':'pdf-text-correction','version':1,'page':1,
            'base_generation':original['generation'],'raw_sha256':original['raw_sha256'],
            'page_identity':original['page_identity'],'replacements':[],
            'visual_review':{'kind':'source-image-review','source_sha256':'a'*64,'page':1,
                'input_png_sha256':'c'*64,'basis':'Full source image'},
            'additions':[{'region_id':'v'+'1'*16,'text':'P','box':[.1,.3,.2,.4],
                'quad':[[.1,.3],[.2,.3],[.2,.4],[.1,.4]],
                'evidence':{'crop_sha256':'f'*64,'reason':'Printed source glyph'}}],
            'region_order':['b0','v'+'1'*16]}
        layers[0]=text.accept_proposal(original,proposal,actor='explicit-review')
        with self.assertRaisesRegex(admission.QualityRejection,'different source image'):
            admission.check(self.spec,layers,self.read)
        proposal['visual_review']['input_png_sha256']='d'*64
        layers[0]=text.accept_proposal(original,proposal,actor='explicit-review')
        report=admission.check(self.spec,layers,self.read)
        self.assertEqual(report['quality_check']['status'],'passed')
        self.assertEqual(report['pages'][0]['text_generation'],layers[0]['generation'])

    def test_separating_unresolved_margin_mark_does_not_make_book_approved_body(self):
        import copy
        layers=recognized_layers(['Printed','Last page'],'a'*64,self.objects)
        original=layers[0];accepted=copy.deepcopy(original)
        mark={'id':'b99','text':'9','box':[.01,.3,.04,.35],'quad':[[.01,.3],[.04,.3],[.04,.35],[.01,.35]],
              'confidence':.4,'source':'ocr'}
        accepted.update(revision='accepted',quality='partially-reviewed',parent_generation=original['generation'],
            acceptances=[{'proposal_sha256':'c'*64,'actor':'user-source-review'}],
            annotations=[{'id':'b99','classification':'marginal-mark','status':'unresolved','literal_text':None,
                'box':mark['box'],'quad':mark['quad'],'original_region':mark,
                'evidence':{'crop_sha256':'f'*64,'reason':'User original handwritten margin mark'}}])
        accepted['generation']=text.digest({k:v for k,v in accepted.items() if k!='generation'})
        text.validate(accepted);layers[0]=accepted
        with self.assertRaisesRegex(admission.QualityRejection,'unresolved source annotations'):
            admission.check(self.spec,layers,self.read)

    def test_retaining_undetected_handwriting_cannot_bypass_body_admission(self):
        layers=recognized_layers(['Printed','Last page'],'a'*64,self.objects);original=layers[0]
        proposal={'kind':'pdf-text-correction','version':1,'page':1,'base_generation':original['generation'],
            'raw_sha256':original['raw_sha256'],'page_identity':original['page_identity'],'replacements':[],
            'visual_review':{'kind':'source-image-review','source_sha256':'a'*64,'page':1,
                'input_png_sha256':'d'*64,'basis':'Original undetected margin ink'},
            'annotation_additions':[{'id':'a'+'1'*16,'classification':'handwritten-annotation','status':'unresolved',
                'literal_text':None,'box':[.8,.2,.9,.3],'quad':[[.8,.2],[.9,.2],[.9,.3],[.8,.3]],
                'evidence':{'crop_sha256':'f'*64,'reason':'Keep literal reading unresolved'}}]}
        layers[0]=text.accept_proposal(original,proposal,actor='explicit-source-review')
        with self.assertRaisesRegex(admission.QualityRejection,'unresolved source annotations'):
            admission.check(self.spec,layers,self.read)
