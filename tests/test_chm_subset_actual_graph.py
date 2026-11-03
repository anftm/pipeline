import copy
import hashlib
import unittest

from scripts import chm_subset_actual_graph as actual, reader_gc_graph as graph
from tests.test_reader_gc_graph import MemoryStore
from tests import test_chm_subset_isolated_root as isolated_tests

A = graph.READER_ASSETS_BUCKET


class ActualSubsetGraphTests(unittest.TestCase):
    def setUp(self):
        def row(n, raw):
            return {'bucket': A, 'path': f'native/ebook/chm/{n:064x}/document.epub',
                    'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        self.rows = [row(1, b'old'), row(2, b'candidate'), row(3, b'withheld')]
        self.old = [{'bucket': A, 'status': 'verified-absent', 'paths': [self.rows[0]['path']]}]
        self.selected, self.withheld = self.rows[1:2], self.rows[2:]
        self.identity = {'old_plan_sha256': 'a' * 64,
                         'inventory_sha256': actual.subset.chm.preview.INVENTORY_SHA256,
                         'old_progress_sha256': 'b' * 64, 'old_receipts_sha256': actual.journal.digest(self.old),
                         'classification_sha256': 'd' * 64, 'maintenance_id': 'e' * 32,
                         'transaction_id': 'f' * 32}
        self.root = actual.isolated.build(self.withheld, self.identity)
        self.store = MemoryStore()
        self.store.put(A, self.selected[0]['path'], b'candidate')
        self.store.put(A, self.withheld[0]['path'], b'withheld')
        self.store.put(A, self.root['path'], self.root['raw'])

    def build(self):
        traversal = graph.ReferenceGraph(self.store, workers=1)
        report = traversal.build()
        return traversal, report

    def capture(self, traversal, report, **kwargs):
        return actual.capture(traversal, report, self.rows, self.old, self.selected, self.withheld,
                              root_raw=self.root['raw'], identity=self.identity, **kwargs)

    def test_actual_capture_never_grants_authorization(self):
        traversal, report = self.build()
        result = self.capture(traversal, report)
        self.assertEqual(result['retained_targets'], 1)
        self.assertFalse(result['deletion_authorized'])
        self.assertFalse(result['root_recheck_complete'])
        with self.assertRaisesRegex(ValueError, 'rechecks are incomplete'):
            actual.compare_original({}, result, self.withheld, self.identity, self.root['raw'])

    def test_candidate_or_old_target_reappearing_blocks(self):
        self.store.put(A, 'reader-index/foreign.json', {'resources': [{'bucket': A, 'path': self.selected[0]['path']}]})
        traversal, report = self.build()
        with self.assertRaisesRegex(ValueError, 'exactly the withheld'):
            self.capture(traversal, report)
        self.store.objects[A].pop('reader-index/foreign.json')
        self.store.put(A, self.rows[0]['path'], b'old')
        traversal, report = self.build()
        with self.assertRaisesRegex(ValueError, 'original or subset receipts'):
            self.capture(traversal, report)

    def test_root_bytes_and_hash_must_match(self):
        traversal, report = self.build()
        with self.assertRaisesRegex(ValueError, 'root bytes differ'):
            actual.capture(traversal, report, self.rows, self.old, self.selected, self.withheld,
                           root_raw=self.root['raw'] + b' ', identity=self.identity)
        traversal.loaded_digests[(A, self.root['path'])] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'root or closure differs'):
            self.capture(traversal, report)

    def test_verified_subset_receipt_is_required_for_missing_selected_target(self):
        self.store.objects[A].pop(self.selected[0]['path'])
        traversal, report = self.build()
        with self.assertRaisesRegex(ValueError, 'original or subset receipts'):
            self.capture(traversal, report)
        receipt = {'bucket': A, 'status': 'verified-absent', 'paths': [self.selected[0]['path']]}
        proof = self.capture(traversal, report, subset_receipts=[receipt])
        self.assertEqual(proof['subset_receipt_objects'], 1)
        self.assertFalse(proof['deletion_authorized'])

    def test_foreign_or_out_of_order_subset_receipt_is_rejected(self):
        traversal, report = self.build()
        receipt = {'bucket': A, 'status': 'verified-absent', 'paths': [self.withheld[0]['path']]}
        with self.assertRaises(ValueError):
            self.capture(traversal, report, subset_receipts=[receipt])

    def test_stage_comparison_accepts_exact_closure_after_verified_subset_receipt(self):
        traversal, report = self.build()
        before = self.capture(traversal, report, root_rechecked=True, consumers_checked=True)
        self.store.objects[A].pop(self.selected[0]['path'])
        traversal, report = self.build()
        receipt = {'bucket': A, 'status': 'verified-absent', 'paths': [self.selected[0]['path']]}
        after = self.capture(traversal, report, subset_receipts=[receipt], root_rechecked=True,
                             consumers_checked=True)
        args = (self.rows, self.old, self.selected, self.withheld, self.identity, self.root['raw'])
        result = actual.compare_stage(before, after, *args)
        self.assertEqual((result['previous_receipts'], result['current_receipts']), (0, 1))
        self.assertFalse(result['deletion_authorized'])
        with self.assertRaisesRegex(ValueError, 'regressed'):
            actual.compare_stage(after, before, *args)

    def test_stage_comparison_rejects_closure_or_protection_change(self):
        traversal, report = self.build()
        before = self.capture(traversal, report, root_rechecked=True, consumers_checked=True)
        for field in ('root_sha256', 'closure_sha256'):
            changed = copy.deepcopy(before)
            changed[field][0]['sha256'] = '0' * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                actual.compare_stage(before, changed, self.rows, self.old, self.selected,
                                     self.withheld, self.identity, self.root['raw'])
        with self.assertRaisesRegex(ValueError, 'root bytes differ'):
            actual.compare_stage(before, before, self.rows, self.old, self.selected,
                                 self.withheld, self.identity, self.root['raw'] + b' ')

    def test_original_receipts_must_match_root_identity(self):
        traversal, report = self.build()
        bad = {**self.identity, 'old_receipts_sha256': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'original receipt identity'):
            actual.capture(traversal, report, self.rows, self.old, self.selected, self.withheld,
                           root_raw=self.root['raw'], identity=bad)

    def test_receipt_identity_and_exact_selected_order_are_checked(self):
        self.store.objects[A].pop(self.selected[0]['path'])
        traversal, report = self.build()
        receipt = {'bucket': A, 'status': 'verified-absent', 'paths': [self.selected[0]['path']]}
        proof = self.capture(traversal, report, subset_receipts=[receipt], root_rechecked=True,
                             consumers_checked=True)
        args = (self.rows, self.old, self.selected, self.withheld, self.identity, self.root['raw'])
        for field, value in (('subset_receipt_objects', 0), ('subset_receipts_sha256', '0' * 64)):
            changed = copy.deepcopy(proof)
            changed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'receipt history'):
                actual.compare_stage(proof, changed, *args)
        changed = copy.deepcopy(proof)
        changed['subset_receipts'][0]['paths'] = [self.withheld[0]['path']]
        changed['subset_receipts_sha256'] = actual.journal.digest(changed['subset_receipts'])
        with self.assertRaises(ValueError):
            actual.compare_stage(proof, changed, *args)

    def test_stage_rejects_rebatching_old_receipts_even_with_same_targets(self):
        raw = b'candidate2'
        second = {'bucket': A, 'path': f'native/ebook/chm/{4:064x}/document.epub',
                  'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        self.rows.insert(2, second)
        self.selected.append(second)
        self.store.put(A, second['path'], raw)
        self.store.objects[A].pop(self.selected[0]['path'])
        traversal, report = self.build()
        first = {'bucket': A, 'status': 'verified-absent', 'paths': [self.selected[0]['path']]}
        before = self.capture(traversal, report, subset_receipts=[first], root_rechecked=True,
                              consumers_checked=True)
        self.store.objects[A].pop(second['path'])
        traversal, report = self.build()
        rebatched = {'bucket': A, 'status': 'verified-absent',
                     'paths': [self.selected[0]['path'], second['path']]}
        after = self.capture(traversal, report, subset_receipts=[rebatched], root_rechecked=True,
                             consumers_checked=True)
        args = (self.rows, self.old, self.selected, self.withheld, self.identity, self.root['raw'])
        with self.assertRaisesRegex(ValueError, 'receipt history regressed or was rewritten'):
            actual.compare_stage(before, after, *args)

    def test_receipt_snapshot_does_not_alias_caller_data(self):
        self.store.objects[A].pop(self.selected[0]['path'])
        traversal, report = self.build()
        receipt = {'bucket': A, 'status': 'verified-absent', 'paths': [self.selected[0]['path']]}
        proof = self.capture(traversal, report, subset_receipts=[receipt])
        receipt['paths'].clear()
        self.assertEqual(proof['subset_receipts'][0]['paths'], [self.selected[0]['path']])

    def test_independent_root_comparison_rejects_foreign_graph_change(self):
        fixture = isolated_tests.IsolatedRootTests()
        fixture.setUp()
        before, after = fixture.proofs()
        proof = {**after, 'kind': 'actual-chm-subset-graph-observation', 'mode': 'actual'}
        result = actual.compare_original(before, proof, fixture.withheld, fixture.identity, fixture.root['raw'])
        self.assertEqual(result['mode'], 'actual-graph-comparison-only')
        self.assertFalse(result['deletion_authorized'])
        changed = copy.deepcopy(proof)
        changed['root_sha256'][0]['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            actual.compare_original(before, changed, fixture.withheld, fixture.identity, fixture.root['raw'])


if __name__ == '__main__':
    unittest.main()
