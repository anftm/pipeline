"""Exact conditionals and recovery, using only in-memory S3 and checkpoints."""
import io
import json
import unittest
from base64 import b64encode
from dataclasses import asdict, replace
from types import SimpleNamespace

from scripts import reader_gc_cache, reader_gc_graph as gc, reader_image_history as history
from scripts import reader_image_history_store as adapter
from tests import test_reader_image_history as fixture


class S3Error(Exception):
    def __init__(self, code, status, retries=0):
        self.response = {'Error': {'Code': code}, 'ResponseMetadata': {
            'HTTPStatusCode': status, 'RetryAttempts': retries}}


class MemoryS3:
    def __init__(self):
        self.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts': 1}))
        self.objects, self.puts = {}, []
        self.before_put = lambda: None
        self.fault = None
        self.get_error = None
        self.bad_get = {}
        self.bad_ack = {}

    def get_object(self, *, Bucket, Key):
        assert Bucket == 'reader-assets-v2'
        if self.get_error:
            raise self.get_error
        if Key not in self.objects:
            raise S3Error('NoSuchKey', 404)
        body, etag = self.objects[Key]
        return {'Body': io.BytesIO(body), 'ContentLength': len(body), 'ETag': etag} | self.bad_get

    def put_object(self, *, Bucket, Key, Body, ContentType, **condition):
        assert (Bucket, ContentType) == ('reader-assets-v2', 'application/json')
        self.puts.append((Key, condition))
        before, self.before_put = self.before_put, lambda: None
        before()
        if self.fault == 'before':
            raise TimeoutError('request lost')
        if condition == {'IfNoneMatch': '*'}:
            if Key in self.objects:
                raise S3Error('ConditionalRequestConflict', 409)
        elif Key not in self.objects or condition != {'IfMatch': self.objects[Key][1]}:
            raise S3Error('PreconditionFailed', 412)
        etag = f'"put-{len(self.puts)}"'
        self.objects[Key] = Body, etag
        if self.fault == 'after':
            raise TimeoutError('response lost')
        return {'ETag': etag, 'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 0}} | self.bad_ack


class S3ClaimStoreTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ImageHistoryTests()
        self.f.setUp()
        self.f.legacy()
        self.intent = self.f.intent
        self.key = self.intent.claim_key
        self.path = f'reader-index/gc/image-history-claims/v1/{self.key}.json'
        self.claimed = history.Record(self.intent.sha256, 'claimed')
        self.observed = history.Record(self.intent.sha256, 'observed', '"target"')
        self.finalized = replace(self.observed, state='finalized')
        self.s3 = MemoryS3()
        self.store = adapter.S3ClaimStore(self.s3)

    def claim(self):
        return self.store.claim(self.key, self.intent, self.claimed)

    def test_single_canonical_record_and_finalizer_only_reload(self):
        self.assertIsNone(self.store.load(self.key))
        self.assertTrue(self.claim())
        raw, etag = self.s3.objects[self.path]
        fields = asdict(self.intent) | {name: b64encode(getattr(self.intent, name)).decode('ascii')
                                       for name in ('parent', 'candidate')}
        wire = {'kind': 'reader-image-history-claim', 'version': 2, 'intent': fields,
                'record': asdict(self.claimed)}
        self.assertEqual(raw, (json.dumps(wire, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode())
        self.assertTrue(self.store.advance(self.key, self.claimed, self.observed))

        # Recovery starts from the journal and a pre-existing checkpoint; it
        # must neither use the original Intent object nor call checkpoint PUT.
        self.f.bucket.raw, self.f.bucket.etag = self.intent.candidate, '"target"'
        self.f.journal = adapter.S3ClaimStore(self.s3)
        intent, record = self.f.journal.load(self.key)
        self.assertIsNot(intent, self.intent)
        self.assertEqual((intent, record), (self.intent, self.observed))
        self.assertEqual(intent.sha256, self.intent.sha256)
        self.assertEqual(intent.claim_key, self.key)
        self.assertEqual(intent.parent, self.f.parent_raw)
        self.assertNotEqual(intent.preflight.source.revision, intent.preflight.current_source.revision)
        self.assertEqual(self.f.run_stage(history.finalize, intent=intent), self.finalized)
        self.assertEqual(self.f.run_stage(history.finalize, intent=intent), self.finalized)
        self.assertEqual(self.f.calls, ['after', 'after'])
        self.assertFalse(self.f.bucket.puts)
        self.assertEqual(self.s3.puts, [(self.path, {'IfNoneMatch': '*'}), (self.path, {'IfMatch': etag}),
                                       (self.path, {'IfMatch': '"put-2"'})])
        self.assertEqual(set(self.s3.objects), {self.path})
        self.assertEqual(json.loads(self.s3.objects[self.path][0]), wire | {'record': asdict(self.finalized)})

    def test_create_conflict_never_grants_claim_even_for_identical_bytes(self):
        self.assertTrue(self.claim())
        self.assertFalse(self.claim())
        alt = self.f.seal(operation='3' * 32)
        self.assertEqual(alt.claim_key, self.key)
        self.assertFalse(self.store.claim(self.key, alt, history.Record(alt.sha256, 'claimed')))
        self.assertEqual(self.store.load(self.key), (self.intent, self.claimed))
        self.assertEqual(len(self.s3.puts), 3)

    def test_advance_checks_exact_record_then_cas_even_with_identical_winner(self):
        self.assertFalse(self.store.advance(self.key, self.claimed, self.observed))
        self.assertTrue(self.claim())
        self.assertFalse(self.store.advance(self.key, self.observed, self.finalized))
        for proposed in (self.finalized, replace(self.observed, intent_sha256='0' * 64),
                         replace(self.observed, target_etag='W/"target"')):
            with self.assertRaises(ValueError):
                self.store.advance(self.key, self.claimed, proposed)
        self.assertEqual(len(self.s3.puts), 1)
        self.s3.before_put = lambda: self.assertTrue(adapter.S3ClaimStore(self.s3).advance(
            self.key, self.claimed, self.observed))
        self.assertFalse(self.store.advance(self.key, self.claimed, self.observed))
        self.assertEqual(self.store.read(self.key), self.observed)
        with self.assertRaises(ValueError):
            self.store.advance(self.key, self.observed, replace(self.finalized, target_etag='"other"'))
        self.assertEqual(len(self.s3.puts), 3)

    def test_claim_timeout_before_and_after_never_authorize_checkpoint(self):
        for fault, expected in [('before', None), ('after', (self.intent, self.claimed))]:
            with self.subTest(fault=fault):
                self.f.journal = adapter.S3ClaimStore(s3 := MemoryS3())
                s3.fault = fault
                with self.assertRaisesRegex(adapter.WriteUnconfirmed, 'do not retry'):
                    self.f.run_stage(history.apply_once)
                self.assertEqual(adapter.S3ClaimStore(s3).load(self.key), expected)
                self.assertEqual(s3.puts, [(self.path, {'IfNoneMatch': '*'})])
                self.assertEqual(self.f.bucket.puts, [])

    def test_advance_timeout_before_and_after_requires_explicit_reload(self):
        self.f.bucket.raw, self.f.bucket.etag = self.intent.candidate, '"target"'
        for fault, expected in [('before', self.observed), ('after', self.finalized)]:
            with self.subTest(fault=fault):
                self.f.journal = store = adapter.S3ClaimStore(s3 := MemoryS3())
                self.assertTrue(store.claim(self.key, self.intent, self.claimed))
                self.assertTrue(store.advance(self.key, self.claimed, self.observed))
                s3.fault = fault
                with self.assertRaisesRegex(adapter.WriteUnconfirmed, 'do not retry'):
                    self.f.run_stage(history.finalize)
                self.f.journal = adapter.S3ClaimStore(s3)
                self.assertEqual(self.f.journal.load(self.key), (self.intent, expected))
                if fault == 'after':
                    intent, _ = self.f.journal.load(self.key)
                    self.assertEqual(self.f.run_stage(history.finalize, intent=intent), self.finalized)
                self.assertEqual(len(s3.puts), 3)
                self.assertEqual(self.f.bucket.puts, [])

    def test_acknowledgement_and_readback_are_required(self):
        for ack, get in (({'ETag': 'W/"weak"'}, {}),
                         ({'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 1}}, {}),
                         ({}, {'ETag': '"other"'}), ({}, {'ContentLength': 1}),
                         ({}, {'Body': io.BytesIO(b'{}'), 'ContentLength': 2})):
            with self.subTest(ack=ack, get=get):
                store = adapter.S3ClaimStore(s3 := MemoryS3())
                s3.bad_ack, s3.bad_get = ack, get
                with self.assertRaises(adapter.WriteUnconfirmed):
                    store.claim(self.key, self.intent, self.claimed)
                self.assertEqual(len(s3.puts), 1)

        # A conditional error after a reported retry is not a definitive loss.
        def retried_conflict():
            raise S3Error('PreconditionFailed', 412, retries=1)

        self.s3.before_put = retried_conflict
        with self.assertRaises(adapter.WriteUnconfirmed):
            self.claim()
        self.assertEqual(len(self.s3.puts), 1)
        self.s3.meta.config.retries = {'total_max_attempts': 2}
        with self.assertRaisesRegex(ValueError, 'single-attempt'):
            adapter.S3ClaimStore(self.s3)

    def test_invalid_paths_intent_bindings_and_corrupt_wire_fail_closed(self):
        for key in ('../' + self.key, self.key.upper(), 'a' * 64):
            with self.assertRaises(ValueError):
                self.store.claim(key, self.intent, self.claimed)
        with self.assertRaises(ValueError):
            self.store.claim(self.key, self.intent, replace(self.claimed, intent_sha256='0' * 64))
        self.assertFalse(self.s3.puts)
        self.assertTrue(self.claim())
        raw, etag = self.s3.objects[self.path]
        self.s3.objects[adapter.PREFIX + 'a' * 64 + '.json'] = raw, etag
        with self.assertRaises(ValueError):
            self.store.load('a' * 64)
        for field, value in [('version', True), ('version', 1), ('extra', None),
                             ('intent', json.loads(raw)['intent'] | {'candidate': '!!!!'}),
                             ('record', asdict(self.claimed) | {'intent_sha256': '0' * 64})]:
            self.s3.objects[self.path] = history._encode(json.loads(raw) | {field: value}), etag
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.advance(self.key, self.claimed, self.observed)
        self.s3.objects[self.path] = raw.replace(b'"version": 2', b'"version": 2, "version": 2'), etag
        with self.assertRaises(ValueError):
            self.store.read(self.key)
        self.assertEqual(len(self.s3.puts), 1)
        # A pre-v2 envelope occupying the same key cannot be silently replaced.
        old_wire = json.loads(raw)
        old_wire['version'] = 1
        del old_wire['intent']['preflight']['current_source']
        del old_wire['intent']['preflight']['search_sha256']
        old_raw = history._encode(old_wire)
        self.s3.objects[self.path] = old_raw, etag
        with self.assertRaises(ValueError):
            self.store.load(self.key)
        self.assertFalse(self.claim())
        self.assertEqual(self.s3.objects[self.path], (old_raw, etag))
        for error in (S3Error('NoSuchBucket', 404), S3Error('AccessDenied', 403), TimeoutError()):
            self.s3.get_error = error
            with self.assertRaises(type(error)):
                self.store.load(self.key)

    def test_gc_retains_control_record_without_traversing_its_payload(self):
        self.assertTrue(self.claim())
        path = self.path
        asset = self.intent.preflight.old.path
        objects = {'reader-index/reader_assets.json.gz': b'{"v":1,"f":{}}', path: self.s3.objects[path][0],
                   asset: b'webp'}

        class GCInventory:
            def list_files(self, bucket, prefixes):
                return {p for p in objects if p.startswith(prefixes)} if bucket == history.BUCKET else set()

            def read_bytes(self, bucket, key):
                if key == path:
                    raise AssertionError('GC must not read the journal')
                return objects[key]

        report = gc.ReferenceGraph(GCInventory()).build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        self.assertFalse(gc.is_root(path, history.BUCKET))
        self.assertFalse(reader_gc_cache.cacheable(path))
        self.assertEqual(report['buckets'][history.BUCKET]['retained'], [path])
        self.assertEqual(report['buckets'][history.BUCKET]['candidates'], [asset])
        self.assertNotIn(path, report['buckets'][history.BUCKET]['unmanaged'])


if __name__ == '__main__':
    unittest.main()
