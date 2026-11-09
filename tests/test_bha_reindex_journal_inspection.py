"""BHA journal observation never sends an index request or accepts a receipt."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import bha_reindex_delivery as delivery
from scripts import bha_reindex_journal as journal
from scripts import bha_reindex_journal_inspection as subject
from tests.test_bha_reindex_delivery import A, T, JOB, Store, live_status, plan


class BhaJournalInspectionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(); self.plan = plan()
        self.status = live_status(self.plan)

    def test_no_journal_does_not_claim_index_delivery_or_writer_isolation(self):
        result = subject.inspect(self.store, 'fixture', read_status=lambda *_: self.status)
        self.assertIsNone(result['journal']); self.assertEqual(result['remote_writes'], 0)
        self.assertFalse(result['reindex_requested']); self.assertFalse(result['publication_ready'])
        self.assertFalse(result['external_writers_verified']); self.assertEqual(self.store.writes, 0)

    def test_claimed_unrelated_job_is_not_assumed_to_belong_to_operation(self):
        current = delivery.prepare(self.store, plan=self.plan, mode='incremental', inspect_live=lambda: self.plan, operation=T)
        journal.claim(self.store, current)
        result = subject.inspect(self.store, 'fixture', read_status=lambda *_: self.status)
        self.assertEqual(result['journal']['state'], 'claimed')
        self.assertFalse(result['job_matches_journal']); self.assertFalse(result['reindex_requested'])
        self.assertEqual(journal.load(self.store).record['state'], 'claimed')

    def test_accepted_job_is_correlated_but_not_accepted_as_consumer_proof(self):
        delivery.prepare(self.store, plan=self.plan, mode='incremental', inspect_live=lambda: self.plan, operation=T)
        delivery.apply_once(self.store, operation=T, token='fixture', inspect_live=lambda: self.plan,
                            post=lambda *_a, **_k: {'ok': True, 'status': 'accepted', 'mode': 'incremental', 'job_id': JOB})
        result = subject.inspect(self.store, 'fixture', read_status=lambda *_: self.status)
        self.assertTrue(result['job_matches_journal']); self.assertEqual(result['journal']['state'], 'accepted')
        self.assertFalse(result['publication_ready']); self.assertEqual(journal.load(self.store).record['state'], 'accepted')

    def test_changed_head_missing_status_or_unknown_job_is_rejected(self):
        before = delivery.prepare(self.store, plan=self.plan, mode='incremental', inspect_live=lambda: self.plan, operation=T)
        def change(_token):
            journal.claim(self.store, before)
            return self.status
        with self.assertRaisesRegex(ValueError, 'changed during'):
            subject.inspect(self.store, 'fixture', read_status=change)
        for invalid in ({}, {'index_status': 'ready', 'switch_pending': None, 'archive_revisions': {}},
                        {'index_status': 'ready', 'switch_pending': False, 'archive_revisions': {}, 'job': {'state': 'unknown'}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                subject.inspect(self.store, 'fixture', read_status=lambda _token: invalid)

    def test_manual_workflow_is_read_only(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/bha-reindex-inspect.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'update-bha-index')
        commands = '\n'.join(x.get('run', '') for x in flow['jobs']['inspect']['steps'])
        self.assertIn('scripts/bha_reindex_journal_inspection.py', commands)
        self.assertNotIn('git push', commands); self.assertNotIn('/api/reindex', commands)


if __name__ == '__main__':
    unittest.main()
