"""Fake S3/Reader only: actual transport, planner, and journal code."""
import gzip
import io
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from scripts import reader_ebook_rebuild_transport as subject, reader_lifecycle as lifecycle
from scripts.reader_bucket_store import S3NotFound
from scripts.reader_image_history_store import WriteUnconfirmed

B, index, legacy, v3 = subject.BUCKET, subject.index, subject.legacy, subject.v3
NOW = '2026-11-23T00:00:00+00:00'
OP = '1' * 32
REVIEW_SHA = 'a' * 64


def digest(raw):
    return sha256(raw).hexdigest()


def tag(raw):
    return f'"{digest(raw)}"'


def s3_error(code, status):
    return ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'mock')


class MemoryS3:
    def __init__(self):
        self.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts': 1}))
        self.data, self.read_etags, self.puts = {}, {}, []
        self.lose_response = lambda _: False

    def _location(self, bucket):
        return tuple(bucket.rsplit('/', 1)) if '/' in bucket else ('vomebook', bucket)

    def _client(self, namespace):
        assert namespace == 'vomebook'
        return self

    def read_bytes(self, bucket, path):
        assert bucket == B
        if path not in self.data:
            raise S3NotFound(path)
        raw = self.data[path]
        self.read_etags[bucket, path] = tag(raw)
        return raw

    def get_object(self, *, Bucket, Key):
        assert Bucket == self._location(B)[1]
        try:
            raw = self.read_bytes(B, Key)
        except S3NotFound:
            raise s3_error('NoSuchKey', 404) from None
        return {'Body': io.BytesIO(raw), 'ETag': tag(raw), 'ContentLength': len(raw),
                'ResponseMetadata': {'HTTPStatusCode': 200}}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        assert Bucket == self._location(B)[1]
        condition = {k: v for k, v in kwargs.items() if k.startswith('If')}
        assert len(condition) == 1, 'every write in the wrapper must be conditional'
        self.puts.append((Key, condition))
        prior = self.data.get(Key)
        if (condition.get('IfNoneMatch') == '*' and prior is not None or
                'IfMatch' in condition and (prior is None or condition['IfMatch'] != tag(prior))):
            raise s3_error('PreconditionFailed', 412)
        raw = Body if isinstance(Body, bytes) else Body.read()
        if 'ContentLength' in kwargs:
            assert len(raw) == kwargs['ContentLength']
        self.data[Key] = raw
        if self.lose_response(Key):
            raise TimeoutError('lost write response')
        return {'ETag': tag(raw), 'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 0}}


class Reader:
    def __init__(self, data):
        self.data = data
        self.stale = {}
        self.assert_serialized_writer = Mock()

    def read_bytes(self, bucket, path):
        assert bucket == B
        try:
            return self.stale[path] if path in self.stale else self.data[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def digest(self, bucket, path):
        return digest(self.read_bytes(bucket, path))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.s3 = MemoryS3()
        self.reader = Reader(self.s3.data)
        self.guard = Mock(return_value=True)
        self.io = subject.GuardedTransport(self.s3, self.reader, guard=self.guard)
        temp = self.enterContext(TemporaryDirectory())
        self.bundle = Path(temp)
        self.key = 'Org/Books\0one.epub'
        old_manifest, new_manifest = b'prior manifest bytes', b'new manifest bytes'
        source = 'a' * 64
        old_root, new_root = (f'{index.ROOT}/{source}/{n * 16}' for n in ('b', 'c'))
        self.spec = index.Replacement(self.key, source, old_root, f'{old_root}/{index.MANIFEST}', None,
                                      digest(old_manifest), new_root, f'{new_root}/{index.MANIFEST}',
                                      f'{new_root}/{index.SEARCH}', digest(new_manifest), 1)
        self.s3.data[self.spec.old_manifest] = old_manifest
        self.members = []
        for name, raw in ((index.MANIFEST, new_manifest), (index.SEARCH, b'search'), ('one.xhtml', b'chapter')):
            (self.bundle / name).write_bytes(raw)
            self.members.append({'path': name, 'sha256': digest(raw), 'bytes': len(raw)})
        self.read_bundle = self.enterContext(patch.object(subject.review_io, 'read_bundle',
            return_value=({'chapters': [None]}, self.members, 'c' * 16)))
        row = dict(key=self.key, repo='Org/Books', path='one.epub', bucket=B, extension='epub',
                   mode='chapter-stream', manifest=self.spec.old_manifest, root=old_root, source_sha256=source)
        self.parent_index = b' ' + v3.encode(dict(version=1, kind=index.KIND, files=[row]))
        self.s3.data[index.INDEX] = self.parent_index
        self.target_index = index.build_successor(self.parent_index, reviewed=[self.spec], operation_id=OP)

        def sidecar(manifest):
            return {'v': 1, 'f': {self.key: {'s': 2, 'm': 'e', 'p': manifest, 'b': B, 'c': manifest, 'cb': B}}}

        initial = sidecar(self.spec.old_manifest)
        self.before = gzip.compress(v3.encode(initial), mtime=0)
        self.after = gzip.compress(v3.encode(sidecar(self.spec.new_manifest)), mtime=0)
        self.s3.data[legacy.SIDECAR] = self.before
        catalog, _ = lifecycle.catalog_generation(initial, now=NOW)
        self.legacy_raw = v3.encode(catalog)
        self.s3.data[legacy.CATALOG] = self.legacy_raw
        self.legacy_parent = legacy.Version(digest(self.legacy_raw), len(self.legacy_raw), tag(self.legacy_raw))
        old_v3 = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'parent_generation': None}
        pointer = v3.commit_catalog(self.reader, old_v3, None)['pointer']
        self.s3.data[pointer['catalog']['path']] = v3.encode(old_v3)
        self.parent_pointer = b' ' + v3.encode(pointer)
        self.s3.data[v3.POINTER] = self.parent_pointer
        self.s3.data[legacy.SIDECAR] = self.after
        target_pointer = v3.unify(self.reader, expected_pointer=pointer, expected_canonical=digest(self.after))['pointer']
        self.s3.data[legacy.SIDECAR] = self.before
        summary = subject.plan_legacy(io.BytesIO(self.legacy_raw), io.BytesIO(), before=self.before, after=self.after,
                                     old={self.key: row}, new={self.key: {'manifest': self.spec.new_manifest}},
                                     now=NOW, parent_sha=self.legacy_parent.sha256)
        self.review = dict(kind='ebook-rebuild-review', remote_writes=0, operation=OP, timestamp=NOW,
            canonical_replacements=[asdict(self.spec)], canonical_parent_sha256=digest(self.parent_index),
            target_index_sha256=digest(self.target_index), sidecar_parent_sha256=digest(self.before),
            target_sidecar_sha256=digest(self.after), reader_generation=pointer['generation'],
            target_pointer=target_pointer, legacy=summary, changes=[dict(key=self.key, source_sha256=source,
                prior_manifest=self.spec.old_manifest, prior_manifest_sha256=digest(old_manifest),
                new_manifest=self.spec.new_manifest, members=self.members)])

    def stage(self, **options):
        return self.io.stage(review=self.review, review_sha256=REVIEW_SHA, bundles={self.key: self.bundle},
                             after_sidecar=self.after, **options)

    def canonical(self, staged, **options):
        return self.io.canonical(review=self.review, review_sha256=REVIEW_SHA, staged=staged,
                                 parent_raw=self.parent_index, parent_etag=tag(self.parent_index), **options)

    def legacy(self, **options):
        return self.io.legacy(review=self.review, parent_catalog=io.BytesIO(self.legacy_raw),
                              parent=self.legacy_parent, before_sidecar=self.before, after_sidecar=self.after,
                              sidecar_etag=tag(self.before), parent_raw=self.parent_pointer, **options)

    def test_protected_staging_and_ordered_release(self):
        staged = self.stage()
        paths = [p for p, _ in self.s3.puts]
        self.assertTrue(paths[0].startswith(subject.STAGE_PREFIX))
        self.assertTrue(all(p.startswith('reader-index/processing/epub-exact-') for p in paths[1:3]))
        self.assertTrue(all(p.startswith(self.spec.new_root + '/') for p in paths[3:6]))
        self.assertEqual(self.stage(observe_only=True), staged)
        self.assertEqual(paths, [p for p, _ in self.s3.puts])
        self.assertEqual(self.canonical(staged)['status'], 'written')
        self.assertEqual(self.canonical(staged, resume=True)['status'], 'observed')
        moved = self.legacy()
        self.assertEqual(moved, self.legacy(resume=True))
        with self.assertRaisesRegex(ValueError, 'bind the reviewed v3 parent'):
            self.io.promote(review=self.review, parent_raw=v3.encode(self.review['target_pointer']), legacy_result=moved)
        promoted = self.io.promote(review=self.review, parent_raw=self.parent_pointer, legacy_result=moved)
        self.assertEqual(promoted['pointer'], self.review['target_pointer'])
        self.assertFalse(promoted['pages_dispatched'])
        before = list(self.s3.puts)
        self.assertEqual(promoted, self.io.promote(review=self.review, parent_raw=self.parent_pointer,
                                                  legacy_result=moved, observe_only=True))
        self.assertEqual(self.s3.puts, before)
        mutable = [pair for pair in before if pair[0] in {index.INDEX, legacy.CATALOG, legacy.SIDECAR, v3.POINTER}]
        self.assertEqual(mutable, [(index.INDEX, {'IfMatch': tag(self.parent_index)}),
                                  (legacy.CATALOG, {'IfMatch': tag(self.legacy_raw)}),
                                  (legacy.SIDECAR, {'IfMatch': tag(self.before)}),
                                  (v3.POINTER, {'IfMatch': tag(self.parent_pointer)})])

    def test_lease_loss_blocks_members_and_no_stage_replay(self):
        self.guard.side_effect = lambda phase: phase != 'stage:member-put'
        with self.assertRaisesRegex(RuntimeError, 'guard refused'):
            self.stage()
        self.assertEqual(len(self.s3.puts), 3)
        self.guard.side_effect = None
        with self.assertRaisesRegex(ValueError, 'permit already spent'):
            self.stage()
        with self.assertRaisesRegex(ValueError, 'staged EPUB member missing'):
            self.stage(observe_only=True)
        self.assertEqual(len(self.s3.puts), 3)

    def test_canonical_claim_or_cas_unknown_does_not_replay(self):
        staged = self.stage()
        self.s3.lose_response = lambda key: key == index.INDEX
        with self.assertRaisesRegex(RuntimeError, 'CAS unconfirmed'):
            self.canonical(staged)
        with self.assertRaisesRegex(RuntimeError, 'already spent'):
            self.canonical(staged)
        self.assertEqual(self.canonical(staged, resume=True)['status'], 'observed')
        self.assertEqual(sum(k == index.INDEX for k, _ in self.s3.puts), 1)

    def test_lost_canonical_claim_cannot_authorize_put(self):
        staged = self.stage()
        self.s3.lose_response = lambda key: key.startswith(index.PERMIT_PREFIX)
        with self.assertRaises(WriteUnconfirmed):
            self.canonical(staged)
        self.assertFalse(any(k == index.INDEX for k, _ in self.s3.puts))
        self.assertEqual(self.s3.data[index.INDEX], self.parent_index)

    def test_member_reader_drift_blocks_canonical_before_claim(self):
        staged = self.stage()
        self.reader.stale[self.spec.new_manifest] = b'stale'
        before = list(self.s3.puts)
        with self.assertRaisesRegex(ValueError, 'staged EPUB member'):
            self.canonical(staged)
        self.assertEqual(self.s3.puts, before)

    def test_legacy_lost_catalog_response_only_advances_unspent_sidecar(self):
        self.canonical(self.stage())
        self.s3.lose_response = lambda key: key == legacy.CATALOG
        with self.assertRaises(TimeoutError):
            self.legacy()
        self.assertEqual(self.s3.data[legacy.SIDECAR], self.before)
        self.assertEqual(self.legacy(resume=True)['sha256'], digest(self.after))
        self.assertEqual([k for k, _ in self.s3.puts if k in {legacy.CATALOG, legacy.SIDECAR}],
                         [legacy.CATALOG, legacy.SIDECAR])

    def test_v3_lost_pointer_response_only_observes_original_parent_permit(self):
        self.canonical(self.stage())
        moved = self.legacy()
        args = dict(review=self.review, parent_raw=self.parent_pointer, legacy_result=moved)
        self.s3.lose_response = lambda key: key == v3.POINTER
        with self.assertRaises(TimeoutError):
            self.io.promote(**args)
        self.assertTrue(self.io.promote(**args, observe_only=True)['confirmed'])
        claim = subject.v3_permit.PREFIX + digest(self.parent_pointer) + '.json'
        self.assertEqual([k for k, _ in self.s3.puts if k in {claim, v3.POINTER}], [claim, v3.POINTER])

    def test_single_attempt_and_scope_required(self):
        self.s3.meta.config.retries = {'total_max_attempts': 2}
        with self.assertRaisesRegex(ValueError, 'single-attempt'):
            subject.GuardedTransport(self.s3, self.reader, guard=self.guard)
        with self.assertRaisesRegex(ValueError, 'canonical permit path'):
            self.io.claims.claim(B, 'reader-index/unrelated.json', b'junk')
        with self.assertRaisesRegex(ValueError, 'exact EPUB canonical root'):
            self.io.checkpoints.read(B, 'chapters/ebook/mobi/index.json')
        self.assertEqual(self.s3.puts, [])
