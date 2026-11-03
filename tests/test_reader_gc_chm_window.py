import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import reader_gc_chm_window as guard, reader_gc_collect as collector
from scripts import reader_gc_ccrd as ccrd, reader_gc_documents as documents, reader_gc_inputs as inputs

A = guard.shared.READER_ASSETS_BUCKET


class MemoryStore:
    def __init__(self):
        self.files = {(A, guard.maintenance.DURABLE_JOURNAL): json.dumps({
            'version': 1, 'kind': 'reader-maintenance-journal', 'maintenance_id': 'f' * 32,
        }).encode()}
        self.client = Mock()

    def _location(self, bucket):
        return bucket.split('/')

    def _client(self, namespace):
        return self.client

    def list_files(self, bucket, prefixes):
        return {path for owner, path in self.files if owner == bucket and path.startswith(prefixes)}

    def read_bytes(self, bucket, path):
        if (bucket, path) not in self.files:
            raise FileNotFoundError(path)
        return self.files[(bucket, path)]


class ChmGcWindowTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()

    def test_missing_all_markers_and_new_maintenance_does_not_release_known_transaction(self):
        with self.assertRaisesRegex(RuntimeError, 'evidence is absent.*pending reviewed release'):
            guard.require_clear(self.store)

    def test_any_partial_or_unrecognized_marker_blocks(self):
        for path in (guard.PLAN, guard.PLAN_SHA, guard.PROGRESS, guard.CHM_PREFIX + 'foreign.json'):
            with self.subTest(path=path):
                self.store.files[(A, path)] = b'{}'
                with self.assertRaises(RuntimeError):
                    guard.require_clear(self.store)
                self.store.files.pop((A, path))

    def test_forged_terminal_state_cannot_release_existing_chm_markers(self):
        for path in guard.MARKERS:
            self.store.files[(A, path)] = b'{}'
        self.store.files[(A, guard.PROGRESS)] = json.dumps({'complete': True}).encode()
        with self.assertRaisesRegex(RuntimeError, 'CHM transaction'):
            guard.require_clear(self.store)

    def test_existing_marker_blocks_without_reading_large_plan(self):
        self.store.files[(A, guard.PROGRESS)] = b'{}'
        original_read = self.store.read_bytes
        self.store.read_bytes = Mock(side_effect=original_read)
        with self.assertRaisesRegex(RuntimeError, 'CHM transaction controls are present'):
            guard.require_clear(self.store)
        self.assertEqual(self.store.read_bytes.call_count, 2)
        self.store.read_bytes.assert_any_call(A, guard.maintenance.DURABLE_JOURNAL)
        self.assertFalse(any(call.args[1] == guard.PLAN for call in self.store.read_bytes.call_args_list))

    def test_original_maintenance_stops_before_expensive_listing(self):
        self.store.files[(A, guard.maintenance.DURABLE_JOURNAL)] = json.dumps({
            'kind': 'reader-maintenance-journal', 'version': 1,
            'maintenance_id': guard.ORIGINAL_MAINTENANCE, 'callback_recovery_required': True,
        }).encode()
        self.store.list_files = Mock(side_effect=AssertionError('must not list history'))
        with self.assertRaisesRegex(RuntimeError, 'unfinished CHM callback'):
            guard.require_clear(self.store)
        self.store.list_files.assert_not_called()

    def test_missing_bad_or_original_maintenance_cannot_look_clear(self):
        key = (A, guard.maintenance.DURABLE_JOURNAL)
        original = self.store.files.pop(key)
        with self.assertRaisesRegex(RuntimeError, 'missing maintenance'):
            guard.require_clear(self.store)
        self.store.files[key] = b'not-json'
        with self.assertRaisesRegex(RuntimeError, 'invalid maintenance'):
            guard.require_clear(self.store)
        self.store.files[key] = json.dumps({'kind': 'reader-maintenance-journal', 'version': 1,
                                           'maintenance_id': guard.ORIGINAL_MAINTENANCE}).encode()
        with self.assertRaisesRegex(RuntimeError, 'unfinished CHM callback'):
            guard.require_clear(self.store)
        self.store.files[key] = original

    def test_retention_root_cannot_be_ignored_even_if_journal_markers_disappear(self):
        self.store.files[(A, 'reader-index/chm-epub-withdrawal-retention.json')] = b'{}'
        with self.assertRaisesRegex(RuntimeError, 'retention root blocks'):
            guard.require_clear(self.store)

    def test_inaccessible_bucket_cannot_be_interpreted_as_absent(self):
        self.store.client.head_bucket.side_effect = OSError('unavailable')
        with self.assertRaisesRegex(RuntimeError, 'bucket availability'):
            guard.require_clear(self.store)

    def test_hub_backend_must_independently_identify_bucket(self):
        store = MemoryStore()
        store._location = None
        store._client = None
        store.api = Mock()
        store.api.bucket_info.return_value.id = A
        store.token = Mock(return_value='example-token')
        with self.assertRaisesRegex(RuntimeError, 'evidence is absent.*pending reviewed release'):
            guard.require_clear(store)
        store.api.bucket_info.return_value.id = 'vomebook/foreign'
        with self.assertRaisesRegex(RuntimeError, 'bucket availability'):
            guard.require_clear(store)

    def test_retention_appearing_during_check_blocks(self):
        initial_read = self.store.read_bytes
        calls = 0
        def changing(bucket, path):
            nonlocal calls
            if path == 'reader-index/chm-epub-withdrawal-retention.json':
                calls += 1
                if calls == 2:
                    return b'new root'
            return initial_read(bucket, path)
        self.store.read_bytes = Mock(side_effect=changing)
        with self.assertRaisesRegex(RuntimeError, 'retention root appeared'):
            guard.require_clear(self.store)

    def test_bucket_or_inventory_failure_is_not_accepted_as_absence(self):
        self.store.list_files = Mock(side_effect=OSError('unavailable'))
        with self.assertRaises(OSError):
            guard.require_clear(self.store)

    def test_inventory_or_maintenance_change_during_check_blocks(self):
        initial = self.store.list_files
        self.store.list_files = Mock(side_effect=[set(), {guard.PROGRESS}])
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            guard.require_clear(self.store)
        self.store.list_files = initial
        initial_read = self.store.read_bytes
        def changed(bucket, path):
            if path == guard.maintenance.DURABLE_JOURNAL:
                return initial_read(bucket, path) if changed.calls == 0 else b'changed'
            return initial_read(bucket, path)
        changed.calls = 0
        def changing(bucket, path):
            result = changed(bucket, path)
            if path == guard.maintenance.DURABLE_JOURNAL:
                changed.calls += 1
            return result
        self.store.read_bytes = Mock(side_effect=changing)
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            guard.require_clear(self.store)

    def test_all_three_apply_entrypoints_block_before_any_mutating_work(self):
        for module, argv, factory in (
                (collector, ['collect', '--apply', '--storage', 's3', '--output', 'ignored.json'],
                 'scripts.reader_gc_store.CollectionStore'),
                (documents, ['documents', '--apply', '--scope', 'document-pages',
                             '--reset-id', 'documents-20261025', '--output', 'ignored.json'], None),
                (inputs, ['inputs', '--apply', '--output', 'ignored.json'], 'scripts.reader_gc_store.CollectionStore')):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory(prefix='gc-chm-guard-test-') as tmp:
                store = Mock()
                blocked = RuntimeError('blocked by CHM')
                actual_argv = [*argv[:-1], str(Path(tmp) / 'output.json')]
                with patch('sys.argv', actual_argv), patch.object(module, 'assert_collector_context'), \
                     patch.object(guard, 'require_clear', side_effect=blocked) as call, \
                     patch('scripts.reader_gc_store.CollectionStore', return_value=store), \
                     patch.object(documents, 'Store', return_value=store), \
                     patch.object(inputs, 'GitHubProducerFence') as fence:
                    fence.return_value.check.return_value = None
                    with self.assertRaisesRegex(RuntimeError, 'blocked by CHM'):
                        module.main()
                    call.assert_called_once_with(store)
                    store.delete.assert_not_called()
                    store.put_bytes.assert_not_called()
                    store.put_json.assert_not_called()

    def test_ccrd_apply_blocks_before_control_or_data_mutation(self):
        store = Mock()
        with patch.object(ccrd, 'assert_workflow_context'), \
             patch.object(guard, 'require_clear', side_effect=RuntimeError('blocked by CHM')) as check, \
             patch('scripts.reader_gc_store.CcrdCollectionStore', return_value=store):
            with self.assertRaisesRegex(RuntimeError, 'blocked by CHM'):
                ccrd.main(['--apply', '--storage', 's3'])
        check.assert_called_once_with(store)
        store.delete.assert_not_called()
        store.put_bytes.assert_not_called()

    def test_direct_mutation_helpers_cannot_skip_the_guard(self):
        store = Mock()
        reference = Mock()
        reference.files = {}
        calls = (
            lambda: collector.retire_catalog(store, {}, {}, 0, apply=True),
            lambda: collector.collect_retired_snapshots(store, Mock(), {}, apply=True),
            lambda: collector.collect(store, Mock(), reference, {'graph_complete': True}, {}, apply=True),
            lambda: documents.collect(store, A, ('x/',), set(), set(), workers=1, journal={},
                                      barrier=lambda: None, checkpoint=lambda _: None),
            lambda: inputs.collect(store, {'roots': []}),
            lambda: ccrd.apply(store, {'kind': 'ccrd-ocr-gc-plan', 'bucket': ccrd.BUCKET}),
            lambda: ccrd.legacy_apply(store, {'kind': 'ccrd-legacy-gc-plan', 'bucket': ccrd.BUCKET}),
        )
        for call in calls:
            with self.subTest(call=call), patch.object(guard, 'require_clear',
                                                       side_effect=RuntimeError('blocked by CHM')):
                with self.assertRaisesRegex(RuntimeError, 'blocked by CHM'):
                    call()
        store.delete.assert_not_called()
        store.put_json.assert_not_called()
        store.put_bytes.assert_not_called()

    def test_generic_batch_rechecks_after_submitted_checkpoint(self):
        from tests.test_reader_gc_collect import Store
        from scripts import reader_gc_graph as graph
        store = Store()
        store.put(graph.PDF_PAGES_BUCKET, 'objects/orphan/0/document.pdf', b'orphan')
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        submitted = []
        def checkpoint(value):
            if value['batches']:
                submitted.append(True)
        def fail_after_submission(_store):
            if submitted:
                raise RuntimeError('CHM marker appeared')
        with patch.object(guard, 'require_clear'), patch.object(store, 'delete', wraps=store.delete) as delete:
            with self.assertRaisesRegex(RuntimeError, 'CHM marker appeared'):
                collector.collect(store, Mock(), reference, report, {}, apply=True, batch_size=1,
                                  checkpoint=checkpoint, chm_guard=fail_after_submission)
        delete.assert_not_called()

    def test_catalog_rechecks_before_either_remote_control_write(self):
        class StopCollector(Exception):
            pass
        store = Mock()
        identity = {'current': 'a' * 32, 'digest': 'b' * 64}
        with patch.object(collector, 'catalog_acknowledged', return_value=(True, 'test')), \
             patch.object(collector, 'read_catalog_stream', return_value=(b'{}', 'x' * 64, {
                 'kept': 1, 'retired_generations': []})), \
             patch.object(collector, 'snapshot_digest', return_value=identity['digest']), \
             patch.object(guard, 'require_clear', side_effect=[None, StopCollector('CHM changed')]):
            with self.assertRaises(StopCollector):
                collector.retire_catalog(store, identity, {}, 0, apply=True)
        store.put_bytes.assert_not_called()

    def test_generic_collection_rechecks_before_first_state_write(self):
        from tests.test_reader_gc_collect import Store
        from scripts import reader_gc_graph as graph
        store = Store()
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        with patch.object(guard, 'require_clear', side_effect=[None, RuntimeError('CHM changed')]), \
             patch.object(store, 'put_json', wraps=store.put_json) as write:
            with self.assertRaisesRegex(RuntimeError, 'CHM changed'):
                collector.collect(store, Mock(), reference, report, {}, apply=True)
        write.assert_not_called()

    def test_document_batch_rechecks_immediately_before_delete(self):
        from tests.test_reader_gc_documents import Store
        store = Store({'objects/one'})
        with patch.object(guard, 'require_clear'), patch.object(store, 'delete', wraps=store.delete) as delete:
            with self.assertRaisesRegex(RuntimeError, 'CHM marker appeared'):
                documents.collect(store, documents.reset.INPUTS, ('objects/',), store.paths.copy(),
                                  store.paths.copy(), workers=1, journal={}, barrier=lambda: None,
                                  checkpoint=lambda _: None,
                                  chm_guard=lambda _: (_ for _ in ()).throw(RuntimeError('CHM marker appeared')))
        delete.assert_not_called()

    def test_ocr_input_rechecks_after_submitted_checkpoint(self):
        store = Mock()
        report = {'roots': [{'root': 'example', 'pages': ['example/page.png'], 'retirement': {}}]}
        submitted = []
        def checkpoint(value):
            submitted.append(True)
        def fail_after_submission(_store):
            if submitted:
                raise RuntimeError('CHM marker appeared')
        with patch.object(guard, 'require_clear'), patch.object(inputs, 'eligible_identity'):
            with self.assertRaisesRegex(RuntimeError, 'CHM marker appeared'):
                inputs.collect(store, report, checkpoint=checkpoint, chm_guard=fail_after_submission)
        store.delete.assert_not_called()

    def test_ccrd_ocr_rechecks_before_rewriting_first_control(self):
        from tests.test_reader_gc_ccrd import build_store, Live
        store, releases = build_store()
        live = Live(releases[-1]['generation'])
        report = ccrd.plan(store, client=live)
        guard_calls = 0
        def block_after_initial_barrier(_store):
            nonlocal guard_calls
            guard_calls += 1
            if guard_calls == 3:
                raise RuntimeError('CHM marker appeared')
        with patch.object(guard, 'require_clear', side_effect=block_after_initial_barrier), \
             patch.object(store, 'put_json', wraps=store.put_json) as rewrite, \
             patch.object(store, 'delete', wraps=store.delete) as delete:
            with self.assertRaisesRegex(RuntimeError, 'CHM marker appeared'):
                ccrd.apply(store, report, client=live)
        self.assertEqual(guard_calls, 3)
        rewrite.assert_not_called()
        delete.assert_not_called()

    def test_ccrd_legacy_rechecks_after_submitted_checkpoint(self):
        from tests.test_reader_gc_ccrd import build_legacy, LegacyLive
        store, directories, counts = build_legacy(orphan=True)
        live = LegacyLive(directories, counts)
        report = ccrd.legacy_plan(store, client=live)
        submitted = []
        def checkpoint(value):
            if value['batches']:
                submitted.append(True)
        def block_after_submission(_store):
            if submitted:
                raise RuntimeError('CHM marker appeared')
        with patch.object(guard, 'require_clear', side_effect=block_after_submission), \
             patch.object(store, 'delete', wraps=store.delete) as delete:
            with self.assertRaisesRegex(RuntimeError, 'CHM marker appeared'):
                ccrd.legacy_apply(store, report, client=live, checkpoint=checkpoint)
        delete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
