"""Offline runner integration: real journal and delivery, bounded fake I/O."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import bha_reindex_run as runner
from scripts import bha_reindex_delivery as delivery
from scripts import bha_reindex_journal as journal
from scripts import search_snapshot
from tests.test_bha_reindex_delivery import (
    B, T, Store, live_status, plan as delivery_plan, receipt, snapshot_health, snapshot_report,
)


SAMPLES = [{'archive': 0, 'path': 'mao/publication/002/002fff5d4e.json'}]


def rehash(value):
    value['plan_sha256'] = hashlib.sha256(search_snapshot.canonical(
        {key: item for key, item in value.items() if key != 'plan_sha256'})).hexdigest()
    return value


def plan(changed=True):
    return rehash({**delivery_plan(changed=changed), 'post_index_sample_missing_archives': [],
                   'samples': [], 'deferred_samples': SAMPLES if changed else []})


class CountingStore(Store):
    def __init__(self):
        super().__init__()
        self.record_writes = 0
        self.plans = {}
        self.plan_writes = 0

    def read_plan(self, key):
        return self.plans.get(key)

    def create_plan(self, key, raw):
        self.plan_writes += 1
        if key in self.plans:
            raise ValueError('exists')
        self.plans[key] = raw

    def create_record(self, key, raw):
        self.record_writes += 1
        return super().create_record(key, raw)


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ReindexRunTests(unittest.TestCase):
    def setUp(self):
        self.store = CountingStore()
        self.plan = plan()
        self.collect = Mock(side_effect=lambda: copy.deepcopy(self.plan))
        self.reviewed = Mock(side_effect=lambda: copy.deepcopy(self.plan))
        self.clock = Clock()
        self.events = []
        self.status = Mock(side_effect=lambda _: live_status(self.plan))
        self.send = Mock(side_effect=lambda _token, *, mode, payload:
                         receipt(self.plan, mode=mode, operation=payload['operation_id']))
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        # The real delivery module still performs correlation, health, snapshot
        # identity and journal transitions; only remote boundaries are replaced.
        reconcile = delivery.reconcile
        self.reconcile = self.stack.enter_context(patch.object(delivery, 'reconcile', side_effect=
            lambda *args, **kwargs: reconcile(*args, **kwargs, status=self.status, fetch=snapshot_health)))
        self.stack.enter_context(patch.object(delivery, '_post', self.send))
        self.snapshot = self.stack.enter_context(patch.object(runner.snapshot, 'inspect', side_effect=snapshot_report))
        self.stack.enter_context(patch.object(runner.consumer, 'get', side_effect=snapshot_health))
        self.observe = self.stack.enter_context(patch.object(runner.consumer, 'observe_one', side_effect=
            lambda archive, commit, path: {'archive': archive, 'commit': commit, 'path': path,
                'preview_article_equal': True, 'exact_title_search_found': True}))
        self.stack.enter_context(patch.object(runner.consumer.revisions, 'archive_identity',
                                              side_effect=AssertionError('must not look up moved source heads')))

    def run_action(self, action='apply', **kwargs):
        return runner.run(action=action, store=self.store, collect_plan=self.collect,
                          load_reviewed=self.reviewed, selected=SAMPLES, token='fixture',
                          emit=self.events.append, clock=self.clock, sleep=self.clock.sleep,
                          **kwargs)

    def pending(self, state='accepted'):
        current = delivery.prepare(self.store, plan=self.plan, mode='incremental',
                                   inspect_live=lambda: self.plan, operation=T)
        if state != 'prepared':
            current = journal.claim(self.store, current)
        if state in {'accepted', 'failed', 'verified'}:
            current = journal.accepted(self.store, current, job_id='2' * 32)
        if state in {'failed', 'verified'}:
            current = journal.terminal(self.store, current, state=state, result={'fixture': True})
        return current

    def test_auto_no_change_after_absent_verified_or_abandoned_writes_nothing(self):
        for state in (None, 'verified', 'abandoned'):
            with self.subTest(state=state):
                self.store = CountingStore()
                self.plan = plan()
                if state == 'verified':
                    self.pending(state)
                    self.plan['consumer_revisions'] = dict(self.plan['archive_revisions'])
                    self.plan['changed_archives'] = []
                    rehash(self.plan)
                else:
                    if state == 'abandoned':
                        current = self.pending('prepared')
                        journal.abandon_prepared(self.store, current, 'stale-plan')
                    self.plan = plan(False)
                before = (self.store.writes, self.store.record_writes, self.store.plan_writes)
                retain = Mock(side_effect=AssertionError('no artifact for no change'))
                result = self.run_action('auto', retain_plan=retain)
                self.assertTrue(result['ok'])
                self.assertEqual(result['status'], 'no-change')
                self.assertEqual(result['remote_writes'], 0)
                self.assertEqual((self.store.writes, self.store.record_writes, self.store.plan_writes), before)
                retain.assert_not_called()
        self.reviewed.assert_not_called()
        self.send.assert_not_called()

    def test_auto_pending_reconciles_then_returns_without_planning_or_retention(self):
        for state in ('claimed', 'accepted'):
            with self.subTest(state=state):
                self.store = CountingStore()
                self.pending(state)
                retain = Mock(side_effect=AssertionError('pending must not retain'))
                result = self.run_action('auto', retain_plan=retain)
                self.assertTrue(result['ok'])
                self.assertEqual(result['operation_id'], T)
                self.assertEqual(result['reindex_post_attempts'], 0)
                retain.assert_not_called()
        self.collect.assert_not_called()
        self.reviewed.assert_not_called()
        self.send.assert_not_called()
        self.snapshot.assert_called_with(self.plan['archive_revisions'], full_sidecars=True,
                                         expected_generation='bha-fixture', not_before=100)

    def test_auto_prepared_failed_and_broken_history_block_before_planning(self):
        for state in ('prepared', 'failed', 'verified'):
            with self.subTest(state=state):
                self.store = CountingStore()
                self.pending(state)
                if state == 'verified':
                    del self.store.records[next(iter(self.store.records))]
                before = (self.store.writes, self.store.record_writes)
                with self.assertRaises(ValueError):
                    self.run_action('auto')
                self.assertEqual((self.store.writes, self.store.record_writes), before)
                self.assertEqual(self.store.plan_writes, 0)
        self.collect.assert_not_called()
        self.send.assert_not_called()

    def test_auto_coverage_blocks_before_artifact_journal_or_post(self):
        for declared in ([], [1]):
            with self.subTest(declared=declared):
                self.plan['archive_revisions']['1'] = 'd' * 40
                self.plan['changed_archives'] = [0, 1]
                self.plan['post_index_sample_missing_archives'] = declared
                rehash(self.plan)
                retain = Mock()
                with self.assertRaisesRegex(runner.RunError, 'all changed archives'):
                    self.run_action('auto', retain_plan=retain)
                retain.assert_not_called()
                self.assertEqual((self.store.writes, self.store.record_writes, self.store.plan_writes), (0, 0, 0))
        self.send.assert_not_called()

    def test_auto_retention_failure_or_wrong_provenance_blocks_begin(self):
        for retain in (Mock(side_effect=TimeoutError('artifact failed')), Mock(return_value=None),
                       Mock(return_value={'bucket': journal.BUCKET, 'key': 'wrong', 'sha256': 'f' * 64})):
            with self.subTest(retain=retain), patch.object(journal, 'begin') as begin:
                with self.assertRaises((TimeoutError, runner.RunError)):
                    self.run_action('auto', retain_plan=retain)
                begin.assert_not_called()
                self.assertIsNone(self.store.head)
                self.assertEqual(self.store.record_writes, 0)
        self.send.assert_not_called()

    def test_auto_changed_retains_exact_plan_before_begin_and_posts_once(self):
        begin = journal.begin
        retain = Mock(wraps=journal.retain_plan)
        def checked_begin(store, value, **kwargs):
            retain.assert_called_once()
            raw = search_snapshot.canonical(value)
            digest = hashlib.sha256(raw).hexdigest()
            self.assertEqual(store.plans[journal._plan_key(digest)], raw)
            self.assertEqual(self.events[-1]['plan_file_sha256'], digest)
            return begin(store, value, **kwargs)
        self.status.side_effect = lambda _: {
            **live_status(self.plan), 'job': {**live_status(self.plan)['job'],
                'operation_id': journal.load(self.store).record['operation']}}
        with patch.object(journal, 'begin', side_effect=checked_begin):
            result = self.run_action('auto', retain_plan=retain)
        self.assertTrue(result['ok'])
        self.assertEqual(result['plan_artifact']['sha256'], result['plan_file_sha256'])
        self.assertEqual(result['plan_artifact']['bucket'], journal.BUCKET)
        self.assertEqual(self.store.plan_writes, 1)
        self.assertEqual(self.collect.call_count, 3)
        self.reviewed.assert_not_called()
        self.send.assert_called_once()
        self.assertEqual(journal.audit(self.store, journal.load(self.store)), 4)
        self.snapshot.assert_called_once_with(self.plan['archive_revisions'], full_sidecars=True,
                                             expected_generation='bha-fixture', not_before=100)

    def test_auto_changed_after_terminal_uses_fresh_plan_and_new_operation(self):
        for state in ('verified', 'abandoned'):
            with self.subTest(state=state):
                self.store = CountingStore()
                self.plan = plan()
                if state == 'verified':
                    self.pending('verified')
                    self.plan['consumer_revisions'] = dict(self.plan['archive_revisions'])
                else:
                    current = self.pending('prepared')
                    journal.abandon_prepared(self.store, current, 'superseded-plan')
                self.plan['archive_revisions']['0'] = 'e' * 40
                rehash(self.plan)
                self.collect.reset_mock()
                self.send.reset_mock()
                self.status.side_effect = lambda _: {
                    **live_status(self.plan), 'job': {**live_status(self.plan)['job'],
                        'operation_id': journal.load(self.store).record['operation']}}
                result = self.run_action('auto')
                self.assertTrue(result['ok'])
                self.assertNotEqual(result['operation_id'], T)
                self.assertEqual(self.collect.call_count, 3)
                self.assertEqual(journal.load(self.store).record['previous_terminal']['operation'], T)
                self.assertEqual(self.store.plan_writes, 1)
                self.send.assert_called_once()
                journal.audit(self.store, journal.load(self.store))
        self.reviewed.assert_not_called()

    def test_auto_pending_coverage_failure_does_not_advance_journal(self):
        self.plan['archive_revisions']['1'] = 'e' * 40
        self.plan['changed_archives'] = [0, 1]
        rehash(self.plan)
        current = self.pending('claimed')
        before = (self.store.writes, self.store.record_writes)
        with self.assertRaisesRegex(runner.RunError, 'all changed archives'):
            self.run_action('auto')
        self.assertEqual(journal.load(self.store), current)
        self.assertEqual((self.store.writes, self.store.record_writes), before)
        self.assertEqual(self.store.plan_writes, 0)
        self.collect.assert_not_called()
        self.reconcile.assert_not_called()
        self.send.assert_not_called()

    def test_auto_full_plan_drift_at_prepare_or_claim_stops_post(self):
        changed = rehash({**self.plan, 'samples': [{'changed': 'evidence'}]})
        for during in ('prepare', 'claim'):
            with self.subTest(during=during):
                self.store = CountingStore()
                self.collect.side_effect = ([self.plan, changed] if during == 'prepare'
                                            else [self.plan, self.plan, changed])
                with self.assertRaisesRegex(runner.RunError, 'retained plan is stale'):
                    self.run_action('auto')
                current = journal.load(self.store)
                self.assertEqual(current.record['state'] if current else None,
                                 'prepared' if during == 'claim' else None)
                self.assertEqual(self.store.plan_writes, 1)
        self.send.assert_not_called()

    def test_auto_lost_response_unknown_stays_claimed_next_auto_reconciles(self):
        self.send.side_effect = TimeoutError('lost response')
        with self.assertRaises(TimeoutError):
            self.run_action('auto')
        claimed = journal.load(self.store)
        self.assertEqual(claimed.record['state'], 'claimed')
        self.reconcile.assert_not_called()
        self.collect.reset_mock()
        self.status.side_effect = lambda _: {'job': None}
        result = self.run_action('auto', timeout_seconds=35)
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(journal.load(self.store), claimed)
        self.status.side_effect = lambda _: {
            **live_status(self.plan), 'job': {**live_status(self.plan)['job'],
                'operation_id': claimed.record['operation']}}
        self.assertTrue(self.run_action('auto')['ok'])
        self.collect.assert_not_called()
        self.reviewed.assert_not_called()
        self.assertEqual(self.store.plan_writes, 1)
        self.send.assert_called_once()

    def test_pending_apply_recovers_before_collecting_or_loading_plan(self):
        for state in ('claimed', 'accepted'):
            with self.subTest(state=state):
                self.store = CountingStore()
                self.pending(state)
                self.collect.side_effect = AssertionError('planner cannot run while indexing')
                self.reviewed.side_effect = AssertionError('no reviewed plan needed for recovery')
                result = self.run_action()
                self.assertTrue(result['ok'])
                self.assertEqual(result['status'], 'verified')
                self.assertEqual(result['operation_id'], T)
                self.assertEqual(result['reindex_post_attempts'], 0)
                self.assertEqual(journal.load(self.store).record['state'], 'verified')
        self.collect.assert_not_called()
        self.reviewed.assert_not_called()
        self.send.assert_not_called()
        self.snapshot.assert_called_with(self.plan['archive_revisions'], full_sidecars=True,
                                         expected_generation='bha-fixture', not_before=100)

    def test_reconcile_requires_exact_existing_operation_and_never_plans(self):
        self.pending()
        for operation in (None, '9' * 32):
            with self.subTest(operation=operation), self.assertRaises(runner.RunError):
                self.run_action('reconcile', operation_id=operation)
        self.assertTrue(self.run_action('reconcile', operation_id=T)['ok'])
        self.collect.assert_not_called()
        self.reviewed.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(self.observe.call_args.args, (0, B, SAMPLES[0]['path']))
        self.assertEqual(journal.load(self.store).record['result']['samples'][0]['tree'],
                         self.plan['archive_revisions']['0'])

    def test_plan_with_pending_work_is_readonly_and_does_not_collect(self):
        self.pending('claimed')
        before = (self.store.writes, self.store.record_writes)
        result = self.run_action('plan')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'pending-reconciliation')
        self.assertEqual((self.store.writes, self.store.record_writes), before)
        self.collect.assert_not_called()
        self.reconcile.assert_not_called()
        self.send.assert_not_called()

    def test_no_change_apply_is_success_with_zero_remote_writes(self):
        self.plan = plan(False)
        result = self.run_action()
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'no-change')
        self.assertEqual(result['remote_writes'], 0)
        self.assertEqual((self.store.writes, self.store.record_writes), (0, 0))
        self.assertIsNone(self.store.head)
        self.send.assert_not_called()
        self.reconcile.assert_not_called()

    def test_coverage_rejected_before_any_prepare_or_post(self):
        for declared in ([1], []):
            with self.subTest(declared=declared):
                self.plan['archive_revisions']['1'] = 'd' * 40
                self.plan['changed_archives'] = [0, 1]
                self.plan['post_index_sample_missing_archives'] = declared
                rehash(self.plan)
                with self.assertRaisesRegex(runner.RunError, 'post_index_sample_missing_archives'):
                    self.run_action()
                self.assertEqual((self.store.writes, self.store.record_writes), (0, 0))
        self.send.assert_not_called()

    def test_declared_coverage_failure_cannot_be_silently_ignored(self):
        self.plan['post_index_sample_missing_archives'] = [0]
        rehash(self.plan)
        with self.assertRaisesRegex(runner.RunError, 'post_index_sample_missing_archives'):
            self.run_action()
        self.assertIsNone(self.store.head)

    def test_config_four_archives_cannot_cover_a_fifth_and_is_not_truncated(self):
        path = Path(__file__).resolve().parents[1] / 'config/bha-consumer-samples.json'
        _, spec = runner.read_json(path)
        self.assertEqual({row['archive'] for row in spec['samples']}, {0, 2, 4, 25})
        runner.validate_samples(spec['samples'])
        with self.assertRaisesRegex(runner.RunError, 'all changed archives'):
            runner.require_coverage([0, 1, 2, 4, 25], spec['samples'], self.plan)
        self.assertEqual(len(spec['samples']), 4)

    def test_stale_reviewed_plan_and_fresh_coverage_fail_before_writes(self):
        reviewed = copy.deepcopy(self.plan)
        self.reviewed.side_effect = lambda: reviewed
        self.plan['archive_commits']['31'] = 'e' * 40
        rehash(self.plan)
        with self.assertRaisesRegex(runner.RunError, 'stale'):
            self.run_action()
        self.assertEqual((self.store.writes, self.store.record_writes), (0, 0))
        self.send.assert_not_called()
        self.plan['archive_revisions']['1'] = 'e' * 40
        self.plan['changed_archives'] = [0, 1]
        self.plan['post_index_sample_missing_archives'] = [1]
        rehash(self.plan)
        with self.assertRaisesRegex(runner.RunError, 'post_index_sample_missing_archives'):
            self.run_action()
        self.assertEqual((self.store.writes, self.store.record_writes), (0, 0))

    def test_staleness_at_prepare_or_claim_stops_post(self):
        changed = rehash({**self.plan, 'consumer_fingerprint': 'moved'})
        for during in ('prepare', 'claim'):
            with self.subTest(during=during):
                self.store = CountingStore()
                self.collect.side_effect = ([self.plan, changed] if during == 'prepare'
                                            else [self.plan, self.plan, changed])
                with self.assertRaisesRegex(runner.RunError, 'stale'):
                    self.run_action()
                current = journal.load(self.store)
                if during == 'prepare':
                    self.assertIsNone(current)
                    self.assertEqual(self.store.record_writes, 0)
                else:
                    self.assertEqual(current.record['state'], 'prepared')
        self.send.assert_not_called()

    def test_prepared_resume_is_explicit_reviewed_and_fresh(self):
        self.pending('prepared')
        with self.assertRaisesRegex(runner.RunError, 'explicit --operation-id'):
            self.run_action()
        self.collect.assert_not_called()
        with self.assertRaisesRegex(runner.RunError, 'prepared operation requires apply'):
            self.run_action('reconcile', operation_id=T)
        self.assertTrue(self.run_action(operation_id=T)['ok'])
        self.assertEqual(self.collect.call_count, 2)
        self.send.assert_called_once()
        self.assertEqual(journal.load(self.store).record['state'], 'verified')

    def test_explicit_abandon_audits_prepared_and_allows_new_plan_and_apply(self):
        self.pending('prepared')
        result = self.run_action('abandon', operation_id=T, reason='stale-plan')
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'abandoned')
        self.assertEqual(result['operation_id'], T)
        self.assertEqual(result['reindex_post_attempts'], 0)
        self.assertTrue(result['journal_audited'])
        current = journal.load(self.store)
        self.assertEqual(current.record['state'], 'abandoned')
        self.assertEqual(result['audited_records'], journal.audit(self.store, current))
        self.collect.assert_not_called()
        self.reviewed.assert_not_called()
        self.send.assert_not_called()
        self.reconcile.assert_not_called()
        # An abandoned preparation never established a serving baseline. A new
        # reviewed plan may therefore capture different source AND consumer.
        self.plan['archive_commits']['31'] = 'd' * 40
        self.plan['consumer_revisions']['31'] = 'e' * 40
        self.plan['archive_revisions']['31'] = 'e' * 40
        rehash(self.plan)
        self.assertTrue(self.run_action('plan')['ok'])
        self.status.side_effect = lambda _: {
            **live_status(self.plan), 'job': {**live_status(self.plan)['job'],
                'operation_id': journal.load(self.store).record['operation']}}
        applied = self.run_action()
        self.assertTrue(applied['ok'])
        self.assertNotEqual(applied['operation_id'], T)
        self.send.assert_called_once()
        self.assertEqual(journal.load(self.store).record['previous_terminal']['operation'], T)
        journal.audit(self.store, journal.load(self.store))

    def test_abandon_requires_exact_prepared_operation_and_bounded_reason(self):
        self.pending('prepared')
        before = (self.store.writes, self.store.record_writes)
        for operation, reason in ((None, 'stale'), ('9' * 32, 'stale'), (T, None),
                                  (T, ''), (T, '  '), (T, 'x' * 513), (T, 'unsupported')):
            with self.subTest(operation=operation, reason=reason), self.assertRaises(runner.RunError):
                self.run_action('abandon', operation_id=operation, reason=reason)
        self.assertEqual((self.store.writes, self.store.record_writes), before)
        self.assertEqual(journal.load(self.store).record['state'], 'prepared')
        self.collect.assert_not_called()
        self.send.assert_not_called()
        for state in ('claimed', 'accepted', 'verified', 'failed'):
            self.store = CountingStore()
            self.pending(state)
            before = (self.store.writes, self.store.record_writes)
            with self.subTest(state=state), self.assertRaises(runner.RunError):
                self.run_action('abandon', operation_id=T, reason='stale')
            self.assertEqual((self.store.writes, self.store.record_writes), before)
            self.assertEqual(journal.load(self.store).record['state'], state)
        self.reconcile.assert_not_called()

    def test_abandon_loses_race_to_claim_without_overwriting_it(self):
        self.pending('prepared')
        abandon = journal.abandon_prepared
        def raced(store, current, reason):
            journal.claim(store, current)
            return abandon(store, current, reason=reason)
        with patch.object(journal, 'abandon_prepared', side_effect=raced):
            with self.assertRaisesRegex(ValueError, 'changed before prepared abandonment'):
                self.run_action('abandon', operation_id=T, reason='stale-plan')
        self.assertEqual(journal.load(self.store).record['state'], 'claimed')
        journal.audit(self.store, journal.load(self.store))
        self.send.assert_not_called()
        self.collect.assert_not_called()

    def test_abandon_without_journal_and_reason_on_other_actions_reject(self):
        with self.assertRaisesRegex(runner.RunError, 'no existing journal'):
            self.run_action('abandon', operation_id=T, reason='stale')
        for action in ('plan', 'apply', 'reconcile'):
            with self.subTest(action=action), self.assertRaisesRegex(runner.RunError, 'only valid for abandon'):
                self.run_action(action, reason='stale')
        self.assertEqual((self.store.writes, self.store.record_writes), (0, 0))
        self.send.assert_not_called()

    def test_prepared_resume_rejects_changed_review_before_fresh_read(self):
        self.pending('prepared')
        self.plan['consumer_fingerprint'] = 'new'
        rehash(self.plan)
        before = self.store.writes
        with self.assertRaisesRegex(runner.RunError, 'differs from the prepared'):
            self.run_action(operation_id=T)
        self.assertEqual(self.store.writes, before)
        self.collect.assert_not_called()
        self.send.assert_not_called()

    def test_successful_apply_posts_once_after_auditable_checkpoint(self):
        def send(_token, *, mode, payload):
            self.assertEqual(self.events[-1]['status'], 'posting')
            self.assertEqual(self.events[-1]['operation_id'], payload['operation_id'])
            self.assertEqual(journal.load(self.store).record['state'], 'claimed')
            return receipt(self.plan, mode=mode, operation=payload['operation_id'])
        self.send.side_effect = send
        self.status.side_effect = lambda _: {
            **live_status(self.plan), 'job': {**live_status(self.plan)['job'],
                'operation_id': journal.load(self.store).record['operation']}}
        result = self.run_action()
        self.assertTrue(result['ok'])
        self.assertEqual(result['reindex_post_attempts'], 1)
        self.assertEqual(self.collect.call_count, 3)
        self.send.assert_called_once()
        self.assertEqual(journal.audit(self.store, journal.load(self.store)), 4)

    def test_lost_response_stays_claimed_until_explicit_subsequent_run(self):
        self.send.side_effect = TimeoutError('sensitive upstream detail')
        with self.assertRaises(TimeoutError):
            self.run_action()
        current = journal.load(self.store)
        self.assertEqual(current.record['state'], 'claimed')
        self.assertEqual(self.events[-1]['operation_id'], current.record['operation'])
        self.reconcile.assert_not_called()
        self.status.side_effect = lambda _: {
            **live_status(self.plan), 'job': {**live_status(self.plan)['job'],
                'operation_id': current.record['operation']}}
        self.collect.reset_mock()
        self.reviewed.reset_mock()
        result = self.run_action('reconcile', operation_id=current.record['operation'])
        self.assertTrue(result['ok'])
        self.send.assert_called_once()
        self.collect.assert_not_called()
        self.reviewed.assert_not_called()

    def test_timeout_preserves_pending_journal_and_bounds_sleep(self):
        for state in ('claimed', 'accepted'):
            with self.subTest(state=state):
                self.store = CountingStore()
                original = self.pending(state)
                self.status.side_effect = lambda _: ({'job': None} if state == 'claimed'
                                                     else live_status(self.plan, state='running'))
                result = self.run_action('reconcile', operation_id=T, timeout_seconds=65)
                self.assertFalse(result['ok'])
                self.assertEqual(result['status'], 'timeout')
                self.assertTrue(result['journal_preserved'])
                self.assertEqual(journal.load(self.store), original)
                self.assertEqual(self.clock.sleeps[-3:], [30, 30, 5])
        self.send.assert_not_called()
        self.collect.assert_not_called()

    def test_recovered_claim_timeout_reports_accepted_state(self):
        self.pending('claimed')
        self.status.side_effect = lambda _: live_status(self.plan, state='running')
        result = self.run_action('reconcile', operation_id=T, timeout_seconds=35)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'timeout')
        self.assertEqual(result['journal_state'], 'accepted')
        self.assertEqual(journal.load(self.store).record['state'], 'accepted')
        self.send.assert_not_called()
        self.collect.assert_not_called()

    def test_failed_job_records_failure_and_blocks_future_work(self):
        self.pending('claimed')
        self.status.side_effect = lambda _: live_status(self.plan, state='failed')
        result = self.run_action()
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(journal.load(self.store).record['state'], 'failed')
        before = self.store.writes
        for action in ('plan', 'apply', 'reconcile'):
            with self.subTest(action=action), self.assertRaisesRegex(runner.RunError, 'failed journal'):
                self.run_action(action, operation_id=T)
        self.assertEqual(self.store.writes, before)
        self.collect.assert_not_called()
        self.send.assert_not_called()

    def test_full_operation_and_invalid_poll_budgets_are_rejected(self):
        delivery.prepare(self.store, plan=self.plan, mode='full', inspect_live=lambda: self.plan, operation=T)
        with self.assertRaisesRegex(runner.RunError, 'not incremental'):
            self.run_action()
        for seconds in (0, -1, float('nan'), float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(runner.RunError):
                self.run_action(timeout_seconds=seconds)
        with self.assertRaises(runner.RunError):
            self.run_action(poll_seconds=31)
        self.send.assert_not_called()

    def test_target_health_uses_existing_semantics_and_rejects_count_drift(self):
        record = self.pending().record
        before = json.loads(snapshot_health())
        after = {**before, 'bucket_snapshot': {'state': 'irrelevant-for-sampling'}, 'uptime': 123}
        with patch.object(runner.consumer, 'get', side_effect=[json.dumps(before).encode(), json.dumps(after).encode()]):
            report = runner.observe_target(record, SAMPLES)
        self.assertTrue(report['consumer_health_observed_twice'])
        after['document_count'] += 1
        with patch.object(runner.consumer, 'get', side_effect=[json.dumps(before).encode(), json.dumps(after).encode()]):
            with self.assertRaisesRegex(runner.RunError, 'health changed'):
                runner.observe_target(record, SAMPLES)


class AllArchivePlanningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='bha-32-samples-test-')
        self.addCleanup(self.directory.cleanup)
        self.baseline = Path(self.directory.name) / 'baseline.json'
        self.baseline.write_text(json.dumps({str(i): 'a' * 40 for i in range(32)}))
        self.selected = [{'archive': i, 'path': SAMPLES[0]['path']} for i in range(32)]
        self.target = plan(False)
        self.count = 9
        self.calls = []
        self.drift = lambda: None
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.observe = self.stack.enter_context(patch.object(runner.consumer, 'observe_one', side_effect=
            lambda archive, commit, path, **kwargs: {'archive': archive, 'commit': commit, 'path': path,
                'preview_article_equal': True, 'exact_title_search_found': True}))
        inspect = runner.planning.inspect

        def capture(github_token, token, baseline, *, selected_samples):
            self.calls.append(selected_samples)
            if len(self.calls) == 2:
                self.drift()
            return inspect(github_token, token, baseline, selected_samples=selected_samples,
                           lookup=lambda token, archive: (str(archive),
                               self.target['archive_commits'][str(archive)],
                               self.target['archive_revisions'][str(archive)]),
                           read_status=lambda token: {'index_status': 'ready', 'switch_pending': False,
                               'archive_revisions': self.target['consumer_revisions'],
                               'build_fingerprint': self.target['consumer_fingerprint'],
                               'job': self.target['last_job']},
                           fetch=lambda *args: json.dumps({**json.loads(snapshot_health()),
                                                          'document_count': self.count}).encode())
        self.stack.enter_context(patch.object(runner.planning, 'inspect', side_effect=capture))

    def collect(self, selected=None):
        return runner.inspect_plan_with_samples('fixture-github', 'fixture-bha', self.baseline,
                                                selected_samples=self.selected if selected is None else selected)

    def test_all_32_samples_are_observed_against_one_identity_and_rehashed(self):
        result = self.collect()
        self.assertEqual([len(chunk) for chunk in self.calls], [16, 16])
        self.assertEqual(self.calls[0] + self.calls[1], self.selected)
        self.assertEqual(result['sample_count'], 32)
        self.assertEqual([row['archive'] for row in result['samples']], list(range(32)))
        self.assertEqual(result['deferred_samples'], [])
        self.assertEqual(result['post_index_sample_missing_archives'], [])
        self.assertEqual(self.observe.call_count, 32)
        for row in result['samples']:
            key = str(row['archive'])
            self.assertEqual((row['commit'], row['tree']),
                             (result['archive_commits'][key], result['archive_revisions'][key]))
        runner.validate_plan(result)

    def test_31_changed_archives_all_deferred_and_covered_including_second_chunk(self):
        for i in range(31):
            self.target['archive_revisions'][str(i)] = 'c' * 40
        result = self.collect()
        self.assertEqual(result['changed_archives'], list(range(31)))
        self.assertEqual(result['deferred_samples'], self.selected[:31])
        self.assertEqual([row['archive'] for row in result['samples']], [31])
        self.assertEqual(result['sample_count'], 1)
        self.assertEqual(result['post_index_sample_missing_archives'], [])
        runner.require_coverage(result['changed_archives'], self.selected, result)
        runner.validate_plan(result)
        self.observe.assert_called_once()
        # Omitting the changed archive in the second chunk must remain visible.
        self.calls.clear()
        incomplete = self.selected[:30] + self.selected[31:]
        missing = self.collect(incomplete)
        self.assertEqual(missing['post_index_sample_missing_archives'], [30])
        with self.assertRaisesRegex(runner.RunError, 'all changed archives'):
            runner.require_coverage(missing['changed_archives'], incomplete, missing)

    def test_changes_between_chunks_reject_the_aggregate(self):
        original = copy.deepcopy(self.target)
        mutations = [
            lambda: self.target['archive_commits'].__setitem__('0', 'd' * 40),
            lambda: self.target['archive_revisions'].__setitem__('31', 'd' * 40),
            lambda: self.target['consumer_revisions'].__setitem__('31', 'd' * 40),
            lambda: self.target.__setitem__('consumer_fingerprint', 'moved'),
            lambda: self.target.__setitem__('last_job', {'job_id': T, 'state': 'succeeded', 'mode': 'incremental'}),
            lambda: setattr(self, 'count', 10),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.target = copy.deepcopy(original)
                self.count = 9
                self.calls.clear()
                self.drift = mutation
                with self.assertRaisesRegex(runner.RunError, 'changed between sample chunks'):
                    self.collect()
                self.assertEqual(len(self.calls), 2)

    def test_small_selection_uses_one_inspection_and_consumer_limit_stays_16(self):
        result = self.collect(self.selected[:16])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['sample_count'], 16)
        with self.assertRaisesRegex(ValueError, 'one to sixteen'):
            runner.consumer.observe('fixture', self.selected)

    def test_runner_bounds_and_duplicate_detection_apply_across_chunks(self):
        runner.validate_samples(self.selected)
        for invalid in ([], self.selected + [self.selected[0]],
                        self.selected[:31] + [self.selected[0]]):
            with self.subTest(size=len(invalid)), self.assertRaises(runner.RunError):
                runner.validate_samples(invalid)
        self.assertEqual(self.calls, [])

    def test_target_acceptance_samples_all_32_with_health_before_and_after(self):
        record = {'archive_commits': self.target['archive_commits'],
                  'archive_revisions': self.target['archive_revisions'], 'changed_archives': list(range(32))}
        events = []
        self.observe.side_effect = lambda archive, commit, path: (events.append(archive) or
            {'archive': archive, 'commit': commit, 'path': path,
             'preview_article_equal': True, 'exact_title_search_found': True})
        with patch.object(runner.consumer, 'get', side_effect=lambda *args: (events.append('health') or snapshot_health())):
            result = runner.observe_target(record, self.selected)
        self.assertEqual(events, ['health', *range(32), 'health'])
        self.assertEqual(len(result['samples']), 32)
        self.assertTrue(result['consumer_health_observed_twice'])
        self.assertEqual(result['remote_writes'], 0)


class ReindexCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='bha-run-test-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.samples = self.root / 'samples.json'
        self.samples.write_text(json.dumps({'version': 1, 'samples': SAMPLES}))
        self.output = self.root / 'result.json'
        self.plan_path = self.root / 'plan.json'
        self.store = CountingStore()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(journal, 'S3Store', return_value=self.store))
        self.collect = self.stack.enter_context(patch.object(runner.planning, 'inspect', return_value=plan(False)))
        self.send = self.stack.enter_context(patch.object(delivery, '_post', side_effect=AssertionError('unexpected POST')))
        self.stdout = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def invoke(self, action, *extra):
        return runner.main(['--action', action, '--output', str(self.output),
                            '--samples', str(self.samples), *extra])

    def write_plan(self, value):
        self.plan_path.write_text(json.dumps(value) + '\n')
        return hashlib.sha256(self.plan_path.read_bytes()).hexdigest()

    def test_auto_cli_rejects_manual_controls_before_any_io(self):
        for option, value in (('--plan-sha256', 'a' * 64), ('--operation-id', T), ('--reason', 'stale-plan')):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke('auto', option, value)
            self.assertFalse(self.output.exists())
        self.collect.assert_not_called()
        self.send.assert_not_called()

    def test_auto_cli_no_change_optional_local_output_and_no_remote_writes(self):
        self.assertEqual(self.invoke('auto'), 0)
        self.assertEqual(json.loads(self.output.read_bytes())['status'], 'no-change')
        self.output = self.root / 'auto-local-result.json'
        self.assertEqual(self.invoke('auto', '--plan', str(self.plan_path)), 0)
        result = json.loads(self.output.read_bytes())
        self.assertEqual(self.plan_path.read_bytes(), search_snapshot.canonical(result['plan']))
        self.assertEqual(result['plan_file_sha256'], hashlib.sha256(self.plan_path.read_bytes()).hexdigest())
        self.assertEqual(result['plan_path'], str(self.plan_path))
        self.assertNotIn('plan_artifact', result)
        self.assertEqual((self.store.writes, self.store.record_writes, self.store.plan_writes), (0, 0, 0))
        self.send.assert_not_called()

    def test_auto_cli_retains_local_and_remote_plan_before_lost_post(self):
        self.collect.return_value = plan()
        def lost(_token, *, mode, payload):
            result = json.loads(self.output.read_bytes())
            artifact = result['plan_artifact']
            self.assertEqual(self.plan_path.read_bytes(), self.store.plans[artifact['key']])
            self.assertEqual(result['plan_file_sha256'], hashlib.sha256(self.plan_path.read_bytes()).hexdigest())
            self.assertEqual(result['journal_state'], 'claimed')
            raise TimeoutError('fixture-secret-never-log')
        self.send.side_effect = lost
        self.assertEqual(self.invoke('auto', '--plan', str(self.plan_path)), 1)
        result = json.loads(self.output.read_bytes())
        self.assertEqual(result['journal_state'], 'claimed')
        self.assertIn('plan_artifact', result)
        self.assertNotIn('fixture-secret-never-log', self.output.read_text())
        self.send.assert_called_once()

    def test_auto_cli_never_overwrites_existing_plan_output(self):
        self.write_plan(plan())
        before = self.plan_path.read_bytes()
        self.assertEqual(self.invoke('auto', '--plan', str(self.plan_path)), 1)
        self.assertEqual(self.plan_path.read_bytes(), before)
        self.collect.assert_not_called()
        self.assertEqual(self.store.plan_writes, 0)

    def test_exact_workflow_contract_plan_then_apply_noop(self):
        self.assertEqual(self.invoke('plan', '--plan', str(self.plan_path)), 0)
        result = json.loads(self.output.read_bytes())
        digest = hashlib.sha256(self.plan_path.read_bytes()).hexdigest()
        self.assertEqual(result['plan_file_sha256'], digest)
        self.assertEqual(json.loads(self.plan_path.read_bytes()), result['plan'])
        self.output = self.root / 'apply-result.json'
        self.assertEqual(self.invoke('apply', '--plan', str(self.plan_path), '--plan-sha256', digest), 0)
        self.assertEqual(json.loads(self.output.read_bytes())['status'], 'no-change')
        self.assertEqual((self.store.writes, self.store.record_writes), (0, 0))
        self.send.assert_not_called()
        for line in self.stdout.getvalue().splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_cli_collects_both_sample_chunks_and_emits_complete_plan(self):
        selected = [{'archive': i, 'path': SAMPLES[0]['path']} for i in range(32)]
        self.samples.write_text(json.dumps({'version': 1, 'samples': selected}))
        value = plan()
        value['archive_revisions'] = {str(i): 'c' * 40 for i in range(32)}
        value['changed_archives'] = list(range(32))
        def inspect(*args, selected_samples):
            return rehash({**value, 'samples': [], 'sample_count': 0,
                           'deferred_samples': selected_samples,
                           'post_index_sample_missing_archives': sorted(
                               set(range(32)) - {row['archive'] for row in selected_samples})})
        self.collect.side_effect = inspect
        self.assertEqual(self.invoke('plan', '--plan', str(self.plan_path)), 0)
        result = json.loads(self.plan_path.read_bytes())
        self.assertEqual(result['deferred_samples'], selected)
        self.assertEqual(result['post_index_sample_missing_archives'], [])
        runner.validate_plan(result)
        self.assertEqual(self.collect.call_count, 2)
        self.assertEqual((self.store.writes, self.store.record_writes), (0, 0))
        self.send.assert_not_called()

    def test_file_sha_binds_bytes_and_content_hash_is_independently_checked(self):
        digest = self.write_plan(plan(False))
        self.plan_path.write_bytes(self.plan_path.read_bytes() + b' ')
        self.assertEqual(self.invoke('apply', '--plan', str(self.plan_path), '--plan-sha256', digest), 1)
        self.assertIn('file SHA-256 differs', json.loads(self.output.read_bytes())['error'])
        self.collect.assert_not_called()
        self.output = self.root / 'tampered-result.json'
        tampered = plan(False)
        tampered['consumer_fingerprint'] = 'tampered'
        digest = self.write_plan(tampered)
        self.assertEqual(self.invoke('apply', '--plan', str(self.plan_path), '--plan-sha256', digest), 1)
        self.assertIn('content digest differs', json.loads(self.output.read_bytes())['error'])
        self.assertEqual((self.store.writes, self.store.record_writes), (0, 0))

    def test_pending_reconcile_needs_no_plan_file_and_timeout_exits_nonzero(self):
        current = journal.begin(self.store, plan(), mode='incremental', operation=T)
        journal.claim(self.store, current)
        with patch.object(delivery, 'reconcile', return_value={'status': 'job-unknown'}):
            self.assertEqual(self.invoke('reconcile', '--operation-id', T,
                                         '--timeout-seconds', '0.001', '--poll-seconds', '0.001'), 1)
        self.assertEqual(json.loads(self.output.read_bytes())['status'], 'timeout')
        self.assertEqual(journal.load(self.store).record['state'], 'claimed')
        self.collect.assert_not_called()
        self.send.assert_not_called()

    def test_cli_lost_post_keeps_operation_checkpoint_and_redacts_exception(self):
        value = plan()
        self.collect.return_value = value
        digest = self.write_plan(value)
        def lost(_token, *, mode, payload):
            result = json.loads(self.output.read_bytes())
            self.assertEqual(result['operation_id'], payload['operation_id'])
            self.assertEqual(result['status'], 'posting')
            self.assertEqual(result['reindex_post_attempts'], 1)
            raise TimeoutError('fixture-secret-never-log')
        self.send.side_effect = lost
        with patch.object(delivery, 'reconcile') as reconcile:
            self.assertEqual(self.invoke('apply', '--plan', str(self.plan_path), '--plan-sha256', digest), 1)
        result = json.loads(self.output.read_bytes())
        self.assertEqual(result['operation_id'], journal.load(self.store).record['operation'])
        self.assertEqual(result['journal_state'], 'claimed')
        self.assertEqual(result['reindex_post_attempts'], 1)
        self.assertNotIn('fixture-secret-never-log', self.output.read_text() + self.stdout.getvalue())
        reconcile.assert_not_called()
        self.send.assert_called_once()

    def test_cli_abandon_needs_no_plan_samples_or_service_credentials(self):
        journal.begin(self.store, plan(), mode='incremental', operation=T)
        self.samples.unlink()
        with patch.dict(runner.os.environ, {}, clear=True):
            self.assertEqual(self.invoke('abandon', '--operation-id', T, '--reason', 'stale-plan'), 0)
        result = json.loads(self.output.read_bytes())
        self.assertEqual(result['status'], 'abandoned')
        self.assertEqual(result['reason'], 'stale-plan')
        self.assertTrue(result['journal_audited'])
        self.assertEqual(result['reindex_post_attempts'], 0)
        self.assertEqual(journal.load(self.store).record['state'], 'abandoned')
        self.collect.assert_not_called()
        self.send.assert_not_called()

    def test_cli_abandon_missing_reason_preserves_prepared_and_reports_failure(self):
        journal.begin(self.store, plan(), mode='incremental', operation=T)
        before = self.store.writes
        self.assertEqual(self.invoke('abandon', '--operation-id', T), 1)
        result = json.loads(self.output.read_bytes())
        self.assertIn('nonempty --reason', result['error'])
        self.assertEqual(result['operation_id'], T)
        self.assertEqual(self.store.writes, before)
        self.assertEqual(journal.load(self.store).record['state'], 'prepared')
        self.send.assert_not_called()

    def test_cli_failed_operation_is_nonzero_without_plan_read(self):
        current = journal.begin(self.store, plan(), mode='incremental', operation=T)
        current = journal.claim(self.store, current)
        journal.terminal(self.store, current, state='failed', result={'fixture': True})
        self.assertEqual(self.invoke('apply'), 1)
        self.assertIn('failed journal', json.loads(self.output.read_bytes())['error'])
        self.collect.assert_not_called()
        self.send.assert_not_called()

    def test_cli_rejects_output_alias_and_never_overwrites_reviewed_plan(self):
        self.write_plan(plan())
        before = self.plan_path.read_bytes()
        self.output = self.plan_path
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke('apply', '--plan', str(self.plan_path), '--plan-sha256', 'a' * 64)
        self.assertEqual(self.plan_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
