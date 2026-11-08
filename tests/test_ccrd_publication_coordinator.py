"""Recovery orchestration uses durable intents and independently observed bytes."""
import unittest
from types import SimpleNamespace

from scripts import ccrd_lifecycle
from scripts import ccrd_publication_coordinator as coord
from scripts import ccrd_transaction as tx
from tests.test_ccrd_activation_recovery import RecoveryTests
from tests.test_ccrd_metadata_patch import NEW
from tests.test_ccrd_pointer_transition import PointerMemory
from tests.test_ccrd_space_activation import NEW_REV
from tests.test_ccrd_transaction import MemoryStore


class CoordinatorTests(RecoveryTests):
    def prepared_transaction(self):
        planned = self.activation_plan(); journal = MemoryStore()
        current = coord.prepare(journal, parent_pointer=self.pointer, candidate_plan=self.plan_value,
                                description=self.description, activation_plan=planned, transaction='1' * 32)
        self.assertEqual(current.record['state'], 'prepared')
        coord.start_space(journal)
        return journal, planned

    def test_uncertain_no_commit_is_observed_without_automatic_replay(self):
        journal, _plan = self.prepared_transaction()
        result = coord.observe_space(journal, api=self.api, get=self.get, child_of=lambda *_: False)
        self.assertEqual(result['status'], 'parent'); self.assertFalse(result['journal_advanced'])
        self.assertEqual(tx.load(journal).record['state'], 'space-intent')
        self.api.create_commit.assert_not_called()

    def test_candidate_observation_requires_fresh_consumer_and_advances_journal(self):
        journal, planned = self.prepared_transaction(); self.activation_plan_cache = planned
        self.api.space_info.return_value = SimpleNamespace(sha=NEW_REV)
        self.api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': NEW_REV})
        activation = {'kind': 'ccrd-space-activation-observation', 'revision': NEW_REV,
                      'generation': self.plan_value['generation'], 'manifest_sha256': self.plan_value['manifest_sha256'],
                      'runtime_observed_twice': True}
        observed_consumer = {'generation': self.plan_value['generation'],
                             'cw0': {'bytes': len(NEW.encode()), 'sha256': self.description['new_text']['sha256']},
                             'cw0_bytes_verified': True, 'status_observed_twice': True,
                             'loaded_sources': ['CCRD', 'CW', 'PLA', 'RMRB']}
        result = coord.observe_space(journal, api=self.api, get=self.candidate_get, child_of=lambda *_: True,
                                     observe_activation=lambda *_args, **_kwargs: activation,
                                     observe_consumer=lambda *_args: observed_consumer)
        self.assertTrue(result['journal_advanced']); self.assertEqual(tx.load(journal).record['state'], 'space-verified')
        coord.start_pointer(journal); self.assertEqual(tx.load(journal).record['state'], 'pointer-intent')
        store = PointerMemory(self.pointer)
        parent = coord.observe_pointer(journal, store)
        self.assertEqual(parent['status'], 'parent'); self.assertFalse(parent['journal_advanced'])
        store.raw = ccrd_lifecycle.canonical(self.plan_value['manifest'])
        candidate = coord.observe_pointer(journal, store)
        self.assertEqual(candidate['status'], 'candidate'); self.assertTrue(candidate['journal_advanced'])
        self.assertEqual(tx.load(journal).record['state'], 'pointer-verified')
        self.api.create_commit.assert_not_called()

    def test_wrong_consumer_or_foreign_pointer_stops_before_journal_advance(self):
        journal, planned = self.prepared_transaction(); self.activation_plan_cache = planned
        self.api.space_info.return_value = SimpleNamespace(sha=NEW_REV)
        self.api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': NEW_REV})
        with self.assertRaisesRegex(ValueError, 'consumer did not load'):
            coord.observe_space(journal, api=self.api, get=self.candidate_get, child_of=lambda *_: True,
                                observe_activation=lambda *_a, **_k: {'kind': 'ccrd-space-activation-observation',
                                    'revision': NEW_REV, 'generation': self.plan_value['generation'],
                                    'manifest_sha256': self.plan_value['manifest_sha256'], 'runtime_observed_twice': True},
                                observe_consumer=lambda *_a: {'cw0': {}, 'cw0_bytes_verified': False})
        self.assertEqual(tx.load(journal).record['state'], 'space-intent')
        self.api.create_commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
