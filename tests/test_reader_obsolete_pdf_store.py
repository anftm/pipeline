import unittest
from unittest.mock import Mock
from scripts.reader_obsolete_pdf_store import ObsoleteReferenceStore
from scripts import reader_obsolete_pdf_apply as operation
from tests.test_reader_obsolete_pdf_apply import ObsoletePDFApplyTests

class StoreTests(unittest.TestCase):
    def setUp(self):
        fixture = ObsoletePDFApplyTests()
        fixture.setUp()
        self.plan = fixture.plan
        self.client = Mock()
        s3 = Mock()
        s3._location.side_effect = lambda bucket: tuple(bucket.split('/'))
        s3._client.return_value = self.client
        self.store = ObsoleteReferenceStore(Mock(), self.plan['mutations'], s3)

    def test_write_cannot_escape_reviewed_after_or_root(self):
        row = self.plan['mutations'][0]
        for bucket,path,raw in [(row['bucket'],row['path'],b'wrong'),('vomebook/bha-data',row['path'],row['after']),('vomebook/ccrd-index','control/current.json',row['after'])]:
            with self.assertRaises(ValueError):self.store.put_if_match(bucket,path,raw,'"a"')
        self.client.put_object.assert_not_called()
        self.store.put_if_match(row['bucket'],row['path'],row['after'],'"a"')
        self.assertEqual(self.client.put_object.call_args.kwargs['IfMatch'],'"a"')

    def test_write_transport_error_never_retried(self):
        row=self.plan['mutations'][0]
        self.client.put_object.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):self.store.put_if_match(row['bucket'],row['path'],row['after'],'"a"')
        self.client.put_object.assert_called_once()

    def test_audit_namespace_does_not_become_gc_root(self):
        from scripts.reader_gc_graph import is_root
        self.assertFalse(is_root(operation.INTENTS+'a'*64+'.json'))
