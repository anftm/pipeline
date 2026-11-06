"""Original PDF/DjVu stay direct documents; retired page streams never run."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from scripts import (build_reader_assets_index, build_reader_assets_v2_sidecar,
                     djvu_pages, pdf_reading_v3 as v3, reader_page_codec, shared)


class DirectDocumentPolicyTests(unittest.TestCase):
    def test_pdf_and_djvu_primary_builders_skip_page_images(self):
        with pymupdf.open() as document:
            document.new_page(width=100, height=200)
            pdf = document.tobytes()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for kind, raw in (('pdf', pdf), ('djvu', b'AT&TFORM-djvu-fixture')):
                with self.subTest(kind=kind):
                    source = hashlib.sha256(raw).hexdigest()
                    path = f"objects/{source[:2]}/{source}/{'a'*16}/document.{kind}"
                    ref = {'bucket': shared.PDF_PAGES_BUCKET, 'path': path, 'sha256': source,
                           'bytes': len(raw), 'role': 'runtime'}
                    spec = {'source_key': 'repo\0book.'+kind, 'source_sha256': source,
                            'primary': ref, 'previews': [{'must_not': 'read'}],
                            'require_complete_preview': True}
                    bundle = root / kind
                    def read(requested):
                        if requested == ref:
                            return raw
                        return (bundle / requested['path']).read_bytes()
                    geometry = {'version': 1, 'kind': 'pdf-page-map', 'source_sha256': source,
                                'source_format': 'djvu', 'pages': [{'page': 1, 'width': 100, 'height': 200}]}
                    with patch.object(djvu_pages, 'sniff', return_value='djvu'), \
                         patch.object(djvu_pages, 'page_map', return_value=geometry):
                        generated = v3.generate_previews(spec, lambda _: self.fail('preview read forbidden'), bundle)
                        self.assertEqual(generated['previews'], [])
                        reading, manifest = v3.build_reading(generated, read, bundle)
                        self.assertEqual(v3.verify_reading(reading, read), manifest)
                    self.assertEqual(manifest['preview'], {'page_count': 1, 'complete': False,
                        'ready_pages': 0, 'partitions': [], 'status': 'skipped'})
                    self.assertTrue(v3.reading_surface_ready(manifest))
                    self.assertFalse(list(bundle.rglob('*.png')))
                    self.assertFalse(list(bundle.rglob('*.webp')))
                    self.assertFalse(list(bundle.rglob('*.jxl')))

    def test_stale_page_stream_is_not_readvertised_as_primary(self):
        image = {'status': 'ready', 'reader_mode': 'image', 'path': 'images/scan.png'}
        old = {'status': 'rendered', 'source_format': 'djvu', 'render_manifest': {'path': 'x'},
               'page_manifest': {'path': 'objects/old/page-manifest.json'}}
        index = build_reader_assets_index.build_index({'files': {'image': image}},
                 ocr_manifest={'files': {'book': old}})
        self.assertNotIn('book', index['f'])
        self.assertEqual(index['f']['image']['p'], 'images/scan.png')
        files = {'image': {'s': 2, 'm': 'i', 'p': 'image-page-manifest.json'}}
        build_reader_assets_v2_sidecar.add_pdf_streams(files, {'files': {'book': old}}, shared.PDF_PAGES_BUCKET)
        self.assertEqual(set(files), {'image'})

    def test_no_document_page_producer_entrypoint_remains(self):
        root = Path(__file__).resolve().parents[1] / 'scripts'
        self.assertFalse((root / 'pdf_assets.py').exists())
        self.assertFalse((root / 'migrate_pdf_page_manifests.py').exists())
        for function in ('render', 'render_page'):
            self.assertFalse(hasattr(djvu_pages, function))
        for function in ('encode_png', 'encode_image', '_encode'):
            self.assertFalse(hasattr(reader_page_codec, function))


if __name__ == '__main__':
    unittest.main()
