import hashlib
import unittest
from unittest.mock import Mock
from scripts import reader_page_consumer_store as consumer
from scripts import publish_reader_v3 as publication
from tests.test_reader_page_history_store import response

class ConsumerStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        s3 = Mock()
        s3._location.return_value = ('vomebook', 'reader-assets-v2')
        s3._client.return_value = self.client
        self.store = consumer.ConditionalConsumerStore(Mock(), s3)

    def test_only_consumer_root_can_be_read_for_update_or_changed(self):
        self.client.get_object.return_value = response(b'old')
        self.assertEqual(self.store.read_for_update(publication.ASSETS, consumer.CONSUMERS), (b'old', '"actual"'))
        for root in ['reader-index/v3/retirement.json','reader-index/v3/current.json']:
            with self.assertRaises(ValueError):
                self.store.put_if_match(publication.ASSETS, root, b'new', '"actual"')
        self.client.put_object.assert_not_called()
        self.store.put_if_match(publication.ASSETS, consumer.CONSUMERS, b'new', '"actual"')
        self.assertEqual(self.client.put_object.call_args.kwargs['IfMatch'], '"actual"')

    def test_audit_has_content_address_and_is_not_a_gc_root(self):
        from scripts import reader_gc_graph as graph
        raw = publication.encode({'kind':'reader-consumer-refresh-intent'})
        path = consumer.INTENTS + hashlib.sha256(raw).hexdigest() + '.json'
        self.assertEqual(self.store._immutable_type(publication.ASSETS, path, raw), 'application/json')
        self.assertFalse(graph.is_root(path))
        for bad in ['reader-index/v3/history-replacements/derived-pdf-intents/'+hashlib.sha256(raw).hexdigest()+'.json', consumer.INTENTS+'0'*64+'.json']:
            with self.assertRaises(ValueError):
                self.store._immutable_type(publication.ASSETS, bad, raw)

    def test_catalog_observation_preserves_streaming_without_whole_reads(self):
        from scripts import reader_gc_collect as collector
        primary = self.store.primary
        primary.read_bytes.side_effect = AssertionError('whole catalog read forbidden')
        primary.digest.return_value = 'a' * 64
        primary.read_graph_json.return_value = ({'current': 'generation'}, 'b' * 64)
        self.assertEqual(collector.snapshot_digest(self.store, publication.ASSETS, collector.CATALOG), 'a' * 64)
        self.assertEqual(collector.catalog_identity(self.store), {'current': 'generation', 'digest': 'b' * 64})
        primary.digest.assert_called_once_with(publication.ASSETS, collector.CATALOG)
        primary.read_graph_json.assert_called_once()
        primary.read_bytes.assert_not_called()

    def test_unknown_consumer_cas_is_not_retried(self):
        self.client.put_object.side_effect = TimeoutError('response lost')
        with self.assertRaises(TimeoutError):
            self.store.put_if_match(publication.ASSETS, consumer.CONSUMERS, b'new', '"actual"')
        self.client.put_object.assert_called_once()
