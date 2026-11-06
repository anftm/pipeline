import hashlib
import unittest
from unittest.mock import Mock, patch

from scripts import chm_final20_execute as run, chm_final20_journal as model, chm_subset_journal as common
from tests.test_chm_subset_journal_backend import FakeBackend


class Memory(FakeBackend):
    def __init__(self, targets, root):
        super().__init__()
        self.objects = {row['path']: f'book-{i}'.encode() for i, row in enumerate(targets)}
        self.objects[root['path']] = root['raw']
        self.bound = model.plan(targets, 'a' * 64, 'b' * 64)
        self.root = root
        self.calls = []
        self.mode = None

    def read_bytes(self, bucket, path):
        if path not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[path]

    def delete_targets(self, paths):
        self.calls.append(('targets', list(paths)))
        if self.mode == 'partial':
            self.objects.pop(paths[0])
            raise run.Uncertain('partial response')
        for path in paths:
            self.objects.pop(path)
        if self.mode == 'full':
            raise run.Uncertain('lost response')

    def delete_root(self):
        self.calls.append(('root', self.root['path']))
        self.objects.pop(self.root['path'])
        if self.mode == 'root':
            raise run.Uncertain('root response lost')


class Final20ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.targets = []
        for i in range(4):
            body = f'book-{i}'.encode()
            self.targets.append({'bucket': common.subset.ASSETS, 'path': f'native/ebook/chm/{i+1:064x}/document.epub',
                                 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()})
        self.root = {'path': 'reader-index/our-protection.json', 'raw': b'our-root'}
        self.store = Memory(self.targets, self.root)
        self.controls = patch.object(run, 'controls')
        self.controls.start()
        self.addCleanup(self.controls.stop)

    def call(self):
        return run.execute(self.store, self.store, Mock(), self.targets, self.root)

    def test_success_deletes_only_exact_targets_then_own_root(self):
        result = self.call()
        self.assertEqual(result['verified_absent'], 4)
        self.assertTrue(result['complete'])
        self.assertTrue(result['operator_root_removed'])
        self.assertFalse(result['original_complete'])
        self.assertEqual(self.store.calls, [('targets', [row['path'] for row in self.targets]),
                                            ('root', self.root['path'])])

    def test_partial_or_lost_target_response_reconciles_without_blind_repeat(self):
        for mode in ('partial', 'full'):
            with self.subTest(mode=mode):
                store = Memory(self.targets, self.root)
                store.mode = mode
                with self.assertRaises(run.Uncertain):
                    run.execute(store, store, Mock(), self.targets, self.root)
                store.mode = None
                result = run.execute(store, store, Mock(), self.targets, self.root)
                self.assertTrue(result['complete'])
                calls = [value for kind, value in store.calls if kind == 'targets']
                self.assertEqual(len(calls), 2 if mode == 'partial' else 1)
                if mode == 'partial':
                    self.assertNotIn(self.targets[0]['path'], calls[1])

    def test_root_unknown_response_is_reconciled_without_repeat(self):
        self.store.mode = 'root'
        with self.assertRaises(run.Uncertain):
            self.call()
        self.store.mode = None
        self.assertTrue(self.call()['complete'])
        self.assertEqual(len([1 for kind, _ in self.store.calls if kind == 'root']), 1)

    def test_changed_or_missing_target_before_intent_stops(self):
        self.store.objects[self.targets[0]['path']] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            self.call()
        self.assertFalse(self.store.calls)
        self.store.objects.pop(self.targets[0]['path'])
        with self.assertRaisesRegex(ValueError, 'absent before durable intent'):
            self.call()
        self.assertFalse(self.store.calls)

    def test_missing_root_without_cleanup_intent_stops(self):
        self.store.objects.pop(self.root['path'])
        with self.assertRaisesRegex(ValueError, 'root missing'):
            self.call()
        self.assertFalse(self.store.calls)

    def test_prior_journal_absent_or_partial_does_not_block_20(self):
        reader = Mock()
        reader.list_files.return_value = set()
        self.assertIsNone(run.prior_cursor(reader, {'synthetic': True}))
        self.assertTrue(self.call()['complete'])

    def test_wrong_intent_or_early_root_cleanup_is_rejected(self):
        head = model.initial(self.store.bound)
        paths = [row['path'] for row in self.targets]
        with self.assertRaises(ValueError):
            model.envelope(self.store.bound, {**head['state'], 'pending': paths[:1]}, sequence=1,
                           parent=common.digest(head), nonce='2' * 32)
        with self.assertRaises(ValueError):
            model.envelope(self.store.bound, {**head['state'], 'root_pending': True}, sequence=1,
                           parent=common.digest(head), nonce='2' * 32)

    def test_completed_cursor_rejects_reappearing_root(self):
        self.assertTrue(self.call()['complete'])
        self.store.objects[self.root['path']] = self.root['raw']
        with self.assertRaisesRegex(ValueError, 'root reappeared'):
            self.call()


class FixedScopeTests(unittest.TestCase):
    def setUp(self):
        from scripts import chm_subset_locked_preflight as preflight
        rows = preflight.chm.preview.load_inventory(preflight.INVENTORY)['targets']
        index = {row['path']: row for row in rows}
        import json
        classification = json.loads(preflight.CLASSIFICATION.read_bytes())
        targets = sorted((index[item['path']] for item in classification['targets']
                          if item['category'] in {'needs-independent-review', 'withheld-oversize'}),
                         key=lambda row: row['path'])
        self.bound = model.fixed_plan(targets, run.prior.PLAN_SHA, run.locked.ROOT_SHA)
        self.root = run.isolated.build(targets, run.locked.IDENTITY)
        with patch.dict('os.environ', {'HF_S3_ACCESS_KEY_ID': 'synthetic', 'HF_S3_SECRET_ACCESS_KEY': 'synthetic'}), \
             patch.object(run.boto3, 'client') as factory:
            self.store = run.Store(Mock(), self.bound, self.root)
        self.client = self.store.client

    def test_fixed_plan_and_initial_are_deterministic(self):
        self.assertEqual(common.digest(self.bound), 'f4603a69598c1636d600a0478fff5b3f6f12aa2835b2ac375acb539edbe2a6cc')
        self.assertEqual(common.digest(model.initial(self.bound)), '796266f96ec981b46554b284ab6def14363482cd174584c679f0c39931e96a24')
        self.assertEqual(len(self.store.allowed), 20)

    def test_delete_adapter_rejects_other_paths_and_malformed_responses(self):
        path = self.bound['targets'][0]['path']
        for paths in ([], [path, path], ['native/other'], [None]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.store.delete_targets(paths)
        self.client.delete_objects.assert_not_called()
        self.client.delete_objects.return_value = {'Deleted': [], 'ResponseMetadata': {'HTTPStatusCode': 200}}
        with self.assertRaises(run.Uncertain):
            self.store.delete_targets([path])
        self.client.delete_objects.assert_called_once()

    def test_adapter_writes_only_bound_canonical_journal(self):
        self.client.put_object.return_value = {'ResponseMetadata': {'HTTPStatusCode': 200}}
        self.assertTrue(self.store.create('plan.json', common.encode(self.bound)))
        with self.assertRaises(ValueError):
            self.store.create('plan.json', b'{}')
        with self.assertRaises(ValueError):
            self.store.create('../other', b'{}')
        with self.assertRaises(ValueError):
            self.store.create('event-00000001.json', b'{}')
        self.client.put_object.assert_called_once()

    def test_root_removal_only_exact_root_and_no_automatic_retry(self):
        self.store.reader.read_bytes.return_value = self.root['raw']
        self.client.delete_object.return_value = {'ResponseMetadata': {'HTTPStatusCode': 204}}
        self.store.delete_root()
        self.client.delete_object.assert_called_once_with(Bucket='reader-assets-v2', Key=self.root['path'])
        self.assertEqual(self.client.meta.method_calls, [])


if __name__ == '__main__':
    unittest.main()
