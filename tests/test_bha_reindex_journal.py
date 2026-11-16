"""BHA journal ownership and fail-closed legacy head checks (no live S3)."""
import hashlib
import unittest
from dataclasses import replace
from io import BytesIO
from unittest.mock import Mock, patch

from botocore.config import Config
from botocore.exceptions import ClientError

from scripts import bha_reindex_journal as journal
from scripts import bha_reindex_journal_inspection as inspection
from scripts import search_snapshot
from scripts.reader_bucket_store import S3BucketStore


OPERATION = '1' * 32
HEAD = search_snapshot.canonical({'version': 1, 'kind': 'bha-reindex-head',
                                 'operation': OPERATION, 'record': 'f' * 64, 'sequence': 0})


def error(code):
    return ClientError({'Error': {'Code': code}}, 'GetObject')


def plan():
    baseline = {str(i): 'a' * 40 for i in range(32)}
    value = {'version': 1, 'kind': 'bha-reindex-readonly-plan',
             'archive_commits': {str(i): 'b' * 40 for i in range(32)},
             'archive_revisions': {**baseline, '0': 'c' * 40}, 'consumer_revisions': baseline,
             'changed_archives': [0], 'consumer_fingerprint': 'fixture', 'last_job': None,
             'publication_ready': False, 'remote_writes': 0}
    value['plan_sha256'] = hashlib.sha256(search_snapshot.canonical(value)).hexdigest()
    return value


class BhaJournalOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.client = Mock(spec=['get_object', 'put_object'])
        self.client.get_object.side_effect = self.get
        self.client.put_object.side_effect = self.put
        self.backing = Mock()
        # Exercise the real bucket resolver without credentials or network access.
        self.backing._location.side_effect = lambda bucket: S3BucketStore._location(self.backing, bucket)
        self.backing._client.return_value = self.client
        self.store = journal.S3Store(self.backing)

    def get(self, *, Bucket, Key):
        raw = self.objects.get((Bucket, Key))
        if raw is None:
            raise error('NoSuchKey')
        return {'Body': BytesIO(raw), 'ContentLength': len(raw),
                'ETag': '"' + hashlib.sha256(raw).hexdigest() + '"'}

    def put(self, *, Bucket, Key, Body, ContentType, **condition):
        previous = self.objects.get((Bucket, Key))
        if 'IfNoneMatch' in condition:
            if previous is not None:
                raise error('PreconditionFailed')
        elif previous is None or condition['IfMatch'] != '"' + hashlib.sha256(previous).hexdigest() + '"':
            raise error('PreconditionFailed')
        self.objects[Bucket, Key] = Body

    def begin(self):
        return journal.begin(self.store, plan(), mode='incremental', operation=OPERATION)

    def test_retained_plan_uses_full_canonical_file_sha_and_only_bha_owned_create(self):
        value = plan()
        raw = search_snapshot.canonical(value)
        digest = hashlib.sha256(raw).hexdigest()
        artifact = journal.retain_plan(self.store, value)
        self.assertEqual(artifact, {'bucket': journal.BUCKET,
                                   'key': f'bha-reindex/plans/{digest}.json', 'sha256': digest})
        self.assertNotEqual(digest, value['plan_sha256'])
        self.assertEqual(self.objects['bha-data', artifact['key']], raw)
        self.client.put_object.assert_called_once_with(Bucket='bha-data', Key=artifact['key'],
            Body=raw, ContentType='application/json', IfNoneMatch='*')
        self.assertEqual(self.client.get_object.call_count, 2)
        self.assertEqual(journal.retain_plan(self.store, value), artifact)
        self.assertEqual(self.client.put_object.call_count, 1)
        self.assertNotIn(('bha-data', journal.HEAD), self.objects)

    def test_retained_plan_failure_has_one_create_and_requires_exact_readback(self):
        for outcome in ('not-written', 'lost-response', 'corrupt', 'missing-readback', 'racing-identical'):
            with self.subTest(outcome=outcome):
                self.objects.clear()
                self.client.put_object.reset_mock()
                def put(**kwargs):
                    if outcome == 'not-written':
                        raise TimeoutError('not written')
                    if outcome != 'missing-readback':
                        self.put(**kwargs)
                    if outcome == 'corrupt':
                        self.objects[kwargs['Bucket'], kwargs['Key']] = b'{}'
                    if outcome == 'lost-response':
                        raise TimeoutError('response lost')
                    if outcome == 'racing-identical':
                        raise error('PreconditionFailed')
                self.client.put_object.side_effect = put
                if outcome in {'lost-response', 'racing-identical'}:
                    result = journal.retain_plan(self.store, plan())
                    self.assertEqual(self.store.read_plan(result['key']), search_snapshot.canonical(plan()))
                else:
                    with self.assertRaises((ValueError, TimeoutError)):
                        journal.retain_plan(self.store, plan())
                self.client.put_object.assert_called_once()
                self.assertNotIn(('bha-data', journal.HEAD), self.objects)

    def test_retained_plan_rejects_corrupt_existing_object_without_overwrite(self):
        value = plan()
        digest = hashlib.sha256(search_snapshot.canonical(value)).hexdigest()
        self.objects['bha-data', journal._plan_key(digest)] = b'{}'
        with self.assertRaisesRegex(ValueError, 'content address differs'):
            journal.retain_plan(self.store, value)
        self.client.put_object.assert_not_called()

    def test_plan_paths_cannot_escape_to_records_legacy_or_other_objects(self):
        raw = search_snapshot.canonical(plan())
        key = journal._plan_key(hashlib.sha256(raw).hexdigest())
        for invalid in (journal.HEAD, journal._record_key(OPERATION, 'a' * 64), '../' + key,
                        key + '/extra', 'published/current.json', key.upper(), None):
            with self.subTest(key=invalid):
                with self.assertRaises(ValueError):
                    self.store.read_plan(invalid)
                with self.assertRaises(ValueError):
                    self.store.create_plan(invalid, raw)
        with self.assertRaisesRegex(ValueError, 'outside allowlist'):
            self.store._get(key, legacy=True)
        with self.assertRaisesRegex(ValueError, 'outside allowlist'):
            self.store.read_record(key)
        with self.assertRaises(ValueError):
            self.store.create_record(key, raw)
        self.client.get_object.assert_not_called()
        self.client.put_object.assert_not_called()

    def test_plan_body_is_bounded_canonical_and_checks_inner_and_full_digest(self):
        value = plan()
        tampered = {**value, 'consumer_fingerprint': 'tampered'}
        for raw in (b'', b'x' * (journal.MAX_JSON + 1), b'[]', b'{}',
                    search_snapshot.canonical(value) + b' ', search_snapshot.canonical(tampered)):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                self.store.create_plan(journal._plan_key(hashlib.sha256(raw).hexdigest()), raw)
        raw = search_snapshot.canonical(value)
        with self.assertRaises(ValueError):
            self.store.create_plan(journal._plan_key('f' * 64), raw)
        key = journal._plan_key(hashlib.sha256(raw).hexdigest())
        for changes in ({'ContentLength': journal.MAX_JSON + 1}, {'ContentLength': len(raw) + 1},
                        {'ETag': 'W/"weak"'}):
            body = BytesIO(raw)
            self.client.get_object.side_effect = None
            self.client.get_object.return_value = {'Body': body, 'ContentLength': len(raw), 'ETag': '"tag"', **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.read_plan(key)
            self.assertTrue(body.closed)
        self.client.put_object.assert_not_called()

    def test_new_journal_and_transitions_write_only_bha_data_with_cas(self):
        self.assertEqual(journal.BUCKET, 'vomebook/bha-data')
        current = self.begin()
        current = journal.claim(self.store, current)
        current = journal.accepted(self.store, current, job_id='2' * 32)
        current = journal.terminal(self.store, current, state='verified', result={'fixture': True})
        self.assertEqual(journal.audit(self.store, current), 4)
        puts = [call.kwargs for call in self.client.put_object.call_args_list]
        self.assertEqual(len(puts), 8)
        self.assertTrue(all(call['Bucket'] == 'bha-data' for call in puts))
        heads = [call for call in puts if call['Key'] == journal.HEAD]
        self.assertEqual(heads[0]['IfNoneMatch'], '*')
        self.assertTrue(all('IfMatch' in call and 'IfNoneMatch' not in call for call in heads[1:]))
        self.assertTrue(all(call['IfNoneMatch'] == '*' for call in puts if call['Key'] != journal.HEAD))
        legacy = [call.kwargs for call in self.client.get_object.call_args_list
                  if call.kwargs['Bucket'] == 'search-data-v1']
        self.assertTrue(legacy)
        self.assertTrue(all(call['Key'] == journal.HEAD for call in legacy))

    def test_abandon_prepared_preserves_identity_and_audit_then_allows_new_begin(self):
        prepared = self.begin()
        original_objects = dict(self.objects)
        self.client.put_object.reset_mock()
        with patch('urllib.request.urlopen') as post:
            abandoned = journal.abandon_prepared(self.store, prepared, 'stale-plan')
        post.assert_not_called()
        expected = {**prepared.record, 'state': 'abandoned', 'sequence': 1,
                    'prior': prepared.head['record'], 'result': {'reason': 'stale-plan'}}
        self.assertEqual(abandoned.record, expected)
        self.assertEqual(journal.audit(self.store, abandoned), 2)
        puts = [call.kwargs for call in self.client.put_object.call_args_list]
        self.assertEqual(len(puts), 2)
        self.assertEqual(puts[0]['IfNoneMatch'], '*')
        self.assertEqual(puts[1]['Key'], journal.HEAD)
        self.assertEqual(puts[1]['IfMatch'], prepared.etag)
        for key, raw in original_objects.items():
            if key[1] != journal.HEAD:
                self.assertEqual(self.objects[key], raw)
        # The consumer never indexed the abandoned target. Its actual baseline
        # in the fresh plan still differs from that target.
        successor = journal.begin(self.store, plan(), mode='incremental', operation='3' * 32)
        self.assertEqual(successor.record['previous_terminal'], {
            'operation': OPERATION, 'record': abandoned.head['record'], 'sequence': 1})
        self.assertEqual(journal.audit(self.store, successor), 3)
        successor = journal.claim(self.store, successor)
        successor = journal.accepted(self.store, successor, job_id='4' * 32)
        successor = journal.terminal(self.store, successor, state='verified', result={'fixture': True})
        self.assertEqual(journal.audit(self.store, successor), 6)

    def test_abandon_rejects_old_head_and_altered_current_identity_without_writes(self):
        prepared = self.begin()
        self.client.put_object.reset_mock()
        candidates = [replace(prepared, etag='"old"'),
                      replace(prepared, head_raw=HEAD),
                      replace(prepared, head={**prepared.head, 'operation': '2' * 32}),
                      replace(prepared, record={**prepared.record, 'operation': '2' * 32}),
                      replace(prepared, record={**prepared.record, 'plan_sha256': 'd' * 64})]
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, 'journal changed'):
                journal.abandon_prepared(self.store, candidate, 'stale-plan')
        self.client.put_object.assert_not_called()
        claimed = journal.claim(self.store, prepared)
        self.client.put_object.reset_mock()
        with self.assertRaisesRegex(ValueError, 'journal changed'):
            journal.abandon_prepared(self.store, prepared, 'stale-plan')
        self.client.put_object.assert_not_called()
        self.assertEqual(journal.load(self.store), claimed)

    def test_abandon_rejects_claimed_accepted_and_all_terminal_states(self):
        for state in ('claimed', 'accepted', 'verified', 'failed', 'abandoned'):
            with self.subTest(state=state):
                self.objects.clear()
                current = self.begin()
                if state == 'abandoned':
                    current = journal.abandon_prepared(self.store, current, 'operator-cancelled')
                else:
                    current = journal.claim(self.store, current)
                    if state in {'accepted', 'verified'}:
                        current = journal.accepted(self.store, current, job_id='2' * 32)
                    if state in {'verified', 'failed'}:
                        current = journal.terminal(self.store, current, state=state, result={'fixture': True})
                self.client.put_object.reset_mock()
                with self.assertRaisesRegex(ValueError, 'unclaimed prepared'):
                    journal.abandon_prepared(self.store, current, 'stale-plan')
                self.client.put_object.assert_not_called()
                self.assertEqual(journal.load(self.store), current)

    def test_concurrent_claim_wins_abandonment_cas_without_retry(self):
        prepared = self.begin()
        claimed = None

        def race(**kwargs):
            nonlocal claimed
            # The exact-current check and audit have passed. Claim just before
            # the abandoned immutable record is written and its head CAS runs.
            self.client.put_object.side_effect = self.put
            claimed = journal.claim(self.store, prepared)
            self.put(**kwargs)

        self.client.put_object.reset_mock()
        self.client.put_object.side_effect = race
        with self.assertRaises(ClientError):
            journal.abandon_prepared(self.store, prepared, 'stale-plan')
        heads = [call.kwargs for call in self.client.put_object.call_args_list
                 if call.kwargs['Key'] == journal.HEAD]
        self.assertEqual(len(heads), 2)  # One claim CAS, one rejected abandonment CAS.
        self.assertTrue(all(call['IfMatch'] == prepared.etag for call in heads))
        self.assertEqual(journal.load(self.store), claimed)
        self.assertEqual(journal.audit(self.store, claimed), 2)

    def test_abandon_head_failure_is_not_retried(self):
        prepared = self.begin()

        def fail_head(**kwargs):
            if kwargs['Key'] == journal.HEAD:
                raise TimeoutError('head unavailable')
            self.put(**kwargs)

        self.client.put_object.reset_mock()
        self.client.put_object.side_effect = fail_head
        with self.assertRaises(TimeoutError):
            journal.abandon_prepared(self.store, prepared, 'stale-plan')
        heads = [call for call in self.client.put_object.call_args_list if call.kwargs['Key'] == journal.HEAD]
        self.assertEqual(len(heads), 1)
        self.assertEqual(journal.load(self.store), prepared)

    def test_abandon_lost_head_response_reconciles_without_retry(self):
        prepared = self.begin()

        def lose_response(**kwargs):
            self.put(**kwargs)
            if kwargs['Key'] == journal.HEAD:
                raise TimeoutError('response lost')

        self.client.put_object.reset_mock()
        self.client.put_object.side_effect = lose_response
        abandoned = journal.abandon_prepared(self.store, prepared, 'superseded-plan')
        self.assertEqual(abandoned.record['state'], 'abandoned')
        self.assertEqual(journal.audit(self.store, abandoned), 2)
        self.assertEqual(self.client.put_object.call_count, 2)

    def test_abandon_reason_accepts_only_bounded_nonsecret_codes(self):
        prepared = self.begin()
        self.client.put_object.reset_mock()
        for reason in (None, '', ' ', 'x' * 1024, 'Bearer fixture-secret',
                       'stale-plan\n', {'reason': 'stale-plan'}, ['stale-plan']):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, 'reason code') as caught:
                journal.abandon_prepared(self.store, prepared, reason)
            self.assertNotIn('fixture-secret', str(caught.exception))
        self.client.put_object.assert_not_called()
        for reason in journal.ABANDON_REASONS:
            self.objects.clear()
            prepared = self.begin()
            abandoned = journal.abandon_prepared(self.store, prepared, reason)
            self.assertEqual(abandoned.record['result'], {'reason': reason})

    def test_abandoned_record_validation_and_transition_are_strict(self):
        prepared = self.begin()
        abandoned = journal.abandon_prepared(self.store, prepared, 'stale-plan')
        for changes in ({'sequence': 2}, {'claim': '2' * 32}, {'job_id': '2' * 32},
                        {'prior': None}, {'result': None}, {'result': {}},
                        {'result': {'reason': ''}}, {'result': {'reason': 'fixture-secret'}},
                        {'result': {'reason': 'stale-plan', 'detail': 'fixture-secret'}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                journal._record(search_snapshot.canonical({**abandoned.record, **changes}))
        for state in journal.STATES - {'prepared'}:
            previous = {**prepared.record, 'state': state}
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, 'transition'):
                journal._advance(previous, abandoned.record, prepared.head['record'])
        self.client.put_object.reset_mock()
        with self.assertRaises(ValueError):
            journal.claim(self.store, abandoned)
        with self.assertRaises(ValueError):
            journal.accepted(self.store, abandoned, job_id='2' * 32)
        self.client.put_object.assert_not_called()

    def test_abandon_audits_history_before_writing_and_begin_rejects_reused_identity(self):
        prepared = self.begin()
        abandoned = journal.abandon_prepared(self.store, prepared, 'stale-plan')
        self.client.put_object.reset_mock()
        with self.assertRaisesRegex(ValueError, 'all prior operations'):
            journal.begin(self.store, plan(), mode='incremental', operation=OPERATION)
        self.client.put_object.assert_not_called()
        successor = journal.begin(self.store, plan(), mode='incremental', operation='3' * 32)
        del self.objects['bha-data', journal._record_key(OPERATION, prepared.head['record'])]
        self.client.put_object.reset_mock()
        with self.assertRaisesRegex(ValueError, 'history is missing or changed'):
            journal.abandon_prepared(self.store, successor, 'stale-plan')
        with self.assertRaisesRegex(ValueError, 'history is missing or changed'):
            journal.audit(self.store, abandoned)
        self.client.put_object.assert_not_called()

    def test_unfinished_and_failed_still_block_new_begin(self):
        for state in ('prepared', 'claimed', 'accepted', 'failed-claimed', 'failed-accepted'):
            with self.subTest(state=state):
                self.objects.clear()
                current = self.begin()
                if state != 'prepared':
                    current = journal.claim(self.store, current)
                if state in {'accepted', 'failed-accepted'}:
                    current = journal.accepted(self.store, current, job_id='2' * 32)
                if state.startswith('failed'):
                    journal.terminal(self.store, current, state='failed', result={'fixture': True})
                self.client.put_object.reset_mock()
                with self.assertRaisesRegex(ValueError, 'unfinished or failed'):
                    journal.begin(self.store, plan(), mode='incremental', operation='3' * 32)
                self.client.put_object.assert_not_called()

    def test_existing_legacy_head_blocks_begin_and_direct_head_creation(self):
        for raw in (HEAD, b'not valid journal JSON'):
            self.objects['search-data-v1', journal.HEAD] = raw
            for action in (self.begin, lambda: self.store.write_head(HEAD, None)):
                with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'legacy journal head exists'):
                    action()
                self.client.put_object.assert_not_called()
        self.assertEqual(set(self.objects), {('search-data-v1', journal.HEAD)})

    def test_unknown_legacy_head_blocks_without_any_write(self):
        failures = [error(code) for code in ('AccessDenied', 'NoSuchBucket', 'SlowDown', 'InternalError')]
        failures += [TimeoutError('unreadable'), OSError('stream failed')]
        for failure in failures:
            self.client.get_object.side_effect = failure
            for action in (self.begin, lambda: self.store.write_head(HEAD, None)):
                with self.subTest(failure=failure), self.assertRaisesRegex(RuntimeError, 'absence is unknown'):
                    action()
                self.client.put_object.assert_not_called()

    def test_invalid_legacy_response_blocks_without_any_write(self):
        for changes in ({'ContentLength': 0}, {'ContentLength': journal.MAX_JSON + 1},
                        {'ContentLength': len(HEAD) + 1}, {'ETag': None}, {'ETag': 'W/"weak"'}):
            body = BytesIO(HEAD)
            self.client.get_object.side_effect = None
            self.client.get_object.return_value = {'Body': body, 'ContentLength': len(HEAD), 'ETag': '"tag"', **changes}
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, 'absence is unknown'):
                self.begin()
            self.assertTrue(body.closed)
            self.client.put_object.assert_not_called()

    def test_explicit_missing_legacy_head_allows_creation(self):
        for code in ('404', 'NoSuchKey', 'NotFound'):
            self.client.get_object.side_effect = error(code)
            with self.subTest(code=code):
                self.assertIsNone(journal.load(self.store))
                self.store.write_head(HEAD, None)
                self.assertEqual(self.client.put_object.call_args.kwargs['Bucket'], 'bha-data')
            self.objects.clear()

    def test_legacy_head_is_rechecked_before_first_head_write(self):
        self.assertIsNone(journal.load(self.store))
        self.objects['search-data-v1', journal.HEAD] = HEAD
        with self.assertRaisesRegex(ValueError, 'legacy journal head exists'):
            self.store.write_head(HEAD, None)
        self.client.put_object.assert_not_called()

    def test_inspection_cannot_hide_legacy_history_even_with_new_head(self):
        self.begin()
        self.client.put_object.reset_mock()
        self.objects['search-data-v1', journal.HEAD] = HEAD
        status = Mock()
        with self.assertRaisesRegex(ValueError, 'legacy journal head exists'):
            inspection.inspect(self.store, 'fixture', read_status=status)
        status.assert_not_called()
        self.client.put_object.assert_not_called()

    def test_legacy_reads_allow_only_exact_head_and_other_keys_stay_restricted(self):
        record = journal._record_key(OPERATION, 'f' * 64)
        for key in (record, 'published/current.json', journal.HEAD + '/extra', '../' + journal.HEAD, None):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'outside allowlist'):
                self.store._get(key, legacy=True)
        for key in ('published/current.json', '../' + record, record + '/extra', None):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'outside allowlist'):
                self.store.read_record(key)
        self.client.get_object.assert_not_called()
        with self.assertRaises(ValueError):
            self.store.create_record('published/current.json', b'{}')
        with self.assertRaises(ValueError):
            self.store.write_head(HEAD, 'W/"weak"')
        self.client.put_object.assert_not_called()

    def test_wrong_bucket_mapping_is_rejected(self):
        self.backing._location.side_effect = None
        self.backing._location.return_value = ('vomebook', 'search-data-v1')
        with self.assertRaisesRegex(ValueError, 'bucket mapping'):
            self.store.write_head(HEAD, '"etag"')
        self.client.put_object.assert_not_called()

    def test_head_conflict_is_not_retried(self):
        current = self.begin()
        self.client.put_object.reset_mock()
        self.client.put_object.side_effect = error('PreconditionFailed')
        with self.assertRaises(ClientError):
            self.store.write_head(current.head_raw, current.etag)
        self.client.put_object.assert_called_once()
        self.assertEqual(self.client.put_object.call_args.kwargs['IfMatch'], current.etag)

    def test_default_adapter_keeps_single_attempt_configuration(self):
        self.backing._config = Config(retries={'max_attempts': 8})
        with patch.object(journal, 'S3BucketStore', return_value=self.backing):
            journal.S3Store()
        self.assertEqual(self.backing._config.retries['total_max_attempts'], 1)


if __name__ == '__main__':
    unittest.main()
