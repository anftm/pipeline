import gzip
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from scripts import publish_reader_v3 as v3, reader_image_history_catalog as catalog
from scripts import reader_image_history_publish as publish, reader_lifecycle as lifecycle

KEY = 'Owner/Repo\0history.png'
WEBP = f"pages/image/png/{'a' * 64}/document.webp"
AVIF = f"pages/image/png/{'b' * 64}/document.avif"
NOW = '2026-11-21T00:00:00+00:00'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        def base(member):
            return {'v': 1, 'f': {KEY: {'s': 2, 'm': 'i', 'b': publish.ASSETS, 'p': member}}}

        self.before = gzip.compress(v3.encode(base(WEBP)), mtime=0)
        self.after = gzip.compress(v3.encode(base(AVIF)), mtime=0)
        legacy, self.old = lifecycle.catalog_generation(base(WEBP), now=NOW)
        legacy['generations'][self.old]['acks'] = {'hf': True, 'pages': True}
        legacy['retired_generations'] = {'c' * 32: {'opaque': 'retained'}}
        self.parent = v3.encode(legacy)
        self.plan = catalog.plan(io.BytesIO(self.parent), io.BytesIO(), key=KEY, webp_path=WEBP, avif_path=AVIF,
                                 before_sidecar=self.before, after_sidecar=self.after, now=NOW)
        self.snapshot = self.plan['snapshot']['path']
        old_v3 = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'parent_generation': None}
        old_v3['generation'] = sha(v3.encode(old_v3))
        path = f"reader-index/v3/generations/{old_v3['generation']}/catalog.json"
        ref = v3.metadata(publish.ASSETS, path, v3.encode(old_v3))
        self.pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': old_v3['generation'],
                        'catalog': ref, 'resources': [ref]}
        self.data = {publish.CATALOG: self.parent, publish.SIDECAR: self.before, self.snapshot: self.after,
                     v3.POINTER: v3.encode(self.pointer), path: v3.encode(old_v3)}
        self.tags = {publish.CATALOG: '"catalog-parent"', publish.SIDECAR: '"sidecar-parent"'}
        self.parent_version = self.observe(publish.CATALOG)
        self.sidecar_etag = self.tags[publish.SIDECAR]
        self.record = None
        self.attempts = []
        self.fail = None
        self.guard = lambda phase: True

    def read_bytes(self, bucket, path):
        self.assertEqual(bucket, publish.ASSETS)
        self.assertNotEqual(path, publish.CATALOG, 'legacy catalog must not use read_bytes')
        return self.data[path]

    def observe(self, path):
        raw = self.data[path]
        return publish.Version(sha(raw), len(raw), self.tags[path])

    def cas(self, path, stream, *, size, digest, parent_etag):
        self.attempts.append(path)
        state = 'catalog-permitted' if path == publish.CATALOG else 'sidecar-permitted'
        self.assertEqual(self.record.state, state)
        if path == publish.CATALOG:
            stream.fileno()  # Must be a file, not an in-memory copy of the catalog.
            self.assertEqual(self.data[publish.SIDECAR], self.before)
        else:
            self.assertEqual(self.record.catalog_etag, self.tags[publish.CATALOG])
        raw = stream.read()
        self.assertEqual((size, digest, parent_etag), (len(raw), sha(raw), self.tags[path]))
        if self.fail == (path, 'before'):
            raise OSError('lost PUT')
        self.data[path], self.tags[path] = raw, f'"{sha(raw)}"'
        if self.fail == (path, 'after'):
            raise OSError('lost PUT')
        return '"wrong-etag"' if self.fail == (path, 'etag') else self.tags[path]

    # In-memory stand-in for a durable linearizable journal.
    def read(self, key):
        self.assertEqual(key, self.parent_version.sha256)
        return self.record

    def claim(self, key, record):
        return self.advance(key, None, record)

    def advance(self, key, expected, record):
        if self.read(key) != expected:
            return False
        self.record = record
        return True

    def run_publish(self, **overrides):
        kwargs = dict(parent=self.parent_version, before_sidecar=self.before, sidecar_etag=self.sidecar_etag,
                      after_sidecar=self.after, key=KEY, webp_path=WEBP, avif_path=AVIF, now=NOW,
                      expected_pointer_raw=v3.encode(self.pointer), journal=self, observe=self.observe,
                      cas=self.cas, store=self, guard=self.guard)
        return publish.publish(io.BytesIO(self.parent), **(kwargs | overrides))

    def assert_admission(self, result):
        self.assertEqual(self.attempts, [publish.CATALOG, publish.SIDECAR])
        self.assertEqual((result['claim_id'], result['seal']), (sha(self.parent), self.record.seal))
        self.assertEqual(result['generation'], self.plan['generation'])
        self.assertEqual(result['snapshot'], self.plan['snapshot'])
        self.assertEqual(result['retirement_hint'], self.plan['retirement_hint'])
        self.assertIs(result['deletion_authorized'], False)
        self.assertEqual(result['catalog'], self.observe(publish.CATALOG))
        self.assertEqual(result['sidecar'], self.observe(publish.SIDECAR))
        self.assertEqual(self.record.state, 'legacy-observed')
        self.assertEqual(self.record.catalog_etag, self.tags[publish.CATALOG])
        self.assertEqual(self.record.sidecar_etag, self.tags[publish.SIDECAR])
        self.assertEqual(result['unify'], {'expected_pointer': self.pointer, 'expected_canonical': sha(self.after),
                                           'conditional_pointer': True})
        self.assertFalse(v3.unify(self, **result['unify'])['applied'])

    def test_ordered_publication_preserves_history(self):
        result = self.run_publish()
        self.assert_admission(result)
        actual = json.loads(self.data[publish.CATALOG])
        before = json.loads(self.parent)
        self.assertEqual(actual['retired_generations'], before['retired_generations'])
        prior = actual['generations'][self.old]
        self.assertEqual(prior, before['generations'][self.old] | {'status': 'superseded', 'superseded_at': NOW})
        self.assertEqual(actual['generations'][result['generation']]['snapshot'], result['snapshot'])
        self.assertEqual(self.run_publish(resume=True), result)
        self.assertEqual(self.attempts, [publish.CATALOG, publish.SIDECAR])

    def test_lost_put_responses_recover_without_replay(self):
        for path, state in ((publish.CATALOG, 'catalog-permitted'), (publish.SIDECAR, 'sidecar-permitted')):
            self.fail = path, 'after'
            with self.assertRaisesRegex(OSError, 'lost PUT'):
                self.run_publish(resume=self.record is not None)
            self.assertEqual(self.record.state, state)
        self.assert_admission(self.run_publish(resume=True))

    def test_catalog_permit_is_spent_even_if_parent_did_not_move(self):
        self.fail = publish.CATALOG, 'before'
        with self.assertRaisesRegex(OSError, 'lost PUT'):
            self.run_publish()
        with self.assertRaisesRegex(ValueError, 'already claimed'):
            self.run_publish()
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.run_publish(resume=True)
        self.assertEqual(self.attempts, [publish.CATALOG])

    def test_guard_failure_after_sidecar_permit_cannot_replay_or_complete(self):
        self.guard = lambda phase: self.record is None or self.record.state != 'sidecar-permitted'
        with self.assertRaisesRegex(ValueError, 'guard not confirmed'):
            self.run_publish()
        self.assertEqual(self.record.state, 'sidecar-permitted')
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.run_publish(resume=True, guard=lambda _: True)
        self.assertEqual(self.attempts, [publish.CATALOG])

    def test_final_guard_failure_withholds_admission(self):
        self.guard = lambda phase: phase != 'both-targets'
        with self.assertRaisesRegex(ValueError, 'guard not confirmed'):
            self.run_publish()
        self.assertEqual(self.record.state, 'sidecar-permitted')
        self.assert_admission(self.run_publish(resume=True, guard=lambda _: True))

    def test_pin_readback_to_response_and_durable_etags(self):
        self.fail = publish.CATALOG, 'etag'
        with self.assertRaisesRegex(ValueError, 'bytes or ETag differ'):
            self.run_publish()
        self.assertEqual(self.attempts, [publish.CATALOG])
        self.assert_admission(self.run_publish(resume=True))
        self.tags[publish.SIDECAR] = '"same-bytes-new-etag"'
        with self.assertRaisesRegex(ValueError, 'bytes or ETag differ'):
            self.run_publish(resume=True)
        self.assertEqual(self.attempts, [publish.CATALOG, publish.SIDECAR])

    def test_precreated_snapshot_and_exact_pointer_required(self):
        self.data[self.snapshot] = self.before
        with self.assertRaisesRegex(ValueError, 'precreated legacy snapshot differs'):
            self.run_publish()
        self.data[self.snapshot] = self.after
        self.data[v3.POINTER] += b'\n'
        with self.assertRaisesRegex(ValueError, 'exact v3 pointer changed'):
            self.run_publish()
        self.assertIsNone(self.record)
        self.assertEqual(self.attempts, [])

    def test_ambiguous_journal_claim_never_authorizes_put(self):
        def lose_claim(key, record):
            self.assertTrue(self.advance(key, None, record))
            raise OSError('lost claim')

        with patch.object(self, 'claim', side_effect=lose_claim):
            with self.assertRaisesRegex(OSError, 'lost claim'):
                self.run_publish()
        with self.assertRaisesRegex(ValueError, 'do not replay'):
            self.run_publish(resume=True)
        self.assertEqual(self.attempts, [])
