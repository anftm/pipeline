import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import chm_terminal_release as release, chm_subset_journal as journal
from scripts import chm_subset_journal_backend as chain
from tests.test_chm_subset_journal_backend import FakeBackend


class Reader(FakeBackend):
    def __init__(self):
        super().__init__()
        self.objects = {}
        self.reader = SimpleNamespace(read_etags={})
    def read_bytes(self, bucket, path):
        if (bucket, path) not in self.objects:
            raise FileNotFoundError(path)
        self.reader.read_etags[(bucket, path)] = '"etag"'
        return self.objects[bucket, path]
    def list_files(self, bucket, prefixes):
        return {path for owner, path in self.objects if owner == bucket and path.startswith(prefixes)}


class Writer:
    def __init__(self, reader):
        self.reader, self.calls = reader, []
    def put(self, path, raw, *, etag=None):
        self.calls.append((path, etag))
        self.reader.objects[release.ASSETS, path] = raw


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        for i in range(1, 5):
            raw = f'fake-{i}'.encode()
            self.rows.append({'bucket': release.ASSETS, 'path': f'native/ebook/chm/{i:064x}/document.epub',
                              'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        self.old = [{'bucket': release.ASSETS, 'status': 'verified-absent', 'paths': [self.rows[0]['path']]}]
        self.plan = journal._plan(self.rows[1:3], self.rows[3:], {'old_plan_sha256': 'a' * 64,
            'inventory_sha256': 'b' * 64, 'old_progress_sha256': 'c' * 64, 'old_receipts_sha256': 'd' * 64,
            'classification_sha256': 'e' * 64, 'maintenance_id': 'f' * 32, 'transaction_id': '1' * 32})
        self.progress = {'batches': self.old, 'complete': False}
        self.reader = Reader()
        cursor = chain.Cursor(self.reader, self.plan)
        with patch.object(release.subset, 'INITIAL_NONCE', '1' * 32):
            cursor.initialize(nonce='1' * 32)
        first = cursor.head()
        self.initial_sha = journal.digest(first)
        pending = {'bucket': release.ASSETS, 'status': 'submitted', 'paths': [row['path'] for row in self.plan['selected']]}
        intent = journal.make_envelope(self.plan, {**first['state'], 'pending': pending}, sequence=1,
                                       parent_sha256=journal.digest(first), nonce='2' * 32)
        cursor.append(journal.digest(first), intent)
        received = journal.make_envelope(self.plan, {**intent['state'], 'pending': None,
            'receipts': [{**pending, 'status': 'verified-absent'}]}, sequence=2,
            parent_sha256=journal.digest(intent), nonce='3' * 32)
        cursor.append(journal.digest(intent), received)
        complete = journal.make_envelope(self.plan, {**received['state'], 'subset_complete': True}, sequence=3,
                                         parent_sha256=journal.digest(received), nonce='4' * 32)
        cursor.append(journal.digest(received), complete)
        for name, raw in self.reader.files.items():
            self.reader.objects[release.ASSETS, release.subset.PREFIX + name] = raw
        self.window = {'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': release.preflight.OWNER,
                       'callback_plan_sha256': release.preflight.PLAN_SHA, 'callback_owner_run_id': release.OLD_OWNER,
                       'callback_mutation_started': True, 'callback_handoff_stage': 'references-retired',
                       'callback_recovery_required': True, 'repositories': {'fixture/repo': {'cancelled': []}}}
        self.window_raw = json.dumps(self.window).encode()
        self.reader.objects[release.ASSETS, release.maintenance.DURABLE_JOURNAL] = self.window_raw
        self.writer = Writer(self.reader)
        self.owner = {'id': int(release.OLD_OWNER), 'status': 'completed',
                      'path': '.github/workflows/chm-epub-retirement.yml'}
        self.patches = [patch.object(release.preflight, '_identity', return_value=(b'p', self.window_raw, b'r', self.progress)),
                        patch.object(release.subset, 'INITIAL_SHA', self.initial_sha),
                        patch.object(release.remaining, 'final_head', return_value={'completed': True}),
                        patch.object(release.remaining, 'observe', return_value={str(i): 'absent' for i in range(972)}),
                        patch.object(release, 'terminal_evidence', return_value={'kind': 'synthetic-handoff'})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def prepare(self):
        return release.prepare(self.reader, self.writer, Mock(), self.rows, self.plan, self.progress, '1234', self.owner)

    def test_terminal_bridge_keeps_original_progress_and_records_owner(self):
        path, updated = self.prepare()
        self.assertTrue(updated['callback_committed'])
        self.assertFalse(updated['callback_recovery_required'])
        self.assertEqual(updated['callback_owner_run_id'], '1234')
        self.assertEqual(updated['terminal_handoff']['path'], path)
        self.assertEqual(self.reader.objects[release.ASSETS, release.PREFIX + 'original-maintenance.json'], self.window_raw)
        self.assertEqual(self.writer.calls[-1], (release.maintenance.DURABLE_JOURNAL, '"etag"'))
        self.assertFalse(any(path.endswith('progress.json') for path, _ in self.writer.calls))

    def test_active_old_owner_or_different_journal_stops_before_write(self):
        with self.assertRaisesRegex(ValueError, 'not terminal'):
            release.prepare(self.reader, self.writer, Mock(), self.rows, self.plan, self.progress, '1234',
                            {**self.owner, 'status': 'in_progress'})
        self.assertFalse(self.writer.calls)
        self.reader.objects[release.ASSETS, release.maintenance.DURABLE_JOURNAL] = b'changed'
        with self.assertRaisesRegex(ValueError, 'changed before terminal handoff'):
            self.prepare()
        self.assertNotIn(release.maintenance.DURABLE_JOURNAL, [path for path, _ in self.writer.calls])

    def test_terminal_evidence_rejects_incomplete_or_foreign_initial_chain(self):
        self.patches[-1].stop()
        with patch.object(release.subset, 'INITIAL_SHA', '0' * 64):
            with self.assertRaisesRegex(ValueError, 'initial event'):
                release.terminal_evidence(self.reader, self.rows, self.plan, self.progress)
        name = release.subset.PREFIX + 'event-00000003.json'
        del self.reader.objects[release.ASSETS, name]
        with self.assertRaisesRegex(ValueError, 'not complete'):
            release.terminal_evidence(self.reader, self.rows, self.plan, self.progress)

    def test_bridge_is_content_addressed_and_requires_retention(self):
        path, window = self.prepare()
        retention = b'synthetic retention'
        self.reader.objects[release.ASSETS, release.preflight.RETENTION] = retention
        with patch.object(release.preflight, 'RETENTION_SHA', hashlib.sha256(retention).hexdigest()):
            self.assertEqual(release.check_bridged(self.reader, self.rows, self.plan, self.progress, window), path)
            changed = {**window, 'terminal_handoff': {**window['terminal_handoff'], 'sha256': '0' * 64}}
            with self.assertRaisesRegex(ValueError, 'identity differs'):
                release.check_bridged(self.reader, self.rows, self.plan, self.progress, changed)
            self.reader.objects[release.ASSETS, release.preflight.RETENTION] = b'changed'
            with self.assertRaisesRegex(ValueError, 'retention changed'):
                release.check_bridged(self.reader, self.rows, self.plan, self.progress, window)

    def test_cancelled_runs_are_not_replayed(self):
        changed = {**self.window, 'repositories': {'fixture/repo': {'cancelled': [{'id': 7}]}}}
        raw = json.dumps(changed).encode()
        with patch.object(release.preflight, '_identity', return_value=(b'p', raw, b'r', self.progress)):
            with self.assertRaisesRegex(ValueError, 'maintenance identity'):
                self.prepare()
        self.assertFalse(self.writer.calls)

    def test_restore_excludes_retired_ocr_without_changing_input(self):
        window = {'repositories': {'anftm/pipeline': {'permission_changed': False, 'workflows': [
            {'path': '.github/workflows/reader-chm-repair.yml', 'state': 'active'},
            {'path': '.github/workflows/publish-ccrd-ocr.yml', 'state': 'active'},
            {'path': '.github/workflows/reader-publish-text.yml', 'state': 'active'},
            {'path': '.github/workflows/reader-gc.yml', 'state': 'active'}]},
            'alicetran68/pipeline': {'permission_changed': True, 'workflows': []}}}
        result = release.filter_restore_scope(window)
        central = result['repositories']['anftm/pipeline']['workflows']
        self.assertEqual(central[0]['state'], 'active')
        self.assertEqual(central[1]['state'], 'disabled_manually')
        self.assertEqual(central[2]['state'], 'disabled_manually')
        self.assertFalse(result['repositories']['alicetran68/pipeline']['permission_changed'])
        self.assertTrue(window['repositories']['alicetran68/pipeline']['permission_changed'])


if __name__ == '__main__':
    unittest.main()
