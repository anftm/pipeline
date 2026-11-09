"""Space promotion intent, one-shot mutation and exact loaded-data recovery."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from scripts import search_space_transition as subject
from scripts.search_publication_pointer import make_pointer
from tests.test_search_transition import A, B, SearchTransitionTests, receipt

REV, TIME = 'd' * 40, '2026-11-08T00:00:00Z'


class SearchSpaceTransitionTests(SearchTransitionTests):
    def setUp(self):
        super().setUp()
        self.source_check = patch.object(subject, '_verify_candidate_sources')
        self.source_check.start();self.addCleanup(self.source_check.stop)
        self.manifest_check = patch.object(subject.adoption, 'validate_generation', return_value={})
        self.manifest_check.start();self.addCleanup(self.manifest_check.stop)

    def prepare_space(self):
        current = self.begin(); api = Mock()
        with patch.object(subject.deploy, 'runtime', return_value={'sha': REV, 'stage': 'RUNNING'}), \
                patch.object(subject.deploy, 'variable', return_value=(A, TIME)):
            current = subject.prepare(self.s, api=api, expected_revision=REV, operation_id='3' * 32, created_at=TIME)
        return current, api

    def test_workflow_requires_explicit_modes_and_known_writer_coordination(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/search-promotion-space.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'daily-sync')
        inputs = flow['on']['workflow_dispatch']['inputs']
        self.assertEqual(inputs['mode']['options'], ['prepare', 'rollback-prepare', 'apply', 'reconcile'])
        self.assertEqual(inputs['known_writers_coordinated']['default'], 'false')
        commands = '\n'.join(step.get('run', '') for step in flow['jobs']['switch']['steps'])
        self.assertIn('[[ "$WRITERS" == \'true\' ]]', commands)
        self.assertIn('scripts/search_space_transition.py', commands)
        self.assertNotIn('sync_to_space.py', commands)

    def test_prepare_records_exact_space_revision_and_prior_variable(self):
        current, _api = self.prepare_space()
        self.assertEqual(current.record['pending']['operation_id'], '3' * 32)
        self.assertEqual(current.record['pending']['expected_revision'], REV)
        self.assertEqual(current.record['pending']['config_before'], {'generation': A, 'updated_at': TIME})
        self.assertEqual(self.s.pointer, make_pointer(A, None, '0' * 32))

    def test_one_shot_apply_requires_exact_pending_intent(self):
        _current, api = self.prepare_space()
        with patch.object(subject.deploy, 'set_generation') as write:
            result = subject.apply_once(self.s, api=api, operation_id='3' * 32, hub=Mock())
        self.assertTrue(result['variable_written']); self.assertFalse(result['loaded_verified']); write.assert_called_once()
        with patch.object(subject.deploy, 'set_generation') as write:
            with self.assertRaises(ValueError): subject.apply_once(self.s, api=api, operation_id='4' * 32, hub=Mock())
            write.assert_not_called()

    def test_lost_space_response_is_never_resent_by_a_second_invocation(self):
        _current, api = self.prepare_space()
        with patch.object(subject.deploy, 'set_generation', side_effect=TimeoutError('response lost')) as write:
            with self.assertRaises(TimeoutError):
                subject.apply_once(self.s, api=api, operation_id='3' * 32, hub=Mock())
            with self.assertRaisesRegex(ValueError, 'already claimed'):
                subject.apply_once(self.s, api=api, operation_id='3' * 32, hub=Mock())
        write.assert_called_once()
        with patch.object(subject.deploy, 'variable', return_value=(A, TIME)):
            observed = subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='3' * 32)
        self.assertFalse(observed['journal_advanced'])
        self.assertIsNotNone(subject.tx.load(self.s).record['pending']['write_claim'])

    def test_lost_space_response_reconciles_loaded_candidate_without_retry(self):
        _current, api = self.prepare_space()
        with patch.object(subject.deploy, 'set_generation', side_effect=TimeoutError('response lost')) as send:
            with self.assertRaises(TimeoutError):
                subject.apply_once(self.s, api=api, operation_id='3' * 32, hub=Mock())
        api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': REV})
        with patch.object(subject.deploy, 'variable', return_value=(B, TIME)), \
                patch.object(subject.deploy, 'runtime', return_value={'sha': REV}), \
                patch.object(subject.adoption, 'validate_generation', return_value={'generation': B}), \
                patch.object(subject.receipts, 'observe', return_value=receipt('space', B)):
            result = subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='3' * 32)
        send.assert_called_once()
        self.assertEqual(result['status'], 'candidate-loaded')
        self.assertIsNone(subject.tx.load(self.s).record['pending'])

    def test_concurrent_apply_workers_cannot_both_send_the_write(self):
        _current, api = self.prepare_space()
        barrier, lock = threading.Barrier(2, timeout=5), threading.Lock()
        write_head = self.s.write_head

        def cas(raw, etag):
            barrier.wait()
            with lock:
                return write_head(raw, etag)

        def run():
            try:
                return subject.apply_once(self.s, api=api, operation_id='3' * 32, hub=Mock())
            except Exception as error:
                return error

        with patch.object(self.s, 'write_head', side_effect=cas), \
                patch.object(subject.deploy, 'set_generation') as send, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(sum(isinstance(item, ValueError) for item in results), 1)
        send.assert_called_once()
        self.assertIsNotNone(subject.tx.load(self.s).record['pending']['write_claim'])

    def test_crash_after_claim_before_network_call_leaves_unknown_and_never_replays(self):
        _current, api = self.prepare_space()
        with patch.object(subject.tx, 'check_write_claim', side_effect=RuntimeError('crash')), \
                patch.object(subject.deploy, 'set_generation') as send:
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                subject.apply_once(self.s, api=api, operation_id='3' * 32, hub=Mock())
            send.assert_not_called()
        with patch.object(subject.deploy, 'set_generation') as send:
            with self.assertRaisesRegex(ValueError, 'already claimed'):
                subject.apply_once(self.s, api=api, operation_id='3' * 32, hub=Mock())
            send.assert_not_called()
        self.assertIsNotNone(subject.tx.load(self.s).record['pending']['write_claim'])

    def test_candidate_source_drift_or_missing_objects_never_claims_or_writes(self):
        _current, api = self.prepare_space()
        with patch.object(subject.adoption, 'validate_generation', side_effect=ValueError('object missing')), \
                patch.object(subject.deploy, 'set_generation') as write:
            with self.assertRaisesRegex(ValueError, 'object missing'):
                subject.apply_once(self.s, api=api, operation_id='3' * 32, hub=Mock())
            write.assert_not_called()
        with patch.object(subject, '_verify_candidate_sources', side_effect=ValueError('source changed')), \
                patch.object(subject.deploy, 'set_generation') as write:
            with self.assertRaisesRegex(ValueError, 'source changed'):
                subject.apply_once(self.s, api=api, operation_id='3' * 32, hub=Mock())
            write.assert_not_called()
        self.assertIsNone(subject.tx.load(self.s).record['pending']['write_claim'])

    def test_exact_source_revision_digest_and_live_heads_are_required(self):
        import hashlib
        self.source_check.stop()
        revisions = {repo: 'a' * 40 for repo in subject.fetch.REPOS}
        expected = hashlib.sha256(subject.search_snapshot.canonical(revisions)).hexdigest()
        with patch.object(subject.fetch, 'get_repo_sha', return_value='a' * 40):
            self.assertIsNone(subject._verify_candidate_sources({'source_revisions': revisions}, expected))
            with self.assertRaisesRegex(ValueError, 'durable intent'):
                subject._verify_candidate_sources({'source_revisions': revisions}, 'f' * 64)
            with self.assertRaisesRegex(ValueError, 'durable intent'):
                subject._verify_candidate_sources({'source_revisions': {}}, expected)
        with patch.object(subject.fetch, 'get_repo_sha', return_value='b' * 40):
            with self.assertRaisesRegex(ValueError, 'sources changed'):
                subject._verify_candidate_sources({'source_revisions': revisions}, expected)

    def test_pending_parent_or_building_does_not_claim_loaded(self):
        _current, api = self.prepare_space()
        with patch.object(subject.deploy, 'variable', return_value=(A, TIME)):
            result = subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='3' * 32)
        self.assertEqual(result['status'], 'parent-configured'); self.assertFalse(result['journal_advanced'])
        api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING_BUILDING', raw={'sha': REV})
        with patch.object(subject.deploy, 'variable', return_value=(B, TIME)):
            result = subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='3' * 32)
        self.assertEqual(result['status'], 'building'); self.assertFalse(result['journal_advanced'])

    def test_actual_candidate_receipt_advances_only_after_repeated_observation(self):
        _current, api = self.prepare_space()
        api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': REV})
        with patch.object(subject.deploy, 'variable', return_value=(B, TIME)), \
                patch.object(subject.deploy, 'runtime', return_value={'sha': REV}), \
                patch.object(subject.adoption, 'validate_generation', return_value={'generation': B}), \
                patch.object(subject.receipts, 'observe', return_value=receipt('space', B)):
            result = subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='3' * 32)
        self.assertEqual(result['status'], 'candidate-loaded'); self.assertTrue(result['journal_advanced'])
        self.assertEqual(subject.tx.load(self.s).record['observed']['space']['generation'], B)

    def test_space_rollback_claims_once_and_accepts_only_loaded_parent(self):
        _current, api = self.prepare_space()
        api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': REV})
        with patch.object(subject.deploy, 'variable', return_value=(B, TIME)), \
                patch.object(subject.deploy, 'runtime', return_value={'sha': REV}), \
                patch.object(subject.adoption, 'validate_generation', return_value={'generation': B}), \
                patch.object(subject.receipts, 'observe', return_value=receipt('space', B)):
            subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='3' * 32)
            c = subject.prepare(self.s, api=api, expected_revision=REV, operation_id='5' * 32,
                                created_at=TIME, rollback=True, hub=Mock(), get=lambda *_: b'')
        self.assertEqual(c.record['pending']['operation'], 'rollback')
        with patch.object(subject.deploy, 'set_generation', side_effect=TimeoutError('lost')) as send, \
                patch.object(subject.adoption, 'validate_generation', return_value={'generation': A}):
            with self.assertRaises(TimeoutError):
                subject.apply_once(self.s, api=api, operation_id='5' * 32, hub=Mock())
            self.assertEqual(send.call_args.kwargs['target'], A)
            with self.assertRaisesRegex(ValueError, 'already claimed'):
                subject.apply_once(self.s, api=api, operation_id='5' * 32, hub=Mock())
            send.assert_called_once()
        with patch.object(subject.deploy, 'variable', return_value=(B, TIME)):
            result = subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='5' * 32)
        self.assertEqual(result['status'], 'candidate-configured')
        self.assertFalse(result['journal_advanced'])
        with patch.object(subject.deploy, 'variable', return_value=(A, TIME)), \
                patch.object(subject.deploy, 'runtime', return_value={'sha': REV}), \
                patch.object(subject.adoption, 'validate_generation', return_value={'generation': A}), \
                patch.object(subject.receipts, 'observe', return_value=receipt('space', A)):
            result = subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='5' * 32)
        self.assertEqual(result['status'], 'parent-loaded')
        self.assertEqual(subject.tx.load(self.s).record['state'], 'rolling-back')
        self.assertEqual(subject.tx.load(self.s).record['observed']['space']['generation'], A)

    def test_rollback_preparation_rejects_missing_target_or_source_evidence(self):
        c = self.begin()
        c = self.start_site(c, site='space', operation_id='3' * 32)
        self.observe(c, 'space', B, b'candidate')
        api = Mock()
        with patch.object(subject.deploy, 'runtime'), \
                patch.object(subject.deploy, 'variable', return_value=(B, TIME)):
            with self.assertRaisesRegex(ValueError, 'fresh generation'):
                subject.prepare(self.s, api=api, expected_revision=REV, operation_id='5' * 32,
                                created_at=TIME, rollback=True)
            with patch.object(subject.adoption, 'validate_generation', side_effect=ValueError('object missing')):
                with self.assertRaisesRegex(ValueError, 'object missing'):
                    subject.prepare(self.s, api=api, expected_revision=REV, operation_id='5' * 32,
                                    created_at=TIME, rollback=True, hub=Mock(), get=lambda *_: b'')
        self.assertIsNone(subject.tx.load(self.s).record['pending'])

    def test_foreign_state_revision_or_pointer_cannot_advance(self):
        _current, api = self.prepare_space(); api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': 'f' * 40})
        with patch.object(subject.deploy, 'variable', return_value=(B, TIME)), self.assertRaisesRegex(ValueError, 'unexpected revision'):
            subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='3' * 32)
        self.s.pointer = make_pointer('f' * 64, A, '2' * 32)
        with self.assertRaisesRegex(ValueError, 'pointer changed'):
            subject.reconcile(self.s, api=api, hub=Mock(), get=lambda *_: b'', operation_id='3' * 32)


if __name__ == '__main__': unittest.main()
