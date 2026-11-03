import copy
import hashlib
import unittest

from scripts import chm_subset_reconcile as reconcile, chm_subset_recovery_analysis as recovery

A = reconcile.subset.ASSETS


def make_row(number):
    raw = f'book-{number}'.encode()
    return {'bucket': A, 'path': f'native/ebook/chm/{number:064x}/document.epub',
            'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}, raw


class RecoveryAnalysisTests(unittest.TestCase):
    def setUp(self):
        pairs = [make_row(n) for n in range(1, 7)]
        self.rows = [row for row, _ in pairs]
        self.bodies = {row['path']: raw for row, raw in pairs}
        self.old = [{'bucket': A, 'status': 'verified-absent', 'paths': [self.rows[0]['path']]}]
        self.selected, self.withheld = self.rows[1:5], self.rows[5:]
        self.bodies.pop(self.rows[0]['path'])
        self.state = {'receipts': [], 'pending': None, 'subset_complete': False,
                      'original_complete': False, 'producer_restore_authorized': False}

    def observation(self):
        return reconcile.reconcile(self.rows, self.old, self.selected, self.withheld, self.state,
                                   lambda bucket, path: self.bodies.get(path))

    def analyze(self, observation):
        return recovery.analyze(self.rows, self.old, self.selected, self.withheld, self.state, observation)

    def pending(self):
        self.state['pending'] = {'bucket': A, 'status': 'submitted',
                                 'paths': [self.selected[0]['path'], self.selected[1]['path']]}

    def test_no_pending_does_not_authorize_execution(self):
        result = self.analyze(self.observation())
        self.assertEqual(result['status'], 'no-pending-intent')
        self.assertFalse(result['deletion_authorized'])
        self.assertFalse(result['producer_restore_authorized'])

    def test_partial_pending_is_not_a_receipt_or_retry(self):
        self.pending()
        self.bodies.pop(self.selected[0]['path'])
        result = self.analyze(self.observation())
        self.assertEqual(result['status'], 'pending-unresolved')
        self.assertEqual(result['pending_absent'], [self.selected[0]['path']])
        self.assertEqual(result['pending_present'], [self.selected[1]['path']])
        self.assertFalse(result['deletion_authorized'])

    def test_all_pending_absent_requires_further_independent_verification(self):
        self.pending()
        for row in self.selected[:2]:
            self.bodies.pop(row['path'])
        result = self.analyze(self.observation())
        self.assertEqual(result['status'], 'ready-for-independent-verification')
        self.assertFalse(result['deletion_authorized'])
        self.assertFalse(result['original_complete'])

    def test_missing_duplicated_or_changed_row_is_rejected(self):
        observation = self.observation()
        for change in ('missing', 'duplicate', 'sha', 'status'):
            with self.subTest(change=change):
                bad = copy.deepcopy(observation)
                if change == 'missing':
                    bad['results'].pop()
                elif change == 'duplicate':
                    bad['results'][-1] = copy.deepcopy(bad['results'][0])
                elif change == 'sha':
                    bad['results'][1]['sha256'] = '0' * 64
                else:
                    bad['results'][1]['status'] = 'pending-absent'
                with self.assertRaises(ValueError):
                    self.analyze(bad)

    def test_changed_or_missing_totals_cannot_be_claimed(self):
        observation = self.observation()
        observation['counts']['unsubmitted-present']['bytes'] += 1
        with self.assertRaisesRegex(ValueError, 'totals differ'):
            self.analyze(observation)

    def test_observation_from_another_cursor_is_rejected(self):
        observation = self.observation()
        self.pending()
        with self.assertRaisesRegex(ValueError, 'status differs'):
            self.analyze(observation)

    def test_complete_subset_does_not_complete_original(self):
        self.state['receipts'] = [{'bucket': A, 'status': 'verified-absent',
                                   'paths': [row['path'] for row in self.selected]}]
        self.state['subset_complete'] = True
        for row in self.selected:
            self.bodies.pop(row['path'])
        result = self.analyze(self.observation())
        self.assertEqual(result['status'], 'subset-already-marked-complete')
        self.assertFalse(result['original_complete'])
        self.assertFalse(result['producer_restore_authorized'])


if __name__ == '__main__':
    unittest.main()
