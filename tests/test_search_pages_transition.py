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

    def test_workflow_is_manual_and_serialized_with_daily(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/search-promotion-pages.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'daily-sync')
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['mode']['options'], ['prepare', 'apply', 'reconcile'])
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['known_writers_coordinated']['default'], 'false')


if __name__ == '__main__': unittest.main()
