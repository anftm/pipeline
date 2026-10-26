"""Reader images are DjVu-only; OCR inputs and independent images are unrelated."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from scripts import pdf_ocr, pdf_ocr_stages as stages, pdf_reading_v3 as v3, pdf_assets, shared
from scripts import publish_reader_v3 as publish


class DjvuOnlyReadingPolicyTests(unittest.TestCase):
    def test_every_pdf_source_kind_is_png_for_ocr_only_even_with_obsolete_image_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'scan.pdf'
            Image.new('RGB', (100, 150), 'white').save(source, 'PDF', resolution=150)
            original = source.read_bytes()
            for extension in ['pdf', 'caj', 'kdh', 'ppt', 'pptx', 'ps']:
                with self.subTest(extension=extension), \
                        patch.object(pdf_ocr, 'encode_jxl', side_effect=AssertionError('no PDF reading encode')):
                    bundle = root / extension
                    item = {'key': 'VoiceOfML/Test\0book.' + extension, 'source_revision': 'r1',
                        'source_extension': extension, 'source_kind': 'upstream' if extension == 'pdf' else 'generated',
                        'force_image_render': True, 'reader_presentation': {'strategy': 'page-stream'}}
                    result = stages.render_book(item, source, bundle)
                    manifest = json.loads((bundle / result['render_manifest']['path']).read_bytes())
                    stages.validate_render(result, manifest)
                    self.assertFalse(result['image_rendered'])
                    self.assertIsNone(result['page_manifest'])
                    self.assertEqual(result['reader_presentation']['strategy'], 'preserve-pdf')
                    self.assertTrue(all('i' in page and 'j' not in page and 'w' not in page for page in manifest['pages']))
                    self.assertFalse([p for p in bundle.rglob('*') if p.suffix in {'.webp', '.jxl', '.ppm'}])
                    self.assertEqual(source.read_bytes(), original)

    def test_new_pdf_reading_has_no_preview_dependencies_but_keeps_primary_and_text(self):
        from tests.test_publish_reader_v3 import V3PublicationTests
        case = V3PublicationTests();case.setUp()
        try:
            ref = case.candidate()
            manifest = v3.decode((case.bundle / ref['path']).read_bytes())
            self.assertEqual(manifest['preview'], {'page_count': 1, 'complete': False,
                'ready_pages': 0, 'partitions': [], 'status': 'skipped'})
            staged = publish.stage(case.store, ref, case.bundle, apply=True)
            candidate = publish.read_index(case.store, staged['candidate'])
            paths = [r['path'] for r in candidate['resources']]
            self.assertFalse(any('/preview/' in p or '/pages/' in p for p in paths))
            self.assertTrue(any(p.endswith('/document.pdf') for p in paths))
            self.assertTrue(any('/text/page-' in p for p in paths))
        finally:
            case.doCleanups()

    def test_obsolete_pdf_asset_builder_and_renderer_cannot_create_new_reading_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);source = root / 'source.pdf';source.write_bytes(b'%PDF-test')
            with patch.object(pdf_assets, '_render', side_effect=AssertionError('no PDF image samples')):
                result = pdf_assets.build_item({'extension': 'pdf', 'range_risk_candidate': True}, source, root)
            self.assertEqual(result['strategy'], 'preserve-pdf')
            self.assertEqual(result['status'], 'skipped')
            with self.assertRaisesRegex(ValueError, 'only DjVu'):
                pdf_assets._render(source, 1, root)

    def test_retiring_stale_pdf_image_route_never_removes_ocr_metadata(self):
        path = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        old = {'s': 2, 'm': 'p', 'p': path + '/page-manifest.json', 'b': shared.PDF_PAGES_BUCKET,
            'o': path + '/ocr-manifest.json', 'om': 'scan',
            'pd': path + '/document.pdf', 'pdb': shared.PDF_PAGES_BUCKET}
        result = shared.merge_pdf_ocr_sidecar_entry(old, {'status': 'ready', 'source_kind': 'generated',
            'page_manifest': {'path': path + '/obsolete/page-manifest.json'}, 'ocr_manifest': old['o']})
        self.assertEqual(result['p'], old['pd'])
        self.assertEqual(result['o'], old['o'])
        self.assertNotIn('pd', result)
        self.assertEqual(old['p'], path + '/page-manifest.json', 'do not mutate historical source')
