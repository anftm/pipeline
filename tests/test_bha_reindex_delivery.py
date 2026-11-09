"""BHA reindex requests are claimed once and require real observations."""
import hashlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from unittest.mock import Mock, patch

from scripts import bha_reindex_delivery as delivery
from scripts import bha_reindex_journal as journal
from scripts import search_snapshot


A, B, C, T, JOB = 'a' * 40, 'b' * 40, 'c' * 40, '1' * 32, '2' * 32


def plan(*, changed=True):
    baseline = {str(i): A for i in range(32)}
    trees = dict(baseline)
    if changed: trees['0'] = C
    value = {'version': 1, 'kind': 'bha-reindex-readonly-plan', 'archive_commits': {str(i): B for i in range(32)},
             'archive_revisions': trees, 'consumer_revisions': baseline, 'changed_archives': [0] if changed else [],
             'consumer_fingerprint': 'fingerprint', 'last_job': None, 'publication_ready': False, 'remote_writes': 0}
    value['plan_sha256'] = hashlib.sha256(search_snapshot.canonical(value)).hexdigest()
    return value


class Store:
    def __init__(self):
        self.head = None; self.etag = None; self.records = {}; self.writes = 0; self.lose_head = False
    def read_head(self): return self.head, self.etag
    def read_record(self, key): return self.records.get(key)
    def create_record(self, key, raw):
        if key in self.records: raise ValueError('exists')
        self.records[key] = raw
    def write_head(self, raw, etag):
        self.writes += 1
        if etag != self.etag: raise ValueError('CAS conflict')
        self.head = raw; self.etag = f'"{self.writes}"'
        if self.lose_head: raise TimeoutError('lost response')


def live_status(value, *, state='succeeded', job_id=JOB):
    return {'index_status': 'ready', 'switch_pending': False, 'archive_revisions': value['archive_revisions'],
            'job': {'job_id': job_id, 'state': state, 'mode': 'incremental',
                    'target_archive_revisions': value['archive_revisions'], 'changed_archives': value['changed_archives']}}


def sample_report(value):
    return {'consumer_health_observed_twice': True, 'remote_writes': 0,
            'samples': [{'archive': 0, 'commit': B, 'tree': value['archive_revisions']['0'],
                         'preview_article_equal': True, 'exact_title_search_found': True}]}


class BhaReindexDeliveryTests(unittest.TestCase):
    def setUp(self): self.s = Store(); self.plan = plan()
    def prepare(self): return delivery.prepare(self.s, plan=self.plan, mode='incremental', inspect_live=lambda: self.plan, operation=T)
    def apply(self, post): return delivery.apply_once(self.s, operation=T, token='fixture', inspect_live=lambda: self.plan, post=post)

    def test_accept_verify_and_next_operation_are_separate(self):
        initial = self.prepare(); self.assertEqual(initial.record['state'], 'prepared')
        send = Mock(return_value={'ok': True, 'status': 'accepted', 'mode': 'incremental', 'job_id': JOB})
        result = self.apply(send); self.assertEqual(result['state'], 'accepted'); send.assert_called_once()
        running = delivery.reconcile(self.s, operation=T, token='fixture', status=lambda *_: live_status(self.plan, state='running'))
        self.assertEqual(running['status'], 'running'); self.assertFalse(running['journal_advanced'])
        done = delivery.reconcile(self.s, operation=T, token='fixture', status=lambda *_: live_status(self.plan),
                                  verify_samples=lambda: sample_report(self.plan))
        self.assertEqual(done['status'], 'verified'); self.assertEqual(journal.load(self.s).record['state'], 'verified')
        self.assertEqual(journal.audit(self.s, journal.load(self.s)), 4)
        self.assertFalse(done['external_writers_verified'])
        with self.assertRaisesRegex(ValueError, 'not in exact prepared'):
            self.apply(send)
        send.assert_called_once()

    def test_lost_post_stays_claimed_and_cannot_adopt_uncorrelated_job(self):
        self.prepare(); send = Mock(side_effect=TimeoutError('response lost'))
        with self.assertRaises(TimeoutError): self.apply(send)
        self.assertEqual(journal.load(self.s).record['state'], 'claimed')
        with self.assertRaisesRegex(ValueError, 'not in exact prepared'): self.apply(send)
        result = delivery.reconcile(self.s, operation=T, token='fixture', status=lambda *_: live_status(self.plan))
        self.assertEqual(result['status'], 'job-uncorrelated'); self.assertFalse(result['journal_advanced'])
        self.assertEqual(journal.load(self.s).record['state'], 'claimed'); send.assert_called_once()

    def test_source_fingerprint_drift_and_noop_incremental_block_before_post(self):
        self.prepare(); send = Mock()
        with self.assertRaisesRegex(ValueError, 'changed before request'):
            delivery.apply_once(self.s, operation=T, token='fixture', post=send,
                                inspect_live=lambda: {**self.plan, 'consumer_fingerprint': 'changed'})
        send.assert_not_called(); self.assertEqual(journal.load(self.s).record['state'], 'prepared')
        other = Store(); unchanged = plan(changed=False)
        with self.assertRaisesRegex(ValueError, 'no applicable change'):
            delivery.prepare(other, plan=unchanged, mode='incremental', inspect_live=lambda: unchanged)
        self.assertIsNone(other.head)

    def test_failed_and_wrong_or_incomplete_receipts_never_prove_success(self):
        self.prepare(); self.apply(Mock(return_value={'ok': True, 'status': 'accepted', 'mode': 'incremental', 'job_id': JOB}))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            delivery.reconcile(self.s, operation=T, token='fixture', status=lambda *_: live_status(self.plan, job_id='3' * 32))
        with self.assertRaisesRegex(ValueError, 'sample verification'):
            delivery.reconcile(self.s, operation=T, token='fixture', status=lambda *_: live_status(self.plan))
        bad = sample_report(self.plan); bad['samples'][0]['tree'] = A
        with self.assertRaisesRegex(ValueError, 'sample does not match'):
            delivery.reconcile(self.s, operation=T, token='fixture', status=lambda *_: live_status(self.plan), verify_samples=lambda: bad)
        result = delivery.reconcile(self.s, operation=T, token='fixture', status=lambda *_: live_status(self.plan, state='failed'))
        self.assertEqual(result['status'], 'failed'); self.assertEqual(journal.load(self.s).record['state'], 'failed')
        with self.assertRaisesRegex(ValueError, 'failed work'):
            delivery.prepare(self.s, plan=self.plan, mode='incremental', inspect_live=lambda: self.plan, operation='4' * 32)

    def test_full_rebuild_is_explicit_and_changed_archive_samples_are_required(self):
        unchanged = plan(changed=False); other = Store()
        current = delivery.prepare(other, plan=unchanged, mode='full', inspect_live=lambda: unchanged, operation='4' * 32)
        self.assertEqual(current.record['mode'], 'full')
        send = Mock(return_value={'ok': True, 'status': 'accepted', 'mode': 'full', 'job_id': JOB})
        delivery.apply_once(other, operation='4' * 32, token='fixture', inspect_live=lambda: unchanged, post=send)
        self.assertEqual(send.call_args.kwargs['mode'], 'full')
        self.assertEqual(send.call_args.kwargs['payload']['changed_archives'], [])
        send.assert_called_once()
        self.prepare(); self.apply(Mock(return_value={'ok': True, 'status': 'accepted', 'mode': 'incremental', 'job_id': JOB}))
        sample = sample_report(self.plan); sample['samples'][0]['archive'] = 1; sample['samples'][0]['tree'] = A
        with self.assertRaisesRegex(ValueError, 'cover all changed'):
            delivery.reconcile(self.s, operation=T, token='fixture', status=lambda *_: live_status(self.plan),
                               verify_samples=lambda: sample)
        self.assertEqual(journal.load(self.s).record['state'], 'accepted')

    def test_changed_or_missing_job_targets_cannot_be_accepted(self):
        self.prepare(); self.apply(Mock(return_value={'ok': True, 'status': 'accepted', 'mode': 'incremental', 'job_id': JOB}))
        status = live_status(self.plan); status['job']['target_archive_revisions'] = {}
        with self.assertRaisesRegex(ValueError, 'job and targets'):
            delivery.reconcile(self.s, operation=T, token='fixture', status=lambda *_: status)
        self.assertEqual(journal.load(self.s).record['state'], 'accepted')

    def test_concurrent_claims_and_lost_head_response_do_not_authorize_two(self):
        current = self.prepare(); barrier, lock, write = threading.Barrier(2, timeout=5), threading.Lock(), self.s.write_head
        def cas(raw, etag):
            barrier.wait()
            with lock: return write(raw, etag)
        def attempt(_):
            try: return journal.claim(self.s, current)
            except Exception as error: return error
        with patch.object(self.s, 'write_head', side_effect=cas), ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(isinstance(x, journal.Current) for x in values), 1)
        self.assertEqual(sum(isinstance(x, ValueError) for x in values), 1)
        self.assertEqual(journal.load(self.s).record['state'], 'claimed')

    def test_claim_lost_head_response_is_resolved_once_and_blocks_new_claim(self):
        current = self.prepare(); self.s.lose_head = True; before = self.s.writes
        claimed = journal.claim(self.s, current)
        self.assertEqual(self.s.writes, before + 1)
        self.assertEqual(journal.load(self.s), claimed)
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            journal.claim(self.s, claimed)
        self.assertEqual(self.s.writes, before + 1)

    def test_remote_error_never_exposes_credential_or_retries_post(self):
        from urllib.error import URLError
        secret = 'fixture-secret-never-log'
        with patch.object(delivery.urllib.request, 'urlopen', side_effect=URLError(secret)) as send:
            with self.assertRaisesRegex(RuntimeError, 'outcome is unconfirmed') as caught:
                delivery._post(secret, mode='incremental', payload={})
        self.assertNotIn(secret, str(caught.exception)); self.assertIsNone(caught.exception.__cause__)
        send.assert_called_once()
        with patch.object(delivery.urllib.request, 'urlopen', side_effect=URLError(secret)) as read:
            with self.assertRaisesRegex(RuntimeError, 'status request failed') as caught:
                delivery._raw_status(secret)
        self.assertNotIn(secret, str(caught.exception)); self.assertIsNone(caught.exception.__cause__)
        read.assert_called_once()
        for mode in ('', 'invalid'):
            with self.assertRaises(ValueError): delivery._post(secret, mode=mode, payload={})

    def test_s3_store_confines_keys_and_conditional_writes(self):
        backing = Mock(); backing._location.return_value = ('vomebook', 'search-data-v1')
        adapter = journal.S3Store(backing)
        head = search_snapshot.canonical({'version': 1, 'kind': 'bha-reindex-head', 'operation': T, 'record': 'f' * 64, 'sequence': 0})
        adapter.write_head(head, None)
        self.assertEqual(backing._client.return_value.put_object.call_args.kwargs['IfNoneMatch'], '*')
        adapter.write_head(head, '"etag"')
        self.assertEqual(backing._client.return_value.put_object.call_args.kwargs['IfMatch'], '"etag"')
        with self.assertRaises(ValueError): adapter.read_record('published/current.json')
        with self.assertRaises(ValueError): adapter.write_head(head, 'W/"etag"')


if __name__ == '__main__':
    unittest.main()
