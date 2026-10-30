import copy
import json
import unittest

from scripts import chm_epub_publication_safety as safety, publish_reader_v3 as publication


class ChmPublicationSafetyTests(unittest.TestCase):
    def setUp(self):
        catalog = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
                   'parent_generation': 'a' * 64, 'document_reset': 'document-rebuild-20261025'}
        catalog['generation'] = publication.text.digest(catalog)
        raw = publication.encode(catalog)
        ref = publication.metadata(publication.ASSETS,
              f"reader-index/v3/generations/{catalog['generation']}/catalog.json", raw)
        pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': catalog['generation'],
                   'catalog': ref, 'resources': [ref]}
        receipt = {'kind': 'reader-document-reset', 'status': 'withdrawn', 'generation': catalog['generation'],
                   'reset_id': catalog['document_reset'], 'base_snapshot': None}
        self.plan = {'new_v3_generation': catalog['generation'],
                     'overrides': {'reader-index/v3/current.json': publication.encode(pointer),
                                   'reader-index/gc/document-reset/withdrawal.json': publication.encode(receipt)},
                     'immutable': {ref['path']: raw}}
        self.reports = {surface: {'version': 1, 'surface': surface, 'active': True,
                        'generation': catalog['generation'], 'catalog_sha256': ref['sha256'],
                        'projection_verified': True} for surface in ('hf', 'pages')}

    def test_both_reader_surfaces_must_accept_exact_catalog(self):
        self.assertEqual(safety.require_projection_acceptance(self.plan, self.reports), {'hf': True, 'pages': True})
        bad = copy.deepcopy(self.reports)
        bad['pages']['catalog_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'has not accepted'):
            safety.require_projection_acceptance(self.plan, bad)

    def test_document_reset_identity_is_checked_but_does_not_authorize_gc(self):
        result = safety.validate_document_reset_identity(self.plan, self.reports)
        self.assertTrue(result['identity_compatible'])
        self.assertFalse(result['document_collection_authorized'])
        damaged = copy.deepcopy(self.plan)
        receipt = json.loads(damaged['overrides']['reader-index/gc/document-reset/withdrawal.json'])
        receipt['reset_id'] = 'different-reset'
        damaged['overrides']['reader-index/gc/document-reset/withdrawal.json'] = publication.encode(receipt)
        with self.assertRaisesRegex(ValueError, 'identity disagree'):
            safety.validate_document_reset_identity(damaged, self.reports)

    def test_control_transition_is_exact_and_idempotent(self):
        self.assertEqual(safety.enforce_transition('path', b'old', b'old', b'new'), 'ready')
        self.assertEqual(safety.enforce_transition('path', b'new', b'old', b'new'), 'already-applied')
        self.assertEqual(safety.enforce_transition('new', None, None, b'new'), 'ready')
        with self.assertRaisesRegex(ValueError, 'outside the exact plan'):
            safety.enforce_transition('path', b'foreign', b'old', b'new')
        with self.assertRaisesRegex(ValueError, 'unexpectedly exists'):
            safety.enforce_transition('new', b'foreign', None, b'new')

    def test_compare_before_write_rejects_stale_s3_content(self):
        objects = {'control': b'old'}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self, bucket, path):
                if path not in objects:raise FileNotFoundError(path)
                return objects[path]
        expected = safety.chm.digest(b'old')
        self.assertTrue(safety.compare_before_write(Store(), 'control', expected, b'new'))
        objects['control'] = b'new'
        self.assertFalse(safety.compare_before_write(Store(), 'control', expected, b'new'))
        objects['control'] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'changed after planning'):
            safety.compare_before_write(Store(), 'control', expected, b'new')

    def test_book_originals_and_chapter_resource_inventories_cannot_change(self):
        root = 'chapters/ebook/chm/' + 'a' * 64 + '/' + 'b' * 16
        before = {('dataset', 'source.chm'): ('sha', 7),
                  (publication.ASSETS, root + '/chapter-manifest.json'): ('manifest-sha', 5)}
        self.assertEqual(safety.verify_originals_and_chapter_retention(before, dict(before), {root})
                         ['unrelated_objects_verified'], 2)
        after = dict(before)
        after[('dataset', 'source.chm')] = ('changed', 7)
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            safety.verify_originals_and_chapter_retention(before, after, {root})

    def test_hypothetical_graph_and_foreign_batch_never_authorize_delete(self):
        rows = [{'bucket': publication.ASSETS, 'path': 'native/ebook/chm/reviewed/document.epub'}]
        proof = {'graph_complete': True, 'mode': 'simulated', 'target_count': 1, 'retained_targets': 0,
                 'origins': [], 'ambiguous_paths': [], 'blockers': [], 'root_recheck_complete': True,
                 'reader_acceptance_complete': True}
        with self.assertRaisesRegex(ValueError, 'actual CHM graph'):
            safety.require_actual_zero_graph(proof, rows)
        proof['mode'] = 'actual'
        safety.require_actual_zero_graph(proof, rows)
        self.assertTrue(safety.validate_delete_batch(rows, [(publication.ASSETS, rows[0]['path'])]))
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            safety.validate_delete_batch(rows, [(publication.ASSETS, 'source.chm')])

    def test_exact_chm_recovery_requires_a_durable_submitted_batch(self):
        data = b'epub'
        row = {'bucket': publication.ASSETS, 'path': 'native/ebook/chm/x/document.epub',
               'bytes': len(data), 'sha256': safety.chm.digest(data)}
        objects = {row['path']: data}
        class Store:
            def assert_chm_bucket_available(self):return True
            def read_bytes(self, bucket, path):
                if path not in objects:
                    raise FileNotFoundError(path)
                return objects[path]
        self.assertEqual(safety.verify_exact_epub_targets(Store(), [row])['verified_objects'], 1)
        objects.pop(row['path'])
        with self.assertRaisesRegex(ValueError, 'unsubmitted'):
            safety.reconcile_epub_deletions(Store(), [row], [])
        receipt = [{'bucket': row['bucket'], 'paths': [row['path']], 'status': 'submitted'}]
        self.assertEqual(safety.reconcile_epub_deletions(Store(), [row], receipt)['confirmed_absent_objects'], 1)
        with self.assertRaisesRegex(RuntimeError, 'availability'):
            safety.reconcile_epub_deletions(object(), [row], receipt)


if __name__ == '__main__':
    unittest.main()
