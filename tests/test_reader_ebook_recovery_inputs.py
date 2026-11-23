"""Exercise EPUB raw parent persistence using only fake S3 and Hub reads."""
import gzip
from hashlib import sha256
import io
import json
from types import SimpleNamespace
import unittest

from scripts import reader_ebook_recovery_inputs as subject
from scripts.reader_image_history_store import S3ClaimStore, WriteUnconfirmed


class S3Error(Exception):
    def __init__(self, code, status, retries=0):
        self.response = {'Error': {'Code': code}, 'ResponseMetadata': {
            'HTTPStatusCode': status, 'RetryAttempts': retries}}


def digest(raw):
    return sha256(raw).hexdigest()


class FakeS3:
    def __init__(self, events):
        self.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts': 1}))
        self.events = events
        self.objects = {}
        self.lengths = {}
        self.get_errors = {}
        self.before_put = lambda key: None
        self.after_put = lambda key: None

    def get_object(self, *, Bucket, Key):
        assert Bucket == 'reader-assets-v2'
        self.events.append(('s3', Key))
        if Key in self.get_errors:
            raise self.get_errors[Key]
        if Key not in self.objects:
            raise S3Error('NoSuchKey', 404)
        raw = self.objects[Key]
        return {'Body': io.BytesIO(raw), 'ContentLength': self.lengths.get(Key, len(raw)),
                'ETag': f'"{digest(raw)}"'}

    def put_object(self, *, Bucket, Key, Body, ContentType, **condition):
        assert Bucket == 'reader-assets-v2' and condition == {'IfNoneMatch': '*'}
        self.events.append(('put', Key))
        self.before_put(Key)
        if Key in self.objects:
            raise S3Error('PreconditionFailed', 412)
        self.objects[Key] = Body
        self.after_put(Key)
        return {'ETag': f'"{digest(Body)}"', 'ResponseMetadata': {'HTTPStatusCode': 200, 'RetryAttempts': 0}}


class FakeHub:
    def __init__(self, s3):
        self.s3 = s3
        self.overrides = {}

    def read_bytes(self, bucket, path):
        assert bucket == 'vomebook/reader-assets-v2'
        self.s3.events.append(('hub', path))
        if path in self.overrides:
            raw = self.overrides[path]
            if isinstance(raw, Exception):
                raise raw
            return raw
        try:
            return self.s3.objects[path]
        except KeyError:
            raise FileNotFoundError(path) from None


class RecoveryInputTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.s3 = FakeS3(self.events)
        self.store = S3ClaimStore(self.s3)
        self.hub = FakeHub(self.s3)
        # Noncanonical whitespace, line endings and gzip metadata are identity.
        self.parents = {
            'index': b' { "version": 1, "kind":"ebook-chapter-stream-index", "files": [] }\r\n',
            'sidecar': gzip.compress(b'{ "f" : {}, "v": 1 }\n', compresslevel=1, mtime=42),
            'pointer': b'\n { "kind": "reader-reading-pointer", "version": 1 } ',
        }
        self.review = dict(version=1, kind='ebook-rebuild-review', remote_writes=0,
                           canonical_parent_sha256=digest(self.parents['index']),
                           sidecar_parent_sha256=digest(self.parents['sidecar']),
                           pointer_parent_sha256=digest(self.parents['pointer']))
        self.approval = ('  ' + json.dumps(self.review, indent=4) + '\r\n').encode()
        self.pin = digest(self.approval)
        self.approval_path = f'reader-index/gc/ebook-exact-reviews/v1/{self.pin}.json'
        self.s3.objects[self.approval_path] = self.approval
        self.paths = {name: f'reader-index/gc/ebook-recovery-inputs/v1/{self.pin}/{filename}'
                      for name, filename in [('index', 'index.json'), ('sidecar', 'sidecar.gz'),
                                             ('pointer', 'pointer.json')]}

    def guard(self, phase):
        self.events.append(('guard', phase))
        return True

    def retain(self, **overrides):
        args = dict(review=self.review, review_sha256=self.pin, guard=self.guard, **self.parents)
        subject.retain(self.store, self.hub, **(args | overrides))

    def load(self):
        return subject.load(self.store, self.hub, self.review, self.pin)

    def seed(self):
        self.s3.objects.update({path: self.parents[name] for name, path in self.paths.items()})

    def writes(self):
        return [path for action, path in self.events if action == 'put']

    def test_exact_bytes_guard_readbacks_and_read_only_reload(self):
        self.retain()
        self.assertEqual(self.writes(), list(self.paths.values()))
        self.assertEqual(set(self.s3.objects), {self.approval_path, *self.paths.values()})
        for name, path in self.paths.items():
            self.assertEqual(self.s3.objects[path], self.parents[name])
            i = self.events.index(('put', path))
            self.assertEqual(self.events[i-1], ('guard', f'stage:recovery-input-put:{name}'))
            j = self.events.index(('hub', path))
            self.assertIn(('s3', path), self.events[i+1:j])

        self.events.clear()
        self.retain()  # Reuse exact immutable objects without needing a create.
        self.assertFalse([event for event in self.events if event[0] == 'guard'])
        self.events.clear()
        self.assertEqual(self.load(), self.parents)
        self.assertEqual(self.events, [('s3', self.approval_path)] + [
            (action, path) for path in self.paths.values() for action in ('s3', 'hub')])

    def test_guard_refusal_stops_before_next_create(self):
        path = self.paths['index']
        answers = iter((True, 1))  # Truthy, but not true.
        with self.assertRaisesRegex(RuntimeError, 'guard refused'):
            self.retain(guard=lambda phase: next(answers))
        self.assertEqual(self.writes(), [path])

    def test_concurrent_create_reconciles_only_exact_bytes(self):
        path = self.paths['index']
        for winner in (self.parents['index'], b'other bytes'):
            with self.subTest(winner=winner):
                self.s3.objects = {self.approval_path: self.approval}
                self.events.clear()

                def race(key):
                    if key == path:
                        self.s3.objects[key] = winner

                self.s3.before_put = race
                if winner == self.parents['index']:
                    self.retain()
                    self.assertEqual(self.load(), self.parents)
                    self.assertEqual(self.writes(), list(self.paths.values()))
                else:
                    with self.assertRaisesRegex(ValueError, 'differ'):
                        self.retain()
                    self.assertEqual(self.writes(), [path])
                self.assertEqual(self.s3.objects[path], winner)

    def test_conflict_without_visible_winner_is_not_retried(self):
        def conflict(key):
            raise S3Error('ConditionalRequestConflict', 409)

        self.s3.before_put = conflict
        with self.assertRaises(FileNotFoundError):
            self.retain()
        self.assertEqual(self.writes(), [self.paths['index']])

    def test_s3_read_denied_is_not_missing(self):
        for path in (self.approval_path, self.paths['index']):
            with self.subTest(path=path):
                self.s3.get_errors = {path: S3Error('AccessDenied', 403)}
                for operation in (self.retain, self.load):
                    with self.assertRaises(S3Error):
                        operation()
        self.assertEqual(self.writes(), [])

    def test_denied_unknown_and_retried_puts_fail_even_if_bytes_exist(self):
        path = self.paths['index']
        for error, persisted in ((S3Error('AccessDenied', 403), False), (TimeoutError(), False),
                                 (TimeoutError(), True), (S3Error('PreconditionFailed', 412, 1), True)):
            with self.subTest(error=error, persisted=persisted):
                self.s3.objects = {self.approval_path: self.approval}
                self.events.clear()

                def fail(key):
                    raise error

                self.s3.before_put = (lambda key: None) if persisted else fail
                self.s3.after_put = fail if persisted else (lambda key: None)
                with self.assertRaises(WriteUnconfirmed) as caught:
                    self.retain()
                self.assertIs(caught.exception.__cause__, error)
                self.assertEqual(path in self.s3.objects, persisted)
                self.assertEqual(self.writes(), [path])
                with self.assertRaises(FileNotFoundError):
                    self.load()  # Cannot complete a partial archive.
                self.assertEqual(self.writes(), [path])

    def test_missing_or_tampered_input_never_falls_back_or_repairs(self):
        self.seed()
        path = self.paths['pointer']
        del self.s3.objects[path]
        with self.assertRaises(FileNotFoundError):
            self.load()
        self.s3.objects[path] = self.parents['pointer'].strip()
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.load()
        with self.assertRaisesRegex(ValueError, 'differ'):
            self.retain()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.s3.objects[path], self.parents['pointer'].strip())

    def test_independent_hub_readback_required_on_retain_and_load(self):
        path = self.paths['index']
        for bad, error in ((FileNotFoundError(path), FileNotFoundError),
                           (PermissionError(path), PermissionError),
                           (self.parents['index'].strip(), ValueError),
                           (bytearray(self.parents['index']), ValueError)):
            with self.subTest(bad=bad):
                self.s3.objects = {self.approval_path: self.approval}
                self.events.clear()
                self.hub.overrides[path] = bad
                with self.assertRaises(error):
                    self.retain()
                self.assertEqual(self.writes(), [path])  # S3 succeeded; don't create the rest.
                self.seed()
                with self.assertRaises(error):
                    self.load()
                self.assertEqual(self.writes(), [path])

    def test_raw_input_validation_precedes_all_creates_and_s3_reads_are_bounded(self):
        for invalid in (b'', 'file.json', bytearray(self.parents['pointer']), memoryview(self.parents['pointer']),
                        b'x' * (subject.MAX_POINTER + 1)):
            with self.subTest(invalid=type(invalid)), self.assertRaisesRegex(ValueError, 'type or size'):
                self.retain(pointer=invalid)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.retain(pointer=self.parents['pointer'].strip())
        self.assertEqual(self.events, [])
        self.seed()
        for name, maximum in [('index', subject.MAX_INDEX), ('sidecar', subject.MAX_SIDECAR),
                              ('pointer', subject.MAX_POINTER)]:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'size'):
                self.s3.lengths = {self.paths[name]: maximum + 1}
                self.load()
        self.assertEqual(self.writes(), [])

    def test_approval_is_pinned_and_required_for_both_operations(self):
        self.seed()
        for raw, error in ((None, FileNotFoundError), (self.approval.strip(), ValueError)):
            with self.subTest(error=error):
                self.s3.objects.pop(self.approval_path, None)
                if raw is not None:
                    self.s3.objects[self.approval_path] = raw
                for operation in (self.load, self.retain):
                    self.events.clear()
                    with self.assertRaises(error):
                        operation()
                    self.assertEqual(self.events, [('s3', self.approval_path)])
        self.s3.objects[self.approval_path] = self.approval
        altered_pointer = self.parents['pointer'].strip()
        with self.assertRaisesRegex(ValueError, 'stored approval'):
            self.retain(pointer=altered_pointer, review=self.review | {'pointer_parent_sha256': digest(altered_pointer)})
        self.assertEqual(self.writes(), [])

    def test_approval_bounds_and_duplicate_fields(self):
        self.s3.lengths[self.approval_path] = subject.MAX_REVIEW + 1
        with self.assertRaisesRegex(ValueError, 'size'):
            self.load()
        raw = self.approval.replace(b'"version": 1', b'"version": 1, "version": 1')
        pin = digest(raw)
        self.s3.objects[f'{subject.REVIEW_PREFIX}{pin}.json'] = raw
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.retain(review_sha256=pin)
        self.assertEqual(self.writes(), [])

    def test_bad_types_and_path_components_rejected_without_io(self):
        for opts in (dict(review_sha256='../' + self.pin), dict(review_sha256=self.pin.upper()),
                     dict(review_sha256=self.pin + '\n'), dict(review_sha256=self.pin.encode()),
                     dict(review=self.review | {'canonical_parent_sha256': 'x/y'}),
                     dict(review=self.review | {'kind': 'other'}), dict(review=self.review | {'version': True}),
                     dict(review=self.review | {'remote_writes': False}), dict(review=[]), dict(guard=None)):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                self.retain(**opts)
        self.assertEqual(self.events, [])


if __name__ == '__main__':
    unittest.main()
