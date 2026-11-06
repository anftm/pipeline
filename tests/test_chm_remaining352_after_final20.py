import hashlib
import unittest
from unittest.mock import Mock, patch

from scripts import chm_remaining352_after_final20 as run, chm_subset_journal as journal
from scripts import chm_subset_journal_backend as chain
from tests.test_chm_subset_journal_backend import FakeBackend


class Memory(FakeBackend):
    def __init__(self, objects):
        super().__init__()
        self.objects = dict(objects)
        self.calls = []
        self.mode = None
    def read_bytes(self, _bucket, path):
        if path not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[path]
    def delete_once(self, paths):
        self.calls.append(list(paths))
        if self.mode == 'partial':
            self.objects.pop(paths[0])
            raise RuntimeError('unknown partial')
        for path in paths:
            self.objects.pop(path)
        if self.mode == 'full':
            raise RuntimeError('unknown full')


class RemainingTests(unittest.TestCase):
    def setUp(self):
        self.rows, objects = [], {}
        for i in range(1, 7):
            raw = f'fixture-{i}'.encode()
            row = {'bucket': journal.subset.ASSETS, 'path': f'native/ebook/chm/{i:064x}/document.epub',
                   'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            self.rows.append(row)
            if 2 <= i <= 5:
                objects[row['path']] = raw
        self.old = [{'bucket': journal.subset.ASSETS, 'status': 'verified-absent', 'paths': [self.rows[0]['path']]}]
        selected, withheld = self.rows[1:5], self.rows[5:]
        self.identity = {'old_plan_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
                         'old_progress_sha256': 'c' * 64, 'old_receipts_sha256': journal.digest(self.old),
                         'classification_sha256': 'd' * 64, 'maintenance_id': 'e' * 32, 'transaction_id': 'f' * 32}
        self.plan = journal._plan(selected, withheld, self.identity)
        self.store = Memory(objects)
        self.initial = journal.make_envelope(self.plan, {'receipts': [], 'pending': None, 'subset_complete': False,
            'original_complete': False, 'producer_restore_authorized': False}, sequence=0, parent_sha256=None,
            nonce=run.storage.INITIAL_NONCE)
        self.patches = [patch.object(run.preflight, '_identity'),
                        patch.object(run, 'final_head', return_value={'synthetic': 'completed'}),
                        patch.object(run.storage, 'INITIAL_SHA', journal.digest(self.initial))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def call(self):
        return run.run(self.store, self.store, Mock(), self.rows, self.old, self.plan)

    def test_remaining_targets_deleted_and_final20_never_recreated(self):
        result = self.call()
        self.assertEqual(result['subset_verified_absent'], 4)
        self.assertTrue(result['subset_complete'])
        self.assertFalse(result['original_complete'])
        self.assertEqual(self.store.calls, [[row['path'] for row in self.plan['selected']]])
        self.assertFalse(self.store.objects)

    def test_partial_unknown_reconciles_only_still_present(self):
        self.store.mode = 'partial'
        with self.assertRaisesRegex(RuntimeError, 'unknown partial'):
            self.call()
        self.store.mode = None
        self.assertTrue(self.call()['subset_complete'])
        self.assertNotIn(self.plan['selected'][0]['path'], self.store.calls[1])

    def test_old_or_final20_reappearance_blocks(self):
        self.store.objects[self.rows[0]['path']] = b'fixture-1'
        with self.assertRaisesRegex(ValueError, 'reappeared'):
            self.call()
        self.assertFalse(self.store.calls)
        del self.store.objects[self.rows[0]['path']]
        self.store.objects[self.rows[5]['path']] = b'fixture-6'
        with self.assertRaisesRegex(ValueError, 'reappeared'):
            self.call()
        self.assertFalse(self.store.calls)

    def test_final20_incomplete_blocks_mutation(self):
        with patch.object(run, 'final_head', side_effect=ValueError('not complete')):
            with self.assertRaisesRegex(ValueError, 'not complete'):
                self.call()
        self.assertEqual(self.store.files, {})
        self.assertFalse(self.store.calls)

    def test_prior_plan_only_recovery_and_lost_full_response(self):
        self.store.files['plan.json'] = journal.encode(self.plan)
        self.store.mode = 'full'
        with self.assertRaisesRegex(RuntimeError, 'unknown full'):
            self.call()
        self.assertIn('event-00000000.json', self.store.files)
        self.store.mode = None
        self.assertTrue(self.call()['subset_complete'])
        self.assertEqual(len(self.store.calls), 1)


if __name__ == '__main__':
    unittest.main()
