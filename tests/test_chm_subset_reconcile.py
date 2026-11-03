import copy
import hashlib
import unittest

from scripts import chm_subset_reconcile as reconcile

A = reconcile.subset.ASSETS


def row(number):
    body = f'body-{number}'.encode()
    return {'bucket': A, 'path': f'native/ebook/chm/{number:064x}/document.epub',
            'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}, body


class SubsetReconciliationTests(unittest.TestCase):
    def setUp(self):
        pairs = [row(number) for number in range(1, 7)]
        self.rows = [item[0] for item in pairs]
        self.objects = {item[0]['path']: item[1] for item in pairs}
        self.selected = self.rows[1:5]
        self.withheld = self.rows[5:]
        self.old = [{'bucket': A, 'paths': [self.rows[0]['path']], 'status': 'verified-absent'}]
        self.objects.pop(self.rows[0]['path'])
        self.state = {'receipts': [], 'pending': None, 'subset_complete': False,
                      'original_complete': False, 'producer_restore_authorized': False}

    def read(self, bucket, path):
        return self.objects.get(path)

    def check(self):
        return reconcile.reconcile(self.rows, self.old, self.selected, self.withheld, self.state, self.read)

    def test_fresh_partition_reconciles_every_target(self):
        result = self.check()
        self.assertEqual(result['counts']['old-verified-absent']['objects'], 1)
        self.assertEqual(result['counts']['unsubmitted-present']['objects'], 4)
        self.assertEqual(result['counts']['withheld-present']['objects'], 1)
        self.assertFalse(result['deletion_authorized'])

    def test_partial_result_only_allowed_for_recorded_pending_intent(self):
        pending = [self.rows[1]['path'], self.rows[2]['path']]
        self.state['pending'] = {'bucket': A, 'status': 'submitted', 'paths': pending}
        self.objects.pop(pending[0])
        result = self.check()
        self.assertEqual(result['counts']['pending-absent']['objects'], 1)
        self.assertEqual(result['counts']['pending-present']['objects'], 1)
        self.assertEqual(result['counts']['unsubmitted-present']['objects'], 2)

    def test_absent_unsubmitted_or_withheld_is_rejected(self):
        for index, message in ((1, 'unsubmitted'), (5, 'withheld')):
            with self.subTest(index=index):
                original = self.objects.pop(self.rows[index]['path'])
                with self.assertRaisesRegex(ValueError, message):
                    self.check()
                self.objects[self.rows[index]['path']] = original

    def test_reappearance_of_old_or_new_verified_absence_is_rejected(self):
        self.objects[self.rows[0]['path']] = b'body-1'
        with self.assertRaisesRegex(ValueError, 'original verified-absent'):
            self.check()
        self.objects.pop(self.rows[0]['path'])
        self.state['receipts'] = [{'bucket': A, 'status': 'verified-absent', 'paths': [self.rows[1]['path']]}]
        with self.assertRaisesRegex(ValueError, 'subset verified-absent'):
            self.check()

    def test_changed_bytes_are_rejected_even_when_length_is_same(self):
        path = self.rows[1]['path']
        self.objects[path] = b'BODY-2'
        with self.assertRaisesRegex(ValueError, 'exact target changed'):
            self.check()

    def test_complete_subset_reconciliation_does_not_complete_original(self):
        self.state['receipts'] = [{'bucket': A, 'status': 'verified-absent',
                                   'paths': [row['path'] for row in self.selected]}]
        for row in self.selected:
            self.objects.pop(row['path'])
        self.state['subset_complete'] = True
        result = self.check()
        self.assertEqual(result['counts']['subset-verified-absent']['objects'], 4)
        self.assertFalse(result['deletion_authorized'])
        self.assertEqual(len(self.objects), 1)

    def test_reader_failure_cannot_be_treated_as_absence(self):
        def failure(_bucket, _path):
            raise PermissionError('unavailable')
        with self.assertRaises(PermissionError):
            reconcile.reconcile(self.rows, self.old, self.selected, self.withheld, self.state, failure)


if __name__ == '__main__':
    unittest.main()
