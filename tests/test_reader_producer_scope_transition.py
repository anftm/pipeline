import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import reader_gc_fence as fence, reader_gc_maintenance as maintenance
from scripts import reader_producer_scope as legacy, reader_producer_scope_transition as transition


class ScopeTransitionTests(unittest.TestCase):
    def setUp(self):
        self.prepared = transition.prepare()
        self.value = self.prepared['value']

    def write_config(self, value, root):
        path = Path(root) / 'transition.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def test_prepared_scope_binds_original_eight_and_lists_both_discovered_forks(self):
        self.assertEqual(self.value['legacy_repositories'], legacy.load_config()['repositories'])
        self.assertEqual(len(self.value['legacy_repositories']), 8)
        self.assertEqual(self.value['additional_repository_ids'], transition.ADDITIONAL)
        self.assertEqual(self.value['retired_repository_ids'], transition.EXPECTED)
        self.assertEqual(self.value['legacy_scope_sha256'], hashlib.sha256(legacy.SCOPE.read_bytes()).hexdigest())
        self.assertEqual(self.prepared['status'], 'prepared-not-active')
        self.assertIsNone(self.value['activation_receipt'])

    def test_prepared_scope_never_changes_actual_legacy_inventory(self):
        self.assertEqual(fence.producer_repositories(), sorted(self.value['legacy_repositories']))
        with self.assertRaisesRegex(RuntimeError, 'not activated'):
            transition.require_activated(self.prepared)

    def test_changed_identity_missing_repo_or_forged_activation_is_rejected(self):
        alterations = {
            'retired-id': lambda x: x['retired_repository_ids'].__setitem__('alicetran68/pipeline', 9),
            'boolean-id': lambda x: x['additional_repository_ids'].__setitem__('vomebook/pipeline', True),
            'unresolved-removed': lambda x: x['additional_repository_ids'].pop('vomebook/pipeline'),
            'legacy-removed': lambda x: x['legacy_repositories'].pop(),
            'active-added': lambda x: x['future_active_repositories'].append('vomebook/pipeline'),
            'fake-activated': lambda x: x.__setitem__('status', 'active'),
            'fake-receipt': lambda x: x.__setitem__('activation_receipt', 'a' * 64),
            'extra-field': lambda x: x.__setitem__('deleted_means_safe', True),
            'central-id': lambda x: x.__setitem__('publisher_repository_id', True),
            'legacy-hash': lambda x: x.__setitem__('legacy_scope_sha256', '0' * 64),
        }
        for label, change in alterations.items():
            value = copy.deepcopy(self.value)
            change(value)
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(ValueError):
                    transition.prepare(self.write_config(value, root))

    def test_duplicate_json_fields_cannot_silently_override_identity(self):
        with tempfile.TemporaryDirectory() as root:
            raw = transition.TRANSITION.read_text()
            raw = raw.replace('"version": 2,', '"version": 1, "version": 2,', 1)
            path = Path(root) / 'duplicate.json'
            path.write_text(raw)
            with self.assertRaises(ValueError):
                transition.prepare(path)

    def test_journal_binding_preserves_old_unbound_recovery(self):
        self.assertIsNone(transition.validate_journal_binding({'repositories': {'old/repo': {}}}))
        self.assertEqual(transition.journal_binding()['repositories'], fence.producer_repositories())

    def test_binding_rejects_a_scope_changed_during_validation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'scope.json'
            path.write_bytes(legacy.SCOPE.read_bytes())
            original = legacy.load_config
            def change_after_parse(target):
                value = original(target)
                path.write_bytes(path.read_bytes() + b' ')
                return value
            with patch.object(legacy, 'load_config', side_effect=change_after_parse):
                with self.assertRaisesRegex(RuntimeError, 'changed during'):
                    transition.journal_binding(path)

    def test_bound_complete_partial_and_tampered_journals(self):
        binding = transition.journal_binding()
        state = {'producer_scope': binding, 'repositories': {r: {} for r in binding['repositories']}}
        self.assertEqual(transition.validate_journal_binding(state, expected=binding), binding)
        state['repositories'].pop(binding['repositories'][0])
        self.assertEqual(transition.validate_journal_binding(state, require_complete=False), binding)
        with self.assertRaisesRegex(ValueError, 'repository set differs'):
            transition.validate_journal_binding(state)
        state['repositories']['foreign/repo'] = {}
        with self.assertRaisesRegex(ValueError, 'repository set differs'):
            transition.validate_journal_binding(state, require_complete=False)
        state['repositories'] = {r: {} for r in binding['repositories']}
        fake = copy.deepcopy(binding)
        fake['sha256'] = 'a' * 64
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            transition.validate_journal_binding(state, expected=fake)

    def test_unfinished_legacy_recovery_still_uses_the_recorded_repository(self):
        state = {'version': 1, 'kind': 'reader-maintenance-journal', 'maintenance_id': 'a' * 32,
                 'repositories': {'old/repo': {'permission': {'enabled': False},
                                               'workflows': [], 'cancelled': []}}}
        with tempfile.TemporaryDirectory() as root, patch.object(maintenance, 'durable_store', return_value=None), \
             patch.object(maintenance, 'journal'), patch.object(maintenance, 'kick_restored'), \
             patch.object(maintenance, 'producer_repositories', side_effect=AssertionError('must use saved journal')):
            path = Path(root) / 'state.json'
            path.write_text(json.dumps(state))
            maintenance.restore(path)

    def test_invalid_bound_journal_is_rejected_before_any_restoration(self):
        binding = transition.journal_binding()
        state = {'version': 1, 'kind': 'reader-maintenance-journal', 'maintenance_id': 'a' * 32,
                 'producer_scope': binding, 'repositories': {'foreign/repo': {'workflows': [], 'cancelled': []}}}
        with tempfile.TemporaryDirectory() as root, patch.object(maintenance, 'durable_store', return_value=None), \
             patch.object(maintenance, 'api', side_effect=AssertionError('must not mutate')):
            path = Path(root) / 'state.json'
            path.write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, 'repository set differs'):
                maintenance.restore(path)

    def test_failed_preflight_partial_journal_can_still_recover_honestly(self):
        binding = transition.journal_binding()
        state = {'version': 1, 'kind': 'reader-maintenance-journal', 'maintenance_id': 'a' * 32,
                 'producer_scope': binding, 'repositories': {}, 'restored': True}
        class Store:
            def read_bytes(self, _bucket, _path):
                return json.dumps(state).encode()
        with tempfile.TemporaryDirectory() as root, patch.object(maintenance, 'durable_store', return_value=Store()), \
             patch.object(maintenance, 'archive_journal') as archive, \
             patch.object(maintenance, 'restore', side_effect=AssertionError('already restored')):
            maintenance.recover(Path(root) / 'state.json')
        archive.assert_called_once()

    def test_new_durable_window_records_exact_scope_before_isolation(self):
        binding = {'version': 1, 'sha256': 'a' * 64, 'repositories': ['central/repo']}
        inventory = {'central/repo': {'permission': {}, 'workflows': [], 'cancelled': []}}
        records = []
        def record(_path, state):
            records.append(copy.deepcopy(state))
        with tempfile.TemporaryDirectory() as root, \
             patch.object(maintenance, 'load_config', return_value={'publisher_repository': 'central/repo'}), \
             patch.object(maintenance, 'durable_store', return_value=object()), \
             patch.object(maintenance, 'journal_binding', return_value=binding), \
             patch.object(maintenance, 'recover'), patch.object(maintenance, 'preflight', return_value=inventory), \
             patch.object(maintenance, 'pages', return_value=[]), patch.object(maintenance, 'journal', side_effect=record), \
             patch.object(maintenance, 'restore'):
            result = maintenance.run(Path(root) / 'state.json', isolated_callback=lambda _: 'done',
                                     preserve_queued_runs=True)
        self.assertEqual(result, 'done')
        self.assertEqual(records[0]['producer_scope'], binding)
        self.assertEqual(records[0]['repositories'], {})
        self.assertTrue(any(set(item['repositories']) == {'central/repo'} for item in records))

    def test_scope_drift_blocks_before_a_producer_is_disabled(self):
        binding = {'version': 1, 'sha256': 'a' * 64, 'repositories': ['central/repo']}
        altered = {**binding, 'sha256': 'b' * 64}
        inventory = {'central/repo': {'permission': {}, 'workflows': [
            {'id': 1, 'path': '.github/workflows/pdf-account-worker.yml', 'state': 'active'}], 'cancelled': []}}
        with tempfile.TemporaryDirectory() as root, \
             patch.object(maintenance, 'load_config', return_value={'publisher_repository': 'central/repo'}), \
             patch.object(maintenance, 'durable_store', return_value=object()), \
             patch.object(maintenance, 'journal_binding', side_effect=[binding, altered]), \
             patch.object(maintenance, 'recover'), patch.object(maintenance, 'preflight', return_value=inventory), \
             patch.object(maintenance, 'journal'), patch.object(maintenance, 'restore'), \
             patch.object(maintenance, 'workflow_state') as change:
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                maintenance.run(Path(root) / 'state.json', isolated_callback=lambda _: 'never')
        change.assert_not_called()

    def test_callback_cannot_mutate_the_bound_scope_even_in_place(self):
        binding = {'version': 1, 'sha256': 'a' * 64, 'repositories': ['central/repo']}
        inventory = {'central/repo': {'permission': {}, 'workflows': [], 'cancelled': []}}
        def tamper(state):
            state['producer_scope']['sha256'] = 'b' * 64
            return 'done'
        with tempfile.TemporaryDirectory() as root, \
             patch.object(maintenance, 'load_config', return_value={'publisher_repository': 'central/repo'}), \
             patch.object(maintenance, 'durable_store', return_value=object()), \
             patch.object(maintenance, 'journal_binding', side_effect=lambda **_: copy.deepcopy(binding)), \
             patch.object(maintenance, 'recover'), patch.object(maintenance, 'preflight', return_value=inventory), \
             patch.object(maintenance, 'pages', return_value=[]), patch.object(maintenance, 'journal'), \
             patch.object(maintenance, 'restore'):
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                maintenance.run(Path(root) / 'state.json', isolated_callback=tamper, preserve_queued_runs=True)


if __name__ == '__main__':
    unittest.main()
