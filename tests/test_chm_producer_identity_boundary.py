"""Synthetic identity checks; old producer evidence must not authorize a new repo.

All APIs and journals below are in-memory fixtures. This is not a live writer
inventory, migration receipt, credentials check, or permission to run GC.
"""

import copy
import json
import unittest
from unittest.mock import Mock, patch

from scripts import reader_producer_scope_transition as scope
from scripts import reader_producer_v2_credentials_preflight as credentials
from scripts import reader_producer_v2_shadow as shadow
from scripts import reader_producer_v2_window as window


OLD_CENTRAL_ID = 1260985830
REBUILT_CENTRAL_ID = 1443726000


class ProducerIdentityBoundaryTests(unittest.TestCase):
    def test_rebuilt_repository_id_stops_old_shadow_before_any_workflow_listing(self):
        paths = []

        def api(path):
            paths.append(path)
            if path == 'repos/anftm/pipeline':
                return [{'id': REBUILT_CENTRAL_ID, 'full_name': 'anftm/pipeline'}]
            raise AssertionError('identity mismatch must stop all later reads')

        self.assertEqual(shadow.CENTRAL_ID, OLD_CENTRAL_ID)
        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            shadow.check_central(api)
        self.assertEqual(paths, ['repos/anftm/pipeline'])

    def test_rebuilt_repository_id_stops_old_credential_preflight_before_inventory(self):
        calls = []

        def get(url, **_kwargs):
            calls.append(url)
            if not url.endswith('/repos/anftm/pipeline'):
                raise AssertionError('identity mismatch must stop all later reads')
            response = Mock(status_code=200)
            response.json.return_value = {'id': REBUILT_CENTRAL_ID, 'full_name': 'anftm/pipeline'}
            return response

        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            credentials.central_check(get=get, token_for=lambda _repo: 'synthetic-fixture')
        self.assertEqual(len(calls), 1)

    def test_unbound_legacy_journal_is_not_a_complete_new_scope_binding(self):
        old = {'version': 1, 'kind': 'reader-maintenance-journal', 'maintenance_id': 'a' * 32,
               'restored': True, 'repositories': {'anftm/pipeline': {'workflows': [], 'cancelled': []}}}
        # Legacy restoration may continue using a recorded journal, but a
        # missing scope binding is not positive evidence for a new window.
        self.assertIsNone(scope.validate_journal_binding(copy.deepcopy(old)))
        candidate = scope.prepare()
        self.assertEqual(candidate['value']['publisher_repository_id'], OLD_CENTRAL_ID)
        self.assertEqual(candidate['status'], 'prepared-not-active')
        with self.assertRaisesRegex(RuntimeError, 'not activated'):
            scope.require_activated(candidate)
        binding = window.binding('b' * 64, 'c' * 64)
        self.assertEqual(binding['central_repository_id'], OLD_CENTRAL_ID)
        with self.assertRaisesRegex(RuntimeError, 'not isolated and bound'):
            window.require_window(json.dumps(old).encode(), expected_maintenance_id='a' * 32,
                                  expected_binding=binding)

    def test_old_scope_digests_cannot_be_rebound_to_rebuilt_repository_by_editing_id(self):
        binding = window.binding('b' * 64, 'c' * 64)
        journal = {'version': 1, 'kind': 'reader-maintenance-journal', 'maintenance_id': 'a' * 32,
                   'producer_scope_v2': copy.deepcopy(binding), 'producer_scope_inventory_validated': True,
                   'producers_isolated': True, 'repositories': {'anftm/pipeline': {
                       'workflows': [{'id': 1, 'state': 'active'}], 'cancelled': []}}}
        journal['producer_scope_v2']['central_repository_id'] = REBUILT_CENTRAL_ID
        with self.assertRaisesRegex(RuntimeError, 'not isolated and bound'):
            window.require_window(json.dumps(journal).encode(), expected_maintenance_id='a' * 32,
                                  expected_binding=binding)
        forged = {**binding, 'central_repository_id': REBUILT_CENTRAL_ID}
        with self.assertRaisesRegex(RuntimeError, 'not isolated and bound'):
            window.require_window(json.dumps(journal).encode(), expected_maintenance_id='a' * 32,
                                  expected_binding=forged)

    def test_404_and_disabled_fixture_never_confer_external_fence_or_gc_authority(self):
        absent = shadow.check_deleted_names(lambda _repository, _repository_id: (404, 404))
        self.assertFalse(absent['absence_is_isolation_proof'])
        with patch.object(shadow, 'verify_evidence', return_value={'object_count': 1}), \
                patch.object(shadow, 'check_central', return_value={'repository': 'synthetic'}):
            result = shadow.check(object(), object())
        for key in ('external_writers_fenced', 'source_credentials_revoked', 'scope_activated', 'gc_authorized'):
            self.assertFalse(result[key], key)


if __name__ == '__main__':
    unittest.main()
