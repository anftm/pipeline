import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from scripts import reader_resource_repair as repair, pdf_reading_v3 as v3
from scripts import pdf_text_layer as text, pdf_ocr, reader_lifecycle
from tests.test_pdf_text_layer import raw_page
from tests.test_reader_gc_collect import Store


class ReaderResourceRepairTests(unittest.TestCase):
    def test_source_artifacts_are_only_completed_central_gc_workflows(self):
        self.assertEqual(repair.report_artifact({'path': '.github/workflows/reader-gc.yml',
            'status': 'completed'}), ('reader-gc-report', 'reader-gc-report.json'))
        self.assertEqual(repair.report_artifact({'path': '.github/workflows/reader-gc-collect.yml',
            'status': 'completed'}), ('reader-gc-collection', 'collection.json'))
        for run in ({'path': '.github/workflows/reader-gc.yml', 'status': 'in_progress'},
                    {'path': '.github/workflows/foreign.yml', 'status': 'completed'}):
            with self.assertRaises(ValueError):
                repair.report_artifact(run)

    def test_image_identity_mismatch_stops_before_output_is_named(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def download(url, target, token):
                Image.new('RGB', (32, 24), 'white').save(target, 'PNG')
            item = {'repo': 'VoiceOfML/test', 'path': 'book.png', 'revision': 'a' * 40,
                    'extension': 'png', 'source_sha256': 'b' * 64}
            with patch.object(repair.images, 'download', side_effect=download):
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    repair.images.build_one(item, root, None, repair.ASSETS)

    def test_missing_image_plan_preserves_all_keys_and_rejects_foreign_sources(self):
        path = 'pages/image/png/' + 'a' * 64 + '/page-manifest.json'
        entry = {'s': 2, 'm': 'i', 'p': path, 'b': repair.ASSETS}
        missing = {(repair.ASSETS, path)}
        sidecar = {'v': 1, 'f': {'VoiceOfML/test\0one.png': entry, 'VoiceOfML/test\0two.png': entry}}
        plan = repair.image_plan(sidecar, missing)
        self.assertEqual(len(plan[(repair.ASSETS, path)]), 2)
        sidecar['f'] = {'foreign/test\0one.png': entry}
        with self.assertRaises(ValueError):
            repair.image_plan(sidecar, missing)

    def legacy_fixture(self, store, bundle):
        source = 'a' * 64
        root = Path('objects/aa/' + source + '/' + 'b' * 16)
        payload = raw_page('Exact text')
        raw = gzip.compress(json.dumps(payload).encode(), mtime=0)
        raw_path = 'objects/aa/' + source + '/' + 'c' * 16 + '/ocr/page-000001.json.gz'
        store.objects[repair.PDF][raw_path] = raw
        raw_ref = {'bucket': repair.PDF, 'path': raw_path, 'bytes': len(raw),
                   'sha256': hashlib.sha256(raw).hexdigest(), 'role': 'provenance'}
        layer = text.from_page(payload, source, 'en', raw_ref, layout_options={})
        page = {'p': 1, 'source': 'ocr', 'o': raw_path, 'ob': len(raw), 'os': raw_ref['sha256']}
        ref = v3.build_text_bundle([layer], [page], root, bundle, include_search=False)
        book_path = (root / 'ocr/book-text.json.gz').as_posix()
        book_raw = gzip.compress(json.dumps({'complete': True, 'text_layer': ref, 'pages': [
            {'page': 1, 'text': layer['text'], 'text_generation': layer['generation']}]}).encode(), mtime=0)
        store.objects[repair.PDF][book_path] = book_raw
        manifest = {'kind': 'pdf-ocr', 'complete': True, 'source_sha256': source,
                    'page_count': 1, 'language': 'en', 'pages': [page], 'text_layer': ref,
                    'book_text': {'path': book_path, 'bytes': len(book_raw),
                                  'sha256': hashlib.sha256(book_raw).hexdigest()}}
        store.put(repair.PDF, (root / 'ocr-manifest.json').as_posix(), manifest)
        return ref, manifest

    def test_text_restoration_matches_legacy_checksum_and_published_generation(self):
        store = Store()
        with tempfile.TemporaryDirectory() as original, tempfile.TemporaryDirectory() as rebuilt:
            ref, _ = self.legacy_fixture(store, Path(original))
            actual, files = repair.restore_text_bundle(store, ref['path'], Path(rebuilt))
            self.assertEqual(actual, ref)
            self.assertGreater(len(files), 3)
            for path, target in files.items():
                self.assertEqual(target.read_bytes(), (Path(original) / path).read_bytes())

    def test_incorrect_companion_checksum_cannot_be_published(self):
        store = Store()
        with tempfile.TemporaryDirectory() as original, tempfile.TemporaryDirectory() as rebuilt:
            ref, manifest = self.legacy_fixture(store, Path(original))
            manifest['text_layer']['sha256'] = 'c' * 64
            root = Path(ref['path']).parent
            store.put(repair.PDF, (root / 'ocr-manifest.json').as_posix(), manifest)
            with self.assertRaises(ValueError):
                repair.restore_text_bundle(store, ref['path'], Path(rebuilt))

    def test_immutable_conflict_stops_publication_and_image_processing_root_is_valid(self):
        store = Store()
        path = 'pages/image/png/' + 'a' * 64 + '/page-manifest.json'
        store.put(repair.ASSETS, path, b'existing')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'manifest.json'
            target.write_bytes(b'different')
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                repair.publish_bundle(store, repair.ASSETS, {path: target}, {'path': path}, apply=False)
        record = reader_lifecycle.processing_record([{'bucket': repair.ASSETS,
            'root': path.rsplit('/', 1)[0]}], 'repair', {})
        reader_lifecycle.validate_processing_handoff(record, {'path': path})
        with self.assertRaises(ValueError):
            reader_lifecycle.processing_record([{'bucket': repair.PDF,
                'root': path.rsplit('/', 1)[0]}], 'repair', {})


if __name__ == '__main__':
    unittest.main()
