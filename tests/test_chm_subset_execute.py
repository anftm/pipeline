import json
import unittest
from unittest.mock import Mock, patch

from scripts import chm_subset_execute as run, chm_subset_journal as model
from tests.test_chm_subset_delete_engine import DeleteEngineTests


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        fixture = DeleteEngineTests()
        fixture.setUp()
        self.fixture = fixture
        self.store, self.plan = fixture.store, fixture.plan
        self.progress = {'batches': fixture.old}
        self.root = {'path': 'fixed-root', 'raw': b'fixed-root', 'sha256': run.locked.ROOT_SHA, 'objects': 20}
        self.patches = [patch.object(run.isolated, 'build', return_value=self.root),
                        patch.object(run, 'live_controls'),
                        patch.object(run, 'open_cursor', return_value=fixture.cursor),
                        patch.object(run, 'build_graph', return_value=(Mock(), {})),
                        patch.object(run, 'barrier')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def call(self):
        return run.execute(self.store, self.store, Mock(), Mock(), self.fixture.rows, self.plan,
                           self.progress, {}, {})

    def test_complete_subset_never_marks_original_complete(self):
        result = self.call()
        self.assertTrue(result['subset_complete'])
        self.assertEqual(result['subset_verified_absent'], 4)
        self.assertFalse(result['original_complete'])
        self.assertFalse(result['producer_restore_authorized'])
        self.assertIn(self.fixture.withheld[0]['path'], self.store.objects)

    def test_missing_protection_stops_before_journal_or_delete(self):
        with patch.object(run, 'live_controls', side_effect=ValueError('root missing')):
            with self.assertRaisesRegex(ValueError, 'root missing'):
                self.call()
        run.open_cursor.assert_not_called()
        self.assertEqual(self.store.calls, [])

    def test_graph_or_barrier_failure_prevents_any_delete(self):
        with patch.object(run, 'build_graph', side_effect=ValueError('new reference')):
            with self.assertRaisesRegex(ValueError, 'new reference'):
                self.call()
        self.assertEqual(self.store.calls, [])
        run.barrier.side_effect = RuntimeError('root changed')
        with self.assertRaisesRegex(RuntimeError, 'root changed'):
            self.call()
        self.assertEqual(self.store.calls, [])

    def test_unknown_delete_stops_with_pending_intent(self):
        self.store.mode = 'partial-lost'
        with self.assertRaises(OSError):
            self.call()
        self.assertIsNotNone(self.fixture.cursor.head()['state']['pending'])
        self.assertEqual(len(self.store.calls), 1)

    def test_real_fixed_plan_binds_original_progress(self):
        rows = run.preflight.chm.preview.load_inventory(run.preflight.INVENTORY)['targets']
        classification = json.loads(run.preflight.CLASSIFICATION.read_bytes())
        index = {row['path']: row for row in rows}
        selected = sorted((index[item['path']] for item in classification['targets']
                           if item['category'] == 'mapped-with-saved-evidence'), key=lambda row: row['path'])
        withheld = sorted((index[item['path']] for item in classification['targets']
                           if item['category'] in {'needs-independent-review', 'withheld-oversize'}),
                          key=lambda row: row['path'])
        plan = model._plan(selected, withheld, run.locked.IDENTITY)
        state = {'receipts': [], 'pending': None, 'subset_complete': False,
                 'original_complete': False, 'producer_restore_authorized': False}
        first = model.make_envelope(plan, state, sequence=0, parent_sha256=None, nonce=run.storage.INITIAL_NONCE)
        self.assertEqual(model.digest(plan), run.storage.PLAN_SHA)
        self.assertEqual(model.digest(first), run.storage.INITIAL_SHA)

    def test_actual_graph_rejects_new_candidate_reference(self):
        # This class mocks the isolated root. Run the graph fixture with that
        # mock stopped so its deterministic synthetic root is actually parsed.
        from tests.test_chm_subset_actual_graph import ActualSubsetGraphTests
        self.patches[0].stop()
        fixture = ActualSubsetGraphTests()
        fixture.setUp()
        traversal, report = fixture.build()
        proof = fixture.capture(traversal, report, root_rechecked=True, consumers_checked=True)
        self.assertEqual(proof['retained_targets'], 1)
        fixture.store.put(model.subset.ASSETS, 'reader-index/new-reference.json', {
            'resources': [{'bucket': model.subset.ASSETS, 'path': fixture.selected[0]['path']}]})
        traversal, report = fixture.build()
        with self.assertRaisesRegex(ValueError, 'exactly the withheld'):
            fixture.capture(traversal, report, root_rechecked=True, consumers_checked=True)

    def test_pending_recovery_uses_exact_durable_intent(self):
        row = self.fixture.selected[0]
        initial = self.fixture.cursor.head()
        pending = {'bucket': model.subset.ASSETS, 'status': 'submitted', 'paths': [row['path']]}
        event = model.make_envelope(self.plan, {**initial['state'], 'pending': pending}, sequence=1,
                                    parent_sha256=model.digest(initial), nonce='a' * 32)
        self.fixture.cursor.append(model.digest(initial), event)
        self.store.objects.pop(row['path'])
        result = self.call()
        self.assertTrue(result['subset_complete'])
        self.assertTrue(all(row['path'] not in paths for paths in self.store.calls))

    def test_plan_only_recovery_requires_fresh_graph_before_initial_event(self):
        self.patches[2].stop()  # exercise the real cursor initialization path
        self.store.files = {'plan.json': model.encode(self.plan)}
        empty = {'receipts': [], 'pending': None, 'subset_complete': False,
                 'original_complete': False, 'producer_restore_authorized': False}
        first = model.make_envelope(self.plan, empty, sequence=0, parent_sha256=None, nonce=run.storage.INITIAL_NONCE)
        with patch.object(run.storage, 'INITIAL_SHA', model.digest(first)), \
             patch.object(run, 'build_graph', side_effect=RuntimeError('fresh graph failed')):
            with self.assertRaisesRegex(RuntimeError, 'fresh graph failed'):
                self.call()
        self.assertEqual(set(self.store.files), {'plan.json'})
        self.assertEqual(self.store.calls, [])
        with patch.object(run.storage, 'INITIAL_SHA', model.digest(first)):
            result = self.call()
        self.assertTrue(result['subset_complete'])
        self.assertEqual(self.store.files['event-00000000.json'], model.encode(first))


if __name__ == '__main__':
    unittest.main()
