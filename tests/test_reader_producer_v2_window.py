import copy
import json
import unittest

from scripts import reader_producer_v2_window as window


class V2WindowTests(unittest.TestCase):
    def setUp(self):
        self.identity = 'a' * 32
        self.binding = window.binding('b' * 64, 'c' * 64)
        self.state = {'version': 1, 'kind': 'reader-maintenance-journal', 'maintenance_id': self.identity,
                      'producer_scope_v2': self.binding, 'producer_scope_inventory_validated': True,
                      'producers_isolated': True, 'repositories': {window.CENTRAL: {
                          'workflows': [{'id': 1, 'state': 'active'}], 'cancelled': []}}}

    def check(self, value):
        return window.require_window(json.dumps(value).encode(), expected_maintenance_id=self.identity,
                                     expected_binding=self.binding)

    def test_exact_window_is_not_itself_gc_authorization(self):
        result = self.check(self.state)
        self.assertFalse(result['gc_authorized'])
        self.assertFalse(result['external_writers_fenced'])

    def test_drift_partial_isolation_recovery_and_foreign_scope_block(self):
        changes = [
            ('maintenance_id', 'd' * 32), ('producers_isolated', False),
            ('producer_scope_inventory_validated', False), ('restored', True),
            ('callback_recovery_required', True), ('callback_mutation_started', True),
            ('restore_errors', ['error']), ('repositories', {}),
            ('producer_scope_v2', {**self.binding, 'scope_sha256': 'f' * 64}),
        ]
        for field, value in changes:
            state = {**self.state, field: value}
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.check(state)

    def test_canceled_or_unrestored_central_snapshot_cannot_be_skipped(self):
        for field, value in (('cancelled', [{'id': 1}]), ('restored', True), ('workflows', []),
                             ('permission_changed', True)):
            state = copy.deepcopy(self.state)
            state['repositories'][window.CENTRAL][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.check(state)


if __name__ == '__main__':
    unittest.main()
