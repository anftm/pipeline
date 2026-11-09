"""Recovery orchestration uses durable intents and independently observed bytes."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
                                description=self.description, activation_plan=planned,
                                pointer_store=PointerMemory(self.pointer), transaction='1' * 32)
        self.assertEqual(current.record['state'], 'prepared')
        coord.start_space(journal)
        return journal, planned

    def pointer_args(self):
        plan = self.plan_value
        source = {**plan['identity']['new_text'], 'source_stable': True,
                  'revision': plan['identity']['source_revision'], 'url': plan['identity']['source_url']}
        activation = {'kind': 'ccrd-space-activation-observation', 'revision': NEW_REV,
                      'generation': plan['generation'], 'manifest_sha256': plan['manifest_sha256'],
                      'runtime_observed_twice': True,
                      'activation': {'kind': 'ccrd-index-activation', 'bucket': ccrd_lifecycle.BUCKET,
                                     'path': f"generations/{plan['generation']}/manifest.json",
                                     'sha256': plan['manifest_sha256']}}
        consumer = {'generation': plan['generation'], 'cw0': plan['identity']['new_text'],
                    'cw0_bytes_verified': True, 'status_observed_twice': True,
                    'loaded_sources': ['CCRD', 'CW', 'PLA', 'RMRB']}
        self.store.generation = plan['generation']; self.store.raw_manifest = self.target_manifest
        return {'candidate_plan': plan, 'description': self.description, 'staging_store': self.store,
                'api': self.api, 'capture': lambda: source,
                'observe_activation': lambda *_a, **_k: activation, 'observe_consumer': lambda *_a: consumer}

    def test_prepare_checks_live_pointer_before_first_journal_write(self):
        planned = self.activation_plan()
        journal = MemoryStore()
        pointer = PointerMemory(b'foreign')
        with self.assertRaisesRegex(ValueError, 'live pointer differs'):
            coord.prepare(journal, parent_pointer=self.pointer, candidate_plan=self.plan_value,
                          description=self.description, activation_plan=planned,
                          pointer_store=pointer, transaction='1' * 32)
        self.assertIsNone(journal.head)
        self.assertEqual(pointer.writes, 0)
        pointer = PointerMemory(self.pointer)
        original = pointer.read_for_update
        calls = []
        def changing():
            calls.append(True)
            return original() if len(calls) == 1 else (self.pointer, '"changed"')
        pointer.read_for_update = changing
        with self.assertRaisesRegex(ValueError, 'pointer changed during first intent'):
            coord.prepare(journal, parent_pointer=self.pointer, candidate_plan=self.plan_value,
                          description=self.description, activation_plan=planned,
                          pointer_store=pointer, transaction='1' * 32)
        self.assertEqual(tx.load(journal).record['state'], 'prepared')
        self.assertEqual(pointer.writes, 0)

    def test_prepare_starts_successor_only_from_verified_rollback_parent(self):
        planned = self.activation_plan()
        journal = MemoryStore(); pointer = PointerMemory(self.pointer)
        first = coord.prepare(journal, parent_pointer=self.pointer, candidate_plan=self.plan_value,
                              description=self.description, activation_plan=planned,
                              pointer_store=pointer, transaction='1' * 32)
        first = tx.advance(journal, first, 'space-intent')
        first = tx.advance(journal, first, 'rollback-intent')
        from tests.test_ccrd_transaction import rollback_evidence
        evidence = rollback_evidence(parent_sha256=self.pointer_sha, candidate_generation=self.plan_value['generation'],
                                     space_plan_sha256=planned['plan_sha256'], revision=planned['parent_revision'])
        first = tx.advance(journal, first, 'rolled-back', space_revision=planned['parent_revision'], evidence=evidence)
        current = coord.prepare(journal, parent_pointer=self.pointer, candidate_plan=self.plan_value,
                                description=self.description, activation_plan=planned,
                                pointer_store=pointer, transaction='2' * 32)
        self.assertEqual(current.record['previous_transaction']['record'], first.head['record'])
        self.assertEqual(tx.audit(journal, current), 5)
        self.assertEqual(pointer.writes, 0)

    def test_space_commit_response_lost_is_reconciled_without_second_commit(self):
        journal, planned = self.prepared_transaction()
        self.api.create_commit.side_effect = TimeoutError('lost response')
        args = {'candidate_plan': self.plan_value, 'description': self.description, 'api': self.api,
                'staging_store': self.store, 'capture': self.capture, 'observe': lambda: self.observed,
                'candidate_root': self.output, 'new_text': NEW.encode(), 'current_manifest': self.pointer,
                'candidate_manifest': self.target_manifest, 'get': self.get}
        with self.assertRaises(TimeoutError):
            coord.apply_space_once(journal, **args)
        self.assertEqual(tx.load(journal).record['state'], 'space-claimed')
        with self.assertRaisesRegex(ValueError, 'expected state'):
            coord.apply_space_once(journal, **args)
        self.api.create_commit.assert_called_once()
        observed = coord.observe_space(journal, api=self.api, get=self.get, child_of=lambda *_: False)
        self.assertEqual(observed['status'], 'parent')
        self.assertFalse(observed['journal_advanced'])
        self.assertEqual(tx.load(journal).record['state'], 'space-claimed')

    def test_space_stale_claim_is_checked_at_commit_boundary(self):
        journal, _planned = self.prepared_transaction()
        args = {'candidate_plan': self.plan_value, 'description': self.description, 'api': self.api,
                'staging_store': self.store, 'capture': self.capture, 'observe': lambda: self.observed,
                'candidate_root': self.output, 'new_text': NEW.encode(), 'current_manifest': self.pointer,
                'candidate_manifest': self.target_manifest, 'get': self.get}
        with patch.object(coord.tx, 'check_write_claim', side_effect=ValueError('head changed')):
            with self.assertRaisesRegex(ValueError, 'head changed'):
                coord.apply_space_once(journal, **args)
        self.api.create_commit.assert_not_called()
        self.assertEqual(tx.load(journal).record['state'], 'space-claimed')

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
        pointer_args = self.pointer_args()
        parent = coord.observe_pointer(journal, store, **pointer_args)
        self.assertEqual(parent['status'], 'parent'); self.assertFalse(parent['journal_advanced'])
        store.raw = ccrd_lifecycle.canonical(self.plan_value['manifest'])
        candidate = coord.observe_pointer(journal, store, **pointer_args)
        self.assertEqual(candidate['status'], 'candidate'); self.assertTrue(candidate['journal_advanced'])
        self.assertEqual(tx.load(journal).record['state'], 'pointer-verified')
        self.api.create_commit.assert_not_called()

    def ready_for_pointer(self):
        journal, planned = self.prepared_transaction(); self.activation_plan_cache = planned
        self.api.space_info.return_value = SimpleNamespace(sha=NEW_REV)
        self.api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': NEW_REV})
        activation = self.pointer_args()['observe_activation']()
        consumed = self.pointer_args()['observe_consumer']()
        coord.observe_space(journal, api=self.api, get=self.candidate_get, child_of=lambda *_: True,
                            observe_activation=lambda *_a, **_k: activation,
                            observe_consumer=lambda *_a: consumed)
        coord.start_pointer(journal)
        return journal

    def test_pointer_cas_lost_response_claims_once_and_reconciles_live_evidence(self):
        journal = self.ready_for_pointer()
        store = PointerMemory(self.pointer); store.lost = True
        result = coord.apply_pointer_once(journal, store, **self.pointer_args())
        self.assertEqual(result['status'], 'written-response-unknown')
        self.assertEqual(store.writes, 1)
        self.assertEqual(tx.load(journal).record['state'], 'pointer-claimed')
        with self.assertRaisesRegex(ValueError, 'expected state'):
            coord.apply_pointer_once(journal, store, **self.pointer_args())
        done = coord.observe_pointer(journal, store, **self.pointer_args())
        self.assertTrue(done['journal_advanced'])
        self.assertEqual(tx.load(journal).record['state'], 'pointer-verified')
        self.assertEqual(store.writes, 1)

    def test_pointer_apply_requires_live_staged_manifest_before_claim_and_cas(self):
        journal = self.ready_for_pointer(); store = PointerMemory(self.pointer)
        args = self.pointer_args()
        self.store.raw_manifest = None
        with self.assertRaisesRegex(ValueError, 'missing or differs'):
            coord.apply_pointer_once(journal, store, **args)
        self.assertEqual(tx.load(journal).record['state'], 'pointer-intent')
        self.assertEqual(store.writes, 0)
        self.store.raw_manifest = self.target_manifest
        values = iter([self.target_manifest, b'changed'])
        self.store.read_manifest = lambda: next(values)
        with self.assertRaisesRegex(ValueError, 'missing or differs'):
            coord.apply_pointer_once(journal, store, **args)
        self.assertEqual(tx.load(journal).record['state'], 'pointer-claimed')
        self.assertEqual(store.writes, 0)

    def test_pointer_source_change_after_claim_stops_cas_and_requires_inspection(self):
        journal = self.ready_for_pointer(); store = PointerMemory(self.pointer)
        args = self.pointer_args(); source = args['capture']()
        observations = iter([source, source, {**source, 'revision': 'f' * 40}, {**source, 'revision': 'f' * 40}])
        args['capture'] = lambda: next(observations)
        with self.assertRaisesRegex(ValueError, 'lacks exact'):
            coord.apply_pointer_once(journal, store, **args)
        self.assertEqual(store.writes, 0)
        self.assertEqual(tx.load(journal).record['state'], 'pointer-claimed')
        result = coord.observe_pointer(journal, store, **self.pointer_args())
        self.assertEqual(result['status'], 'parent')
        self.assertFalse(result['journal_advanced'])
        with self.assertRaisesRegex(ValueError, 'expected state'):
            coord.apply_pointer_once(journal, store, **self.pointer_args())

    def test_pointer_candidate_reconciliation_rejects_stale_live_evidence(self):
        journal = self.ready_for_pointer(); store = PointerMemory(self.target_manifest)
        args = self.pointer_args(); original = args['observe_activation']()
        values = iter([original, {**original, 'revision': 'f' * 40}])
        args['observe_activation'] = lambda *_a, **_k: next(values)
        with self.assertRaisesRegex(ValueError, 'changed during'):
            coord.observe_pointer(journal, store, **args)
        self.assertEqual(tx.load(journal).record['state'], 'pointer-intent')
        self.assertEqual(store.writes, 0)

    def test_finish_requires_exact_live_manifest_pointer_source_and_consumer(self):
        journal = self.ready_for_pointer(); store = PointerMemory(self.pointer)
        args = self.pointer_args()
        coord.apply_pointer_once(journal, store, **args)
        coord.observe_pointer(journal, store, **args)
        self.assertEqual(tx.load(journal).record['state'], 'pointer-verified')
        self.store.generation = self.plan_value['generation']
        self.store.raw_manifest = self.target_manifest
        finish_args = {key: value for key, value in args.items() if key != 'immutable_manifest'}
        finish_args['staging_store'] = self.store
        first = coord.finish(journal, store, **finish_args)
        self.assertEqual(first.record['state'], 'complete')
        self.assertEqual(store.writes, 1)
        self.assertEqual(tx.audit(journal, first), 7)

    def test_finish_missing_manifest_foreign_generation_or_consumer_fails_closed(self):
        journal = self.ready_for_pointer(); store = PointerMemory(self.pointer)
        args = self.pointer_args()
        coord.apply_pointer_once(journal, store, **args)
        coord.observe_pointer(journal, store, **args)
        self.store.generation = 'ccrd-four-' + '0' * 64
        finish_args = {key: value for key, value in args.items() if key != 'immutable_manifest'}
        finish_args['staging_store'] = self.store
        with self.assertRaisesRegex(ValueError, 'different generation'):
            coord.finish(journal, store, **finish_args)
        self.store.generation = self.plan_value['generation']; self.store.raw_manifest = None
        with self.assertRaisesRegex(ValueError, 'missing or differs'):
            coord.finish(journal, store, **finish_args)
        self.store.raw_manifest = self.target_manifest
        finish_args['observe_consumer'] = lambda *_: {'generation': self.plan_value['generation']}
        with self.assertRaisesRegex(ValueError, 'lacks exact'):
            coord.finish(journal, store, **finish_args)
        self.assertEqual(tx.load(journal).record['state'], 'pointer-verified')
        self.assertEqual(store.writes, 1)

    def test_finish_detects_pointer_etag_change_and_does_not_advance(self):
        journal = self.ready_for_pointer(); store = PointerMemory(self.pointer)
        args = self.pointer_args()
        coord.apply_pointer_once(journal, store, **args)
        coord.observe_pointer(journal, store, **args)
        self.store.generation = self.plan_value['generation']; self.store.raw_manifest = self.target_manifest
        finish_args = {key: value for key, value in args.items() if key != 'immutable_manifest'}
        finish_args['staging_store'] = self.store
        original = store.read_for_update; count = []
        def drift():
            count.append(True)
            return original() if len(count) == 1 else (store.raw, '"foreign"')
        store.read_for_update = drift
        with self.assertRaisesRegex(ValueError, 'changed during completion'):
            coord.finish(journal, store, **finish_args)
        self.assertEqual(tx.load(journal).record['state'], 'pointer-verified')
        self.assertEqual(store.writes, 1)

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
