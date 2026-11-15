import unittest
from unittest.mock import patch
from scripts.reader_gc_store import CollectionStore, CcrdCollectionStore
from scripts.reader_bucket_store import S3BucketStore

class CollectionDomainTests(unittest.TestCase):
    def test_reader_adapter_rejects_other_domains_before_delete(self):
        store = object.__new__(CollectionStore)
        with patch.object(S3BucketStore, 'delete') as delete:
            for bucket in ['melsm/pdf-archive-v2', 'vomebook/ccrd-index', 'vomebook/bha-data', 'vomebook/search-data-v1']:
                with self.subTest(bucket=bucket), self.assertRaises(ValueError):
                    store.delete(bucket, ['objects/example'])
            delete.assert_not_called()
            for bucket in ['vomebook/reader-assets-v2', 'vomebook/pdf-pages-v2']:
                store.delete(bucket, ['objects/example'])
            self.assertEqual(delete.call_count, 2)

    def test_ccrd_adapter_remains_an_independent_domain(self):
        store = object.__new__(CcrdCollectionStore)
        with patch.object(S3BucketStore, 'delete') as delete:
            with self.assertRaises(ValueError):
                store.delete('vomebook/reader-assets-v2', ['objects/example'])
            delete.assert_not_called()
            store.delete('vomebook/ccrd-index', ['generations/example'])
            delete.assert_called_once()
