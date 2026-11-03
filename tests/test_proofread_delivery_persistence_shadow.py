"""Disposable SQLite experiment, NOT a production queue or persistence adapter.

The temporary database contains only synthetic fixtures. SQLite serializes
these test writers; it does not fence GitHub workers or establish that a PR,
comment or tracker issue exists. Its checkpoint is in the same database as the
journal and cannot detect rollback of both together. No production paths or
credentials are used.
"""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from scripts import proofread_delivery_shadow as shadow
from tests import test_proofread_delivery_shadow as fixtures


class DisposableJournal:
    """Test-only local transaction fixture, deliberately not importable by runtime."""

    def __init__(self, directory, journal):
        self.path = Path(directory) / 'synthetic-journal.sqlite'
        self.plan = journal['plan_sha256']
        with sqlite3.connect(self.path) as database:
            database.execute('CREATE TABLE journal (id INTEGER PRIMARY KEY CHECK(id=1), '
                             'plan TEXT NOT NULL, rev INTEGER NOT NULL, head TEXT NOT NULL, body TEXT NOT NULL)')
            database.execute('INSERT INTO journal VALUES(1, ?, 0, ?, ?)',
                             (self.plan, self.plan, json.dumps(journal)))

    def load(self):
        with sqlite3.connect(self.path) as database:
            plan, revision, head, body = database.execute(
                'SELECT plan, rev, head, body FROM journal WHERE id=1').fetchone()
        if plan != self.plan:
            raise ValueError('fixture plan changed')
        journal = json.loads(body)
        shadow.replay(journal, plan, revision, head)
        return journal, revision, head

    def commit(self, expected_revision, expected_head, candidate, *, crash_before_commit=False):
        """Compare/replay and atomically update this test DB, or roll back."""
        candidate_head = candidate['events'][-1]['event_sha256']
        shadow.replay(candidate, self.plan, expected_revision + 1, candidate_head)
        with sqlite3.connect(self.path, timeout=10) as database:
            database.execute('BEGIN IMMEDIATE')
            row = database.execute('SELECT rev, head, body FROM journal WHERE id=1 AND plan=?',
                                   (self.plan,)).fetchone()
            if row is None or row[:2] != (expected_revision, expected_head):
                database.rollback()
                return False
            old = json.loads(row[2])
            shadow.replay(old, self.plan, expected_revision, expected_head)
            if candidate['plan'] != old['plan'] or candidate['events'][:-1] != old['events']:
                raise ValueError('fixture event history differs')
            cursor = database.execute(
                'UPDATE journal SET rev=?, head=?, body=? WHERE id=1 AND plan=? AND rev=? AND head=?',
                (expected_revision + 1, candidate_head, json.dumps(candidate), self.plan,
                 expected_revision, expected_head))
            if cursor.rowcount != 1:
                raise ValueError('fixture compare-and-swap failed')
            if crash_before_commit:
                database.rollback()
                return False
            database.commit()
            return True


class DeliveryPersistenceShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='delivery-shadow-')
        self.addCleanup(self.temp.cleanup)
        self.fixture = fixtures.DeliveryShadowTests('test_partial_parse_and_tracker_require_individual_verified_evidence')
        self.fixture.setUp()
        self.plan = self.fixture.plan_sha
        self.store = DisposableJournal(self.temp.name, self.fixture.journal)

    def next_event(self, event, *, crash_before_commit=False):
        journal, revision, head = self.store.load()
        candidate = shadow.append(journal, self.plan, revision, head, event)
        committed = self.store.commit(revision, head, candidate, crash_before_commit=crash_before_commit)
        return committed, journal, candidate

    def effect(self, kind, effect, *, crash_before_commit=False, **extra):
        return self.next_event({'type': kind, 'effect': effect, 'owner': 'worker-one', 'epoch': 1, **extra},
                               crash_before_commit=crash_before_commit)

    def claim(self):
        self.next_event({'type': 'claim', 'owner': 'worker-one', 'epoch': 1})

    def verify(self, name):
        self.effect('begin', name)
        self.effect('verify', name, evidence=self.fixture.evidence(name))

    def test_transaction_crash_before_commit_and_lost_ack_after_commit(self):
        event = {'type': 'claim', 'owner': 'worker-one', 'epoch': 1}
        committed, original, candidate = self.next_event(event, crash_before_commit=True)
        self.assertFalse(committed)
        recovered, revision, head = self.store.load()
        self.assertEqual(recovered, original)
        self.assertEqual((revision, head), (0, self.plan))
        self.assertTrue(self.store.commit(0, self.plan, candidate))
        # Simulate losing the success response: reusing the old checkpoint is
        # a conflict, and a fresh read is needed. It is not a second claim.
        self.assertFalse(self.store.commit(0, self.plan, candidate))
        recovered, revision, head = self.store.load()
        self.assertEqual(revision, 1)
        self.assertEqual(shadow.replay(recovered, self.plan, revision, head)['owner'], 'worker-one')

    def test_two_synthetic_writers_cannot_both_commit_same_revision(self):
        original, revision, head = self.store.load()
        candidates = [shadow.append(original, self.plan, revision, head,
                                    {'type': 'claim', 'owner': owner, 'epoch': 1})
                      for owner in ('worker-one', 'worker-two')]
        barrier = threading.Barrier(2)

        def write(candidate):
            barrier.wait(timeout=10)
            return self.store.commit(revision, head, candidate)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, candidates))
        self.assertEqual(sorted(results), [False, True])
        journal, revision, head = self.store.load()
        report = shadow.replay(journal, self.plan, revision, head)
        self.assertEqual(revision, 1)
        self.assertIn(report['owner'], ('worker-one', 'worker-two'))
        self.assertFalse(report['external_writers_fenced'])

    def test_process_restart_keeps_partial_prs_and_uncertain_status(self):
        self.claim()
        self.verify('config_pr')
        self.effect('begin', 'patch_pr')
        self.effect('timeout', 'patch_pr')
        journal, revision, head = self.store.load()
        receipt = shadow.receipt(journal, self.plan, revision, head, self.fixture.capability)
        self.assertEqual(receipt['status'], 'uncertain')
        with self.assertRaises(ValueError):
            shadow.append(journal, self.plan, revision, head,
                          {'type': 'begin', 'effect': 'patch_pr', 'owner': 'worker-one', 'epoch': 1})
        self.effect('verify', 'patch_pr', evidence=self.fixture.evidence('patch_pr'))
        self.verify('tracker_issue')
        self.verify('tracker_comment:0')
        journal, revision, head = self.store.load()
        report = shadow.replay(journal, self.plan, revision, head)
        self.assertEqual(report['objects'], {'config_pr': 11, 'patch_pr': 12,
                                             'tracker_issue': 31, 'tracker_comment:0': 51})
        self.assertEqual(shadow.receipt(journal, self.plan, revision, head, self.fixture.capability)['status'], 'partial')
        self.assertFalse(report['production_recovery_authorized'])
        self.assertFalse(report['production_retry_authorized'])

    def test_crash_after_external_effect_before_record_does_not_imply_absence(self):
        self.claim()
        self.effect('begin', 'config_pr')
        # The actual effect is deliberately not modeled; pretend the response
        # was lost before the synthetic acknowledgment transaction committed.
        committed, original, _candidate = self.effect('ack', 'config_pr', crash_before_commit=True)
        self.assertFalse(committed)
        journal, revision, head = self.store.load()
        self.assertEqual(journal, original)
        self.assertEqual(shadow.receipt(journal, self.plan, revision, head, self.fixture.capability)['status'],
                         'verification_pending')
        self.effect('observe_absent', 'config_pr')
        journal, revision, head = self.store.load()
        with self.assertRaises(ValueError):
            shadow.append(journal, self.plan, revision, head,
                          {'type': 'begin', 'effect': 'config_pr', 'owner': 'worker-one', 'epoch': 1})
        self.assertFalse(shadow.replay(journal, self.plan, revision, head)['production_retry_authorized'])

    def test_old_checkpoint_rejects_journal_rollback_or_fork(self):
        self.claim()
        snapshot, revision, head = self.store.load()
        self.effect('begin', 'config_pr')
        current, current_revision, current_head = self.store.load()
        with self.assertRaises(ValueError):
            shadow.replay(snapshot, self.plan, current_revision, current_head)
        fork = shadow.append(snapshot, self.plan, revision, head,
                             {'type': 'begin', 'effect': 'config_pr', 'owner': 'worker-one', 'epoch': 1})
        self.assertEqual(fork, current)  # identical events are not evidence of distinct execution
        fork = copy.deepcopy(fork)
        fork['events'][-1]['type'] = 'timeout'
        fork['events'][-1]['event_sha256'] = shadow._digest({
            key: value for key, value in fork['events'][-1].items() if key != 'event_sha256'})
        with self.assertRaises(ValueError):
            shadow.replay(fork, self.plan, current_revision, current_head)

    def test_restoring_entire_database_to_an_old_valid_row_needs_independent_head(self):
        self.claim()
        old, old_revision, old_head = self.store.load()
        self.effect('begin', 'config_pr')
        _new, new_revision, new_head = self.store.load()
        # Local fixture only: roll back both the journal and the checkpoint to
        # an internally consistent prior revision, as a stale replica could.
        with sqlite3.connect(self.store.path) as database:
            database.execute('UPDATE journal SET rev=?, head=?, body=? WHERE id=1',
                             (old_revision, old_head, json.dumps(old)))
        loaded, revision, head = self.store.load()
        self.assertEqual((revision, head), (old_revision, old_head))
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            shadow.replay(loaded, self.plan, new_revision, new_head)
        # The DB alone cannot detect this rollback. A trusted independent head
        # must be obtained and compared; the model does not provide one.
        self.assertFalse(shadow.replay(loaded, self.plan, revision, head)['external_writers_fenced'])


if __name__ == '__main__':
    unittest.main()
