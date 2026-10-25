import copy
import unittest

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
