"""Local fault injection through both real CAS adapters; no production I/O."""
import json
import unittest
from dataclasses import replace

from scripts import reader_image_history as history
from scripts import reader_image_history_store as legacy
from scripts import reader_image_history_transition as subject
from scripts.reader_stream_checkpoint_cas import ConditionalCheckpointStore
from tests import test_reader_image_history as fixtures
from tests import test_reader_image_history_shard as shard_fixtures
from tests.test_reader_image_history_store import MemoryS3


class Bucket(MemoryS3):
    def __init__(self):
        super().__init__()
        self.read_etags = {}
        self.faults = {}
        self.interrupt_read = None

    def read_bytes(self, bucket, path):
        assert bucket == history.BUCKET
        if self.interrupt_read == path:
            self.interrupt_read = None
            raise TimeoutError('checkpoint readback interrupted')
        raw, self.read_etags[bucket, path] = self.objects[path]
        return raw

    def _location(self, bucket):
        assert bucket == history.BUCKET
        return legacy.LOCATION

    def _client(self, namespace):
        assert namespace == legacy.LOCATION[0]
        return self

    def put_object(self, *, Key, **kwargs):
        previous, self.fault = self.fault, self.faults.pop(Key, None)
        try:
            return super().put_object(Key=Key, **kwargs)
        except TimeoutError:
            if self.fault == 'after':
                self.interrupt_read = Key
            raise
        finally:
            self.fault = previous


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ImageHistoryTests()
        self.f.setUp()
        self.f.legacy()
        physical = shard_fixtures.ShardSpliceTests()
        physical.setUp()
        self.f.new_row = physical.new
        self.f.preflight = replace(self.f.preflight, new=replace(self.f.preflight.new,
                                   root=physical.new['root'], path=physical.new['path']))
        self.f.set_parent_row(self.f.old_row)
        self.canonical = self.f.seal()
        self.pin = physical.pin
        self.parent = physical.shard
        self.intent = subject.seal(self.canonical, shard=self.parent, pin=self.pin)
        self.shard = self.pin.path
        self.path = legacy._path(self.intent.claim_key)
        self.s3 = Bucket()
        self.s3.objects = {self.shard: (self.parent.raw, self.pin.etag),
                           history.CANONICAL: (self.canonical.parent, self.canonical.parent_etag)}
        self.journal = subject.S3TransitionJournal(self.s3)
        self.checkpoints = ConditionalCheckpointStore(self.s3)
        self.fresh_calls, self.fence_calls = [], []
        self.protection = self.canonical.preflight
        self.fenced = True
        self.start = lambda **kwargs: self.run_phase(subject.start, **kwargs)
        self.reconcile = lambda **kwargs: self.run_phase(subject.reconcile, **kwargs)

    def run_phase(self, fn, *, intent=None, **kwargs):
        return fn(self.journal, intent or self.intent,
                  **({'checkpoints': self.checkpoints, 'revalidate': self.fresh,
                      'require_fence': self.fence} | kwargs))

    def fresh(self, intent, phase):
        self.fresh_calls.append(phase)
        return history.Checks(self.protection, self.f.project(json.loads(intent.canonical.parent)),
                              self.f.project(json.loads(intent.canonical.candidate)))

    def fence(self, _, phase):
        self.fence_calls.append(phase)
        return self.fenced

    def state(self):
        return self.journal.load(self.intent.claim_key)[1].state

    def index_puts(self):
        return [(key, condition) for key, condition in self.s3.puts if key != self.path]

    def test_exact_two_indexes_then_journal_only_proof(self):
        self.assertEqual(self.intent.claim_key, self.canonical.claim_key)
        self.start()
        self.assertEqual(self.state(), 'shard-permitted')
        self.assertEqual(self.s3.objects[self.shard][0], self.intent.shard_target)
        self.assertEqual(self.s3.objects[history.CANONICAL], (self.canonical.parent, self.canonical.parent_etag))
        shard_target = json.loads(self.intent.shard_target)
        self.assertEqual(shard_target, {**self.parent.value, 'checkpoint_operation': self.canonical.operation,
                         'files': [self.f.new_row, *self.parent.value['files'][1:]]})
        # Recovery uses the serialized seal and a new journal instance.
        self.journal = subject.S3TransitionJournal(self.s3)
        persisted, _ = self.journal.load(self.intent.claim_key)
        self.assertIsNot(persisted, self.intent)
        self.assertEqual(persisted, self.intent)
        result = self.reconcile(intent=persisted)
        self.assertEqual(self.s3.objects[history.CANONICAL][0], self.canonical.candidate)
        self.assertEqual(result, subject.Record(self.intent.sha256, 'applied', '"put-2"', '"put-4"'))
        self.assertEqual(self.reconcile(), result)
        self.assertEqual(self.s3.puts, [
            (self.path, {'IfNoneMatch': '*'}), (self.shard, {'IfMatch': self.pin.etag}),
            (self.path, {'IfMatch': '"put-1"'}), (history.CANONICAL, {'IfMatch': self.canonical.parent_etag}),
            (self.path, {'IfMatch': '"put-3"'}),
        ])
        self.assertEqual(self.fresh_calls, ['parents', 'parents', 'shard-target', 'shard-target',
                                           'both-targets', 'both-targets'])
        self.assertEqual(self.fence_calls, [p for p in self.fresh_calls for _ in range(2)])
        self.assertEqual(set(self.s3.objects), {self.path, self.shard, history.CANONICAL})

    def test_seal_or_start_rejects_unreviewed_shard_and_nonexact_parents(self):
        with self.assertRaisesRegex(ValueError, 'owning image shard'):
            subject.seal(self.canonical, shard=self.parent, pin=replace(self.pin, path=history.CANONICAL))
        value = {**self.parent.value, 'files': self.parent.value['files'][1:]}
        missing = shard_fixtures.snapshot(value, self.parent.etag)
        pin = replace(self.pin, bytes=len(missing.raw), sha256=missing.raw_sha256)
        with self.assertRaisesRegex(ValueError, 'not already mapped'):
            subject.seal(self.canonical, shard=missing, pin=pin)
        wrong_root = replace(self.intent, shard_target=self.intent.shard_target.rstrip())
        with self.assertRaisesRegex(ValueError, 'sealed physical shard delta'):
            self.start(intent=wrong_root)
        for path, (raw, etag) in tuple(self.s3.objects.items()):
            for observed in ((history._encode(json.loads(raw)), etag), (raw, '"rotated"')):
                with self.subTest(path=path, observed=observed[1]):
                    self.s3.objects[path] = observed
                    with self.assertRaisesRegex(ValueError, 'parent differs'):
                        self.start()
            self.s3.objects[path] = raw, etag
        self.assertEqual(self.s3.puts, [])

    def test_shard_put_before_commit_leaves_spent_permit_even_with_foreign_target(self):
        self.s3.faults[self.shard] = 'before'
        with self.assertRaisesRegex(RuntimeError, 'unconfirmed or conflicted'):
            self.start()
        self.assertEqual(self.state(), 'shard-permitted')
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.reconcile()
        self.s3.objects[self.shard] = self.intent.shard_target.rstrip(), '"foreign"'
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.reconcile()
        alternate = subject.seal(self.f.seal(operation='f' * 32), shard=self.parent, pin=self.pin)
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.start(intent=alternate)
        self.assertEqual(self.index_puts(), [(self.shard, {'IfMatch': self.pin.etag})])

    def test_lost_shard_put_with_exact_target_can_win_canonical_permit(self):
        self.s3.faults[self.shard] = 'after'
        with self.assertRaisesRegex(TimeoutError, 'readback interrupted'):
            self.start()
        self.assertEqual(self.state(), 'shard-permitted')
        self.assertEqual(self.reconcile().state, 'applied')
        self.assertEqual([key for key, _ in self.index_puts()], [self.shard, history.CANONICAL])

    def test_lost_shard_permit_after_commit_never_authorizes_index_write(self):
        self.s3.faults[self.path] = 'after'
        with self.assertRaises(legacy.WriteUnconfirmed):
            self.start()
        self.assertEqual(self.state(), 'shard-permitted')
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.reconcile()
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.start()
        self.assertEqual(self.index_puts(), [])

    def test_failed_or_lost_canonical_permit_distinguishes_durable_state(self):
        for fault, expected in (('before', 'shard-permitted'), ('after', 'canonical-permitted')):
            with self.subTest(fault=fault):
                self.setUp()
                self.start()
                self.s3.faults[self.path] = fault
                with self.assertRaises(legacy.WriteUnconfirmed):
                    self.reconcile()
                self.assertEqual(self.state(), expected)
                self.assertEqual(len(self.index_puts()), 1)
                if fault == 'before':
                    self.assertEqual(self.reconcile().state, 'applied')
                else:
                    with self.assertRaisesRegex(ValueError, 'do not replay'):
                        self.reconcile()
                    self.assertEqual(len(self.index_puts()), 1)

    def test_losing_identical_canonical_permit_race_cannot_write(self):
        self.start()
        def winner():
            current = self.journal.load(self.intent.claim_key)[1]
            record = subject.Record(self.intent.sha256, 'canonical-permitted', self.s3.objects[self.shard][1])
            self.assertTrue(self.journal.advance(self.intent.claim_key, current, record))

        self.s3.before_put = winner
        with self.assertRaisesRegex(RuntimeError, 'advance lost'):
            self.reconcile()
        self.assertEqual(self.state(), 'canonical-permitted')
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.reconcile()
        self.assertEqual([key for key, _ in self.index_puts()], [self.shard])

    def test_canonical_put_before_commit_cannot_be_replayed(self):
        self.start()
        self.s3.faults[history.CANONICAL] = 'before'
        with self.assertRaisesRegex(RuntimeError, 'unconfirmed or conflicted'):
            self.reconcile()
        self.assertEqual(self.state(), 'canonical-permitted')
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.reconcile()
        self.s3.objects[history.CANONICAL] = self.canonical.candidate.rstrip(), '"foreign"'
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.reconcile()
        self.assertEqual([key for key, _ in self.index_puts()], [self.shard, history.CANONICAL])

    def test_lost_canonical_put_and_lost_applied_response_only_reobserve(self):
        self.start()
        self.s3.faults[history.CANONICAL] = 'after'
        with self.assertRaisesRegex(TimeoutError, 'readback interrupted'):
            self.reconcile()
        self.assertEqual(self.state(), 'canonical-permitted')
        self.s3.faults[self.path] = 'after'
        with self.assertRaises(legacy.WriteUnconfirmed):
            self.reconcile()
        self.assertEqual(self.state(), 'applied')
        self.assertEqual(self.reconcile().state, 'applied')
        self.assertEqual([key for key, _ in self.index_puts()], [self.shard, history.CANONICAL])
        for path in (self.shard, history.CANONICAL):
            raw, etag = self.s3.objects[path]
            self.s3.objects[path] = raw, '"rotated"'
            with self.assertRaisesRegex(ValueError, 'do not replay'):
                self.reconcile()
            self.s3.objects[path] = raw, etag

    def test_protection_or_fence_loss_after_journal_permit_blocks_index_put(self):
        for which, revoke, message in (
                ('shard', lambda: setattr(self, 'fenced', False), 'fence not confirmed'),
                ('canonical', lambda: setattr(self, 'fenced', False), 'fence not confirmed'),
                ('canonical', lambda: setattr(self, 'protection', replace(self.protection, pins_sha256='0' * 64)),
                 'preflight differs')):
            with self.subTest(which=which, message=message):
                self.setUp()
                if which == 'canonical':
                    self.start()
                self.s3.before_put = revoke
                with self.assertRaisesRegex(ValueError, message):
                    (self.start if which == 'shard' else self.reconcile)()
                self.fenced, self.protection = True, self.canonical.preflight
                self.assertEqual(self.state(), which + '-permitted')
                with self.assertRaisesRegex(ValueError, 'do not replay'):
                    self.reconcile()
                self.assertEqual(len(self.index_puts()), which == 'canonical')

    def test_fresh_checks_and_readback_needed_at_each_observation(self):
        self.fenced = False
        with self.assertRaisesRegex(ValueError, 'fence not confirmed'):
            self.start()
        self.assertFalse(self.s3.puts)
        self.fenced = True
        self.start()
        self.protection = replace(self.protection, rollback_sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'preflight differs'):
            self.reconcile()
        self.assertEqual(self.state(), 'shard-permitted')
        self.protection = self.canonical.preflight
        shard = self.s3.objects[self.shard]

        def rotate_during_check(intent, phase):
            self.s3.objects[self.shard] = shard[0], '"rotated"'
            return self.fresh(intent, phase)

        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.reconcile(revalidate=rotate_during_check)
        self.s3.objects[self.shard] = shard
        self.assertEqual(self.reconcile().state, 'applied')
        self.protection = replace(self.protection, new=replace(self.protection.new, size=10))
        with self.assertRaisesRegex(ValueError, 'preflight differs'):
            self.reconcile()
        self.fenced = False
        with self.assertRaisesRegex(ValueError, 'fence not confirmed'):
            self.reconcile()

    def test_shared_namespace_fails_closed_both_ways_and_on_malformed_v3(self):
        previous = legacy.S3ClaimStore(self.s3)
        key = self.intent.claim_key
        claim = history.Record(self.canonical.sha256, 'claimed')
        self.assertTrue(previous.claim(key, self.canonical, claim))
        self.assertEqual(previous.load(key), (self.canonical, claim))
        v2, etag = self.s3.objects[self.path]
        before = self.s3.puts.copy()
        for raw in (v2, history._encode(json.loads(v2) | {'version': 1}), b'{}'):
            self.s3.objects[self.path] = raw, etag
            with self.assertRaisesRegex(ValueError, 'unsupported'):
                self.start()
            with self.assertRaisesRegex(ValueError, 'unsupported'):
                self.reconcile()
            self.assertEqual(self.s3.puts, before)
            self.assertEqual(self.s3.objects[self.path], (raw, etag))
        self.s3.puts.clear()
        del self.s3.objects[self.path]
        self.start()
        raw, etag = self.s3.objects[self.path]
        with self.assertRaisesRegex(ValueError, 'invalid image history claim envelope'):
            previous.load(key)
        self.assertFalse(previous.claim(key, self.canonical, claim))
        self.assertEqual(self.s3.objects[self.path], (raw, etag))
        for tampered in (raw.replace(b'"version": 3', b'"version": 3, "version": 3'),
                         history._encode(json.loads(raw) | {'shard_target': '!!!!'})):
            self.s3.objects[self.path] = tampered, etag
            with self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual([key for key, _ in self.index_puts()], [self.shard])


if __name__ == '__main__':
    unittest.main()
