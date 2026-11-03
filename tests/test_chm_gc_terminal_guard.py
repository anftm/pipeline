import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import chm_gc_terminal_guard as guard, chm_final20_journal as final
from scripts import chm_subset_journal as journal, chm_subset_journal_backend as chain
from tests.test_chm_subset_journal_backend import FakeBackend


class Store:
    def __init__(self):
        self.files = {}
    def read_bytes(self, bucket, path):
        if (bucket, path) not in self.files:
            raise FileNotFoundError(path)
        return self.files[bucket, path]
    def list_files(self, bucket, prefixes):
        return {path for owner, path in self.files if owner == bucket and path.startswith(prefixes)}
    def assert_chm_bucket_available(self):
        return True


class GcTerminalGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = guard.preflight.chm.preview.load_inventory(Path(__file__).resolve().parents[1] /
                      'config/chm-history-inventory-20261030.json')['targets']
        classification = json.loads(guard.preflight.CLASSIFICATION.read_bytes())
        index = {row['path']: row for row in cls.rows}
        cls.selected = sorted((index[x['path']] for x in classification['targets'] if x['category'] == 'mapped-with-saved-evidence'),
                              key=lambda row: row['path'])
        cls.withheld = sorted((index[x['path']] for x in classification['targets'] if x['category'] in
                               {'needs-independent-review', 'withheld-oversize'}), key=lambda row: row['path'])
        chosen = {row['path'] for row in [*cls.selected, *cls.withheld]}
        old = [row for row in cls.rows if row['path'] not in chosen]
        batches = [{'bucket': guard.ASSETS, 'status': 'verified-absent',
                    'paths': [row['path'] for row in old[i:i + 100]]} for i in range(0, len(old), 100)]
        cls.plan_raw = b'synthetic old plan'
        cls.maintenance_raw = journal.encode({'repositories': {
            'anftm/pipeline': {'cancelled': [], 'permission': {'enabled': True}, 'workflows': [
                {'id': 1, 'path': '.github/workflows/publish-ccrd-ocr.yml', 'state': 'active'},
                {'id': 2, 'path': '.github/workflows/reader-publish-text.yml', 'state': 'active'},
                {'id': 3, 'path': '.github/workflows/reader-chm-repair.yml', 'state': 'active'}]},
            'old/pipeline': {'cancelled': [], 'permission': {'enabled': True}, 'permission_changed': True,
                             'workflows': []}}})
        cls.identity = {**guard.locked.IDENTITY, 'old_plan_sha256': hashlib.sha256(cls.plan_raw).hexdigest(),
                        'old_receipts_sha256': journal.digest(batches)}
        cls.progress = {'kind': 'chm-epub-retirement', 'plan_sha256': cls.identity['old_plan_sha256'],
                        'inventory_sha256': cls.identity['inventory_sha256'],
                        'maintenance_id': cls.identity['maintenance_id'], 'complete': False, 'batches': batches,
                        'confirmed_deleted_objects': len(old), 'confirmed_deleted_bytes': sum(row['bytes'] for row in old)}
        cls.progress_raw = journal.encode(cls.progress)
        cls.identity['old_progress_sha256'] = hashlib.sha256(cls.progress_raw).hexdigest()
        cls.plan = journal.bind_plan(cls.rows, batches, cls.selected, cls.withheld, cls.identity,
                                     old_progress_raw=cls.progress_raw)['plan']

    def setUp(self):
        patches = [patch.object(guard.locked, 'IDENTITY', self.identity),
                   patch.object(guard.preflight, 'PLAN_SHA', self.identity['old_plan_sha256']),
                   patch.object(guard.preflight, 'PROGRESS_SHA', self.identity['old_progress_sha256']),
                   patch.object(guard.preflight, 'MAINTENANCE_SHA', hashlib.sha256(self.maintenance_raw).hexdigest()),
                   patch.object(guard.subset, 'PLAN_SHA', journal.digest(self.plan))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.store = Store()
        backend = FakeBackend()
        cursor = chain.Cursor(backend, self.plan)
        cursor.initialize(nonce=guard.subset.INITIAL_NONCE)
        initial_patch = patch.object(guard.subset, 'INITIAL_SHA', journal.digest(cursor.head()))
        initial_patch.start()
        self.addCleanup(initial_patch.stop)
        n = 1
        for i in range(0, 352, 100):
            head = cursor.head()
            pending = {'bucket': guard.ASSETS, 'status': 'submitted',
                       'paths': [row['path'] for row in self.selected[i:i + 100]]}
            n += 1
            intent = journal.make_envelope(self.plan, {**head['state'], 'pending': pending},
                sequence=head['sequence'] + 1, parent_sha256=journal.digest(head), nonce=f'{n:032x}')
            cursor.append(journal.digest(head), intent)
            n += 1
            receipt = journal.make_envelope(self.plan, {**intent['state'], 'pending': None,
                'receipts': [*intent['state']['receipts'], {**pending, 'status': 'verified-absent'}]},
                sequence=intent['sequence'] + 1, parent_sha256=journal.digest(intent), nonce=f'{n:032x}')
            cursor.append(journal.digest(intent), receipt)
        head = cursor.head()
        n += 1
        finished = journal.make_envelope(self.plan, {**head['state'], 'subset_complete': True},
            sequence=head['sequence'] + 1, parent_sha256=journal.digest(head), nonce=f'{n:032x}')
        cursor.append(journal.digest(head), finished)
        for name, raw in backend.files.items():
            self.store.files[guard.ASSETS, guard.subset.PREFIX + name] = raw
        final_plan = final.fixed_plan(self.withheld, guard.subset.PLAN_SHA, guard.locked.ROOT_SHA)
        last_backend = FakeBackend()
        last_cursor = final.Cursor(last_backend, final_plan)
        head = last_cursor.initialize()
        paths = [row['path'] for row in self.withheld]
        transitions = [{'pending': paths, 'receipt': None, 'root_pending': False, 'complete': False},
                       {'pending': None, 'receipt': paths, 'root_pending': False, 'complete': False},
                       {'pending': None, 'receipt': paths, 'root_pending': True, 'complete': False},
                       {'pending': None, 'receipt': paths, 'root_pending': False, 'complete': True}]
        for n, state in enumerate(transitions, 2):
            event = final.envelope(final_plan, state, sequence=head['sequence'] + 1,
                                   parent=journal.digest(head), nonce=f'{n:032x}')
            last_cursor.append(head, event)
            head = event
        for name, raw in last_backend.files.items():
            self.store.files[guard.ASSETS, guard.FINAL_PREFIX + name] = raw
        evidence = {'kind': 'chm-exact-terminal-handoff', 'version': 1,
                    'inventory_sha256': guard.preflight.chm.preview.INVENTORY_SHA256,
                    'original_plan_sha256': guard.preflight.PLAN_SHA,
                    'original_progress_sha256': guard.preflight.PROGRESS_SHA,
                    'original_maintenance_sha256': guard.preflight.MAINTENANCE_SHA,
                    'original_receipts_sha256': guard.locked.IDENTITY['old_receipts_sha256'], 'original_confirmed': 600,
                    'subset_plan_sha256': guard.subset.PLAN_SHA, 'subset_head_sha256': journal.digest(finished),
                    'subset_confirmed': 352, 'final20_head_sha256': journal.digest(head), 'final20_confirmed': 20,
                    'verified_absent_objects': 972, 'verified_absent_bytes': guard.preflight.chm.preview.DATA_BYTES,
                    'original_progress_unchanged': True, 'ordinary_gc_authorized': False}
        evidence_raw = journal.encode(evidence)
        handoff_sha = hashlib.sha256(evidence_raw).hexdigest()
        handoff_path = guard.PREFIX + handoff_sha + '.json'
        expected_scope = guard.filter_restore_scope(json.loads(self.maintenance_raw))
        for record in expected_scope['repositories'].values():
            record['restored'] = True
        state = {'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': guard.preflight.OWNER,
                 'callback_plan_sha256': guard.preflight.PLAN_SHA, 'callback_handoff_stage': 'exact-terminal-bridge',
                 'callback_committed': True, 'callback_recovery_required': False, 'restored': True,
                 'repositories': expected_scope['repositories'],
                 'terminal_restore_exclusions': expected_scope['terminal_restore_exclusions'],
                 'terminal_handoff': {'bucket': guard.ASSETS, 'path': handoff_path, 'sha256': handoff_sha}}
        raw = journal.encode(state)
        history_sha = hashlib.sha256(raw).hexdigest()
        self.marker = {'kind': 'chm-gc-release', 'version': 1, 'maintenance_sha256': history_sha,
                       'handoff_sha256': handoff_sha}
        self.store.files[guard.ASSETS, guard.PREFIX + 'gc-release.json'] = journal.encode(self.marker)
        self.store.files[guard.ASSETS, guard.maintenance.DURABLE_HISTORY_PREFIX + history_sha + '.json'] = raw
        self.store.files[guard.ASSETS, guard.maintenance.DURABLE_JOURNAL] = raw
        self.store.files[guard.ASSETS, handoff_path] = evidence_raw
        self.store.files[guard.ASSETS, guard.original.PROGRESS] = self.progress_raw
        self.store.files[guard.ASSETS, guard.original.PLAN] = self.plan_raw
        self.store.files[guard.ASSETS, guard.original.PLAN_SHA] = (guard.preflight.PLAN_SHA + '\n').encode()
        self.store.files[guard.ASSETS, guard.PREFIX + 'original-maintenance.json'] = self.maintenance_raw
        self.retention = b'synthetic retention'
        self.store.files[guard.ASSETS, guard.preflight.RETENTION] = self.retention
        self.retention_patch = patch.object(guard.preflight, 'RETENTION_SHA', hashlib.sha256(self.retention).hexdigest())
        self.retention_patch.start()
        self.addCleanup(self.retention_patch.stop)

    def test_complete_receipt_chain_restored_history_is_accepted(self):
        self.assertTrue(guard.verify(self.store))

    def test_missing_or_changed_marker_history_journals_and_root_block(self):
        keys = [guard.PREFIX + 'gc-release.json', guard.maintenance.DURABLE_HISTORY_PREFIX +
                self.marker['maintenance_sha256'] + '.json', guard.original.PROGRESS,
                guard.original.PLAN_SHA, guard.PREFIX + 'original-maintenance.json',
                guard.subset.PREFIX + 'event-00000002.json', guard.FINAL_PREFIX + 'event-00000004.json']
        for path in keys:
            with self.subTest(path=path):
                key = guard.ASSETS, path
                raw = self.store.files.pop(key)
                with self.assertRaises((RuntimeError, ValueError, FileNotFoundError, TypeError)):
                    guard.verify(self.store)
                self.store.files[key] = raw
        root = guard.locked.isolated.root_path(guard.locked.IDENTITY['transaction_id'])
        self.store.files[guard.ASSETS, root] = b'old root'
        with self.assertRaisesRegex(RuntimeError, 'root still exists'):
            guard.verify(self.store)

    def test_unfinished_current_or_forged_receipt_blocks(self):
        key = guard.ASSETS, guard.maintenance.DURABLE_JOURNAL
        original = self.store.files[key]
        self.store.files[key] = journal.encode({'kind': 'reader-maintenance-journal', 'version': 1,
                    'maintenance_id': 'a' * 32, 'repositories': {'r': {}}, 'callback_recovery_required': True})
        with self.assertRaisesRegex(RuntimeError, 'unfinished callback'):
            guard.verify(self.store)
        self.store.files[key] = original
        path = guard.subset.PREFIX + 'event-00000009.json'
        self.store.files[guard.ASSETS, path] = b'{}'
        with self.assertRaises(ValueError):
            guard.verify(self.store)

    def test_new_completed_maintenance_cursor_does_not_erase_archived_evidence(self):
        self.store.files[guard.ASSETS, guard.maintenance.DURABLE_JOURNAL] = journal.encode({
            'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': 'a' * 32,
            'repositories': {'r': {'restored': True}}, 'restored': True})
        self.assertTrue(guard.verify(self.store))

    def test_candidate_is_validated_before_it_is_published(self):
        key = guard.ASSETS, guard.PREFIX + 'gc-release.json'
        raw = self.store.files.pop(key)
        self.assertTrue(guard.verify(self.store, candidate_marker=raw))
        self.store.files[guard.ASSETS, guard.original.PROGRESS] = b'foreign'
        with self.assertRaisesRegex(RuntimeError, 'progress differs'):
            guard.verify(self.store, candidate_marker=raw)
        self.assertNotIn(key, self.store.files)

    def test_chapter_retention_change_stays_fail_closed(self):
        self.store.files[guard.ASSETS, guard.preflight.RETENTION] = b'changed'
        with self.assertRaisesRegex(RuntimeError, 'chapter retention differs'):
            guard.verify_retention(self.store)

    def test_restore_scope_must_match_original_producer_snapshot(self):
        expected = guard.filter_restore_scope(json.loads(self.maintenance_raw))
        for record in expected['repositories'].values():
            record['restored'] = True
        guard.verify_restore_scope(self.maintenance_raw, expected)
        altered = json.loads(json.dumps(expected))
        altered['repositories']['anftm/pipeline']['workflows'][0]['state'] = 'active'
        with self.assertRaisesRegex(RuntimeError, 'producer scope differs'):
            guard.verify_restore_scope(self.maintenance_raw, altered)
        altered = json.loads(json.dumps(expected))
        altered['repositories'].pop('old/pipeline')
        with self.assertRaisesRegex(RuntimeError, 'producer scope differs'):
            guard.verify_restore_scope(self.maintenance_raw, altered)

    def test_release_and_guard_use_same_real_journal_chain(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from scripts import publish_chm_gc_release as publish
        marker_key = guard.ASSETS, guard.PREFIX + 'gc-release.json'
        marker = self.store.files.pop(marker_key)
        self.store.files[guard.ASSETS, guard.maintenance.DURABLE_JOURNAL] = journal.encode({
            'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': 'a' * 32,
            'repositories': {'r': {}}})
        self.store.reader = self.store
        traversal = SimpleNamespace(files={bucket: set() for bucket in publish.graph.BUCKETS},
                                    references=set(), trees=set())
        proof = {'graph_complete': True, 'blockers': [], 'missing': [], 'ambiguous_paths': [],
                 'buckets': {bucket: {} for bucket in publish.graph.BUCKETS}}
        store = self.store
        class Writer:
            calls = 0
            def create(self, raw):
                self.calls += 1
                store.files[marker_key] = raw
        writer = Writer()
        with patch.object(publish, 'verify_retention_graph'), patch.object(publish.collector, 'verify_roots'), \
             patch.object(publish, 'require_complete_graph_stability'), \
             patch.object(publish.collector, 'observe_projections', return_value=({}, {})):
            result = publish.publish(self.store, writer, Mock(), traversal, proof, {}, {}, self.rows,
                                     window_id='a' * 32, client=Mock())
        self.assertEqual(writer.calls, 1)
        self.assertEqual(self.store.files[marker_key], marker)
        self.assertFalse(result['gc_executed'])
        self.assertTrue(guard.verify(self.store))
        self.store.files.pop(marker_key)
        self.store.files[guard.ASSETS, guard.original.PLAN] = b'foreign'
        writer = Writer()
        with patch.object(publish, 'verify_retention_graph'):
            with self.assertRaisesRegex(ValueError, 'original CHM plan bytes'):
                publish.publish(self.store, writer, Mock(), traversal, proof, {}, {}, self.rows,
                                window_id='a' * 32, client=Mock())
        self.assertEqual(writer.calls, 0)
        self.store.files[guard.ASSETS, guard.original.PLAN] = self.plan_raw
        self.store.files[guard.ASSETS, guard.FINAL_PREFIX + 'event-00000004.json'] = b'foreign'
        writer = Writer()
        with patch.object(publish, 'verify_retention_graph'), patch.object(publish.collector, 'verify_roots'), \
             patch.object(publish.collector, 'observe_projections', return_value=({}, {})):
            with self.assertRaises(ValueError):
                publish.publish(self.store, writer, Mock(), traversal, proof, {}, {}, self.rows,
                                window_id='a' * 32, client=Mock())
        self.assertEqual(writer.calls, 0)
        self.assertNotIn(marker_key, self.store.files)


if __name__ == '__main__':
    unittest.main()
