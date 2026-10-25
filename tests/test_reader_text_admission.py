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
