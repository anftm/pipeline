import unittest
from unittest.mock import Mock, patch

from scripts import chm_subset_publish_protection as protect


class PublishProtectionTests(unittest.TestCase):
    def setUp(self):
        self.root = {'path': protect.isolated.root_path(protect.locked.IDENTITY['transaction_id']),
                     'raw': b'test root', 'sha256': protect.locked.ROOT_SHA, 'objects': 20}
        self.store = Mock()
        self.writer = Mock()
        self.fence = Mock()
        self.report = {'remote_mutations': 0, 'deletion_authorized': False,
                       'partition': {'selected': 352, 'withheld': 20}}
        self.patches = [patch.object(protect, 'candidate', return_value=self.root),
                        patch.object(protect.preflight, 'check', return_value=self.report),
                        patch.object(protect.preflight, '_identity')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def call(self):
        return protect.protect(self.store, self.writer, self.fence, Mock(), [], b'{}')

    def test_new_root_is_created_once_and_read_back(self):
        self.store.read_bytes.side_effect = [FileNotFoundError(), self.root['raw']]
        report = self.call()
        self.writer.create.assert_called_once_with(self.root)
        self.assertEqual(report['status'], 'created')
        self.assertEqual(report['protected_objects'], 20)
        self.assertEqual(report['deleted_objects'], 0)
        self.assertFalse(report['original_complete'])

    def test_exact_existing_root_is_accepted_without_write(self):
        self.store.read_bytes.return_value = self.root['raw']
        self.assertEqual(self.call()['status'], 'already-exact')
        self.writer.create.assert_not_called()

    def test_foreign_root_and_uncertain_readback_stop(self):
        self.store.read_bytes.return_value = b'foreign'
        with self.assertRaisesRegex(ValueError, 'different bytes'):
            self.call()
        self.writer.create.assert_not_called()
        self.store.read_bytes.side_effect = [FileNotFoundError(), b'foreign']
        with self.assertRaises(protect.RootWriteUnknown):
            self.call()
        self.writer.create.assert_called_once()

    def test_preflight_failure_and_unknown_put_stop_without_retry(self):
        with patch.object(protect.preflight, 'check', side_effect=RuntimeError('changed')):
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                self.call()
        self.writer.create.assert_not_called()
        self.store.read_bytes.side_effect = FileNotFoundError()
        self.writer.create.side_effect = protect.RootWriteUnknown('lost')
        with self.assertRaises(protect.RootWriteUnknown):
            self.call()
        self.writer.create.assert_called_once()

    def test_real_fixed_root_matches_local_plan(self):
        rows = protect.preflight.chm.preview.load_inventory(protect.preflight.INVENTORY)['targets']
        raw = protect.preflight.CLASSIFICATION.read_bytes()
        with patch.object(protect, 'candidate', wraps=self.patches[0].temp_original):
            root = protect.candidate(rows, raw)
        self.assertEqual(root['sha256'], protect.locked.ROOT_SHA)
        self.assertEqual(root['objects'], 20)
        self.assertTrue(protect.graph.is_root(root['path'], protect.preflight.chm.ASSETS))

    def test_writer_only_accepts_fixed_root_and_never_retries(self):
        writer = protect.SingleRootWriter.__new__(protect.SingleRootWriter)
        writer.client = Mock()
        rows = protect.preflight.chm.preview.load_inventory(protect.preflight.INVENTORY)['targets']
        with patch.object(protect, 'candidate', wraps=self.patches[0].temp_original):
            root = protect.candidate(rows, protect.preflight.CLASSIFICATION.read_bytes())
        with self.assertRaises(ValueError):
            writer.create({**root, 'path': root['path'] + '.foreign'})
        writer.client.put_object.assert_not_called()
        writer.client.put_object.side_effect = OSError('response lost')
        with self.assertRaises(protect.RootWriteUnknown):
            writer.create(root)
        writer.client.put_object.assert_called_once()
        call = writer.client.put_object.call_args.kwargs
        self.assertEqual(call['Bucket'], 'reader-assets-v2')
        self.assertEqual(call['Key'], root['path'])
        self.assertEqual(call['Body'], root['raw'])
        self.assertEqual(call['IfNoneMatch'], '*')


if __name__ == '__main__':
    unittest.main()
