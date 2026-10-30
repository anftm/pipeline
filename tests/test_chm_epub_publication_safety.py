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


if __name__ == '__main__':
    unittest.main()
