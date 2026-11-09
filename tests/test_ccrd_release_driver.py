"""Release driver never repeats a Space or pointer mutation after interruption."""
import unittest
from unittest.mock import Mock, patch

from scripts import ccrd_release_driver as driver
from scripts import ccrd_transaction as tx
from tests.test_ccrd_auto_activate import AutoActivateTests
from tests.test_ccrd_pointer_transition import PointerMemory
from tests.test_ccrd_transaction import MemoryStore


class ReleaseDriverTests(AutoActivateTests):
    def setup_transaction(self):
        from scripts import ccrd_publication_coordinator as coord
        bundle = self.bundle(); journal = MemoryStore(); pointer = PointerMemory(self.pointer)
        plan = self.activation_plan()
        c = coord.prepare(journal, parent_pointer=self.pointer, candidate_plan=self.plan_value,
                          description=self.description, activation_plan=plan, pointer_store=pointer, transaction='1' * 32)
        c = tx.advance(journal, c, 'space-intent')
        c = tx.claim_write(journal, c, operation='space')
        return bundle, journal, pointer, c

    def test_fresh_driver_finishes_only_after_each_observed_step(self):
        bundle, journal, pointer, _ = self.setup_transaction()
        activation = self.pointer_args()['observe_activation']()
        modes = []
        def step(mode, _transaction, **_common):
            modes.append(mode); current = tx.load(journal)
            if mode == 'observe-space':
                proof = tx.lifecycle.canonical({'kind': 'ccrd-activation-evidence', 'revision': activation['revision'],
                    'generation': current.record['candidate_generation'], 'manifest_sha256': current.record['candidate_manifest_sha256'],
                    'space_plan_sha256': current.record['activation_plan_sha256'], 'consumer_verified': True, 'activation_verified': True})
                tx.advance(journal, current, 'space-verified', space_revision=activation['revision'], evidence=proof)
                return {'result': {'journal_advanced': True}}
            if mode == 'start-pointer':
                tx.advance(journal, current, 'pointer-intent')
            elif mode == 'apply-pointer':
                tx.claim_write(journal, current, operation='pointer')
            elif mode == 'observe-pointer':
                tx.advance(journal, current, 'pointer-verified')
                return {'result': {'journal_advanced': True}}
            elif mode == 'finish':
                tx.advance(journal, current, 'complete')
            current = tx.load(journal)
            return {'state': current.record['state'], 'sequence': current.record['sequence'], 'result': {}}
        result = driver._run('fresh', '1' * 32, bundle=bundle, token='fixture', api=self.api,
                             journal=journal, pointer_store=pointer, timeout=60, poll=1,
                             sleep=lambda _: None, clock=lambda: 0, step=step)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(modes, ['observe-space', 'start-pointer', 'apply-pointer', 'observe-pointer', 'finish'])
        self.assertEqual(pointer.writes, 0)  # fake step deliberately never mutates an external pointer
        self.assertFalse(result['external_writers_verified'])

    def test_claimed_unknown_never_replays_and_timeout_preserves_state(self):
        bundle, journal, pointer, _ = self.setup_transaction(); calls = []; clock = iter([0, 0, 1, 3])
        def step(mode, *_a, **_k):
            calls.append(mode); return {'result': {'journal_advanced': False, 'status': 'parent'}}
        result = driver._run('resume', '1' * 32, bundle=bundle, token='fixture', api=self.api,
                             journal=journal, pointer_store=pointer, timeout=2, poll=1,
                             sleep=lambda _: None, clock=lambda: next(clock), step=step)
        self.assertEqual(result['status'], 'waiting'); self.assertEqual(result['state'], 'space-claimed')
        self.assertEqual(calls, ['observe-space', 'observe-space']); self.assertEqual(pointer.writes, 0)

    def test_resume_after_observed_space_can_continue_once(self):
        bundle, journal, pointer, c = self.setup_transaction()
        from tests.test_ccrd_transaction import CHILD
        proof = tx.lifecycle.canonical({'kind': 'ccrd-activation-evidence', 'revision': CHILD,
                    'generation': c.record['candidate_generation'], 'manifest_sha256': c.record['candidate_manifest_sha256'],
                    'space_plan_sha256': c.record['activation_plan_sha256'], 'consumer_verified': True, 'activation_verified': True})
        tx.advance(journal, c, 'space-verified', space_revision=CHILD, evidence=proof)
        calls = []
        def step(mode, *_a, **_k):
            calls.append(mode); current = tx.load(journal)
            if mode == 'start-pointer': tx.advance(journal, current, 'pointer-intent')
            elif mode == 'apply-pointer': tx.claim_write(journal, current, operation='pointer')
            elif mode == 'observe-pointer':
                tx.advance(journal, current, 'pointer-verified'); return {'result': {'journal_advanced': True}}
            elif mode == 'finish': tx.advance(journal, current, 'complete')
            current = tx.load(journal)
            return {'state': current.record['state'], 'sequence': current.record['sequence'], 'result': {}}
        result = driver.resume(bundle, '1' * 32, token='fixture', api=self.api, journal=journal,
                               pointer_store=pointer, timeout=60, poll=1, sleep=lambda _: None,
                               clock=lambda: 0, step=step)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(calls, ['start-pointer', 'apply-pointer', 'observe-pointer', 'finish'])
        self.assertEqual(pointer.writes, 0)

    def test_unowned_pointer_intent_never_replays(self):
        bundle, journal, pointer, c = self.setup_transaction()
        from tests.test_ccrd_transaction import CHILD
        proof = tx.lifecycle.canonical({'kind': 'ccrd-activation-evidence', 'revision': CHILD,
                    'generation': c.record['candidate_generation'], 'manifest_sha256': c.record['candidate_manifest_sha256'],
                    'space_plan_sha256': c.record['activation_plan_sha256'], 'consumer_verified': True, 'activation_verified': True})
        c = tx.advance(journal, c, 'space-verified', space_revision=CHILD, evidence=proof)
        tx.advance(journal, c, 'pointer-intent')
        step = Mock(side_effect=AssertionError('must not issue a pointer request'))
        result = driver._run('resume', '1' * 32, bundle=bundle, token='fixture', api=self.api,
                             journal=journal, pointer_store=pointer, timeout=10, poll=1,
                             sleep=lambda _: None, clock=lambda: 0, step=step)
        self.assertEqual(result['status'], 'manual-recovery'); self.assertEqual(result['state'], 'pointer-intent')
        step.assert_not_called(); self.assertEqual(pointer.writes, 0)

    def test_already_complete_journal_is_not_fresh_live_acceptance(self):
        bundle, journal, pointer, c = self.setup_transaction()
        from tests.test_ccrd_transaction import CHILD
        proof = tx.lifecycle.canonical({'kind': 'ccrd-activation-evidence', 'revision': CHILD,
                    'generation': c.record['candidate_generation'], 'manifest_sha256': c.record['candidate_manifest_sha256'],
                    'space_plan_sha256': c.record['activation_plan_sha256'], 'consumer_verified': True, 'activation_verified': True})
        c = tx.advance(journal, c, 'space-verified', space_revision=CHILD, evidence=proof)
        c = tx.advance(journal, c, 'pointer-intent'); c = tx.claim_write(journal, c, operation='pointer')
        c = tx.advance(journal, c, 'pointer-verified'); tx.advance(journal, c, 'complete')
        result = driver.resume(bundle, '1' * 32, token='fixture', api=self.api, journal=journal,
                               pointer_store=pointer, timeout=60, poll=1, sleep=lambda _: None,
                               clock=lambda: 0, step=Mock())
        self.assertEqual(result['status'], 'already-complete-unchecked')
        self.assertEqual(pointer.writes, 0)

    def test_workflow_keeps_explicit_writer_coordination_and_single_staged_run(self):
        import yaml
        from pathlib import Path
        flow = yaml.load((Path(__file__).resolve().parents[1] / '.github/workflows/ccrd-activate-staged.yml').read_text(),
                         Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'publish-ccrd-index')
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['known_writers_coordinated']['default'], 'false')
        commands = '\n'.join(step.get('run', '') for step in flow['jobs']['activate']['steps'])
        self.assertIn('scripts/ccrd_release_driver.py', commands)
        self.assertNotIn('git push', commands)

    def test_interrupted_activation_does_not_start_next_actions(self):
        bundle, journal, pointer, _ = self.setup_transaction(); step = Mock()
        with self.assertRaises(TimeoutError):
            driver.start(bundle, metadata_root=self.root / 'metadata', token='fixture', api=self.api,
                         journal=journal, pointer_store=pointer, activate=Mock(side_effect=TimeoutError('unknown')), step=step)
        step.assert_not_called(); self.assertEqual(tx.load(journal).record['state'], 'space-claimed')


if __name__ == '__main__':
    unittest.main()
