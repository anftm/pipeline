import copy
import hashlib
import unittest

from scripts import chm_subset_journal as journal

A = journal.subset.ASSETS


def row(n):
    return {'bucket': A, 'path': f'native/ebook/chm/{n:064x}/document.epub', 'bytes': n,
            'sha256': hashlib.sha256(str(n).encode()).hexdigest()}


class SubsetJournalTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(n) for n in range(1, 5)]
        self.old = [{'bucket': A, 'status': 'verified-absent', 'paths': [self.rows[0]['path']]}]
        self.selected, self.withheld = self.rows[1:3], self.rows[3:]
        self.identity = {'old_plan_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
                         'old_progress_sha256': 'c' * 64, 'old_receipts_sha256': journal.digest(self.old),
                         'classification_sha256': 'd' * 64, 'maintenance_id': 'e' * 32,
                         'transaction_id': 'f' * 32}
        self.progress = {'kind': 'chm-epub-retirement', 'plan_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
                         'maintenance_id': 'e' * 32, 'complete': False, 'batches': self.old,
                         'confirmed_deleted_objects': 1, 'confirmed_deleted_bytes': self.rows[0]['bytes']}
        self.progress_raw = journal.encode(self.progress)
        self.identity['old_progress_sha256'] = hashlib.sha256(self.progress_raw).hexdigest()
        self.plan = journal.bind_plan(self.rows, self.old, self.selected, self.withheld, self.identity,
                                      old_progress_raw=self.progress_raw)['plan']
        self.state = {'receipts': [], 'pending': None, 'subset_complete': False,
                      'original_complete': False, 'producer_restore_authorized': False}
        self.before = journal.make_envelope(self.plan, self.state, sequence=0, parent_sha256=None, nonce='8' * 32)

    def next(self, previous, state, nonce):
        return journal.make_envelope(self.plan, state, sequence=previous['sequence'] + 1,
                                     parent_sha256=journal.digest(previous), nonce=nonce * 32)

    def test_single_steps_bind_intent_receipt_and_subset_completion(self):
        pending = {'bucket': A, 'status': 'submitted', 'paths': [x['path'] for x in self.selected]}
        state = {**self.state, 'pending': pending}
        one = self.next(self.before, state, '1')
        self.assertEqual(journal.verify_transition(self.plan, self.before, one)['transition'], 'intent')
        state = {**self.state, 'receipts': [{**pending, 'status': 'verified-absent'}]}
        two = self.next(one, state, '2')
        self.assertEqual(journal.verify_transition(self.plan, one, two)['transition'], 'receipt')
        state = {**state, 'subset_complete': True}
        three = self.next(two, state, '3')
        result = journal.verify_transition(self.plan, two, three)
        self.assertEqual(result['transition'], 'subset-complete')
        self.assertFalse(result['deletion_authorized'])
        with self.assertRaisesRegex(ValueError, 'cannot advance'):
            journal.verify_transition(self.plan, three, self.next(three, state, '4'))

    def test_original_receipt_binding_and_unchanged_original_are_required(self):
        identity = {**self.identity, 'old_receipts_sha256': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'receipt digest'):
            journal.bind_plan(self.rows, self.old, self.selected, self.withheld, identity, old_progress_raw=self.progress_raw)
        progress = {**self.progress, 'complete': True}
        raw = journal.encode(progress)
        identity = {**self.identity, 'old_progress_sha256': hashlib.sha256(raw).hexdigest()}
        with self.assertRaisesRegex(ValueError, 'original CHM progress'):
            journal.bind_plan(self.rows, self.old, self.selected, self.withheld, identity, old_progress_raw=raw)
        with self.assertRaisesRegex(ValueError, 'byte SHA differs'):
            journal.bind_plan(self.rows, self.old, self.selected, self.withheld, self.identity,
                              old_progress_raw=self.progress_raw + b' ')

    def test_parent_sequence_and_nonce_are_required(self):
        pending = {'bucket': A, 'status': 'submitted', 'paths': [self.selected[0]['path']]}
        next_envelope = self.next(self.before, {**self.state, 'pending': pending}, '1')
        for field, value in (('parent_sha256', '9' * 64), ('sequence', 7), ('nonce', '8' * 32)):
            with self.subTest(field=field):
                bad = {**next_envelope, field: value}
                with self.assertRaisesRegex(ValueError, 'chain or nonce'):
                    journal.verify_transition(self.plan, self.before, bad)

    def test_no_skipping_intent_or_editing_receipt(self):
        receipt = {'bucket': A, 'status': 'verified-absent', 'paths': [self.selected[0]['path']]}
        state = {**self.state, 'receipts': [receipt]}
        with self.assertRaisesRegex(ValueError, 'single step'):
            journal.verify_transition(self.plan, self.before, self.next(self.before, state, '1'))
        intent = self.next(self.before, {**self.state, 'pending': {**receipt, 'status': 'submitted'}}, '1')
        changed = {'bucket': A, 'status': 'verified-absent', 'paths': [x['path'] for x in self.selected]}
        with self.assertRaisesRegex(ValueError, 'single step'):
            journal.verify_transition(self.plan, intent, self.next(intent, {**self.state, 'receipts': [changed]}, '2'))

    def test_nonce_does_not_mean_etag_aba_is_solved(self):
        altered = copy.deepcopy(self.before)
        altered['nonce'] = '1' * 32
        self.assertNotEqual(journal.digest(self.before), journal.digest(altered))
        self.assertFalse(altered['deletion_authorized'])

    def test_foreign_plan_or_withheld_intent_is_rejected(self):
        pending = {'bucket': A, 'status': 'submitted', 'paths': [self.withheld[0]['path']]}
        with self.assertRaisesRegex(ValueError, 'pending intent'):
            self.next(self.before, {**self.state, 'pending': pending}, '1')
        bad = copy.deepcopy(self.plan)
        bad['selected'][0]['bytes'] += 1
        pending = {'bucket': A, 'status': 'submitted', 'paths': [self.selected[0]['path']]}
        new = self.next(self.before, {**self.state, 'pending': pending}, '1')
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            journal.verify_transition(bad, self.before, new)

    def test_initial_cursor_cannot_already_claim_a_receipt(self):
        state = {**self.state, 'receipts': [{'bucket': A, 'status': 'verified-absent',
                                            'paths': [self.selected[0]['path']]}]}
        with self.assertRaisesRegex(ValueError, 'initial subset cursor'):
            journal.make_envelope(self.plan, state, sequence=0, parent_sha256=None, nonce='8' * 32)

    def test_all_zero_nonce_and_transaction_id_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'sequence, parent, or nonce'):
            journal.make_envelope(self.plan, self.state, sequence=0, parent_sha256=None, nonce='0' * 32)
        identity = {**self.identity, 'transaction_id': '0' * 32}
        with self.assertRaisesRegex(ValueError, 'all-zero placeholder'):
            journal.bind_plan(self.rows, self.old, self.selected, self.withheld, identity,
                              old_progress_raw=self.progress_raw)


if __name__ == '__main__':
    unittest.main()
