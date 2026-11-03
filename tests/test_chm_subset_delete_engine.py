import hashlib
import unittest

from scripts import chm_subset_delete_engine as engine, chm_subset_journal as model
from scripts import chm_subset_journal_backend as backend
from tests.test_chm_subset_journal_backend import FakeBackend


class FakeStore(FakeBackend):
    def __init__(self, objects):
        super().__init__()
        self.objects = dict(objects)
        self.calls = []
        self.mode = 'normal'

    def read_bytes(self, bucket, path):
        if bucket != model.subset.ASSETS:
            raise AssertionError('foreign bucket')
        if path not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[path]

    def assert_chm_bucket_available(self):
        return True

    def delete(self, bucket, paths):
        self.calls.append(list(paths))
        if self.mode == 'partial-lost':
            self.objects.pop(paths[0])
            raise OSError('lost response after partial delete')
        if self.mode == 'full-lost':
            for path in paths:
                self.objects.pop(path)
            raise OSError('lost response after full delete')
        for path in paths:
            self.objects.pop(path)


class DeleteEngineTests(unittest.TestCase):
    def setUp(self):
        self.rows, objects = [], {}
        for n in range(1, 7):
            raw = f'book-{n}'.encode()
            row = {'bucket': model.subset.ASSETS, 'path': f'native/ebook/chm/{n:064x}/document.epub',
                   'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            self.rows.append(row)
            if n > 1:
                objects[row['path']] = raw
        self.old = [{'bucket': model.subset.ASSETS, 'status': 'verified-absent', 'paths': [self.rows[0]['path']]}]
        self.selected, self.withheld = self.rows[1:5], self.rows[5:]
        progress = {'kind': 'chm-epub-retirement', 'plan_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
                    'maintenance_id': 'e' * 32, 'complete': False, 'batches': self.old,
                    'confirmed_deleted_objects': 1, 'confirmed_deleted_bytes': self.rows[0]['bytes']}
        raw = model.encode(progress)
        identity = {'old_plan_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
                    'old_progress_sha256': hashlib.sha256(raw).hexdigest(),
                    'old_receipts_sha256': model.digest(self.old), 'classification_sha256': 'd' * 64,
                    'maintenance_id': 'e' * 32, 'transaction_id': 'f' * 32}
        self.plan = model.bind_plan(self.rows, self.old, self.selected, self.withheld, identity,
                                    old_progress_raw=raw)['plan']
        self.store = FakeStore(objects)
        self.cursor = backend.Cursor(self.store, self.plan)
        self.cursor.initialize(nonce='1' * 32)
        self.n = 1
        self.barrier_calls = 0
        self.stop_after = None

    def nonce(self):
        self.n += 1
        return f'{self.n:032x}'

    def barrier(self):
        self.barrier_calls += 1
        if self.stop_after == self.barrier_calls:
            raise RuntimeError('barrier changed')

    def run_engine(self):
        return engine.run(self.store, self.cursor, self.rows, self.old, self.plan,
                          self.barrier, nonce=self.nonce, batch_size=2)

    def test_only_selected_targets_are_deleted_and_receipted(self):
        head = self.run_engine()
        self.assertTrue(head['state']['subset_complete'])
        self.assertFalse(head['state']['original_complete'])
        self.assertFalse(head['state']['producer_restore_authorized'])
        self.assertEqual(self.store.calls, [[r['path'] for r in self.selected[:2]],
                                            [r['path'] for r in self.selected[2:]]])
        self.assertEqual(self.store.objects, {self.withheld[0]['path']: b'book-6'})

    def test_partial_unknown_result_is_reconciled_before_remainder(self):
        self.store.mode = 'partial-lost'
        with self.assertRaisesRegex(OSError, 'partial delete'):
            self.run_engine()
        self.assertIsNotNone(self.cursor.head()['state']['pending'])
        self.assertEqual(len(self.store.calls), 1)
        self.store.mode = 'normal'
        result = self.run_engine()
        self.assertTrue(result['state']['subset_complete'])
        self.assertEqual(self.store.calls[1], [self.selected[1]['path']])
        self.assertEqual(self.store.objects, {self.withheld[0]['path']: b'book-6'})

    def test_full_unknown_result_is_receipted_without_duplicate_delete(self):
        self.store.mode = 'full-lost'
        with self.assertRaisesRegex(OSError, 'full delete'):
            self.run_engine()
        self.store.mode = 'normal'
        self.assertTrue(self.run_engine()['state']['subset_complete'])
        self.assertEqual(len(self.store.calls), 2)
        self.assertEqual(self.store.calls[1], [row['path'] for row in self.selected[2:]])

    def test_changed_withheld_or_barrier_prevents_delete(self):
        self.store.objects[self.withheld[0]['path']] = b'changed'
        with self.assertRaises(ValueError):
            self.run_engine()
        self.assertEqual(self.store.calls, [])
        self.store.objects[self.withheld[0]['path']] = b'book-6'
        self.stop_after = self.barrier_calls + 2
        with self.assertRaisesRegex(RuntimeError, 'barrier changed'):
            self.run_engine()
        self.assertIsNotNone(self.cursor.head()['state']['pending'])
        self.assertEqual(self.store.calls, [])

    def test_missing_bucket_is_not_a_valid_absence(self):
        self.store.assert_chm_bucket_available = lambda: False
        with self.assertRaisesRegex(RuntimeError, 'bucket health'):
            self.run_engine()
        self.assertEqual(self.store.calls, [])


if __name__ == '__main__':
    unittest.main()
