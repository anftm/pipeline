"""Exact canonical publication against an in-memory single-attempt S3 client."""
from copy import deepcopy
from hashlib import sha256
import io
import json
from types import SimpleNamespace
import unittest

from scripts import reader_web_index_guard as subject
from scripts.reader_stream_checkpoint_cas import ConditionalCheckpointStore

BUCKET = 'vomebook/reader-assets-v2'
ROOT = 'documents/web/mht/index.json'
PINS = {'stage': 'a' * 64, 'retention': 'b' * 64}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def row(html_sha, root):
    return {'key': 'org/books\0path.mht', 'repo': 'org/books', 'path': 'path.mht',
            'extension': 'mht', 'source_revision': 'rev', 'source_sha256': 'c' * 64,
            'mode': 'document-stream', 'bucket': BUCKET, 'object': root + '/document.html',
            'manifest': root + '/document-manifest.json', 'bytes': 17, 'sha256': html_sha,
            'checkpointed_at': '2026-01-01T00:00:00Z'}


def version(first_row):
    return {'version': 1, 'kind': 'web-document-stream-index', 'files': [first_row, {'key': 'unrelated'}],
            'failures': [{'key': 'failure', 'error': 'unchanged'}]}


class S3Error(Exception):
    def __init__(self, code, status):
        self.response = {'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}


class MemoryS3:
    def __init__(self, events):
        self.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts': 1}))
        self.events, self.objects = events, {}
        self.faults = {}
        self.after_claim = None
        self.interrupt_readback = False

    def get_object(self, *, Bucket, Key):
        assert Bucket == 'reader-assets-v2'
        self.events.append(('get', Key))
        if self.interrupt_readback and Key == ROOT:
            raise TimeoutError('canonical GET response lost')
        if Key not in self.objects:
            raise S3Error('NoSuchKey', 404)
        raw, etag = self.objects[Key]
        return {'Body': io.BytesIO(raw), 'ContentLength': len(raw), 'ETag': etag}

    def put_object(self, *, Bucket, Key, Body, ContentType, **condition):
        assert (Bucket, ContentType) == ('reader-assets-v2', 'application/json')
        self.events.append(('put', Key, condition))
        current = self.objects.get(Key)
        if Key == ROOT:
            if current is None or condition != {'IfMatch': current[1]}:
                raise S3Error('PreconditionFailed', 412)
        elif condition != {'IfNoneMatch': '*'} or current is not None:
            raise S3Error('PreconditionFailed', 412)
        fault = self.faults.pop(Key, None)
        if fault == 'before':
            raise TimeoutError('PUT request lost')
        etag = f'"etag-{len(self.events)}"'
        self.objects[Key] = (Body, etag)
        if Key != ROOT and self.after_claim is not None:
            self.after_claim()
        if fault == 'after-and-get':
            self.interrupt_readback = True
        if fault in ('after', 'after-and-get'):
            raise TimeoutError('PUT response lost')
        return {'ETag': etag, 'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 0}}


class ExactWebIndexTest(unittest.TestCase):
    def setUp(self):
        legacy_root = 'documents/web/mht/' + 'd' * 64
        # Noncanonical serialization binds the exact GET bytes, not logical JSON.
        self.parent_raw = json.dumps(version(row('e' * 64, legacy_root))).encode()
        new_sha = 'f' * 64
        new_root = 'documents/web/mht/' + sha256(('c' * 64 + '\0' + new_sha).encode()).hexdigest()
        self.target = version(row(new_sha, new_root)) | {'checkpoint_operation': '1' * 32}
        self.parent_sha = sha256(self.parent_raw).hexdigest()
        self.claim_key = f'reader-index/gc/web-exact-index/v1/{self.parent_sha}.json'
        self.events = []
        self.client = MemoryS3(self.events)
        self.client.objects[ROOT] = (self.parent_raw, '"parent"')
        self.denied_phase = None

    def guard(self, phase):
        self.events.append(('guard', phase))
        return phase != self.denied_phase

    def apply(self, **changes):
        # Always build new adapter instances, as on restart. State is only in S3.
        bucket = SimpleNamespace(_location=lambda _: ('vomebook', 'reader-assets-v2'),
                                 _client=lambda _: self.client)
        return subject.apply(ConditionalCheckpointStore(bucket), subject.S3Journal(self.client), **({
            'path': ROOT, 'parent_sha256': self.parent_sha, 'target_raw': encode(self.target),
            'prerequisites': PINS, 'guard': self.guard,
        } | changes))

    def puts(self):
        return [event for event in self.events if event[0] == 'put']

    def assert_spent(self):
        count = len(self.puts())
        with self.assertRaisesRegex(RuntimeError, 'already spent'):
            self.apply()
        self.assertEqual(len(self.puts()), count)

    def test_success_claim_and_cas_are_each_single_conditional_and_all_io_is_guarded(self):
        self.assertEqual(self.apply(), 'written')
        self.assertEqual(self.events, [
            ('guard', 'permit-read'), ('get', self.claim_key),
            ('guard', 'canonical-read'), ('get', ROOT),
            ('guard', 'claim'), ('put', self.claim_key, {'IfNoneMatch': '*'}),
            ('guard', 'claim-readback'), ('get', self.claim_key),
            ('guard', 'canonical-read'), ('get', ROOT),
            ('guard', 'put'), ('put', ROOT, {'IfMatch': '"parent"'}),
            ('guard', 'readback'), ('get', ROOT),
        ])
        permit = json.loads(self.client.objects[self.claim_key][0])
        self.assertEqual(permit, {'version': 1, 'kind': 'reader-web-exact-index-permit', 'state': 'spent',
                                 'bucket': BUCKET, 'path': ROOT, 'parent_sha256': self.parent_sha,
                                 'target_sha256': sha256(encode(self.target)).hexdigest(), 'prerequisites': PINS})
        self.assertEqual(self.apply(resume=True), 'observed')
        self.assert_spent()

    def test_lost_cas_response_committed(self):
        self.client.faults[ROOT] = 'after'
        self.assertEqual(self.apply(), 'written-response-unknown')
        self.assertEqual(self.apply(resume=True), 'observed')
        self.assertEqual(len(self.puts()), 2)

    def test_lost_cas_response_and_failed_readback_resume_exact_only(self):
        self.client.faults[ROOT] = 'after-and-get'
        with self.assertRaisesRegex(TimeoutError, 'GET response lost'):
            self.apply()
        self.client.interrupt_readback = False
        for changes in ({'prerequisites': PINS | {'retention': '0' * 64}},
                        {'target_raw': encode(self.target | {'checkpoint_operation': '2' * 32})},
                        {'path': 'documents/web/htm/index.json'}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, 'no matching spent'):
                self.apply(resume=True, **changes)
        self.assertEqual(self.apply(resume=True), 'observed')
        self.assert_spent()
        self.assertEqual(len(self.puts()), 2)

    def test_lost_cas_request_leaves_parent_and_can_never_retry(self):
        self.client.faults[ROOT] = 'before'
        with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
            self.apply()
        self.assertEqual(self.client.objects[ROOT][0], self.parent_raw)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'target bytes not observed'):
                self.apply(resume=True)
            self.assert_spent()
        self.assertEqual(len(self.puts()), 2)

    def test_drift_after_claim_spends_permit_even_if_parent_is_restored(self):
        drift = encode(self.target | {'checkpoint_operation': '3' * 32})
        self.client.after_claim = lambda: self.client.objects.update({ROOT: (drift, '"drift"')})
        with self.assertRaisesRegex(RuntimeError, 'drifted after'):
            self.apply()
        with self.assertRaisesRegex(RuntimeError, 'target bytes not observed'):
            self.apply(resume=True)
        self.client.objects[ROOT] = (self.parent_raw, '"parent"')
        self.assert_spent()
        self.assertEqual(self.puts(), [('put', self.claim_key, {'IfNoneMatch': '*'})])

    def test_target_without_permit_or_with_different_raw_encoding_does_not_resume(self):
        self.client.objects[ROOT] = (encode(self.target), '"target"')
        with self.assertRaisesRegex(RuntimeError, 'no matching spent'):
            self.apply(resume=True)
        with self.assertRaisesRegex(RuntimeError, 'parent SHA-256 differs'):
            self.apply()
        self.assertEqual(self.puts(), [])
        self.client.objects[ROOT] = (self.parent_raw, '"parent"')
        self.apply()
        self.client.objects[ROOT] = (json.dumps(self.target).encode(), '"target"')
        with self.assertRaisesRegex(RuntimeError, 'target bytes not observed'):
            self.apply(resume=True)

    def test_claim_race_never_authorizes_loser_even_with_same_seal(self):
        def guard(phase):
            if phase == 'claim':
                self.assertEqual(self.apply(), 'written')
            return self.guard(phase)

        with self.assertRaisesRegex(RuntimeError, 'permit not acquired'):
            self.apply(guard=guard)
        self.assertEqual([event[1] for event in self.puts()], [self.claim_key, ROOT, self.claim_key])
        self.assertEqual(self.apply(resume=True), 'observed')

    def test_lost_claim_ack_never_authorizes_cas(self):
        self.client.faults[self.claim_key] = 'after'
        with self.assertRaisesRegex(RuntimeError, 'PUT unconfirmed'):
            self.apply()
        with self.assertRaisesRegex(RuntimeError, 'target bytes not observed'):
            self.apply(resume=True)
        self.assert_spent()
        self.assertEqual(self.puts(), [('put', self.claim_key, {'IfNoneMatch': '*'})])

    def test_readback_guard_denial_cannot_be_bypassed_by_cas_unknown_outcome(self):
        self.client.faults[ROOT] = 'after'
        self.denied_phase = 'readback'
        with self.assertRaisesRegex(RuntimeError, 'window not verified: readback'):
            self.apply()
        self.assertEqual(self.events[-1], ('guard', 'readback'))
        self.denied_phase = 'resume-readback'
        with self.assertRaisesRegex(RuntimeError, 'window not verified: resume-readback'):
            self.apply(resume=True)
        self.denied_phase = None
        self.assertEqual(self.apply(resume=True), 'observed')
        self.assertEqual(len(self.puts()), 2)

    def test_window_denial_before_put_burns_permit_but_never_attempts_cas(self):
        self.denied_phase = 'put'
        with self.assertRaisesRegex(RuntimeError, 'window not verified: put'):
            self.apply()
        self.assert_spent()
        self.assertEqual(self.puts(), [('put', self.claim_key, {'IfNoneMatch': '*'})])

    def test_rejects_nonexact_payload_and_unpinned_or_foreign_requests(self):
        target = deepcopy(self.target)
        target['files'][1]['extra'] = 'unreviewed edit'
        for changes in ({'target_raw': encode(target)}, {'target_raw': json.dumps(self.target).encode()},
                        {'prerequisites': {}}, {'path': 'documents/office/docx/index.json'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.apply(**changes)
        self.assertFalse(self.puts())


if __name__ == '__main__':
    unittest.main()
