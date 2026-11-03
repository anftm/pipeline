import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

from scripts import chm_subset_journal as model, chm_subset_journal_backend as backend


class FakeBackend:
    def __init__(self):
        self.files, self.lock = {}, Lock()
        self.lose_response = False

    def names(self):
        with self.lock:
            return set(self.files)

    def read(self, name):
        with self.lock:
            return self.files.get(name)

    def create(self, name, raw):
        with self.lock:
            if name in self.files:
                return False
            self.files[name] = raw
        if self.lose_response:
            self.lose_response = False
            raise backend.OutcomeUnknown('injected lost response')
        return True


class JournalBackendTests(unittest.TestCase):
    def setUp(self):
        def row(n):
            return {'bucket': model.subset.ASSETS, 'path': f'native/ebook/chm/{n:064x}/document.epub',
                    'bytes': n, 'sha256': hashlib.sha256(str(n).encode()).hexdigest()}
        rows = [row(n) for n in range(1, 5)]
        old = [{'bucket': model.subset.ASSETS, 'status': 'verified-absent', 'paths': [rows[0]['path']]}]
        progress = {'kind': 'chm-epub-retirement', 'plan_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
                    'maintenance_id': 'e' * 32, 'complete': False, 'batches': old,
                    'confirmed_deleted_objects': 1, 'confirmed_deleted_bytes': 1}
        raw = model.encode(progress)
        identity = {'old_plan_sha256': 'a' * 64, 'inventory_sha256': 'b' * 64,
                    'old_progress_sha256': hashlib.sha256(raw).hexdigest(), 'old_receipts_sha256': model.digest(old),
                    'classification_sha256': 'd' * 64, 'maintenance_id': 'e' * 32, 'transaction_id': 'f' * 32}
        self.rows = rows
        self.plan = model.bind_plan(rows, old, rows[1:3], rows[3:], identity, old_progress_raw=raw)['plan']
        self.storage = FakeBackend()
        self.cursor = backend.Cursor(self.storage, self.plan)
        self.cursor.initialize(nonce='1' * 32)
        self.first = self.cursor.head()

    def event(self, nonce='2'):
        state = {**self.first['state'], 'pending': {'bucket': model.subset.ASSETS, 'status': 'submitted',
                                                     'paths': [self.rows[1]['path']]}}
        return model.make_envelope(self.plan, state, sequence=1, parent_sha256=model.digest(self.first),
                                   nonce=nonce * 32)

    def test_append_and_reopen(self):
        item = self.event()
        self.assertEqual(self.cursor.append(model.digest(self.first), item)['status'], 'created')
        self.assertEqual(backend.Cursor(self.storage, self.plan).head(), item)
        self.assertFalse(self.cursor.append(model.digest(self.first), item)['deletion_authorized'])

    def test_unknown_create_is_not_blindly_retried(self):
        item = self.event()
        self.storage.lose_response = True
        with self.assertRaises(backend.OutcomeUnknown):
            self.cursor.append(model.digest(self.first), item)
        self.assertEqual(self.cursor.head(), item)
        self.assertEqual(self.cursor.append(model.digest(self.first), item)['status'], 'already-present')

    def test_concurrent_different_events_cannot_both_win(self):
        barrier = Barrier(2)
        choices = [self.event('2'), self.event('3')]
        def one(item):
            barrier.wait()
            try:
                return self.cursor.append(model.digest(self.first), item)['status']
            except backend.Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(one, choices)), ['created', 'conflict'])
        self.assertIn(self.cursor.head(), choices)

    def test_foreign_or_partial_event_stops(self):
        self.storage.files['event-00000001.json'] = b'{'
        with self.assertRaises(ValueError):
            self.cursor.head()
        self.assertEqual(self.storage.files['event-00000001.json'], b'{')

    def test_gaps_unknown_objects_and_plan_tampering_stop(self):
        for name, value in (('event-00000002.json', b'{}'), ('foreign.json', b'{}'), ('plan.json', b'{}')):
            with self.subTest(name=name):
                original = dict(self.storage.files)
                self.storage.files[name] = value
                with self.assertRaises(ValueError):
                    self.cursor.head()
                self.storage.files = original

    def test_initialization_never_overwrites_foreign_namespace(self):
        with self.assertRaises(backend.Conflict):
            self.cursor.initialize(nonce='4' * 32)
        self.assertEqual(self.cursor.head(), self.first)

    def test_initialization_unknown_does_not_claim_success(self):
        storage = FakeBackend()
        storage.lose_response = True
        cursor = backend.Cursor(storage, self.plan)
        with self.assertRaises(backend.OutcomeUnknown):
            cursor.initialize(nonce='4' * 32)
        self.assertEqual(storage.names(), {'plan.json'})
        self.assertEqual(cursor.reconcile_initialization(nonce='4' * 32)['status'], 'plan-only-unresolved')
        with self.assertRaises(backend.Conflict):
            cursor.initialize(nonce='4' * 32)

    def test_unknown_initial_event_can_be_classified_without_writes(self):
        storage = FakeBackend()
        original = storage.create
        def lose_second(name, raw):
            if name == 'event-00000000.json':
                storage.lose_response = True
            return original(name, raw)
        storage.create = lose_second
        cursor = backend.Cursor(storage, self.plan)
        with self.assertRaises(backend.OutcomeUnknown):
            cursor.initialize(nonce='4' * 32)
        before = dict(storage.files)
        report = cursor.reconcile_initialization(nonce='4' * 32)
        self.assertEqual(report['status'], 'initial-objects-observed')
        self.assertFalse(report['deletion_authorized'])
        self.assertEqual(storage.files, before)
        with self.assertRaises(backend.Conflict):
            cursor.reconcile_initialization(nonce='5' * 32)

    def test_identical_plan_race_does_not_authorize_initialization(self):
        storage = FakeBackend()
        original = storage.create
        calls = []
        def other_writer(name, raw):
            calls.append(name)
            if name == 'plan.json':
                original(name, raw)
                return False
            return original(name, raw)
        storage.create = other_writer
        cursor = backend.Cursor(storage, self.plan)
        with self.assertRaisesRegex(backend.Conflict, 'plan appeared'):
            cursor.initialize(nonce='4' * 32)
        self.assertEqual(calls, ['plan.json'])
        self.assertEqual(cursor.reconcile_initialization(nonce='4' * 32)['status'], 'plan-only-unresolved')

    def test_identical_initial_event_race_does_not_claim_ownership(self):
        storage = FakeBackend()
        original = storage.create
        def other_writer(name, raw):
            if name == 'event-00000000.json':
                original(name, raw)
                return False
            return original(name, raw)
        storage.create = other_writer
        cursor = backend.Cursor(storage, self.plan)
        with self.assertRaisesRegex(backend.Conflict, 'initial event appeared'):
            cursor.initialize(nonce='4' * 32)
        self.assertEqual(cursor.reconcile_initialization(nonce='4' * 32)['status'], 'initial-objects-observed')

    def test_opaque_or_failed_backend_response_is_unknown(self):
        self.storage.create = lambda name, raw: None
        with self.assertRaises(backend.OutcomeUnknown):
            self.cursor.append(model.digest(self.first), self.event())

    def test_backend_exception_after_write_is_unknown_and_not_retried(self):
        original = self.storage.create
        calls = []
        def lose_response(name, raw):
            calls.append(name)
            original(name, raw)
            raise OSError('response lost')
        self.storage.create = lose_response
        with self.assertRaisesRegex(backend.OutcomeUnknown, 'may have written'):
            self.cursor.append(model.digest(self.first), self.event())
        self.assertEqual(calls, ['event-00000001.json'])
        self.assertEqual(self.cursor.head(), self.event())

    def test_readback_exception_after_create_is_unknown_and_not_retried(self):
        original_create = self.storage.create
        original_read = self.storage.read
        calls, failed = [], []
        def create(name, raw):
            calls.append(name)
            return original_create(name, raw)
        def read(name):
            if name == 'event-00000001.json' and not failed:
                failed.append(name)
                raise OSError('readback failed')
            return original_read(name)
        self.storage.create = create
        self.storage.read = read
        with self.assertRaisesRegex(backend.OutcomeUnknown, 'readback failed'):
            self.cursor.append(model.digest(self.first), self.event())
        self.assertEqual(calls, ['event-00000001.json'])
        self.assertEqual(self.cursor.head(), self.event())


if __name__ == '__main__':
    unittest.main()
