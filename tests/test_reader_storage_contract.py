"""Offline storage safety contracts, independent of local documentation."""
import hashlib
import unittest
from unittest.mock import Mock

from scripts import reader_gc_collect as collector, reader_gc_graph as graph, reader_v3_automation
from tests.test_reader_gc_collect import Store


class StorageContractTests(unittest.TestCase):
    def test_original_documents_remain_pinned_dataset_sources(self):
        raw = b'synthetic original'
        for ext in ('pdf', 'djvu'):
            with self.subTest(extension=ext):
                key = f'VoiceOfML/Test\0folder/book.{ext}'
                entry = {'status': 'ready', 'repo': 'VoiceOfML/Test',
                         'path': f'folder/book.{ext}', 'source_revision': 'a' * 40,
                         'source_kind': 'upstream', 'source_format': ext,
                         'source_sha256': hashlib.sha256(raw).hexdigest(),
                         'source_bytes': len(raw), 'page_count': 1}
                spec, count = reader_v3_automation.primary_spec(key, entry)
                self.assertEqual(count, 1)
                self.assertEqual(spec['primary']['kind'], 'dataset-source')
                self.assertEqual(spec['primary']['revision'], 'a' * 40)
                self.assertNotIn('bucket', spec['primary'])

    def test_historical_chm_fallback_is_retained_in_the_reference_graph(self):
        store = Store()
        bucket = graph.READER_ASSETS_BUCKET
        source = 'a' * 64
        root = f'chapters/ebook/chm/{source}/' + 'b' * 16
        manifest = root + '/chapter-manifest.json'
        fallback = f'native/ebook/chm/{source}/document.epub'
        store.put(bucket, 'chapters/ebook/chm/index.json', {'files': [
            {'manifest': manifest, 'fallback': fallback, 'bucket': bucket}]})
        store.put(bucket, manifest, {'kind': 'epub-chapters', 'chapters': [
            {'path': 'chapters/chapter-0001.xhtml'}]})
        store.put(bucket, root + '/chapters/chapter-0001.xhtml', b'chapter')
        store.put(bucket, fallback, b'preserved historical fallback')
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        self.assertIn((bucket, fallback), reference.references)
        self.assertNotIn(fallback, report['buckets'][bucket]['candidates'])

    def test_incomplete_or_three_bucket_graph_cannot_authorize_collection(self):
        store = Store()
        path = 'objects/orphan/document.pdf'
        store.put(graph.PDF_PAGES_BUCKET, path, b'not evidence for deletion')
        reference = graph.ReferenceGraph(store)
        report = reference.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        store.delete = Mock(side_effect=AssertionError('must not delete'))
        fence = Mock()
        with self.assertRaisesRegex(RuntimeError, 'graph is incomplete'):
            collector.collect(store, fence, reference, {**report, 'graph_complete': False}, {},
                              apply=True, reader_only=True)
        del reference.files[graph.ccrd_lifecycle.BUCKET]
        with self.assertRaisesRegex(RuntimeError, 'four-bucket'):
            collector.collect(store, fence, reference, report, {}, apply=True, reader_only=True)
        store.delete.assert_not_called()
        fence.check.assert_not_called()
        self.assertIn(path, store.objects[graph.PDF_PAGES_BUCKET])
