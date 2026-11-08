"""Daily candidate evidence is read-only and never authorizes promotion."""
import unittest
from unittest.mock import Mock, patch

from scripts import search_daily_handoff as subject
from scripts import search_transition as tx
from tests.test_search_transition import A, B, C, SearchTransitionTests, default_receipt, receipt


class DailyHandoffTests(unittest.TestCase):
    def setUp(self):
        fixture = SearchTransitionTests(); fixture.setUp()
        current = fixture.ready()
        current = tx.mark_publishing(fixture.s, current, pages_receipt=receipt('pages', B),
                                     space_receipt=receipt('space', B))
        current = tx.publish_pointer(fixture.s, fixture.s, current, pages_receipt=receipt('pages', B),
                                     space_receipt=receipt('space', B))
        for site in ('pages', 'space'):
            current = tx.start_default(fixture.s, current, site=site, operation_id='9' * 32,
                                       expected_revision='d' * 40, created_at='2026-11-08T00:00:00Z')
            current = tx.observe_default(fixture.s, current, site=site, receipt=default_receipt(site, B))
        self.current = tx.finish(fixture.s, current, pages_receipt=receipt('pages', B), space_receipt=receipt('space', B),
                                 pages_default=default_receipt('pages', B), space_default=default_receipt('space', B))
        self.store = fixture.s
        self.check = {'parent': B, 'parent_transition': '1' * 32, 'candidate_records': 7,
                      'candidate_objects_verified': 8, 'source_revisions_sha256': A}
        self.args = {'hub': Mock(), 'session': Mock(), 'api': Mock(), 'store': self.store,
                     'get': Mock(), 'revision': Mock()}

    def test_terminal_matching_journal_gives_only_readonly_handoff(self):
        before = (self.store.writes, self.store.pointer_writes)
        with patch.object(subject.preflight, 'inspect', return_value=self.check) as inspect:
            result = subject.inspect(C, **self.args)
        self.assertEqual(result['journal_state'], 'complete')
        self.assertFalse(result['publication_ready'])
        self.assertFalse(result['external_writers_verified'])
        self.assertEqual(result['remote_writes'], 0)
        self.assertEqual((self.store.writes, self.store.pointer_writes), before)
        inspect.assert_called_once()

    def test_unfinished_journal_rejected_before_candidate_preflight(self):
        tx.begin(self.store, parent_pointer=self.store.pointer, candidate=C, source_revisions_sha256=A,
                 pages_receipt=receipt('pages', B), space_receipt=receipt('space', B),
                 pages_default=default_receipt('pages', B), space_default=default_receipt('space', B),
                 transition='2' * 32)
        with patch.object(subject.preflight, 'inspect') as inspect:
            with self.assertRaisesRegex(ValueError, 'unfinished work'):
                subject.inspect(A, **self.args)
            inspect.assert_not_called()

    def test_journal_or_pointer_change_and_bad_history_fail_closed(self):
        with patch.object(subject.preflight, 'inspect', return_value=self.check):
            self.store.pointer = tx.make_pointer(B, A, '2' * 32)
            with self.assertRaisesRegex(ValueError, 'pointer changed'):
                subject.inspect(C, **self.args)
        self.store.pointer = tx.make_pointer(B, A, '1' * 32)
        first = tx.load(self.store)
        changed = type(first)(first.head, first.head_raw, '"new"', first.record, first.record_raw)
        with patch.object(subject.preflight, 'inspect', return_value=self.check), \
                patch.object(tx, 'load', side_effect=[first, changed]):
            with self.assertRaisesRegex(ValueError, 'journal changed'):
                subject.inspect(C, **self.args)
        self.store.records.clear()
        with patch.object(subject.preflight, 'inspect') as inspect:
            with self.assertRaises(ValueError):
                subject.inspect(C, **self.args)
            inspect.assert_not_called()

    def test_candidate_preflight_failure_propagates_without_writes(self):
        count = (self.store.writes, self.store.pointer_writes)
        with patch.object(subject.preflight, 'inspect', side_effect=ValueError('source changed')):
            with self.assertRaisesRegex(ValueError, 'source changed'):
                subject.inspect(C, **self.args)
        self.assertEqual((self.store.writes, self.store.pointer_writes), count)


if __name__ == '__main__':
    unittest.main()
