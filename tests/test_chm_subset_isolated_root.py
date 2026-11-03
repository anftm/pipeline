import copy
import hashlib
import json
import unittest

from scripts import chm_subset_isolated_root as isolated, reader_gc_graph as graph
from tests.test_reader_gc_graph import MemoryStore

A = graph.READER_ASSETS_BUCKET


def row(n):
    raw = f'book-{n}'.encode()
    return {'bucket': A, 'path': f'native/ebook/chm/{n:064x}/document.epub',
            'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}, raw


class IsolatedRootTests(unittest.TestCase):
    def setUp(self):
        self.target, self.body = row(1)
        self.other, self.other_body = row(2)
        self.withheld = [self.target]
        self.identity = {'old_plan_sha256': 'a' * 64,
                         'inventory_sha256': isolated.subset.chm.preview.INVENTORY_SHA256,
                         'old_progress_sha256': 'b' * 64, 'old_receipts_sha256': 'c' * 64,
                         'classification_sha256': 'd' * 64, 'maintenance_id': 'e' * 32,
                         'transaction_id': 'f' * 32}
        self.root = isolated.build(self.withheld, self.identity)

    def proofs(self):
        old = {'bucket': A, 'path': 'reader-index/original.json'}
        item = {'bucket': A, 'path': self.root['path']}
        base = {'mode': 'actual', 'graph_complete': True, 'target_count': 972, 'retained_targets': 0,
                'origins': [], 'blockers': [], 'ambiguous_paths': [], 'root_recheck_complete': True,
                'reader_acceptance_complete': True, 'remote_mutations': 0, 'root_count': 1,
                'root_members': [old], 'root_sha256': [{**old, 'sha256': '9' * 64}], 'retired_roots': [],
                'closure_sha256': [{**old, 'sha256': '9' * 64}]}
        new = {**copy.deepcopy(base), 'mode': 'hypothetical-overlay', 'deletion_authorized': False,
               'retained_target_paths': [{'bucket': A, 'path': self.target['path']}]}
        new['root_members'].append(item)
        new['root_sha256'].append({**item, 'sha256': self.root['sha256']})
        new['closure_sha256'].append({**item, 'sha256': self.root['sha256']})
        return base, new

    def check(self, before, after):
        return isolated.compare(before, after, isolated_path=self.root['path'], isolated_raw=self.root['raw'],
                                withheld=self.withheld, identity=self.identity)

    def test_graph_engine_protects_withheld_and_preserves_original_root_bytes(self):
        store = MemoryStore()
        store.put(A, self.target['path'], self.body)
        store.put(A, self.other['path'], self.other_body)
        store.put(A, 'reader-index/original.json', {'kind': 'sample', 'resources': []})
        original = copy.deepcopy(store.objects)
        store.put(A, self.root['path'], self.root['raw'])
        traversal = graph.ReferenceGraph(store, workers=1)
        report = traversal.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        self.assertIn((A, self.target['path']), traversal.references)
        self.assertNotIn((A, self.other['path']), traversal.references)
        self.assertEqual(store.read_bytes(A, 'reader-index/original.json'),
                         original[A]['reader-index/original.json'])
        self.assertFalse(report['deletion_enabled'])

    def test_exact_single_root_and_complete_old_closure_are_accepted_as_simulation(self):
        before, after = self.proofs()
        result = self.check(before, after)
        self.assertEqual(result['mode'], 'hypothetical-isolated-root-only')
        self.assertFalse(result['deletion_authorized'])

    def test_changed_original_or_unexpected_new_root_is_rejected(self):
        before, after = self.proofs()
        after['root_sha256'][0]['sha256'] = '1' * 64
        with self.assertRaisesRegex(ValueError, 'closure changed'):
            self.check(before, after)
        before, after = self.proofs()
        after['root_members'].append({'bucket': A, 'path': 'reader-index/foreign.json'})
        with self.assertRaisesRegex(ValueError, 'membership'):
            self.check(before, after)

    def test_missing_selected_protection_or_extra_epub_is_rejected(self):
        before, after = self.proofs()
        after['retained_target_paths'] = []
        with self.assertRaisesRegex(ValueError, 'target references'):
            self.check(before, after)
        after['retained_target_paths'] = [{'bucket': A, 'path': self.target['path']},
                                          {'bucket': A, 'path': self.other['path']}]
        with self.assertRaisesRegex(ValueError, 'target references'):
            self.check(before, after)

    def test_changed_bytes_cannot_be_accepted_even_if_hash_is_recomputed(self):
        before, after = self.proofs()
        raw = self.root['raw'] + b' '
        digest = hashlib.sha256(raw).hexdigest()
        after['root_sha256'][-1]['sha256'] = digest
        after['closure_sha256'][-1]['sha256'] = digest
        with self.assertRaisesRegex(ValueError, 'exact resources differ'):
            isolated.compare(before, after, isolated_path=self.root['path'], isolated_raw=raw,
                             withheld=self.withheld, identity=self.identity)

    def test_graph_protection_is_invalid_when_withheld_object_missing(self):
        store = MemoryStore()
        store.put(A, self.root['path'], self.root['raw'])
        report = graph.ReferenceGraph(store, workers=1).build()
        self.assertFalse(report['graph_complete'])
        self.assertFalse(report['deletion_enabled'])

    def test_root_schema_rejects_other_buckets_duplicate_and_unknown_fields(self):
        for invalid in ([self.target, self.target], [{**self.target, 'bucket': 'foreign'}],
                        [{**self.target, 'extra': 1}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                isolated.build(invalid, self.identity)

    def test_exact_old_active_root_must_be_in_closure(self):
        before, after = self.proofs()
        before['closure_sha256'].clear()
        after['closure_sha256'].pop(0)
        with self.assertRaisesRegex(ValueError, 'closure changed'):
            self.check(before, after)

    def test_root_cannot_be_a_previously_retired_path(self):
        before, after = self.proofs()
        candidate = {'bucket': A, 'path': self.root['path']}
        before['retired_roots'].append(candidate)
        after['retired_roots'].append(candidate)
        with self.assertRaisesRegex(ValueError, 'membership'):
            self.check(before, after)


if __name__ == '__main__':
    unittest.main()
