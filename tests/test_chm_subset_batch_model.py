import hashlib
import unittest

from scripts import chm_subset_batch_model as batch, chm_subset_journal as journal
from scripts import chm_subset_reconcile as reconcile


class BatchModelTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.objects = [], {}
        for n in range(1, 7):
            raw = f'fake-{n}'.encode()
            row = {'bucket': journal.subset.ASSETS, 'path': f'native/ebook/chm/{n:064x}/document.epub',
                   'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            self.rows.append(row)
            if n != 1:
                self.objects[row['path']] = raw
        self.old = [{'bucket': journal.subset.ASSETS, 'status': 'verified-absent', 'paths': [self.rows[0]['path']]}]
        self.selected, self.withheld = self.rows[1:5], self.rows[5:]
        progress = {'kind': 'chm-epub-retirement', 'plan_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
                    'maintenance_id': 'e' * 32, 'complete': False, 'batches': self.old,
                    'confirmed_deleted_objects': 1, 'confirmed_deleted_bytes': self.rows[0]['bytes']}
        raw = journal.encode(progress)
        identity = {'old_plan_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
                    'old_progress_sha256': hashlib.sha256(raw).hexdigest(), 'old_receipts_sha256': journal.digest(self.old),
                    'classification_sha256': 'd' * 64, 'maintenance_id': 'e' * 32, 'transaction_id': 'f' * 32}
        self.plan = journal.bind_plan(self.rows, self.old, self.selected, self.withheld, identity,
                                      old_progress_raw=raw)['plan']
        state = {'receipts': [], 'pending': None, 'subset_complete': False,
                 'original_complete': False, 'producer_restore_authorized': False}
        self.head = journal.make_envelope(self.plan, state, sequence=0, parent_sha256=None, nonce='1' * 32)

    def observe(self):
        return reconcile.reconcile(self.rows, self.old, self.selected, self.withheld, self.head['state'],
                                   lambda bucket, path: self.objects.get(path), workers=1)

    def intent(self, nonce='2'):
        return batch.plan_intent(self.rows, self.old, self.selected, self.withheld, self.plan, self.head,
                                self.observe(), batch_size=2, nonce=nonce * 32)

    def receipt(self, nonce='3'):
        return batch.plan_receipt(self.rows, self.old, self.selected, self.withheld, self.plan, self.head,
                                 self.observe(), nonce=nonce * 32)

    def test_exact_batches_and_subset_only_completion(self):
        for intent_nonce, receipt_nonce in (('2', '3'), ('4', '5')):
            self.head = self.intent(intent_nonce)['envelope']
            for path in self.head['state']['pending']['paths']:
                self.objects.pop(path)
            self.head = self.receipt(receipt_nonce)['envelope']
        result = batch.plan_completion(self.rows, self.old, self.selected, self.withheld, self.plan,
                                       self.head, self.observe(), nonce='6' * 32)
        self.assertTrue(result['envelope']['state']['subset_complete'])
        self.assertFalse(result['original_complete'])
        self.assertFalse(result['producer_restore_authorized'])
        self.assertIn(self.withheld[0]['path'], self.objects)

    def test_partial_pending_never_becomes_receipt(self):
        self.head = self.intent()['envelope']
        self.objects.pop(self.head['state']['pending']['paths'][0])
        with self.assertRaisesRegex(ValueError, 'not entirely observed absent'):
            self.receipt()
        with self.assertRaisesRegex(ValueError, 'pending or complete'):
            self.intent('4')

    def test_withheld_or_old_receipt_change_blocks(self):
        self.objects[self.withheld[0]['path']] = b'changed'
        with self.assertRaises(ValueError):
            self.intent()
        self.objects[self.withheld[0]['path']] = b'fake-6'
        self.objects[self.rows[0]['path']] = b'fake-1'
        with self.assertRaises(ValueError):
            self.intent()

    def test_completion_cannot_skip_unprocessed_items(self):
        with self.assertRaisesRegex(ValueError, 'all selected receipts'):
            batch.plan_completion(self.rows, self.old, self.selected, self.withheld, self.plan,
                                  self.head, self.observe(), nonce='4' * 32)


if __name__ == '__main__':
    unittest.main()
