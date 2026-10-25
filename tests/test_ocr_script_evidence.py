import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from scripts import ocr_script_evidence as evidence


class ScriptEvidenceTests(unittest.TestCase):
    def test_false_cyrillic_vote_requires_independent_chinese_evidence(self):
        original = {'language': 'ru', 'planned_language': 'ch', 'source': 'page-image',
                    'scripts': ['Cyrillic'], 'issues': [], 'probe': {'regions': []}}
        wrong = [{'t': 'CO3 + H2O', 'b': [.1, .1, .9, .2]}]
        candidate = [{'t': '弱碱的阳离子和强酸的阴离子所生成的盐能进行水解', 'b': [.1, .6, .9, .7]}]
        models = {'models': {'rec': {'sha256': 'a' * 64}}}
        def recognize(language):
            self.assertEqual(language, 'ch')
            return candidate, models
        def confirmed(_path, value, _blocks):
            return {**value, 'scripts': ['Han'], 'source': 'page-image-corroborated',
                    'independent_script': {'corroborated': True}}
        with patch.object(evidence, 'corroborate', side_effect=confirmed):
            selected, blocks, actual = evidence.recover_chinese_route('page.png', original, wrong, {}, recognize)
        self.assertEqual(selected['language'], 'ch')
        self.assertEqual(blocks, candidate)
        self.assertEqual(actual, models)
        self.assertEqual(selected['previous_route']['blocks'], wrong)
        self.assertEqual(selected['probe'], original['probe'])
        self.assertEqual(original['language'], 'ru')
        with patch.object(evidence, 'corroborate', side_effect=lambda _p, v, _b: v):
            selected, blocks, _ = evidence.recover_chinese_route('page.png', original, wrong, {}, recognize)
        self.assertEqual(selected['language'], 'ru')
        self.assertEqual(blocks, wrong)

    def test_genuine_kana_cannot_be_replaced_by_chinese_filename(self):
        original = {'language': 'japan', 'planned_language': 'ch', 'source': 'page-image'}
        blocks = [{'t': '日本語の本文です。これは文章です。'}]
        def unexpected(_language):
            self.fail('positive Japanese script must be preserved')
        self.assertEqual(evidence.recover_chinese_route('page.png', original, blocks, {}, unexpected),
                         (original, blocks, {}))

    def test_independent_regions_corroborate_script_and_record_model_and_pixels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = root / 'chi_sim.traineddata'
            model.write_bytes(b'fixture-model')
            image = root / 'page.png'
            Image.new('L', (300, 500), 255).save(image)
            blocks = [{'t': '中国政法大学研究生院', 'b': [.1, .1, .9, .2]},
                      {'t': '厄尔神话的命运阐释', 'b': [.1, .7, .9, .8]}]
            original = {'language': 'ch', 'source': 'filename', 'scripts': [],
                        'probe': {'regions': [{'script': 'Han', 'confidence': .6}]}}
            outputs = [SimpleNamespace(returncode=0, stderr='', stdout='level\ttext\n5\t' + b['t'] + '\n')
                       for b in blocks]
            with patch.dict('os.environ', {'TESSDATA_PREFIX': temp}), \
                    patch.object(evidence.subprocess, 'run', side_effect=outputs):
                result = evidence.corroborate(image, original, blocks)
            self.assertEqual(original['scripts'], [])
            self.assertEqual(result['scripts'], ['Han'])
            self.assertEqual(result['probe'], original['probe'])
            proof = result['independent_script']
            self.assertTrue(proof['corroborated'])
            self.assertEqual(proof['sha256'], hashlib.sha256(model.read_bytes()).hexdigest())
            self.assertTrue(all(len(row['crop_sha256']) == 64 for row in proof['regions']))

    def test_timeout_retains_primary_results_without_claiming_corroboration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'chi_sim.traineddata').write_bytes(b'model')
            image = root / 'page.png'
            Image.new('L', (300, 500), 255).save(image)
            blocks = [{'t': '中国政法大学研究生院', 'b': [.1, .1, .9, .2]}]
            original = {'language': 'ch', 'source': 'filename', 'scripts': []}
            with patch.dict('os.environ', {'TESSDATA_PREFIX': temp}), \
                    patch.object(evidence.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tesseract', 20)):
                result = evidence.corroborate(image, original, blocks)
            self.assertEqual(result['scripts'], [])
            self.assertFalse(result['independent_script']['corroborated'])
            row = result['independent_script']['regions'][0]
            self.assertEqual(row['primary'], blocks[0]['t'])
            self.assertIn('TimeoutExpired', row['error'])

    def test_three_short_table_headers_keep_the_existing_character_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'chi_sim.traineddata').write_bytes(b'fixture-model')
            image=root/'page.png';Image.new('L',(900,600),255).save(image)
            blocks=[{'t':t,'b':[.05+i*.3,.1,.29+i*.3,.2]} for i,t in enumerate(
                ['原子量以氧单位计','最常见的原子价','化整数后数值'])]
            original={'language':'ch','planned_language':'ch','source':'page-image','scripts':[]}
            def recognize(command,**kwargs):
                index=int(Path(command[1]).stem)
                return SimpleNamespace(returncode=0,stderr='',stdout='level\ttext\n5\t'+blocks[index]['t']+'\n')
            with patch.dict('os.environ',{'OCR_SCRIPT_MODEL_DIR':temp}),patch.object(evidence.subprocess,'run',side_effect=recognize):
                result=evidence.corroborate(image,original,blocks)
            self.assertTrue(result['independent_script']['corroborated'])
            self.assertEqual(len(result['independent_script']['regions']),3)
