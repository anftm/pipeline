import copy
import gzip
import unittest
from unittest.mock import Mock

from scripts import reader_gc_documents as gc, reader_document_reset as reset


class Store:
    def __init__(self, paths):
        self.paths = set(paths)
        self.object_sizes = {(reset.INPUTS, path): 17 for path in paths}
        self.deleted = []

    def delete(self, bucket, paths):
        self.deleted.extend(paths)
        self.paths.difference_update(paths)

    def list_files(self, bucket, prefixes):
        return {p for p in self.paths if p.startswith(prefixes)}


class DocumentCollectionTests(unittest.TestCase):
    def test_preflight_accepts_independent_updates_with_withdrawn_pinned_consumers(self):
        from tests.test_reader_document_reset import DocumentResetTests
        store, _ = DocumentResetTests().fixture()
        receipt = reset.withdraw(store, {'reset_id': 'documents-20261025'}, apply=True)
        baseline = reset.decode(store.read_bytes(reset.ASSETS, reset.SIDECAR))
        updated = copy.deepcopy(baseline)
        updated['f']['repo\0book.epub']['p'] = 'chapters/ebook/epub/new/chapter-manifest.json'
        updated['f'].pop('repo\0failed.pdf')
        store.put_bytes(reset.ASSETS, reset.SIDECAR, gzip.compress(reset.encode(updated), mtime=0))
        store.put_bytes(gc.CCRD, 'reports/ccrd-ocr-reset/documents-20261025/withdrawal.json',
                        reset.encode({'status': 'withdrawn'}))
        store.list_files = lambda bucket, prefixes: {p for b, p in store.objects if b == bucket and p.startswith(prefixes)}
        store.put_bytes(gc.CCRD, 'current.json', reset.encode({'version': 1, 'kind': 'ccrd-ocr-retention', 'resources': []}))
        # A known legacy pointer is required; its references are outside the reset.
        from tests.test_reader_gc_ccrd import build_store
        ccrd, _ = build_store()
        pointer = reset.decode(ccrd.objects['current.json'])
        for value in pointer['databases'].values():
            value['path'] = value['path'].replace('legacy-a', 'ccrd-active')
        store.put_bytes(gc.CCRD, 'current.json', reset.encode(pointer))
        client = Mock()
        def get(url, **kwargs):
            response = Mock()
            if url.endswith('/api/index-status'):
                response.json.return_value = {'ready': True, 'ocr_release': None,
                                               'loaded_sources': ['CCRD', 'CW', 'PLA', 'RMRB']}
            else:
                response.content = reset.encode(baseline)
            return response
        client.get.side_effect = get
        result, pins, _, _ = gc.preflight(store, receipt['reset_id'], client)
        self.assertEqual(result, receipt)
        self.assertEqual(reset.decode(pins[reset.ASSETS, reset.SIDECAR]), updated)
        updated['f']['repo\0scan.pdf'] = {'b': reset.PAGES, 'p': 'objects/old/page-manifest.json'}
        store.put_bytes(reset.ASSETS, reset.SIDECAR, reset.encode(updated))
        with self.assertRaisesRegex(ValueError, 'republished'):
            gc.preflight(store, receipt['reset_id'], client)

    def test_withdrawn_sidecar_allows_independent_formats_but_rejects_document_references(self):
        derived = {'files': [{'source_extension': 'caj', 'new_path': 'derived/caj/keep/document.pdf'}]}
        sidecar = {'v': 1, 'f': {'book.epub': {'p': 'chapters/ebook/epub/new/chapter-manifest.json'},
                                'image.tif': {'p': 'pages/image/tif/new/page-manifest.json'},
                                'book.caj': {'b': reset.PAGES, 'p': 'derived/caj/keep/document.pdf'}}}
        gc.withdrawn_sidecar(sidecar, derived)
        for field, bucket, path in [('p', reset.PAGES, 'objects/old/page-manifest.json'),
                                    ('o', reset.PAGES, 'objects/old/ocr-manifest.json'),
                                    ('pd', reset.PAGES, 'derived/djvu/old/document.pdf'),
                                    ('f', reset.INPUTS, 'objects/old/ocr-input/page-000001.png')]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'republished'):
                gc.withdrawn_sidecar({'v': 1, 'f': {'book': {'b': bucket, field: path}}}, derived)

    def test_exact_scope_does_not_select_image_sources_or_original_pdf(self):
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        self.assertTrue(gc.classify('ocr-inputs', reset.INPUTS, root + '/ocr-input/page-000001.png', {}))
        self.assertFalse(gc.classify('ocr-inputs', reset.PAGES, root + '/ocr-input/page-000001.png', {}))
        self.assertFalse(gc.classify('document-pages', reset.PAGES, root + '/document.pdf', {}))
        self.assertFalse(gc.classify('document-pages', reset.ASSETS, 'pages/image/tif/a/pages/page-000001.webp', {}))
        self.assertTrue(gc.classify('ocr-library', gc.CCRD, 'text/objects/aa/' + 'a' * 64 + '.txt', {}))
        self.assertFalse(gc.classify('ocr-library', gc.CCRD, 'indexes/newspapers/RMRB/a.sqlite3', {}))

    def test_processing_root_with_foreign_resource_is_not_retired(self):
        value = {'kind': 'reader-processing-roots', 'resources': [
            {'bucket': reset.PAGES, 'root': 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16}]}
        self.assertTrue(gc.processing_record_target(value))
        value['resources'].append({'bucket': reset.ASSETS, 'root': 'pages/image/tif/a'})
        self.assertFalse(gc.processing_record_target(value))

    def test_batches_require_barrier_and_verify_both_deletion_and_retention(self):
        paths = {'objects/' + str(i) for i in range(1100)}
        store = Store(paths)
        retained = {'objects/original'}
        store.paths.update(retained)
        checkpoints, barriers = [], []
        result = gc.collect(store, reset.INPUTS, ('objects/',), paths | retained, paths,
            workers=2, journal={'accepted_objects': 0}, barrier=lambda: barriers.append(True),
            checkpoint=lambda value: checkpoints.append(copy.deepcopy(value)))
        self.assertEqual(store.paths, retained)
        self.assertEqual(result['confirmed_deleted_objects'], 1100)
        self.assertEqual(result['confirmed_deleted_bytes'], 1100 * 17)
        self.assertEqual(len(result['accepted_batches']), 3)
        self.assertGreaterEqual(len(barriers), 2)
        self.assertTrue(checkpoints[-1]['complete'])

    def test_failed_barrier_prevents_first_delete(self):
        store = Store({'objects/one'})
        def fail():
            raise RuntimeError('writer resumed')
        with self.assertRaisesRegex(RuntimeError, 'writer resumed'):
            gc.collect(store, reset.INPUTS, ('objects/',), store.paths.copy(), store.paths.copy(),
                workers=1, journal={}, barrier=fail, checkpoint=lambda value: None)
        self.assertEqual(store.deleted, [])

    def test_resumed_plan_keeps_original_total_and_skips_accepted_batches(self):
        paths = {'objects/' + str(i) for i in range(501)}
        ordered = sorted(paths)
        first = ordered[:500]
        identity = reset.digest(reset.encode([reset.INPUTS, first]))
        store = Store(paths)
        store.paths.difference_update(first)
        result = gc.collect(store, reset.INPUTS, ('objects/',), paths, paths,
            workers=1, journal={'accepted_objects': 500, 'accepted_batches': [identity]},
            barrier=lambda: None, checkpoint=lambda value: None)
        self.assertEqual(store.deleted, ordered[500:])
        self.assertEqual(result['accepted_objects'], 501)
        self.assertEqual(result['confirmed_deleted_objects'], 501)
