import copy
import gzip
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

    def test_projection_acceptance_compares_full_routes_on_both_sites(self):
        from unittest.mock import patch
        base = {'v': 1, 'f': {'repo\0book.chm': {'s': 2, 'm': 'e', 'p': 'chapter'}}}
        raw_base = gzip.compress(publication.encode(base), mtime=0)
        base_ref = publication.metadata(publication.ASSETS,
                    f'reader-index/snapshots/{safety.chm.digest(raw_base)}/reader_assets.json.gz', raw_base)
        catalog = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
                   'base_sidecar': base_ref, 'parent_generation': 'a' * 64}
        catalog['generation'] = publication.text.digest(catalog)
        raw_catalog = publication.encode(catalog)
        ref = publication.metadata(publication.ASSETS,
                    f"reader-index/v3/generations/{catalog['generation']}/catalog.json", raw_catalog)
        pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': catalog['generation'],
                   'catalog': ref, 'resources': [ref]}
        plan = {'new_v3_generation': catalog['generation'],
                'overrides': {'reader-index/v3/current.json': publication.encode(pointer)},
                'immutable': {ref['path']: raw_catalog, base_ref['path']: raw_base}}
        projection = publication.project_sidecar(base, catalog)
        hf = publication.encode(projection)
        pages = gzip.compress(hf, mtime=0)
        receipts = {site: {'version': 1, 'surface': site, 'active': True,
                    'generation': catalog['generation'], 'catalog_sha256': ref['sha256']} for site in ('hf', 'pages')}
        receipts['pages'].update(reader_contract='pdf-reading-v3-v1', sidecar_sha256=safety.chm.digest(pages))
        class Response:
            def __init__(self, content):self.content = content
            def raise_for_status(self):pass
            def json(self):return json.loads(self.content)
        class Client:
            def get(self, url, **kwargs):
                if url.endswith('reader-v3-status'):return Response(publication.encode(receipts['hf']))
                if url.endswith('reader_v3_receipt.json'):return Response(publication.encode(receipts['pages']))
                return Response(pages if url.endswith('.gz') else hf)
        with patch.object(publication, 'current', return_value=(pointer, catalog)):
            accepted = safety.observe_planned_projection(None, Client(), plan)
            self.assertTrue(accepted['pages']['projection_verified'])
            receipts['hf']['generation'] = 'f' * 64
            with self.assertRaisesRegex(ValueError, 'has not accepted'):
                safety.observe_planned_projection(None, Client(), plan)

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

    def test_document_reset_prerequisite_rejects_djvu_republication(self):
        from unittest.mock import patch
        from scripts import reader_document_reset as reset
        sidecar = {'v': 1, 'f': {}}
        plan = copy.deepcopy(self.plan)
        plan['overrides'][reset.SIDECAR] = gzip.compress(publication.encode(sidecar), mtime=0)
        class Store:
            def assert_buckets_available(self):pass
            def read_bytes(self, bucket, path):
                if path == 'control/ocr-current.json':raise FileNotFoundError(path)
                if path.endswith('/withdrawal.json'):return publication.encode({'status': 'withdrawn'})
                return publication.encode({'files': []})
        with patch.object(reset, 'REGISTRIES', []), patch.object(reset, 'is_djvu', return_value=False):
            result = safety.verify_document_reset_prerequisites(Store(), plan)
        self.assertFalse(result['document_collection_authorized'])
        self.assertEqual(result['remote_mutations'], 0)
        class DjvuStore:
            def read_bytes(self, bucket, path):return publication.encode({'files': [{'key': 'book.djvu'}]})
        with patch.object(reset, 'REGISTRIES', []), patch.object(reset, 'is_djvu', return_value=True):
            with self.assertRaisesRegex(ValueError, 'DjVu derived registry'):
                safety.verify_document_reset_prerequisites(DjvuStore(), plan)

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

    def test_staged_plan_rejects_any_foreign_control(self):
        from unittest.mock import patch
        plan = copy.deepcopy(self.plan)
        plan['kind'] = 'chm-epub-withdrawal-plan'
        plan['overrides'].update({p: b'{}' for p in [safety.chm.SIDECAR, safety.chm.CATALOG,
            'reader-index/v3/retirement.json', 'reader-index/reader_gc_consumers.json',
            'reader-index/chm-epub-withdrawal-retention.json', safety.chm.INDEX,
            *(f'{safety.chm.INDEX_PREFIX}index-0{i}.json' for i in range(8)),
            f"reader-index/v3/promotions/{plan['new_v3_generation']}.json"]})
        with patch.object(safety, 'require_exact_epub_inventory'):
            self.assertTrue(safety.validate_staged_plan(plan, []))
            plan['overrides']['documents/pdf/ps/index.json'] = b'{}'
            with self.assertRaisesRegex(ValueError, 'unexpected control'):
                safety.validate_staged_plan(plan, [])

    def test_preserved_catalog_alias_checks_original_overrides_and_epubs(self):
        old = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'parent_generation': 'd' * 64}
        old['generation'] = publication.text.digest(old)
        base = {'v': 1, 'f': {'book.chm': {'p': 'chapter'}}}
        packed = gzip.compress(publication.encode(base), mtime=0)
        base_ref = publication.metadata(publication.ASSETS,
                   f'reader-index/snapshots/{safety.chm.digest(packed)}/reader_assets.json.gz', packed)
        new = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'base_sidecar': base_ref,
               'parent_generation': 'd' * 64, 'rollback_target': old['generation']}
        new['generation'] = publication.text.digest(new)
        raw = publication.encode(new)
        alias = publication.metadata(publication.ASSETS,
                    f"reader-index/v3/generations/{new['generation']}/catalog.json", raw)
        plan = {'rollback_aliases': {old['generation']: alias},
                'overrides': {'reader-index/v3/retirement.json': publication.encode({'resources': [alias]})},
                'immutable': {alias['path']: raw, base_ref['path']: packed}}
        rows = [{'path': 'native/ebook/chm/target/document.epub'}]
        self.assertTrue(safety.validate_preserved_catalog_roots(plan, {old['generation']: old}, rows))
        plan['immutable'][alias['path']] = b'changed'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            safety.validate_preserved_catalog_roots(plan, {old['generation']: old}, rows)

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

    def test_retention_root_must_pin_every_historical_chapter(self):
        path = 'chapters/ebook/chm/' + 'a' * 64 + '/' + 'b' * 16 + '/chapter-manifest.json'
        retention = {'kind': 'reader-chm-epub-withdrawal-retention', 'inventory_sha256': 'abc',
                     'resources': [{'bucket': publication.ASSETS, 'path': path}]}
        plan = {'inventory_sha256': 'abc', 'chapter_manifests_to_preserve': [path],
                'overrides': {'reader-index/chm-epub-withdrawal-retention.json': publication.encode(retention)}}
        self.assertEqual(safety.verify_prepared_chapter_retention(plan)['protected_chapter_manifests'], 1)
        retention['resources'] = []
        plan['overrides']['reader-index/chm-epub-withdrawal-retention.json'] = publication.encode(retention)
        with self.assertRaisesRegex(ValueError, 'omits'):
            safety.verify_prepared_chapter_retention(plan)

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

    def test_actual_graph_proof_requires_fresh_root_recheck(self):
        from unittest.mock import patch
        rows = [{'bucket': publication.ASSETS, 'path': 'native/ebook/chm/reviewed/document.epub'}]
        class Store:
            def assert_buckets_available(self):pass
        class Fence:
            def check(self):pass
        class FakeGraph:
            def __init__(self, store, **kwargs):
                self.references = set()
                self.direct = {}
                from scripts import reader_gc_graph
                self.files = {bucket: set() for bucket in reader_gc_graph.BUCKETS}
                self.root_hashes = {}
                self.retired_roots = set()
            def build(self):return {'graph_complete': True, 'blockers': [], 'ambiguous_paths': [], 'roots': []}
        with patch('scripts.reader_gc_graph.ReferenceGraph', FakeGraph), \
                patch('scripts.reader_gc_collect.verify_roots') as roots, \
                patch.object(safety, 'observe_planned_projection', return_value=self.reports) as observed:
            proof, _ = safety.actual_graph_evidence(Store(), Fence(), None, rows, self.plan)
        self.assertEqual(proof['mode'], 'actual')
        self.assertEqual(proof['retained_targets'], 0)
        self.assertEqual(roots.call_count, 1)
        self.assertEqual(observed.call_count, 2)
        safety.require_actual_zero_graph(proof, rows)

    def test_predelete_barrier_rejects_changed_root(self):
        from unittest.mock import patch
        rows = [{'bucket': publication.ASSETS, 'path': 'native/ebook/chm/reviewed/document.epub'}]
        proof = {'graph_complete': True, 'mode': 'actual', 'target_count': 1, 'retained_targets': 0,
                 'origins': [], 'root_recheck_complete': True, 'reader_acceptance_complete': True,
                 'root_sha256': [{'bucket': publication.ASSETS, 'path': 'reader-index/root.json',
                                  'sha256': 'a' * 64}],
                 'root_members': [{'bucket': publication.ASSETS, 'path': 'reader-index/root.json'}],
                 'retired_roots': []}
        class Store:
            def assert_buckets_available(self):pass
            def digest(self, bucket, path):return 'b' * 64
        class Fence:
            def check(self):pass
        with patch('scripts.reader_gc_collect.verify_roots'), patch.object(safety, 'observe_planned_projection'):
            with self.assertRaisesRegex(ValueError, 'root changed'):
                safety.recheck_actual_graph(Store(), None, proof, rows, Fence(), None, self.plan)

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
