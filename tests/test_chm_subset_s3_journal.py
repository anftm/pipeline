import unittest
from unittest.mock import Mock, patch

from scripts import chm_subset_journal as model, chm_subset_s3_journal as adapter


class StoreTests(unittest.TestCase):
    def setUp(self):
        from scripts import chm_subset_locked_preflight as preflight
        self.preflight = preflight
        self.rows = preflight.chm.preview.load_inventory(preflight.INVENTORY)['targets']
        self.reader = Mock()
        self.reader.list_files.return_value = set()
        def bind(rows, batches, selected, withheld, identity, old_progress_raw):
            plan = model._plan(selected, withheld, identity)
            return {'plan': plan, 'plan_sha256': model.digest(plan), 'partition': {
                'old_receipts': 600, 'selected': len(selected), 'withheld': len(withheld)}}
        with patch.object(preflight, '_identity', return_value=(b'synthetic', b'', b'', {'batches': []})), \
             patch.object(model, 'bind_plan', side_effect=bind):
            self.plan, _ = adapter.fixed_plan(self.reader, self.rows, preflight.CLASSIFICATION.read_bytes())
        self.store = adapter.FixedS3.__new__(adapter.FixedS3)
        self.store.reader, self.store.plan = self.reader, self.plan
        self.store.allowed = {row['path'] for row in self.plan['selected']}
        self.store.client = Mock()

    def test_fixed_plan_and_initial_event_match_saved_identity(self):
        self.assertEqual(model.digest(self.plan), adapter.PLAN_SHA)
        self.assertEqual(len(self.store.allowed), 352)
        self.assertFalse(self.store.allowed & {row['path'] for row in self.plan['withheld']})
        self.assertTrue(adapter.PREFIX.startswith('reader-index/gc/'))

    def test_journal_put_is_limited_to_exact_keys_and_plan(self):
        self.store.client.put_object.return_value = {'ResponseMetadata': {'HTTPStatusCode': 200}}
        self.assertTrue(self.store.create('plan.json', model.encode(self.plan)))
        self.assertEqual(self.store.client.put_object.call_args.kwargs['Key'], adapter.PREFIX + 'plan.json')
        with self.assertRaises(ValueError):
            self.store.create('plan.json', b'{}')
        with self.assertRaises(ValueError):
            self.store.create('../other.json', b'{}')
        with self.assertRaises(ValueError):
            self.store.create('event-00000001.json', b'{}')
        self.store.client.put_object.assert_called_once()

    def test_uncertain_journal_response_is_not_retried(self):
        self.store.client.put_object.side_effect = OSError('network')
        with self.assertRaises(adapter.chain.OutcomeUnknown):
            self.store.create('plan.json', model.encode(self.plan))
        self.store.client.put_object.assert_called_once()

    def test_delete_only_exact_selected_keys(self):
        path = self.plan['selected'][0]['path']
        kept = self.plan['withheld'][0]['path']
        for paths in ([kept], [path, path], [], ['other'], [None]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.store.delete_once(paths)
        self.store.client.delete_objects.assert_not_called()
        self.store.client.delete_objects.return_value = {'Deleted': [{'Key': path}],
                                                          'ResponseMetadata': {'HTTPStatusCode': 200}}
        self.store.delete_once([path])
        self.store.client.delete_objects.assert_called_once_with(
            Bucket='reader-assets-v2', Delete={'Objects': [{'Key': path}], 'Quiet': False})

    def test_partial_unknown_or_malformed_deletion_never_retries(self):
        path = self.plan['selected'][0]['path']
        for response in ({'Deleted': [], 'ResponseMetadata': {'HTTPStatusCode': 200}},
                         {'Deleted': [None], 'ResponseMetadata': {'HTTPStatusCode': 200}},
                         {'Deleted': [{'Key': path}], 'Errors': [{'Code': 'bad'}],
                          'ResponseMetadata': {'HTTPStatusCode': 200}}):
            self.store.client.delete_objects.reset_mock()
            self.store.client.delete_objects.return_value = response
            with self.assertRaises(adapter.DeleteUnknown):
                self.store.delete_once([path])
            self.store.client.delete_objects.assert_called_once()
        self.store.client.delete_objects.reset_mock()
        self.store.client.delete_objects.side_effect = OSError('response lost')
        with self.assertRaises(adapter.DeleteUnknown):
            self.store.delete_once([path])
        self.store.client.delete_objects.assert_called_once()

    def test_real_client_constructor_disables_retries_and_freezes_scope(self):
        reader = Mock()
        with patch.dict('os.environ', {'HF_S3_ACCESS_KEY_ID': 'synthetic',
                                     'HF_S3_SECRET_ACCESS_KEY': 'synthetic'}, clear=True), \
             patch.object(adapter.boto3, 'client') as factory:
            instance = adapter.FixedS3(reader, self.plan)
        self.assertEqual(factory.call_args.kwargs['config'].retries['total_max_attempts'], 1)
        self.assertEqual(factory.call_args.kwargs['endpoint_url'], 'https://s3.hf.co/vomebook')
        saved = instance.plan['selected'][0]['path']
        self.plan['selected'][0]['path'] = self.plan['withheld'][0]['path']
        self.assertEqual(instance.plan['selected'][0]['path'], saved)
        self.assertIn(saved, instance.allowed)


if __name__ == '__main__':
    unittest.main()
