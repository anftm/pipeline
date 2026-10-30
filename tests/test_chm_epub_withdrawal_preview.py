import copy
from datetime import datetime, timezone
import gzip
import hashlib
import json
import unittest

from scripts import chm_epub_withdrawal_preview as preview
from scripts import reader_lifecycle
from scripts import reader_v3_retirement
from scripts import reader_gc_graph, reader_gc_stream
from scripts import chm_epub_publication_preflight as preflight
from scripts import publish_reader_v3 as publication


class ChmWithdrawalPreviewTests(unittest.TestCase):
    def setUp(self):
        self.source = 'a' * 64
        self.root = 'chapters/ebook/chm/' + self.source + '/' + 'b' * 16
        self.manifest = self.root + '/chapter-manifest.json'
        self.fallback = 'native/ebook/chm/' + self.source + '/document.epub'
        self.key = 'VoiceOfML/Test\0book.chm'
        self.index = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': [{
            'key': self.key, 'source_sha256': self.source, 'bucket': preview.ASSETS,
            'mode': 'chapter-stream', 'root': self.root, 'manifest': self.manifest,
            'search_index': self.root + '/epub-search-index.json.gz', 'chapter_count': 1,
            'fallback': self.fallback, 'object': self.fallback}], 'failures': []}
        self.sidecar = {'v': 1, 'f': {
            self.key: {'s': 2, 'm': 'e', 'p': self.manifest, 'b': preview.ASSETS,
                       'c': self.manifest, 'cb': preview.ASSETS, 'f': self.fallback},
            'other.pdf': {'s': 2, 'm': 'p', 'p': 'derived/other/document.pdf', 'b': 'vomebook/pdf-pages-v2'}}}
        self.catalog, self.generation = reader_lifecycle.catalog_generation(self.sidecar)
        self.rows = [{'path': self.fallback}]

    def test_preview_removes_only_compatibility_fields_and_preserves_old_catalog(self):
        index, sidecar = copy.deepcopy(self.index), copy.deepcopy(self.sidecar)
        result = preview.preview(index, sidecar, self.catalog, self.rows)
        self.assertEqual(result['entries_changed'], 1)
        self.assertEqual(result['unique_fallbacks_removed'], 1)
        self.assertEqual(result['remote_mutations'], 0)
        self.assertNotIn('fallback', result['index']['files'][0])
        self.assertNotIn('object', result['index']['files'][0])
        self.assertEqual(result['index']['files'][0]['manifest'], self.manifest)
        self.assertEqual(result['sidecar']['f'][self.key]['c'], self.manifest)
        self.assertNotIn('f', result['sidecar']['f'][self.key])
        self.assertEqual(result['sidecar']['f']['other.pdf'], sidecar['f']['other.pdf'])
        self.assertIn(self.generation, result['catalog']['generations'])
        self.assertEqual(index, self.index)
        self.assertEqual(sidecar, self.sidecar)

    def test_unreviewed_fallback_never_becomes_a_target(self):
        self.index['files'][0]['fallback'] = 'native/ebook/chm/' + 'c' * 64 + '/document.epub'
        with self.assertRaisesRegex(ValueError, 'outside'):
            preview.preview(self.index, self.sidecar, self.catalog, self.rows)

    def test_different_chapter_route_blocks_preview(self):
        self.sidecar['f'][self.key]['c'] = 'chapters/ebook/chm/other/chapter-manifest.json'
        with self.assertRaisesRegex(ValueError, 'differs'):
            preview.preview(self.index, self.sidecar, self.catalog, self.rows)

    def test_other_sidecar_use_is_a_blocker_not_silently_removed(self):
        self.sidecar['f']['other.pdf']['f'] = self.fallback
        with self.assertRaisesRegex(ValueError, 'another sidecar mapping'):
            preview.preview(self.index, self.sidecar, self.catalog, self.rows)

    def test_duplicate_or_non_chm_source_blocks_preview(self):
        self.index['files'].append(copy.deepcopy(self.index['files'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicated'):
            preview.preview(self.index, self.sidecar, self.catalog, self.rows)

    def test_acknowledged_old_v3_rollback_is_not_retired_early(self):
        import json
        from unittest.mock import patch
        current = 'a' * 64
        old = 'b' * 64
        pointer = {'generation': current, 'catalog': {'path': f'reader-index/v3/generations/{current}/catalog.json'}}
        def promotion(generation, created):
            return json.dumps({'kind': 'reader-reading-promotion', 'generation': generation,
                               'created_at': created, 'retention_days': 30,
                               'acks': {'hf': True, 'pages': True}}).encode()
        objects = {
            f'reader-index/v3/promotions/{current}.json': promotion(current, '2026-10-30T00:00:00+00:00'),
            f'reader-index/v3/promotions/{old}.json': promotion(old, '2026-10-25T17:54:07+00:00'),
        }
        class Store:
            def read_bytes(self, bucket, path):
                if path not in objects:
                    raise FileNotFoundError(path)
                return objects[path]
            def list_files(self, bucket, prefixes):
                return {path for path in objects if path.startswith(prefixes)}
        with patch.object(reader_v3_retirement.publication, 'current',
                          return_value=(pointer, {'files': {}})):
            report = reader_v3_retirement.run(Store(), now=datetime(2026, 10, 30, tzinfo=timezone.utc))
        self.assertEqual(report['retired'], [])

    def test_shards_remove_exact_fallback_without_changing_chapter_mapping(self):
        old = copy.deepcopy(self.index)
        path = 'chapters/ebook/chm/index-00.json'
        result = preview.preview_shards({path: old}, self.index, self.rows)
        self.assertEqual(result['entries_changed'], 1)
        self.assertNotIn('fallback', result['indexes'][path]['files'][0])
        self.assertNotIn('object', result['indexes'][path]['files'][0])
        self.assertEqual(result['indexes'][path]['files'][0]['manifest'], self.manifest)
        self.assertEqual(old, self.index)

    def test_shards_cannot_retire_a_fallback_outside_the_exact_inventory(self):
        old = copy.deepcopy(self.index)
        old['files'][0]['fallback'] = 'native/ebook/chm/' + 'c' * 64 + '/document.epub'
        with self.assertRaisesRegex(ValueError, 'outside'):
            preview.preview_shards({'chapters/ebook/chm/index-00.json': old}, self.index, self.rows)

    def test_simulation_reports_live_consumer_pin_without_mutation(self):
        class Store:
            def __init__(self):
                self.objects = {bucket: {} for bucket in reader_gc_graph.BUCKETS}
                self.object_sizes = {}
            def put(self, bucket, path, value):
                raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                if path.endswith('.gz') and not raw.startswith(b'\x1f\x8b'):
                    raw = gzip.compress(raw)
                self.objects[bucket][path] = raw
            def read_bytes(self, bucket, path):
                try:
                    return self.objects[bucket][path]
                except KeyError:
                    raise FileNotFoundError(path) from None
            def read_graph_json(self, bucket, path, parser=None):
                raw = self.read_bytes(bucket, path)
                return (parser or reader_gc_stream.graph_json)([raw], path.endswith('.gz'))
            def digest(self, bucket, path):
                return hashlib.sha256(self.read_bytes(bucket, path)).hexdigest()
            def list_files(self, bucket, prefixes):
                return {path for path in self.objects[bucket] if path.startswith(prefixes)}
            def list_roots(self, bucket, parents):
                return {path: None for path in self.objects[bucket] if reader_gc_graph.is_root(path, bucket)}
        class Fence:
            def check(self):pass
        store = Store()
        index_path = 'chapters/ebook/chm/index.json'
        sidecar_path = 'reader-index/reader_assets.json.gz'
        catalog_path = 'reader-index/reader_catalog.json'
        store.put(preview.ASSETS, index_path, self.index)
        store.put(preview.ASSETS, sidecar_path, self.sidecar)
        store.put(preview.ASSETS, catalog_path, self.catalog)
        store.put(preview.ASSETS, self.fallback, b'legacy')
        store.put(preview.ASSETS, self.manifest, {'kind': 'epub-chapters', 'chapters': []})
        store.put(preview.ASSETS, self.root + '/epub-search-index.json.gz', b'{}')
        store.put('vomebook/pdf-pages-v2', 'derived/other/document.pdf', b'pdf')
        store.put(preview.ASSETS, 'reader-index/reader_gc_consumers.json', {
            'resources': [{'bucket': preview.ASSETS, 'path': self.fallback}]})
        before = copy.deepcopy(store.objects)
        proposed = preview.preview(self.index, self.sidecar, self.catalog, self.rows)
        report = preview.simulate_retired_indexes(store, proposed, {'indexes': {}}, self.rows, Fence(),
                                                  workers=2, deadline_minutes=1)
        self.assertTrue(report['graph_complete'])
        self.assertEqual(report['targets_still_referenced'], 1)
        self.assertFalse(report['deletion_authorized'])
        self.assertEqual(before, store.objects)

    def test_preflight_is_fail_closed_when_an_index_shard_disappears(self):
        class Store:
            def list_roots(self, bucket, parents):
                return {'chapters/ebook/chm/index.json': None}
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            preflight.read_index_roots(Store())

    def test_stale_shards_cannot_silently_replace_new_chapter_routes(self):
        canonical = copy.deepcopy(self.index)
        shard = copy.deepcopy(self.index)
        from scripts.reader_stream_index import shard_for
        number = shard_for(self.key, 8)
        shard['files'][0]['manifest'] = shard['files'][0]['manifest'].replace('b' * 16, 'c' * 16)
        result = preflight.stale_shard_merge_changes(
            canonical, {f'chapters/ebook/chm/index-0{number}.json': shard})
        self.assertEqual(result, {'changed_entries': 1, 'changed_chapter_routes': 1})
        repaired = preflight.synchronize_stale_shards(
            canonical, {f'chapters/ebook/chm/index-0{number}.json': shard})
        self.assertEqual(repaired['replaced_entries'], 1)
        self.assertEqual(repaired['indexes'][f'chapters/ebook/chm/index-0{number}.json']['files'][0],
                         canonical['files'][0])

    def test_missing_processing_roots_are_reported_before_publication(self):
        class Store:
            def existing_files(self, bucket, paths):
                return set()
        index = copy.deepcopy(self.index)
        index['files'][0]['processing_roots'] = ['reader-index/processing/old.json']
        self.assertEqual(preflight.processing_root_status(Store(), index),
                         {'referenced': 1, 'present': 0, 'missing': 1})

    def test_historical_base_sanitization_preserves_every_other_route(self):
        old = copy.deepcopy(self.sidecar)
        clean, removed = preflight.sanitize_historical_v3_base(old, {self.fallback})
        self.assertEqual(removed, {self.fallback})
        self.assertNotIn('f', clean['f'][self.key])
        self.assertEqual(clean['f']['other.pdf'], old['f']['other.pdf'])
        self.assertEqual(clean['f'][self.key]['c'], old['f'][self.key]['c'])
        self.assertIn('f', old['f'][self.key])

    def test_historical_replacement_is_immutable_and_keeps_original_routes(self):
        raw = gzip.compress(publication.encode(self.sidecar), mtime=0)
        digest = hashlib.sha256(raw).hexdigest()
        snapshot = publication.metadata(preview.ASSETS,
            f'reader-index/snapshots/{digest}/reader_assets.json.gz', raw)
        old = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'base_sidecar': snapshot,
               'document_reset': 'document-rebuild-20261025', 'parent_generation': 'd' * 64}
        old['generation'] = publication.text.digest(old)
        class Store:
            def read_bytes(self, bucket, path):
                self.asserted = (bucket, path)
                return raw
        replacement = preflight.historical_v3_replacement(Store(), old, {self.fallback})
        self.assertNotEqual(replacement['generation'], old['generation'])
        self.assertEqual(replacement['catalog']['document_reset'], old['document_reset'])
        self.assertEqual(replacement['catalog']['parent_generation'], old['parent_generation'])
        self.assertEqual(replacement['catalog']['rollback_target'], old['generation'])
        projected = json.loads(gzip.decompress(replacement['snapshot_bytes']))
        self.assertEqual(projected['f']['other.pdf'], self.sidecar['f']['other.pdf'])
        self.assertNotIn('f', projected['f'][self.key])
        self.assertEqual(projected['f'][self.key]['p'], self.manifest)
        self.assertEqual(replacement['remote_mutations'], 0)

    def test_current_replacement_preserves_current_overrides_and_non_chm(self):
        raw = gzip.compress(publication.encode(self.sidecar), mtime=0)
        digest = hashlib.sha256(raw).hexdigest()
        snapshot = publication.metadata(preview.ASSETS,
            f'reader-index/snapshots/{digest}/reader_assets.json.gz', raw)
        old = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'base_sidecar': snapshot,
               'parent_generation': 'd' * 64}
        old['generation'] = publication.text.digest(old)
        class Store:
            def read_bytes(self, bucket, path):return raw
        sanitized, _ = preflight.sanitize_historical_v3_base(self.sidecar, {self.fallback})
        new = preflight.current_v3_replacement(Store(), old, sanitized, {self.fallback},
                                               parent_generation=old['generation'])
        self.assertEqual(new['catalog']['parent_generation'], old['generation'])
        self.assertEqual(new['catalog']['files'], old['files'])
        alias = preflight.rollback_alias_from_current(new)
        self.assertEqual(alias['catalog']['rollback_target'], old['generation'])
        self.assertEqual(alias['catalog']['base_sidecar'], new['catalog']['base_sidecar'])
        self.assertEqual(alias['catalog']['files'], old['files'])
        corrupted = copy.deepcopy(sanitized)
        corrupted['f']['other.pdf']['p'] = 'derived/foreign/document.pdf'
        with self.assertRaisesRegex(ValueError, 'more than CHM'):
            preflight.current_v3_replacement(Store(), old, corrupted, {self.fallback},
                                             parent_generation=old['generation'])

    def test_history_journal_requires_ack_and_preserves_existing_input_records(self):
        raw = gzip.compress(publication.encode(self.sidecar), mtime=0)
        digest = hashlib.sha256(raw).hexdigest()
        snapshot = publication.metadata(preview.ASSETS,
            f'reader-index/snapshots/{digest}/reader_assets.json.gz', raw)
        old = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'base_sidecar': snapshot,
               'parent_generation': 'd' * 64}
        old['generation'] = publication.text.digest(old)
        class Store:
            def read_bytes(self, bucket, path):return raw
        replacement = preflight.historical_v3_replacement(Store(), old, {self.fallback})
        pointer = {'generation': 'e' * 64, 'catalog': {'bucket': preview.ASSETS,
                   'path': 'reader-index/v3/generations/' + 'e' * 64 + '/catalog.json'}}
        state = {'version': 1, 'kind': 'reader-v3-retirement', 'retired_paths': [], 'resources': [],
                 'retired_input_roots': {'preserve': {'unchanged': True}}}
        with self.assertRaisesRegex(ValueError, 'both consumers'):
            preflight.retirement_journal_for_replaced_history(state, {old['generation']: replacement},
                                                               pointer, {'hf': True, 'pages': False})
        journal = preflight.retirement_journal_for_replaced_history(state, {old['generation']: replacement},
                                                                    pointer, {'hf': True, 'pages': True})
        self.assertEqual(journal['retired_input_roots'], state['retired_input_roots'])
        self.assertIn('reader-index/v3/generations/' + old['generation'] + '/catalog.json', journal['retired_paths'])
        self.assertEqual(len(journal['resources']), 2)
        self.assertEqual(state['retired_paths'], [])

    def test_chapter_closure_includes_historical_route_without_epub(self):
        old = copy.deepcopy(self.sidecar)
        old['f'][self.key]['p'] = old['f'][self.key]['c'] = (
            'chapters/ebook/chm/' + self.source + '/' + 'c' * 16 + '/chapter-manifest.json')
        result = preflight.preserved_chapter_closure([self.sidecar, old])
        self.assertEqual(result, {self.manifest, old['f'][self.key]['c']})
        old['f'][self.key]['c'] = self.fallback
        with self.assertRaisesRegex(ValueError, 'qualified chapter manifest'):
            preflight.preserved_chapter_closure([old])

    def test_retired_chm_rollback_cannot_restore_epub_route(self):
        generation = 'd' * 64
        path = 'reader-index/v3/generations/' + generation + '/catalog.json'
        state = {'version': 1, 'kind': 'reader-v3-retirement', 'retired_paths': [path]}
        class Store:
            def read_bytes(self, bucket, name):return json.dumps(state).encode()
        target = {'bucket': preview.ASSETS, 'path': path, 'bytes': 1, 'sha256': 'a' * 64}
        with self.assertRaisesRegex(ValueError, 'sanitized replacement'):
            preflight.resolve_retired_rollback(Store(), target)
        other = {**target, 'path': path.replace(generation, 'e' * 64)}
        self.assertEqual(preflight.resolve_retired_rollback(Store(), other), other)

    def test_missing_chapter_or_checksum_blocks_epub_retirement(self):
        chapter = b'<html>book</html>'
        search = b'search'
        manifest = {'version': 1, 'kind': 'epub-chapters', 'toc': [],
                    'chapters': [{'index': 1, 'title': 'One', 'path': 'chapters/chapter-0001.xhtml',
                                  'bytes': len(chapter), 'sha256': hashlib.sha256(chapter).hexdigest()}],
                    'search_index': {'path': 'epub-search-index.json.gz', 'bytes': len(search),
                                     'sha256': hashlib.sha256(search).hexdigest()}}
        objects = {self.manifest: json.dumps(manifest).encode(), self.root + '/chapters/chapter-0001.xhtml': chapter,
                   self.root + '/epub-search-index.json.gz': search}
        class Store:
            def read_bytes(self, bucket, path):return objects[path]
            def list_files(self, bucket, prefixes):return {p for p in objects if p.startswith(prefixes)}
        report = preflight.verify_preserved_chapter(Store(), self.manifest, hash_bodies=True)
        self.assertTrue(report['chapter_bodies_hashed'])
        self.assertEqual(report['required'], 3)
        objects[self.root + '/chapters/chapter-0001.xhtml'] = b'wrong'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            preflight.verify_preserved_chapter(Store(), self.manifest, hash_bodies=True)
        objects.pop(self.root + '/epub-search-index.json.gz')
        with self.assertRaisesRegex(ValueError, 'missing'):
            preflight.verify_preserved_chapter(Store(), self.manifest)

    def test_historical_base_sanitization_blocks_foreign_reference(self):
        old = copy.deepcopy(self.sidecar)
        old['f']['other.pdf']['f'] = self.fallback
        with self.assertRaisesRegex(ValueError, 'unverified chapter route'):
            preflight.sanitize_historical_v3_base(old, {self.fallback})

    def test_missing_processing_refs_need_both_unchanged_reader_receipts(self):
        old = copy.deepcopy(self.index)
        path = 'reader-index/processing/office-chapters-' + 'd' * 32 + '.json'
        old['files'][0]['processing_roots'] = [path]
        with self.assertRaisesRegex(ValueError, 'both Reader surfaces'):
            preflight.retire_collected_chm_processing_refs(old, {path}, self.sidecar,
                expected_generation='v3-generation', acknowledged_generations={'hf': 'v3-generation'})
        result = preflight.retire_collected_chm_processing_refs(old, {path}, self.sidecar,
            expected_generation='v3-generation',
            acknowledged_generations={'hf': 'v3-generation', 'pages': 'v3-generation'})
        self.assertEqual(result['removed_processing_refs'], 1)
        self.assertNotIn('processing_roots', result['index']['files'][0])
        self.assertEqual(result['index']['files'][0]['manifest'], self.manifest)
        self.assertIn('processing_roots', old['files'][0])

    def test_index_synchronization_removes_epub_without_changing_routes(self):
        canonical = copy.deepcopy(self.index)
        path = 'reader-index/processing/office-chapters-' + 'd' * 32 + '.json'
        canonical['files'][0]['processing_roots'] = [path]
        indexes = {preflight.INDEX: canonical}
        for number in range(8):
            indexes[f'{preflight.INDEX_PREFIX}index-0{number}.json'] = copy.deepcopy(canonical)
        result = preflight.synchronize_indexes(indexes, self.sidecar, self.catalog, self.rows, {path},
                   current_generation='g', receipts={'hf': 'g', 'pages': 'g'})
        self.assertEqual(len(result['indexes']), 9)
        self.assertEqual(result['missing_processing_refs_removed'], 1)
        for value in result['indexes'].values():
            self.assertEqual(value['files'][0]['manifest'], self.manifest)
            self.assertNotIn('fallback', value['files'][0])
        self.assertEqual(result['sidecar']['f']['other.pdf'], self.sidecar['f']['other.pdf'])
        self.assertEqual(result['remote_mutations'], 0)

    def test_base_history_is_not_retired_before_both_acknowledge(self):
        result = preview.preview(self.index, self.sidecar, self.catalog, self.rows)
        with self.assertRaisesRegex(ValueError, 'not acknowledged'):
            preflight.retire_old_base_catalog_generation(result['catalog'], self.generation,
                                                         result['generation'], acknowledged=False)
        cleaned = preflight.retire_old_base_catalog_generation(result['catalog'], self.generation,
                                                               result['generation'], acknowledged=True)
        self.assertNotIn(self.generation, cleaned['generations'])
        self.assertEqual(cleaned['retired_generations'][self.generation]['replacement_acks'],
                         {'hf': True, 'pages': True})
        self.assertIn(self.generation, result['catalog']['generations'])
        self.assertEqual(cleaned['generations'][result['generation']],
                         result['catalog']['generations'][result['generation']])


if __name__ == '__main__':
    unittest.main()
