import base64
import hashlib
import json
import unittest
from unittest.mock import Mock

from scripts import reader_processing_history as history, reader_gc_graph as graph
from scripts import reader_gc_collect as collector, reader_lifecycle as lifecycle
from tests.test_reader_gc_collect import Store

ASSETS, PDF, INPUT = graph.READER_BUCKETS


class ProcessingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.catalog, self.generation = lifecycle.catalog_generation({'v': 1, 'f': {}}, now='2026-10-25T12:00:00+00:00')
        self.store.put(ASSETS, collector.CATALOG, self.catalog)
        self.consumers = {'version': 1, 'kind': 'reader-gc-consumers', 'receipts': {
            surface: {'projection_verified': True, 'base_sidecar_sha256':
                self.catalog['generations'][self.generation]['sidecar_sha256']}
            for surface in ('hf', 'pages')}}

    def record(self, number, bucket, root, **changes):
        value = lifecycle.processing_record([{'bucket': bucket, 'root': root}], 'upload', {'run_id': '123'})
        value.update(created_at='2026-10-13T10:00:00+00:00', updated_at='2026-10-13T11:00:00+00:00',
                     released_at='2026-10-13T11:00:00+00:00', generation='a' * 32, status='released')
        value.update(changes)
        path = f'reader-index/processing/upload-{number}.json'
        self.store.put(ASSETS, path, value)
        return path

    def test_pruned_generation_releases_every_format_only_after_current_graph(self):
        paths = []
        for number, (bucket, family, file) in enumerate([
            (ASSETS, 'chapters/ebook/epub', 'chapter.html'),
            (ASSETS, 'pages/image/tiff', 'page.png'),
            (ASSETS, 'documents/office/docx', 'document.html'),
            (ASSETS, 'documents/web/html', 'document.html'),
            (ASSETS, 'media/audio/mp3', 'audio.mp3'),
            (ASSETS, 'media/video/mp4', 'video.mp4'),
            (PDF, 'objects/aa', 'page.jxl'),
            (INPUT, 'objects/aa', 'input.png')]):
            root = family + '/' + 'a' * 64
            path = self.record(number, bucket, root)
            content = root + '/' + file
            self.store.put(bucket, content, b'retired')
            paths.append((bucket, content))
        current = paths[0]
        self.store.put(ASSETS, 'reader-index/pinned-book.json', {'resources': [{'bucket': current[0], 'path': current[1]}]})
        evidence = history.reconcile(self.store, self.catalog, self.consumers)
        for (bucket, path), proof in evidence.items():
            self.assertEqual(base64.b64decode(proof['original_record_base64']), self.store.read_bytes(bucket, path))
        reference = graph.ReferenceGraph(self.store, processing_supersessions=evidence)
        scanned = reference.build()
        self.assertTrue(scanned['graph_complete'], scanned['blockers'])
        result = collector.collect(self.store, Mock(), reference, scanned, {}, apply=True, reader_only=True)
        self.assertEqual(result['confirmed_deleted_objects'], 8 + 7)
        self.assertEqual(len(result['processing_retirements']), 8)
        self.assertIn(current[1], self.store.objects[current[0]])
        for bucket, path in paths[1:]:
            self.assertNotIn(path, self.store.objects[bucket])
        self.assertTrue(all(row['supersession']['original_generation'] == 'a' * 32
                            for row in result['processing_retirements']))

    def test_missing_consumer_or_newer_release_is_not_reconciled(self):
        path = self.record(1, ASSETS, 'chapters/ebook/epub/' + 'b' * 64)
        del self.consumers['receipts']['pages']
        with self.assertRaisesRegex(ValueError, 'both current'):
            history.reconcile(self.store, self.catalog, self.consumers)
        self.consumers['receipts']['pages'] = dict(self.consumers['receipts']['hf'])
        self.record(1, ASSETS, 'chapters/ebook/epub/' + 'b' * 64,
                    released_at='2026-10-25T13:00:00+00:00', updated_at='2026-10-25T13:00:00+00:00')
        with self.assertRaisesRegex(ValueError, 'newer'):
            history.reconcile(self.store, self.catalog, self.consumers)

    def test_record_change_invalidates_saved_proof_and_keeps_objects(self):
        root = 'chapters/ebook/epub/' + 'b' * 64
        path = self.record(1, ASSETS, root)
        self.store.put(ASSETS, root + '/chapter.html', b'held')
        proof = history.reconcile(self.store, self.catalog, self.consumers)
        self.record(1, ASSETS, root, operation='different-owner')
        report = graph.ReferenceGraph(self.store, processing_supersessions=proof).build()
        self.assertFalse(report['graph_complete'])
        self.assertEqual(report['buckets'][ASSETS]['candidates'], [])

    def test_open_and_failed_records_never_get_a_supersession(self):
        for status in ['processing', 'uploaded', 'failed']:
            self.record(status, ASSETS, 'chapters/ebook/epub/' + 'b' * 64, status=status)
        self.assertEqual(history.reconcile(self.store, self.catalog, self.consumers), {})
