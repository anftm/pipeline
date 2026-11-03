import hashlib
import unittest

from scripts import chm_subset_protection_overlay as overlay, reader_gc_graph as graph
from tests.test_reader_gc_graph import MemoryStore

A = graph.READER_ASSETS_BUCKET


class Store(MemoryStore):
    def list_roots(self, bucket, parents):
        return {path: None for path in self.objects[bucket] if graph.is_root(path, bucket)}


class IndependentProtectionOverlayTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        def row(number, raw):
            return {'bucket': A, 'path': f'native/ebook/chm/{number:064x}/document.epub',
                    'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        self.old, self.selected, self.withheld = row(1, b'old'), row(2, b'candidate'), row(3, b'withheld')
        self.store.put(A, self.selected['path'], b'candidate')
        self.store.put(A, self.withheld['path'], b'withheld')
        self.store.put(A, 'reader-index/original.json', {'version': 1, 'resources': []})
        self.identity = {'old_plan_sha256': 'a' * 64, 'inventory_sha256': overlay.isolated.subset.chm.preview.INVENTORY_SHA256,
                         'old_progress_sha256': 'b' * 64, 'old_receipts_sha256': 'c' * 64,
                         'classification_sha256': 'd' * 64, 'maintenance_id': 'e' * 32, 'transaction_id': 'f' * 32}

    def run_graph(self):
        view, candidate = overlay.view(self.store, [self.withheld], self.identity)
        traversal = graph.ReferenceGraph(view, workers=1)
        report = traversal.build()
        return traversal, report, candidate

    def capture(self, traversal, report, candidate):
        return overlay.capture(traversal, report, [self.old, self.selected, self.withheld], [self.selected],
                               [self.withheld], candidate=candidate, expected_absent={(A, self.old['path'])})

    def test_only_withheld_is_hypothetically_protected(self):
        before = {bucket: dict(values) for bucket, values in self.store.objects.items()}
        traversal, report, candidate = self.run_graph()
        proof = self.capture(traversal, report, candidate)
        self.assertEqual(proof['retained_target_paths'], [{'bucket': A, 'path': self.withheld['path']}])
        self.assertFalse(proof['deletion_authorized'])
        self.assertEqual(before, self.store.objects)

    def test_candidate_reference_blocks(self):
        self.store.put(A, 'reader-index/foreign.json', {'resources': [{'bucket': A, 'path': self.selected['path']}]})
        traversal, report, candidate = self.run_graph()
        with self.assertRaisesRegex(ValueError, 'exactly the withheld'):
            self.capture(traversal, report, candidate)

    def test_missing_target_or_wrong_saved_absence_blocks(self):
        self.store.objects[A].pop(self.withheld['path'])
        traversal, report, candidate = self.run_graph()
        with self.assertRaises(ValueError):
            self.capture(traversal, report, candidate)

    def test_existing_root_path_is_not_shadowed(self):
        root = overlay.isolated.root_path(self.identity['transaction_id'])
        self.store.put(A, root, b'foreign')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            overlay.view(self.store, [self.withheld], self.identity)

    def test_root_appearing_during_scan_is_not_shadowed(self):
        view, candidate = overlay.view(self.store, [self.withheld], self.identity)
        self.store.put(A, candidate['path'], b'foreign')
        with self.assertRaisesRegex(ValueError, 'appeared during'):
            view.list_files(A, ('reader-index/',))


if __name__ == '__main__':
    unittest.main()
