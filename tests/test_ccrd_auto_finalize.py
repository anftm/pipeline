"""Finalization is one explicit action bound to the durable transaction."""
import unittest
from unittest.mock import Mock, patch

from scripts import ccrd_auto_finalize as subject
from scripts import ccrd_publication_coordinator as coordinator
from tests.test_ccrd_auto_activate import AutoActivateTests
from tests.test_ccrd_pointer_transition import PointerMemory
from tests.test_ccrd_transaction import MemoryStore


class AutoFinalizeTests(AutoActivateTests):
    def prepared(self):
        bundle = self.bundle(); journal = MemoryStore(); pointer = PointerMemory(self.pointer)
        plan = self.activation_plan()
        current = coordinator.prepare(journal, parent_pointer=self.pointer, candidate_plan=self.plan_value,
                                      description=self.description, activation_plan=plan,
                                      pointer_store=pointer, transaction='1' * 32)
        return bundle, journal, pointer, current

    def test_exact_transaction_and_bundle_required_before_action(self):
        bundle, journal, pointer, _ = self.prepared()
        with patch.object(subject.coordinator, 'start_pointer') as action:
            with self.assertRaisesRegex(ValueError, 'does not match requested'):
                subject.step('start-pointer', '2' * 32, bundle=bundle, token='fixture', api=self.api,
                             journal=journal, pointer_store=pointer)
            action.assert_not_called()
        with self.assertRaises(ValueError):
            subject.step('unknown', '1' * 32, bundle=bundle, token='fixture', api=self.api,
                         journal=journal, pointer_store=pointer)
        self.assertEqual(pointer.writes, 0)

    def test_finalize_workflow_is_explicit_and_pinned_to_original_stage_run(self):
        import yaml
        from pathlib import Path
        flow = yaml.load((Path(__file__).resolve().parents[1] / '.github/workflows/ccrd-finalize-staged.yml').read_text(),
                         Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'publish-ccrd-index')
        inputs = flow['on']['workflow_dispatch']['inputs']
        self.assertEqual(inputs['mode']['options'], list(subject.MODES))
        self.assertEqual(inputs['known_writers_coordinated']['default'], 'false')
        commands = '\n'.join(step.get('run', '') for step in flow['jobs']['reconcile']['steps'])
        self.assertIn("run.get('head_sha') != os.environ['STAGE_SHA']", commands)
        self.assertIn('scripts/ccrd_auto_finalize.py', commands)
        self.assertNotIn('git push', commands)

    def test_space_acceptance_requires_unchanged_parent_pointer(self):
        bundle, journal, pointer, _ = self.prepared()
        pointer.raw = b'foreign'
        with patch.object(subject.coordinator, 'observe_space') as observe:
            with self.assertRaisesRegex(ValueError, 'not the exact parent'):
                subject.step('observe-space', '1' * 32, bundle=bundle, token='fixture', api=self.api,
                             journal=journal, pointer_store=pointer, store_factory=lambda **_k: Mock())
            observe.assert_not_called()
        self.assertEqual(pointer.writes, 0)

    def test_each_mode_invokes_only_its_named_action(self):
        bundle, journal, pointer, current = self.prepared()
        actions = {'observe-space': ('coordinator', 'observe_space'), 'start-pointer': ('coordinator', 'start_pointer'),
                   'apply-pointer': ('coordinator', 'apply_pointer_once'), 'observe-pointer': ('coordinator', 'observe_pointer'),
                   'finish': ('coordinator', 'finish'), 'rollback-start': ('rollback', 'start'),
                   'rollback-apply': ('rollback', 'apply_once'), 'rollback-observe': ('rollback', 'observe')}
        for mode, (owner, name) in actions.items():
            with self.subTest(mode=mode), patch.object(getattr(subject, owner), name) as called:
                called.return_value = current if mode in {'start-pointer', 'finish', 'rollback-start'} else {'status': 'observed'}
                result = subject.step(mode, '1' * 32, bundle=bundle, token='fixture', api=self.api,
                                      journal=journal, pointer_store=pointer, store_factory=lambda **_k: Mock())
                called.assert_called_once(); self.assertEqual(result['mode'], mode)
                self.assertFalse(result['external_writers_verified'])
        self.assertEqual(pointer.writes, 0)


if __name__ == '__main__':
    unittest.main()
