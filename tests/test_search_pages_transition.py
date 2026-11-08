"""Pages search rollout separates intent, run and actual deployed bytes."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import search_pages_transition as subject
from tests.test_search_transition import A, B, C, MemoryStore, default_receipt, receipt

REV, TIME, OP = 'd' * 40, '2026-11-08T00:00:00Z', '3' * 32


class PagesTransitionTests(unittest.TestCase):
    def setUp(self):
        self.s = MemoryStore()
        current = subject.tx.begin(self.s, parent_pointer=self.s.pointer, candidate=B, source_revisions_sha256=C,
                                   pages_receipt=receipt('pages', A), space_receipt=receipt('space', A),
                                   pages_default=default_receipt('pages', A), space_default=default_receipt('space', A),
                                   transition='1' * 32)
        current = subject.tx.start_site(self.s, current, site='space', operation_id='4' * 32,
                                        expected_revision=REV, created_at=TIME,
                                        config_before={'generation': A, 'updated_at': TIME})
        subject.tx.observe_site(self.s, current, site='space', generation=B, receipt=receipt('space', B))
        self.session, self.api, self.hub = Mock(), Mock(), Mock()
        self.patches = [patch.object(subject, '_main', return_value=REV),
                        patch.object(subject, '_space_loaded'), patch.object(subject.deploy, 'assert_idle'),
                        patch.object(subject.deploy, 'failed_before_deploy'),
                        patch.object(subject.defaults, 'observe_pages', return_value=default_receipt('pages', A)),
                        patch.object(subject.adoption, 'validate_generation', return_value={}),
                        patch.object(subject.receipts, 'observe', side_effect=lambda site, generation, *_a, **_k: receipt(site, generation))]
        for item in self.patches: item.start(); self.addCleanup(item.stop)

    def prepare_pages(self):
        return subject.prepare(self.s, session=self.session, api=self.api, hub=self.hub, get=lambda *_: b'',
                               expected_revision=REV, operation_id=OP, created_at=TIME)

    def test_prepare_and_dispatch_require_exact_durable_intent(self):
        current = self.prepare_pages()
        self.assertEqual(current.record['pending']['operation_id'], OP)
        with patch.object(subject.deploy, 'inspect_runs', return_value=None), patch.object(subject.deploy, 'dispatch') as send:
            result = subject.apply_once(self.s, session=self.session, api=self.api, hub=self.hub,
                                        get=lambda *_: b'', operation_id=OP)
        self.assertTrue(result['dispatch_confirmed']); self.assertFalse(result['loaded_verified'])
        send.assert_called_once(); self.assertEqual(self.s.pointer, self.s.read_pointer())
        with patch.object(subject.deploy, 'inspect_runs', return_value={'id': 4}), patch.object(subject.deploy, 'dispatch') as send:
            with self.assertRaisesRegex(ValueError, 'reconcile instead'):
                subject.apply_once(self.s, session=self.session, api=self.api, hub=self.hub,
                                   get=lambda *_: b'', operation_id=OP)
            send.assert_not_called()

    def test_lost_dispatch_is_not_replayed_when_run_is_not_yet_visible(self):
        self.prepare_pages()
        with patch.object(subject.deploy, 'inspect_runs', return_value=None), \
                patch.object(subject.deploy, 'dispatch', side_effect=TimeoutError('response lost')) as send:
            with self.assertRaises(TimeoutError):
                subject.apply_once(self.s, session=self.session, api=self.api, hub=self.hub,
                                   get=lambda *_: b'', operation_id=OP)
            with self.assertRaisesRegex(ValueError, 'already claimed'):
                subject.apply_once(self.s, session=self.session, api=self.api, hub=self.hub,
                                   get=lambda *_: b'', operation_id=OP)
            result = subject.reconcile(self.s, session=self.session, api=self.api, hub=self.hub,
                                       get=lambda *_: b'', operation_id=OP)
        send.assert_called_once()
        self.assertEqual(result['status'], 'run-not-visible')
        self.assertFalse(result['journal_advanced'])
        self.assertIsNotNone(subject.tx.load(self.s).record['pending']['write_claim'])

    def test_lost_dispatch_followed_by_exact_live_success_reconciles_without_a_second_send(self):
        self.prepare_pages()
        with patch.object(subject.deploy, 'inspect_runs', return_value=None), \
                patch.object(subject.deploy, 'dispatch', side_effect=TimeoutError('response lost')) as send:
            with self.assertRaises(TimeoutError):
                subject.apply_once(self.s, session=self.session, api=self.api, hub=self.hub,
                                   get=lambda *_: b'', operation_id=OP)
        run = {'id': 7, 'status': 'completed', 'conclusion': 'success'}
        with patch.object(subject.deploy, 'inspect_runs', return_value=run):
            result = subject.reconcile(self.s, session=self.session, api=self.api, hub=self.hub,
                                       get=lambda *_: b'', operation_id=OP)
        send.assert_called_once()
        self.assertEqual(result['status'], 'candidate-loaded')
        self.assertEqual(subject.tx.load(self.s).record['state'], 'verified')

    def test_reconcile_waits_for_actual_success_and_fresh_pages_bytes(self):
        self.prepare_pages()
        with patch.object(subject.deploy, 'inspect_runs', return_value=None):
            self.assertEqual(subject.reconcile(self.s, session=self.session, api=self.api, hub=self.hub,
                                               get=lambda *_: b'', operation_id=OP)['status'], 'run-not-visible')
        run = {'id': 7, 'status': 'completed', 'conclusion': 'success'}
        with patch.object(subject.deploy, 'inspect_runs', return_value=run):
            result = subject.reconcile(self.s, session=self.session, api=self.api, hub=self.hub,
                                       get=lambda *_: b'', operation_id=OP)
        self.assertEqual(result['status'], 'candidate-loaded'); self.assertTrue(result['journal_advanced'])
        self.assertEqual(subject.tx.load(self.s).record['state'], 'verified')
        self.assertEqual(subject.tx.load(self.s).record['observed']['pages']['generation'], B)

    def test_failed_run_changed_main_or_unknown_space_cannot_advance(self):
        self.prepare_pages()
        with patch.object(subject.deploy, 'inspect_runs', return_value={'id': 7, 'status': 'completed', 'conclusion': 'failure'}), \
                self.assertRaisesRegex(ValueError, 'deployment failed'):
            subject.reconcile(self.s, session=self.session, api=self.api, hub=self.hub,
                              get=lambda *_: b'', operation_id=OP)
        self.assertEqual(subject.tx.load(self.s).record['state'], 'applying')

    def test_failed_apply_requires_exact_terminal_run_and_live_parent(self):
        self.prepare_pages()
        run = {'id': 7, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        for invalid in (None, {'id': 7, 'status': 'in_progress', 'conclusion': None},
                        {'id': 7, 'status': 'completed', 'conclusion': 'success'},
                        {'id': 7, 'status': 'completed', 'conclusion': 'action_required'}):
            with self.subTest(invalid=invalid), patch.object(subject.deploy, 'inspect_runs', return_value=invalid):
                with self.assertRaisesRegex(ValueError, 'terminal failed'):
                    subject.recover_failed_apply(self.s, session=self.session, hub=self.hub,
                                                 get=lambda *_: b'', operation_id=OP)
        with patch.object(subject.deploy, 'inspect_runs', return_value=run), \
                patch.object(subject.receipts, 'observe', side_effect=ValueError('not parent')):
            with self.assertRaisesRegex(ValueError, 'not parent'):
                subject.recover_failed_apply(self.s, session=self.session, hub=self.hub,
                                             get=lambda *_: b'', operation_id=OP)
        self.assertEqual(subject.tx.load(self.s).record['pending']['operation_id'], OP)
        with patch.object(subject.deploy, 'inspect_runs', return_value=run), \
                patch.object(subject.deploy, 'failed_before_deploy', side_effect=ValueError('deploy may have started')):
            with self.assertRaisesRegex(ValueError, 'deploy may have started'):
                subject.recover_failed_apply(self.s, session=self.session, hub=self.hub,
                                             get=lambda *_: b'', operation_id=OP)
        self.assertEqual(subject.tx.load(self.s).record['pending']['operation_id'], OP)
        with patch.object(subject.deploy, 'inspect_runs', return_value=run):
            result = subject.recover_failed_apply(self.s, session=self.session, hub=self.hub,
                                                   get=lambda *_: b'', operation_id=OP)
        self.assertEqual(result['status'], 'parent-loaded')
        self.assertFalse(result['external_writers_verified'])
        current = subject.tx.load(self.s)
        self.assertIsNone(current.record['pending'])
        self.assertEqual(current.record['state'], 'rolling-back')
        self.assertEqual(current.record['observed']['space']['generation'], B)
        self.assertGreater(subject.tx.audit_history(self.s, current), 1)
        self.session.post.assert_not_called()

    def test_failed_apply_changed_run_or_pointer_never_clears_pending(self):
        self.prepare_pages()
        run = {'id': 7, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        changed = {**run, 'run_attempt': 2}
        with patch.object(subject.deploy, 'inspect_runs', side_effect=[run, changed]):
            with self.assertRaisesRegex(ValueError, 'run changed'):
                subject.recover_failed_apply(self.s, session=self.session, hub=self.hub,
                                             get=lambda *_: b'', operation_id=OP)
        self.assertIsNotNone(subject.tx.load(self.s).record['pending'])
        from scripts.search_publication_pointer import make_pointer
        self.s.pointer = make_pointer(C, A, '2' * 32)
        with self.assertRaisesRegex(ValueError, 'pointer changed'):
            subject.recover_failed_apply(self.s, session=self.session, hub=self.hub,
                                         get=lambda *_: b'', operation_id=OP)
        self.assertIsNotNone(subject.tx.load(self.s).record['pending'])

    def test_failed_apply_does_not_require_healthy_space_for_parent_acceptance(self):
        self.prepare_pages()
        run = {'id': 7, 'status': 'completed', 'conclusion': 'failure', 'run_attempt': 1}
        with patch.object(subject.deploy, 'inspect_runs', return_value=run), \
                patch.object(subject, '_space_loaded', side_effect=AssertionError('Space is unavailable')):
            result = subject.recover_failed_apply(self.s, session=self.session, hub=self.hub,
                                                   get=lambda *_: b'', operation_id=OP)
        self.assertEqual(result['status'], 'parent-loaded')
        self.assertEqual(subject.tx.load(self.s).record['state'], 'rolling-back')

    def test_pages_rollback_uses_parent_and_no_space_dependency(self):
        self.prepare_pages()
        run = {'id': 7, 'status': 'completed', 'conclusion': 'success'}
        with patch.object(subject.deploy, 'inspect_runs', return_value=run):
            subject.reconcile(self.s, session=self.session, api=self.api, hub=self.hub,
                              get=lambda *_: b'', operation_id=OP)
        with patch.object(subject, '_space_loaded', side_effect=AssertionError('should not block recovery')):
            c = subject.prepare(self.s, session=self.session, api=self.api, hub=self.hub, get=lambda *_: b'',
                                expected_revision=REV, operation_id='5' * 32, created_at=TIME, rollback=True)
            self.assertEqual(c.record['pending']['operation'], 'rollback')
            with patch.object(subject.deploy, 'inspect_runs', return_value=None), \
                    patch.object(subject.deploy, 'dispatch', side_effect=TimeoutError('lost')) as send:
                with self.assertRaises(TimeoutError):
                    subject.apply_once(self.s, session=self.session, api=self.api, hub=self.hub,
                                       get=lambda *_: b'', operation_id='5' * 32)
                self.assertEqual(send.call_args.kwargs['generation'], A)
                with self.assertRaisesRegex(ValueError, 'already claimed'):
                    subject.apply_once(self.s, session=self.session, api=self.api, hub=self.hub,
                                       get=lambda *_: b'', operation_id='5' * 32)
                send.assert_called_once()
            with patch.object(subject.deploy, 'inspect_runs', return_value=run):
                result = subject.reconcile(self.s, session=self.session, api=self.api, hub=self.hub,
                                           get=lambda *_: b'', operation_id='5' * 32)
        self.assertEqual(result['status'], 'parent-loaded')
        self.assertEqual(subject.tx.load(self.s).record['state'], 'rolling-back')
        self.assertEqual(subject.tx.load(self.s).record['observed']['pages']['generation'], A)

    def test_workflow_is_manual_and_serialized_with_daily(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/search-promotion-pages.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'daily-sync')
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['mode']['options'],
                         ['prepare', 'rollback-prepare', 'apply', 'reconcile', 'recover-failed-apply'])
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['known_writers_coordinated']['default'], 'false')


if __name__ == '__main__': unittest.main()
