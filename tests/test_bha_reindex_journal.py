"""BHA journal ownership and fail-closed legacy head checks (no live S3)."""
import hashlib
import unittest
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
