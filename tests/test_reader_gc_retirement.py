from datetime import date, timedelta, datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import reader_bucket, reader_gc_collect as collect, reader_gc_graph as graph
from scripts import reader_gc_retirement as retirement, reader_gc_maintenance as maintenance
from scripts import reader_lifecycle as lifecycle
from tests.test_reader_gc_collect import Store

ASSETS, PDF, INPUT = graph.READER_BUCKETS
SOURCE = 'a' * 64


class RetirementTests(unittest.TestCase):
    def test_global_collection_honors_input_retention_period(self):
        store = Store()
        root = f'objects/aa/{SOURCE}/' + 'b' * 16 + '/ocr-input'
        path = root + '/page-000001.png'
        store.put(INPUT, path, b'input')
        store.put(ASSETS, 'reader-index/input-pin.json', {'resources': [{'bucket': INPUT, 'path': path}]})
        journal = {'version': 1, 'kind': 'reader-v3-retirement', 'resources': [], 'retired_paths': [],
                   'replacement_acks': {'hf': True, 'pages': True}, 'retired_input_roots': {root: {
                       'bucket': INPUT, 'root': root, 'source_sha256': SOURCE,
                       'released_at': datetime.now(timezone.utc).isoformat()}}}
        store.put(ASSETS, 'reader-index/v3/retirement.json', journal)
        reference = graph.ReferenceGraph(store)
        scanned = reference.build()
        self.assertEqual(scanned['buckets'][INPUT]['candidates'], [])
        journal['retired_input_roots'][root]['released_at'] = '2020-01-01T00:00:00+00:00'
        store.put(ASSETS, 'reader-index/v3/retirement.json', journal)
        reference = graph.ReferenceGraph(store)
        scanned = reference.build()
        result = collect.collect(store, Mock(), reference, scanned, {}, apply=True, retirement_roots=set())
        self.assertEqual(result['confirmed_deleted_objects'], 1)

    def test_fourth_domain_mutations_participate_in_global_fence(self):
        from scripts.reader_gc_fence import producer_path
        self.assertTrue(producer_path('.github/workflows/reader-gc-ccrd.yml'))

    def test_restore_dispatches_only_previously_enabled_entrypoints_once(self):
        state = {'repositories': {'anftm/pipeline': {'workflows': [
            {'path': '.github/workflows/pdf-lane-watchdog.yml', 'state': 'active'},
            {'path': '.github/workflows/reader-v3-publish.yml', 'state': 'disabled_manually'}]}}}
        with patch.object(maintenance, 'journal'), patch.object(maintenance, 'api') as api:
            maintenance.kick_restored(Path('unused'), state)
            maintenance.kick_restored(Path('unused'), state)
        self.assertEqual(api.call_count, 1)
        self.assertIn('pdf-lane-watchdog.yml/dispatches', api.call_args.args[1])

    def test_every_format_reclaims_old_bundles_and_preserves_shared_live_objects(self):
        store = Store()
        families = [(ASSETS, f'{family}/{SOURCE}') for family in (
            'pages/image/tif', 'chapters/ebook/epub', 'documents/text/txt', 'documents/office/docx',
            'documents/spreadsheet/xlsx', 'documents/web/mhtml', 'documents/pdf/ps',
            'native/ebook/chm', 'media/audio/mp3', 'media/video/mp4', 'media/swf/swf')]
        families += [(PDF, f'objects/aa/{SOURCE}/' + 'b' * 16),
                     (PDF, 'derived/djvu/' + 'c' * 32), (INPUT, f'objects/aa/{SOURCE}/' + 'd' * 16)]
        old, live = [], []
        for bucket, root in families:
            for name in ['obsolete.bin', 'shared.bin']:
                store.put(bucket, root + '/' + name, name.encode())
            old.append({'bucket': bucket, 'path': root + '/obsolete.bin'})
            live.append({'bucket': bucket, 'path': root + '/shared.bin'})
        hint = retirement.record(old, live, 'c' * 32, 'catalog-replacement')
        store.put(ASSETS, hint[0], hint[1])
        # Use explicit subresources to exercise shared sibling retention.
        store.put(ASSETS, 'reader-index/current-test.json', {'resources': live})
        store.put(PDF, 'objects/unrelated/deferred.bin', b'not in journal')
        reference = graph.ReferenceGraph(store)
        scanned = reference.build()
        self.assertTrue(scanned['graph_complete'], scanned['blockers'])
        # Bundle siblings remain protected; a receipt cannot delete around them.
        result = collect.collect(store, Mock(), reference, scanned, {}, apply=True,
                                 retirement_roots=retirement.targets({hint[0]: hint[1]}))
        self.assertEqual(result['confirmed_deleted_objects'], 0)
        store.put(ASSETS, 'reader-index/current-test.json', {'resources': []})
        reference = graph.ReferenceGraph(store)
        scanned = reference.build()
        result = collect.collect(store, Mock(), reference, scanned, {}, apply=True,
                                 retirement_roots=retirement.targets({hint[0]: hint[1]}))
        self.assertEqual(result['confirmed_deleted_objects'], len(families) * 2)
        self.assertTrue(result['complete'])
        self.assertIn('objects/unrelated/deferred.bin', store.objects[PDF])
        self.assertIn(hint[0], store.objects[ASSETS])

    def test_catalog_receipt_is_durable_before_pointer_update(self):
        old_path = f'documents/text/txt/{SOURCE}/document.txt'
        previous, _ = lifecycle.catalog_generation({'v': 1, 'f': {'book': {'b': ASSETS, 'p': old_path}}})
        events = []
        with patch.object(reader_bucket, 'read_json', return_value=previous), \
                patch.object(reader_bucket, 'publish_indexes', side_effect=lambda *a: events.append('snapshot')), \
                patch.object(reader_bucket, 'publish_json', side_effect=lambda p, *a: events.append(p)):
            reader_bucket.publish_catalog({'v': 1, 'f': {}})
        self.assertTrue(events[0].startswith(retirement.PREFIX))
        self.assertEqual(events[-1], reader_bucket.INDEX_FILES['catalog'])

    def test_handoff_records_persist_candidate_bundles_but_do_not_authorize_deletion(self):
        root = f'objects/aa/{SOURCE}/' + 'b' * 16
        protection = lifecycle.processing_record([{'bucket': PDF, 'root': root}], 'render', {})
        path = 'reader-index/processing/test.json'
        updates = lifecycle.processing_handoffs([{'processing_roots': [path], 'path': root + '/document.pdf'}],
                                               'c' * 32, lambda _: protection)
        self.assertEqual(updates[path]['status'], 'released')
        hint_path = next(p for p in updates if p.startswith(retirement.PREFIX))
        retirement.validate(hint_path, updates[hint_path])
        self.assertEqual(updates[hint_path]['roots'], [{'bucket': PDF, 'root': root}])

    def test_receipt_refuses_unbounded_roots_and_changed_content(self):
        for path in ['objects/', 'documents/office/docx', '../source.pdf', 'objects/aa/' + SOURCE + '/../../x']:
            self.assertIsNone(retirement.bundle_root(path))
        name, value = retirement.record([{'bucket': ASSETS, 'path': f'documents/text/txt/{SOURCE}/document.txt'}],
                                        [], 'c' * 32, 'catalog-replacement')
        value['roots'][0]['root'] = 'documents/text'
        with self.assertRaisesRegex(ValueError, 'receipt'):
            retirement.validate(name, value)

    def test_automatic_cursor_recovers_exact_partial_plan_and_verifies_counts(self):
        store = Store()
        for n in range(3):
            store.put(PDF, f'objects/aa/{SOURCE}/bbbbbbbbbbbbbbbb/page{n}.jxl', b'page')
        reference = graph.ReferenceGraph(store)
        scanned = reference.build()
        plan = collect.collect(store, Mock(), reference, scanned, {}, apply=False, reader_only=True)
        store.put(ASSETS, 'reader-index/gc/123/collection.json', plan)
        retirement.save_cursor(store, '123', 'unreferenced', plan, False)
        store.delete(PDF, [plan['targets'][0]['path']])
        saved, recovered = retirement.pending_plan(store)
        self.assertEqual(saved['run_id'], '123')
        self.assertEqual(recovered, plan)
        reference = graph.ReferenceGraph(store)
        scanned = reference.build()
        result = collect.collect(store, Mock(), reference, scanned, {}, apply=True, reader_only=True, resume=recovered)
        self.assertEqual(result['confirmed_deleted_objects'], 3)
        self.assertEqual(result['confirmed_deleted_bytes'], 12)
        retirement.save_cursor(store, '124', 'unreferenced', result, True)
        self.assertEqual(retirement.pending_plan(store), (None, None))

    def test_missing_or_changed_saved_plan_never_falls_back_to_unchecked_deletion(self):
        store = Store()
        retirement.save_cursor(store, '123', 'unreferenced', {'plan_sha256': 'a' * 64}, False)
        with self.assertRaises(FileNotFoundError):
            retirement.pending_plan(store)
        store.put(ASSETS, 'reader-index/gc/123/collection.json', {'plan_sha256': 'b' * 64, 'scope': 'unreferenced'})
        with self.assertRaisesRegex(ValueError, 'cursor'):
            retirement.pending_plan(store)

    def test_retention_observations_survive_failure_and_budget_limits(self):
        store = Store()
        for n in range(3):
            store.put(PDF, f'objects/unused/{n}.bin', b'old')
        reference = graph.ReferenceGraph(store)
        scanned = reference.build()
        collect.collect(store, Mock(), reference, scanned, {}, apply=True, grace_days=2, limit=1)
        state = json.loads(store.read_bytes(ASSETS, 'reader-index/reader_gc_state.json'))
        self.assertEqual(len(state['orphans']), 3)
        for item in state['orphans'].values():
            item['first_seen'] = (date.today() - timedelta(days=3)).isoformat()
        store.put(ASSETS, 'reader-index/reader_gc_state.json', state)
        reference = graph.ReferenceGraph(store)
        scanned = reference.build()
        with patch.object(store, 'delete', side_effect=OSError('interrupted')), self.assertRaises(OSError):
            collect.collect(store, Mock(), reference, scanned, {}, apply=True, grace_days=2, limit=1)
        self.assertEqual(json.loads(store.read_bytes(ASSETS, 'reader-index/reader_gc_state.json')), state)

    def test_killed_maintenance_runner_recovers_original_producer_state(self):
        store = Store()
        state = {'version': 1, 'kind': 'reader-maintenance-journal', 'repositories': {'repo/test': {
            'workflows': [{'id': 12, 'state': 'active'}], 'cancelled': [], 'permission_changed': True,
            'permission': {'enabled': True, 'allowed_actions': 'all'}}}, 'restored': False}
        store.put(ASSETS, maintenance.DURABLE_JOURNAL, state)
        with tempfile.TemporaryDirectory() as directory, patch.object(maintenance, 'durable_store', return_value=store), \
                patch.object(maintenance, 'permissions') as permissions, patch.object(maintenance, 'workflow_state') as workflows:
            maintenance.recover(Path(directory) / 'journal.json')
        permissions.assert_called_once_with('repo/test', state['repositories']['repo/test']['permission'])
        workflows.assert_called_once_with('repo/test', {'id': 12, 'state': 'active'}, True)
        self.assertTrue(json.loads(store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL))['restored'])

    def test_terminal_maintenance_history_survives_a_new_current_window(self):
        store = Store()
        failed = {'version': 1, 'kind': 'reader-maintenance-journal', 'maintenance_id': 'a' * 32,
            'repositories': {}, 'restored': False,
            'restore_attempt_errors': [{'repository': 'repo/test', 'error': 'MaintenanceReadbackError',
                                        'stage': 'permissions', 'attempt': 1}]}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, 'durable_store', return_value=store):
            path = Path(directory) / 'journal.json'
            maintenance.journal(path, failed)
            failed_raw = path.read_bytes()
            recovered = {**failed, 'restored': True, 'restore_errors': []}
            maintenance.journal(path, recovered)
            recovered_raw = path.read_bytes()
            maintenance.journal(path, {'version': 1, 'kind': 'reader-maintenance-journal',
                'maintenance_id': 'b' * 32, 'repositories': {}})
        for raw in (failed_raw, recovered_raw):
            target = maintenance.DURABLE_HISTORY_PREFIX + hashlib.sha256(raw).hexdigest() + '.json'
            self.assertEqual(store.read_bytes(ASSETS, target), raw)
        current = json.loads(store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL))
        self.assertEqual(current['maintenance_id'], 'b' * 32)
        reference = graph.ReferenceGraph(store)
        scanned = reference.build()
        self.assertTrue(scanned['graph_complete'], scanned['blockers'])
        self.assertEqual(scanned['buckets'][ASSETS]['candidates'], [])

    def test_completed_legacy_cursor_is_archived_without_repeating_restore_or_dispatch(self):
        store = Store()
        completed = {'version': 1, 'kind': 'reader-maintenance-journal', 'repositories': {},
            'maintenance_id': 'a' * 32, 'restored': True, 'restore_attempt_errors': [
                {'repository': 'repo/test', 'error': 'MaintenanceAPIError', 'status': 503}]}
        store.put(ASSETS, maintenance.DURABLE_JOURNAL, completed)
        raw = store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, 'durable_store', return_value=store), \
                patch.object(maintenance, 'restore') as restore, patch.object(maintenance, 'api') as api:
            maintenance.recover(Path(directory) / 'journal.json')
        target = maintenance.DURABLE_HISTORY_PREFIX + hashlib.sha256(raw).hexdigest() + '.json'
        self.assertEqual(store.read_bytes(ASSETS, target), raw)
        self.assertEqual(store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL), raw)
        restore.assert_not_called()
        api.assert_not_called()

    def test_history_readback_failure_does_not_advance_the_durable_recovery_cursor(self):
        store = Store()
        original = {'version': 1, 'kind': 'reader-maintenance-journal',
            'maintenance_id': 'a' * 32, 'repositories': {}, 'restored': False}
        store.put(ASSETS, maintenance.DURABLE_JOURNAL, original)
        before = store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL)
        read = store.read_bytes
        def corrupted_history(bucket, path):
            if path.startswith(maintenance.DURABLE_HISTORY_PREFIX):
                return b'wrong snapshot'
            return read(bucket, path)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, 'durable_store', return_value=store), \
                patch.object(store, 'read_bytes', side_effect=corrupted_history):
            with self.assertRaisesRegex(RuntimeError, 'history readback mismatch'):
                maintenance.journal(Path(directory) / 'journal.json', {**original, 'restored': True})
        self.assertEqual(store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL), before)

    def test_preflight_does_not_write_durable_history_or_current(self):
        store = Store()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, 'durable_store', return_value=store), \
                patch.object(store, 'put_bytes') as put:
            maintenance.journal(Path(directory) / 'preflight.json',
                {'preflight_only': True, 'repositories': {}, 'remote_writes': 0})
        put.assert_not_called()


if __name__ == '__main__':
    unittest.main()
