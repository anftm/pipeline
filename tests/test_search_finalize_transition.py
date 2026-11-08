"""Search finalization never treats one site's success as the other site's receipt."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import search_finalize_transition as subject
from scripts.search_publication_pointer import make_pointer
from tests.test_search_transition import (
    A,
    B,
    SearchTransitionTests,
    default_receipt,
    receipt,
)

REV, TIME, OP = 'd' * 40, '2026-11-08T00:00:00Z', '9' * 32


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        fixture = SearchTransitionTests(); fixture.setUp(); self.store = fixture.s
        self.current = fixture.ready(); self.session, self.api, self.hub = Mock(), Mock(), Mock()
        self.common = {'hub': self.hub, 'api': self.api, 'session': self.session,
                       'get': lambda *_: b'', 'revision': lambda *_: ''}
        self.actual = patch.object(subject, '_actual', return_value=(receipt('pages', B), receipt('space', B)))
        self.actual.start(); self.addCleanup(self.actual.stop)

    def marked(self): return subject.mark_publishing(self.store, **self.common)

    def pointer_observed(self):
        self.marked(); current = subject.publish(self.store, self.store, **self.common)
        self.assertEqual(current.record['state'], 'pointer-observed'); return current

    def test_workflow_is_manual_serialized_and_requires_writer_coordination(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/search-promotion-finalize.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'daily-sync')
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['known_writers_coordinated']['default'], 'false')
        self.assertIn('reconcile-pointer', flow['on']['workflow_dispatch']['inputs']['mode']['options'])
        self.assertIn('pages-default-reconcile', flow['on']['workflow_dispatch']['inputs']['mode']['options'])

    def test_pointer_write_after_both_actual_receipts_and_lost_response(self):
        current = self.marked(); self.assertEqual(current.record['state'], 'publishing')
        self.store.lose_pointer_response = True
        current = subject.publish(self.store, self.store, **self.common)
        self.assertEqual(current.record['state'], 'pointer-observed'); self.assertEqual(self.store.pointer_writes, 1)
        self.assertEqual(self.store.pointer, make_pointer(B, A, '1' * 32))

    def test_lost_pointer_result_is_read_only_reconciled_without_replay(self):
        self.marked(); self.store.pointer = make_pointer(B, A, '1' * 32)
        current = subject.reconcile_pointer(self.store, **self.common)
        self.assertEqual(current.record['state'], 'pointer-observed'); self.assertEqual(self.store.pointer_writes, 0)

    def test_pages_default_write_and_separate_observation_then_finish(self):
        current = self.pointer_observed()
        with patch.object(subject.space, 'runtime'), patch.object(subject.defaults, 'observe_space', return_value=default_receipt('space', B)):
            current = subject.observe_space_default(self.store, **self.common, expected_revision=REV, operation=OP, created_at=TIME)
        self.assertEqual(current.record['defaults']['space']['generation'], B)
        self.session.get.return_value.json.return_value = {'commit': {'sha': REV}}
        self.session.patch.return_value.status_code = 204
        with patch.object(subject.pages, 'assert_idle'), \
                patch.object(subject.defaults, 'observe_pages', side_effect=[default_receipt('pages', A), default_receipt('pages', A),
                                                                            default_receipt('pages', B), default_receipt('pages', B),
                                                                            default_receipt('pages', B)]), \
                patch.object(subject.defaults, 'observe_space', return_value=default_receipt('space', B)):
            current = subject.start_pages_default(self.store, session=self.session, expected_revision=REV,
                                                  operation='8' * 32, created_at=TIME)
            result = subject.set_pages_default(self.store, **self.common, operation='8' * 32)
            self.assertTrue(result['variable_written']); self.assertFalse(result['journal_advanced'])
            self.assertEqual(self.session.patch.call_count, 1)
            current = subject.reconcile_pages_default(self.store, **self.common, operation='8' * 32)
            self.assertEqual(current.record['defaults']['pages']['generation'], B)
            current = subject.finish(self.store, **self.common)
        self.assertEqual(current.record['state'], 'complete')

    def test_foreign_or_missing_pending_default_never_writes(self):
        self.pointer_observed()
        with self.assertRaisesRegex(ValueError, 'no exact'):
            subject.set_pages_default(self.store, **self.common, operation='8' * 32)
        self.session.patch.assert_not_called()


if __name__ == '__main__': unittest.main()
