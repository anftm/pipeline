import hashlib
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from scripts import publish_chm_gc_release as publish, chm_subset_journal as journal
from tests.test_chm_gc_terminal_model import TerminalModelTests


class Store:
    def __init__(self, history):
        self.files = {(publish.guard.ASSETS, publish.maintenance.DURABLE_JOURNAL): history,
                      (publish.guard.ASSETS, publish.maintenance.DURABLE_HISTORY_PREFIX +
                       hashlib.sha256(history).hexdigest() + '.json'): history}
        self.reader = self
    def read_bytes(self, bucket, path):
        if (bucket, path) not in self.files:
            raise FileNotFoundError(path)
        return self.files[bucket, path]
    def list_files(self, bucket, prefixes):
        return {path for owner, path in self.files if owner == bucket and path.startswith(prefixes)}


class Writer:
    def __init__(self, store):
        self.store, self.calls = store, 0
    def create(self, raw):
        self.calls += 1
        self.store.files[publish.guard.ASSETS, publish.guard.PREFIX + 'gc-release.json'] = raw


class PublicationTests(unittest.TestCase):
    def setUp(self):
        fixture = TerminalModelTests()
        fixture.setUp()
        self.fixture = fixture
        self.store, self.proof = Store(fixture.history), {'graph_complete': True, 'blockers': [],
                     'missing': [], 'ambiguous_paths': [], 'buckets': {bucket: {} for bucket in publish.graph.BUCKETS}}
        self.store.files[publish.guard.ASSETS, publish.maintenance.DURABLE_JOURNAL] = journal.encode({
            'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': 'a' * 32,
            'repositories': {'r': {}}})
        self.writer, self.fence = Writer(self.store), Mock()
        self.rows = [{'bucket': publish.guard.ASSETS, 'path': f'test/{i}'} for i in range(972)]
        self.traversal = SimpleNamespace(files={bucket: set() for bucket in publish.graph.BUCKETS},
                                         references=set(), trees=set())
        projections = patch.object(publish.collector, 'observe_projections', return_value=({}, {}))
        projections.start()
        self.addCleanup(projections.stop)
        plan_check = patch.object(publish, 'verify_original_plan')
        plan_check.start()
        self.addCleanup(plan_check.stop)
        self.retention_patch = patch.object(publish, 'verify_retention_graph')
        self.retention_patch.start()
        self.addCleanup(self.retention_patch.stop)
        self.stability_patch = patch.object(publish, 'require_complete_graph_stability', return_value=True)
        self.stability_patch.start()
        self.addCleanup(self.stability_patch.stop)

    def call(self):
        return publish.publish(self.store, self.writer, self.fence, self.traversal, self.proof, {}, {},
                               self.rows, window_id='a' * 32, client=Mock())

    def test_clean_graph_and_restored_history_publish_no_deletion(self):
        with patch.object(publish.guard, 'verify', return_value=True) as verify, \
             patch.object(publish.collector, 'verify_roots'):
            result = self.call()
        self.assertEqual(self.writer.calls, 1)
        self.assertEqual(result['deleted_objects'], 0)
        self.assertFalse(result['gc_executed'])
        self.assertEqual(verify.call_count, 2)
        self.assertIn('candidate_marker', verify.call_args_list[0].kwargs)
        verify.assert_called_with(self.store)

    def test_missing_or_changed_graph_and_history_block_write(self):
        for key, value in (('graph_complete', False), ('blockers', ['missing']),
                           ('missing', ['file']), ('ambiguous_paths', ['path'])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                publish.publish(self.store, self.writer, self.fence, self.traversal,
                                {**self.proof, key: value}, {}, {}, self.rows, window_id='a' * 32, client=Mock())
        self.assertEqual(self.writer.calls, 0)
        self.store.files[publish.guard.ASSETS, publish.maintenance.DURABLE_JOURNAL] = b'{}'
        with self.assertRaises(RuntimeError):
            self.call()
        self.assertEqual(self.writer.calls, 0)

    def test_existing_matching_marker_is_not_rewritten(self):
        with patch.object(publish.guard, 'verify', return_value=True), patch.object(publish.collector, 'verify_roots'):
            self.call()
            self.call()
        self.assertEqual(self.writer.calls, 1)

    def test_candidate_chain_and_reference_failure_precede_marker_write(self):
        with patch.object(publish.guard, 'verify', side_effect=ValueError('chain differs')):
            with self.assertRaisesRegex(ValueError, 'chain differs'):
                self.call()
        self.assertEqual(self.writer.calls, 0)
        self.traversal.trees.add((publish.guard.ASSETS, 'test'))
        with self.assertRaisesRegex(ValueError, 'remains protected'):
            self.call()
        self.assertEqual(self.writer.calls, 0)
        self.traversal.trees.clear()
        self.traversal.references.add((publish.guard.ASSETS, 'test/1'))
        with self.assertRaisesRegex(ValueError, 'remains protected'):
            self.call()
        self.assertEqual(self.writer.calls, 0)

    def test_unprotected_chapter_retention_blocks_release(self):
        self.retention_patch.stop()
        ref = (publish.guard.ASSETS, publish.guard.preflight.RETENTION)
        child = (publish.guard.ASSETS, 'chapters/example/chapter-manifest.json')
        raw = journal.encode({'resources': [{'bucket': child[0], 'path': child[1]}]})
        self.store.files[ref] = raw
        traversal = SimpleNamespace(retired_roots=set(), references={ref, child},
                                    root_hashes={ref: hashlib.sha256(raw).hexdigest()},
                                    loaded_digests={ref: hashlib.sha256(raw).hexdigest()}, direct={ref: {child}})
        with patch.object(publish.guard.preflight, 'RETENTION_SHA', hashlib.sha256(raw).hexdigest()):
            publish.verify_retention_graph(self.store, traversal)
            traversal.references.remove(child)
            with self.assertRaisesRegex(ValueError, 'not protected'):
                publish.verify_retention_graph(self.store, traversal)

    def test_new_window_finds_restored_archive_and_reuses_existing_marker(self):
        self.store.files[publish.guard.ASSETS, publish.maintenance.DURABLE_JOURNAL] = journal.encode({
            'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': 'a' * 32,
            'repositories': {'r': {}}, 'callback_mutation_started': False})
        with patch.object(publish.guard, 'verify', return_value=True), patch.object(publish.collector, 'verify_roots'):
            self.call()
            self.call()
        self.assertEqual(self.writer.calls, 1)

    def test_multiple_restored_archives_with_same_handoff_are_idempotent(self):
        alt = journal.encode({**self.fixture.state, 'resumption_dispatches': {'example': 'suppressed'}})
        self.store.files[publish.guard.ASSETS, publish.maintenance.DURABLE_HISTORY_PREFIX +
                         hashlib.sha256(alt).hexdigest() + '.json'] = alt
        self.store.files[publish.guard.ASSETS, publish.maintenance.DURABLE_JOURNAL] = journal.encode({
            'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': 'a' * 32,
            'repositories': {'r': {}}})
        with patch.object(publish.guard, 'verify', return_value=True), patch.object(publish.collector, 'verify_roots'):
            self.call()
            self.call()
        self.assertEqual(self.writer.calls, 1)

    def test_conflicting_terminal_handoffs_fail_before_publication(self):
        altered = {**self.fixture.state, 'terminal_handoff': {'bucket': publish.guard.ASSETS,
                    'path': publish.guard.PREFIX + 'a' * 64 + '.json', 'sha256': 'a' * 64}}
        raw = journal.encode(altered)
        self.store.files[publish.guard.ASSETS, publish.maintenance.DURABLE_HISTORY_PREFIX +
                         hashlib.sha256(raw).hexdigest() + '.json'] = raw
        self.store.files[publish.guard.ASSETS, publish.maintenance.DURABLE_JOURNAL] = journal.encode({
            'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': 'a' * 32,
            'repositories': {'r': {}}})
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.call()
        self.assertEqual(self.writer.calls, 0)

    def test_isolated_entry_requires_credentials_before_any_maintenance(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(publish.maintenance, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'durable central maintenance'):
                publish.isolated_main()
            run.assert_not_called()

    def test_isolated_entry_validates_bridge_before_maintenance_changes(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline',
               'GITHUB_WORKFLOW_REF': publish.WORKFLOW_REF, 'GITHUB_RUN_ID': '123',
               'READER_GC_DURABLE_MAINTENANCE': 'true', 'GC_PRODUCER_TOKENS_JSON': '{}'}
        with patch.dict('os.environ', env, clear=True), \
             patch.object(publish.guard.preflight, 'ReadOnlyStore', return_value=self.store), \
             patch.object(publish.guard, 'assert_bucket_available'), \
             patch.object(publish.guard, 'verify', side_effect=RuntimeError('bad bridge')), \
             patch.object(publish.maintenance, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'bad bridge'):
                publish.isolated_main()
            run.assert_not_called()

    def test_isolated_entry_never_pauses_or_restores_active_retired_producers(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline',
               'GITHUB_WORKFLOW_REF': publish.WORKFLOW_REF, 'GITHUB_RUN_ID': '123',
               'READER_GC_DURABLE_MAINTENANCE': 'true', 'GC_PRODUCER_TOKENS_JSON': '{}'}
        before = {'repositories': {'anftm/pipeline': {'permission': {}, 'cancelled': [], 'workflows': [
            {'path': '.github/workflows/publish-ccrd-ocr.yml', 'state': 'active'}]}}}
        self.store.files[publish.guard.ASSETS, publish.guard.PREFIX + 'original-maintenance.json'] = journal.encode(before)
        current = {'anftm/pipeline': {'permission': {}, 'workflows': [
            {'path': '.github/workflows/publish-ccrd-ocr.yml', 'state': 'active'}]}}
        with patch.dict('os.environ', env, clear=True), \
             patch.object(publish.guard.preflight, 'ReadOnlyStore', return_value=self.store), \
             patch.object(publish.guard, 'assert_bucket_available'), patch.object(publish.guard, 'verify'), \
             patch.object(publish.guard, 'verify_retention'), \
             patch.object(publish.maintenance, 'preflight', return_value=current), \
             patch.object(publish.maintenance, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'retired CHM-window producer'):
                publish.isolated_main()
            run.assert_not_called()

    def test_new_window_must_match_callback_and_not_already_restored(self):
        state = {'kind': 'reader-maintenance-journal', 'version': 1, 'maintenance_id': 'a' * 32,
                 'repositories': {'r': {}}}
        self.assertEqual(publish.require_gc_window(journal.encode(state), 'a' * 32), state)
        for current, owner in ((state, 'b' * 32), ({**state, 'restored': True,
                                'repositories': {'r': {'restored': True}}}, 'a' * 32),
                               ({**state, 'maintenance_id': publish.guard.preflight.OWNER},
                                publish.guard.preflight.OWNER)):
            with self.subTest(current=current), self.assertRaises(ValueError):
                publish.require_gc_window(journal.encode(current), owner)

    def test_root_recheck_failure_leaves_no_release_marker(self):
        with patch.object(publish.guard, 'verify', return_value=True), \
             patch.object(publish.collector, 'verify_roots', side_effect=RuntimeError('changed')):
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                self.call()
        self.assertEqual(self.writer.calls, 0)

    def test_changed_projection_leaves_no_release_marker(self):
        with patch.object(publish.guard, 'verify', return_value=True), \
             patch.object(publish.collector, 'verify_roots'), \
             patch.object(publish.collector, 'observe_projections', return_value=({'different': True}, {})):
            with self.assertRaisesRegex(ValueError, 'projections changed'):
                self.call()
        self.assertEqual(self.writer.calls, 0)

    def test_changed_or_unverifiable_nested_closure_prevents_marker(self):
        with patch.object(publish.guard, 'verify', return_value=True), \
                patch.object(publish.collector, 'verify_roots'), \
                patch.object(publish, 'require_complete_graph_stability', side_effect=ValueError('changed')):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.call()
            self.assertEqual(self.writer.calls, 0)

    def test_nested_manifest_version_change_after_graph_is_rejected(self):
        self.stability_patch.stop()
        safety = publish.guard.preflight.safety
        class Versions:
            read_etags = {('bucket', 'manifest.json'): '"v1"'}
            def list_exact_versions(self, bucket, paths):
                self.calls += 1
                return {'manifest.json': '"v1"' if self.calls == 1 else '"v2"'}
            calls = 0
        store = Versions()
        traversal = SimpleNamespace(loaded_digests={('bucket', 'manifest.json'): 'a' * 64})
        with patch.object(safety, 'recheck_full_root_inventory', return_value=True), \
             patch.object(publish.collector, 'verify_roots') as roots:
            with self.assertRaisesRegex(ValueError, 'version changed'):
                publish.require_complete_graph_stability(store, traversal)
            roots.assert_not_called()

    def test_workflow_uses_three_groups_and_does_not_call_gc(self):
        from pathlib import Path
        import yaml
        folder = Path(__file__).resolve().parents[1] / '.github/workflows'
        outer = yaml.load((folder / 'publish-chm-gc-release.yml').read_text(), Loader=yaml.BaseLoader)
        inner = yaml.load((folder / 'publish-chm-gc-release-inner.yml').read_text(), Loader=yaml.BaseLoader)
        groups = (outer['concurrency'], outer['jobs']['release']['concurrency'], inner['jobs']['release']['concurrency'])
        self.assertEqual([group['group'] for group in groups],
                         ['publish-ccrd-index', 'reader-global-maintenance', 'reader-sidecar'])
        self.assertTrue(all(group['cancel-in-progress'] == 'false' and group['queue'] == 'max' for group in groups))
        steps = inner['jobs']['release']['steps']
        commands = '\n'.join(step.get('run', '') for step in steps)
        self.assertIn('scripts/publish_chm_gc_release.py', commands)
        self.assertNotIn('scripts/reader_gc_collect.py', commands)


if __name__ == '__main__':
    unittest.main()
