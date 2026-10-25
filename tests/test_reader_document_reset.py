import gzip
import json
import unittest

from scripts import reader_document_reset as reset, reader_lifecycle as lifecycle
from scripts import publish_reader_v3 as publication
from tests.test_publish_reader_v3 import Store


class DocumentResetTests(unittest.TestCase):
    def fixture(self):
        store = Store()
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        sidecar = {'v': 1, 'f': {
            'repo\0scan.pdf': {'s': 2, 'm': 'p', 'b': reset.PAGES, 'p': root + '/page-manifest.json'},
            'repo\0scan.djvu': {'s': 2, 'm': 'p', 'b': reset.PAGES, 'p': 'derived/old/djvu/document.pdf'},
            'repo\0article.caj': {'s': 2, 'm': 'p', 'b': reset.PAGES, 'p': root + '/page-manifest.json'},
            'repo\0image.tif': {'s': 2, 'm': 'i', 'b': reset.ASSETS, 'p': 'pages/image/tif/keep/page-manifest.json'},
            'repo\0image.png': {'s': 2, 'm': 'i', 'b': reset.ASSETS, 'p': 'pages/image/png/keep/document.webp'},
            'repo\0book.epub': {'s': 2, 'm': 'e', 'b': reset.ASSETS, 'p': 'chapters/ebook/epub/keep/chapter-manifest.json'},
        }}
        derived = {'version': 1, 'kind': 'pdf-derived-source-index', 'files': [
            {'key': 'repo\0scan.djvu', 'source_extension': 'djvu', 'new_path': 'derived/old/djvu/document.pdf'},
            {'key': 'repo\0article.caj', 'source_extension': 'caj', 'new_path': 'derived/old/caj/document.pdf'}]}
        store.objects[reset.ASSETS, reset.SIDECAR] = gzip.compress(reset.encode(sidecar), mtime=0)
        store.objects[reset.PAGES, reset.DERIVED] = reset.encode(derived)
        catalog, _ = lifecycle.catalog_generation(sidecar)
        store.objects[reset.ASSETS, 'reader-index/reader_catalog.json'] = reset.encode(catalog)
        publication.unify(store, apply=True)
        store.writes.clear()
        return store, sidecar

    def test_withdrawal_preserves_images_tiff_ebooks_and_non_djvu_documents(self):
        store, before = self.fixture()
        dry = reset.withdraw(store, {'reset_id': 'documents-20261025'})
        self.assertEqual(store.writes, [])
        self.assertEqual(len(dry['djvu_derivatives']), 1)
        report = reset.withdraw(store, {'reset_id': 'documents-20261025'}, apply=True)
        after = reset.decode(store.read_bytes(reset.ASSETS, reset.SIDECAR))
        for key in ('repo\0image.tif', 'repo\0image.png', 'repo\0book.epub'):
            self.assertEqual(after['f'][key], before['f'][key])
        self.assertNotIn('repo\0scan.pdf', after['f'])
        self.assertNotIn('repo\0scan.djvu', after['f'])
        self.assertEqual(after['f']['repo\0article.caj']['p'], 'derived/old/caj/document.pdf')
        pointer, catalog = publication.current(store)
        self.assertEqual(catalog['files'], {})
        self.assertEqual(publication.pinned_base(store, catalog, {}), after)
        self.assertEqual(report['generation'], pointer['generation'])
        self.assertEqual(reset.withdraw(store, {'reset_id': 'documents-20261025'}, apply=True), report)

    def test_interrupted_withdrawal_resumes_saved_plan(self):
        store, before = self.fixture()
        store.fail_pointer = True
        with self.assertRaises(OSError):
            reset.withdraw(store, {'reset_id': 'documents-20261025'}, apply=True)
        store.fail_pointer = False
        report = reset.withdraw(store, {'reset_id': 'documents-20261025'}, apply=True)
        self.assertEqual(report['status'], 'withdrawn')
        self.assertEqual(publication.current(store)[1]['files'], {})

    def test_delete_classification_excludes_originals_and_independent_images(self):
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        self.assertTrue(reset.PAGE_OUTPUT.fullmatch(root + '/preview/page-000001.webp'))
        self.assertTrue(reset.INPUT.fullmatch(root + '/ocr-input/page-000001.png'))
        for path in (root + '/document.pdf', root + '/document.djvu',
                     'pages/image/tif/keep/page-manifest.json', 'pages/image/png/keep/document.webp'):
            self.assertFalse(reset.PAGE_OUTPUT.fullmatch(path))
