import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import ccrd_ocr_exact_retirement as exact, reader_bucket_store
from scripts import expanded_ocr_retirement as expanded
from scripts import run_expanded_ocr_retirement as runner
from tests.test_ccrd_ocr_exact_retirement import Fence, MemoryStore


class RetireStore(MemoryStore):
    def assert_buckets_available(self):
        pass


class ExpandedRetirementExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = patch.object(runner, 'OUTPUT', Path(self.tmp.name))
        self.output.start()
        self.addCleanup(self.output.stop)
        # These orchestration fixtures use three test keys rather than the
        # reviewed 2,710; inventory validation has separate production tests.
        self.inventory_guard = patch.object(expanded, 'assert_same_inventory')
        self.inventory_guard.start()
        self.addCleanup(self.inventory_guard.stop)
        self.plan_controls_guard = patch.object(expanded, 'verify_saved_plan_controls')
        self.plan_controls_guard.start()
        self.addCleanup(self.plan_controls_guard.stop)
        self.store = RetireStore()
        self.rows = []
        for i in range(3):
            raw = bytes([i + 1])
            row = {'bucket': expanded.PAGES, 'path': f'objects/aa/reviewed-{i}',
                   'bytes': 1, 'sha256': exact.sha(raw)}
            self.rows.append(row)
            self.store.put(row['bucket'], row['path'], raw)
        self.plan = {'version': 1, 'kind': 'ccrd-exact-ocr-retirement-plan',
                     'generation': exact.GENERATION, 'inventory_sha256': expanded.INVENTORY_SHA256,
                     'expanded_targets': self.rows, 'processing_updates': {}, 'base_generation': 'base',
                     'catalog_identity': {}, 'reader_acknowledgments': {'hf': {}, 'pages': {}}}

    def proof(self, *, actual):
        return {'graph_complete': True, 'target_count': len(self.rows), 'target_exclusivity_proven': True,
                'retained_target_count': 0, 'non_target_origins': [], 'tree_roots_without_origin': 0,
                'actual_retired_controls_checked': actual, 'internal_target_manifests_omitted': True}

    def test_run_orders_live_graph_and_barrier_before_any_deletion(self):
        events = []
        def simulate(store, plan, fence, *, actual=False, details=None, **kwargs):
            events.append('actual-graph' if actual else 'simulation')
            if details is not None:
                details['reference_graph'] = type('FakeGraph', (), {'store': store})()
            return self.proof(actual=actual)
        def apply(store, plan, fence, client, checkpoint):
            events.append('retire-controls')
            checkpoint('controls-retired')
        original_delete = self.store.delete
        def delete(bucket, paths):
            events.append('delete')
            original_delete(bucket, paths)
        self.store.delete = delete
        with patch.object(expanded, 'prepare', return_value=self.plan), \
                patch.object(expanded, 'simulate', side_effect=simulate), \
                patch.object(expanded, 'apply_planned_reference_retirement', side_effect=apply), \
                patch.object(runner, 'check_simulated_roots_before_mutation'), \
                patch.object(expanded, 'recheck_roots_and_controls', side_effect=lambda *args: events.append('root-recheck')), \
                patch.object(runner.collector, 'verify_roots', side_effect=lambda *args: events.append('graph-root-check')), \
                patch.object(runner, 'check_current_reader_coverage'), \
                patch.object(runner, 'check_reader_consumers', side_effect=lambda *args: events.append('consumers')):
            result = runner.run(self.store, Fence(), None, self.rows)
        self.assertTrue(result['complete'])
        self.assertEqual(result['deletion']['confirmed_deleted_objects'], len(self.rows))
        self.assertLess(events.index('simulation'), events.index('retire-controls'))
        self.assertLess(events.index('retire-controls'), events.index('actual-graph'))
        self.assertLess(events.index('actual-graph'), events.index('root-recheck'))
        self.assertLess(events.index('graph-root-check'), events.index('delete'))
        persisted = json.loads(self.store.read_bytes(expanded.CCRD, runner.PROGRESS))
        self.assertEqual(persisted['phase'], 'complete')
        self.assertEqual(persisted['deletion']['confirmed_deleted_objects'], 3)

    def test_failed_actual_graph_does_not_delete_and_persists_failure(self):
        def simulate(store, plan, fence, *, actual=False, **kwargs):
            if actual:
                raise ValueError('injected actual graph failure')
            return self.proof(actual=False)
        with patch.object(expanded, 'prepare', return_value=self.plan), \
                patch.object(expanded, 'simulate', side_effect=simulate), \
                patch.object(expanded, 'apply_planned_reference_retirement'), \
                patch.object(runner, 'check_simulated_roots_before_mutation'), \
                patch.object(runner, 'check_current_reader_coverage'), \
                patch.object(runner, 'check_reader_consumers'):
            with self.assertRaisesRegex(ValueError, 'actual graph failure'):
                runner.run(self.store, Fence(), None, self.rows)
        persisted = json.loads(self.store.read_bytes(expanded.CCRD, runner.PROGRESS))
        self.assertEqual(persisted['phase'], 'failed')
        self.assertTrue(persisted['references_retired'])
        self.assertEqual(persisted['deletion']['confirmed_deleted_objects'], 0)
        self.assertEqual(self.store.deletions, [])

    def test_resume_after_partial_batch_rechecks_live_closure(self):
        state = {'version': 1, 'kind': 'expanded-ocr-retirement', 'complete': False,
                 'inventory_sha256': expanded.INVENTORY_SHA256,
                 'plan_sha256': expanded.sha(expanded.encode(self.plan)),
                 'references_retired': True, 'reference_mutation_started': True,
                 'deletion': {'batches': [{'bucket': expanded.PAGES,
                    'paths': [r['path'] for r in self.rows], 'status': 'submitted'}]}}
        runner.save_plan(self.store, self.plan)
        runner.save_progress(self.store, state, 'deletion-batch-submitted')
        self.store.delete(expanded.PAGES, [self.rows[0]['path']])
        def simulate(store, plan, fence, *, actual=False, details=None, **kwargs):
            self.assertTrue(actual)
            details['reference_graph'] = type('FakeGraph', (), {'store': store})()
            return self.proof(actual=True)
        with patch.object(expanded, 'simulate', side_effect=simulate) as scan, \
                patch.object(exact, 'verify_reference_retirement'), \
                patch.object(expanded, 'recheck_roots_and_controls'), \
                patch.object(runner.collector, 'verify_roots'), \
                patch.object(runner, 'check_current_reader_coverage'), \
                patch.object(runner, 'check_reader_consumers'):
            result = runner.run(self.store, Fence(), None, self.rows, resume=True)
        self.assertTrue(result['complete'])
        self.assertEqual(result['deletion']['confirmed_deleted_objects'], 3)
        self.assertEqual(scan.call_count, 1)

    def test_partial_control_recovery_rescans_before_resuming_writes(self):
        state = {'version': 1, 'kind': 'expanded-ocr-retirement', 'complete': False,
                 'inventory_sha256': expanded.INVENTORY_SHA256,
                 'plan_sha256': expanded.sha(expanded.encode(self.plan)),
                 'reference_mutation_started': True,
                 'deletion': {'batches': [], 'confirmed_deleted_objects': 0}}
        runner.save_plan(self.store, self.plan)
        runner.save_progress(self.store, state, 'failed')
        events = []
        def simulate(store, plan, fence, *, actual=False, allow_partial=False, details=None, **kwargs):
            events.append('actual' if actual else ('partial' if allow_partial else 'initial'))
            details['reference_graph'] = type('FakeGraph', (), {'store': store})()
            return self.proof(actual=actual)
        def apply(*args):
            events.append('apply')
        with patch.object(expanded, 'simulate', side_effect=simulate), \
                patch.object(expanded, 'apply_planned_reference_retirement', side_effect=apply), \
                patch.object(runner, 'check_simulated_roots_before_mutation'), \
                patch.object(expanded, 'recheck_roots_and_controls'), \
                patch.object(runner.collector, 'verify_roots'), \
                patch.object(runner, 'check_current_reader_coverage'), \
                patch.object(runner, 'check_reader_consumers'):
            result = runner.run(self.store, Fence(), None, self.rows, resume=True)
        self.assertTrue(result['complete'])
        self.assertEqual(events, ['partial', 'apply', 'actual'])

    def test_scope_store_rejects_any_unapproved_delete_before_s3_call(self):
        store = object.__new__(runner.ExactStore)
        store.allowed = {(expanded.PAGES, 'objects/aa/approved')}
        for bucket, paths in [(expanded.PAGES, ['objects/aa/other']),
                              (expanded.ASSETS, ['documents/pdf/ps/book.pdf']),
                              (expanded.PAGES, ['objects/aa/approved', 'objects/aa/approved'])]:
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                store.delete(bucket, paths)

    def test_missing_bucket_never_counts_as_absent_target(self):
        store = object.__new__(runner.ExactStore)
        error = reader_bucket_store.S3NotFound('gone')
        cause = Exception('NoSuchBucket')
        cause.response = {'Error': {'Code': 'NoSuchBucket'}}
        error.__cause__ = cause
        with patch.object(reader_bucket_store.S3BucketStore, 'read_bytes', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'bucket unavailable'):
                store.read_bytes(expanded.PAGES, 'objects/aa/target')

    def test_current_reader_coverage_blocks_dropped_book_route(self):
        sidecar = {'v': 1, 'f': {'repo\0book.pdf': {'b': expanded.PAGES, 'p': 'objects/aa/book.pdf', 'm': 'p'}}}
        self.store.put(expanded.ASSETS, runner.collector.SIDECAR, gzip.compress(exact.encode(sidecar)))
        self.store.put(expanded.ASSETS, exact.READER_CONSUMERS, {'resources': []})
        with patch.object(runner.collector, 'current', return_value=(None, {})), \
                patch.object(runner.collector, 'project_sidecar', return_value=sidecar):
            with self.assertRaisesRegex(ValueError, 'does not retain'):
                runner.check_current_reader_coverage(self.store, self.plan)

    def test_root_recheck_uses_real_bytes_not_only_overlay_digest(self):
        root = (expanded.ASSETS, 'reader-index/live.json')
        raw = self.store.put(*root, {'kind': 'reader-gc-consumers', 'resources': []})
        fake_graph = type('FakeGraph', (), {'store': self.store, 'root_hashes': {root: exact.sha(raw)}})()
        plan = {'processing_updates': {}, 'control_hashes_before': {}, 'reader_consumer_sha256_before': 'x'}
        with patch.object(runner.collector, 'verify_roots'), patch.object(exact, 'inspect_roots', return_value=({}, {})), \
                patch.object(expanded, 'check_partial_controls'):
            runner.check_simulated_roots_before_mutation(self.store, plan, {'reference_graph': fake_graph},
                                                         allow_partial=True)
            self.store.put(*root, {'kind': 'reader-gc-consumers', 'resources': [{'path': 'foreign'}]})
            with self.assertRaisesRegex(ValueError, 'live root changed'):
                runner.check_simulated_roots_before_mutation(self.store, plan, {'reference_graph': fake_graph},
                                                             allow_partial=True)

    def test_existing_progress_rejects_second_fresh_run(self):
        self.store.put(expanded.CCRD, runner.PROGRESS, {'phase': 'started'})
        with self.assertRaisesRegex(ValueError, 'prior OCR/PS retirement'):
            runner.run(self.store, Fence(), None, self.rows)

    def test_saved_progress_without_immutable_digest_cannot_resume(self):
        self.store.put(expanded.CCRD, runner.PLAN, expanded.encode(self.plan))
        self.store.put(expanded.CCRD, runner.PROGRESS, {'kind': 'expanded-ocr-retirement'})
        with self.assertRaisesRegex(ValueError, 'lacks its immutable plan digest'):
            runner.run(self.store, Fence(), None, self.rows, resume=True)


if __name__ == '__main__':
    unittest.main()
