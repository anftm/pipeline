import copy
import gzip
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from scripts import publish_reader_v3 as publication, reader_route_plan as plan, shared
from tests.test_publish_reader_v3 import Store


ASSETS = publication.ASSETS
PDF = shared.PDF_PAGES_BUCKET
KEY = 'VoiceOfML/Test\0目录/一.pdf'
OTHER = 'VoiceOfML/Test\0目录/二.pdf'
ROOT = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16


def setup_store(*, override=False):
    store = Store()
    shared_image = 'pages/image/png/' + 'c' * 64 + '/document.webp'
    base = {'v': 1, 'f': {
        KEY: {'s': 2, 'm': 'p', 'b': PDF, 'p': ROOT + '/document.pdf',
              'pd': shared_image, 'pdb': ASSETS},
        OTHER: {'s': 2, 'm': 'i', 'b': ASSETS, 'p': shared_image},
        'VoiceOfML/Test\0三.txt': {'s': 2, 'm': 't', 'b': ASSETS,
                                  'p': 'documents/text/txt/' + 'd' * 64 + '/document.txt'},
    }}
    store.objects[ASSETS, plan.SIDECAR] = gzip.compress(publication.encode(base), mtime=0)
    publication.unify(store, apply=True)
    if override:
        pointer, catalog = publication.current(store)
        ref = publication.metadata(PDF, ROOT + '/reading-manifest.json', b'manifest')
        files = {KEY: {'source_sha256': 'a' * 64, 'reading_generation': 'e' * 64, 'resource': ref}}
        publication.commit_catalog(store, {'version': 3, 'kind': 'reader-reading-catalog', 'files': files,
            'base_sidecar': catalog['base_sidecar'], 'parent_generation': pointer['generation']}, pointer, apply=True)
    store.writes.clear()
    return store, base


class RoutePreviewTests(unittest.TestCase):
    def test_exact_preview_keeps_every_index_and_object_unchanged(self):
        store, before = setup_store(override=True)
        objects = copy.deepcopy(store.objects)
        report = plan.preview(store, 'VoiceOfML/Test', '目录/一.pdf')
        self.assertEqual(store.objects, objects)
        self.assertEqual(store.writes, [])
        self.assertEqual(report['routes']['canonical'], before['f'][KEY])
        self.assertEqual(report['routes']['pinned_base'], before['f'][KEY])
        self.assertEqual(report['routes']['expected_projection']['p'], ROOT + '/reading-manifest.json')
        self.assertEqual(report['v3_override']['resource']['path'], ROOT + '/reading-manifest.json')
        self.assertEqual(report['simulated_projection']['before_count'], 3)
        self.assertEqual(report['simulated_projection']['after_count'], 2)
        targets = {(x['bucket'], x['path']): x for x in report['direct_resources']}
        self.assertIn((PDF, ROOT + '/document.pdf'), targets)
        self.assertIn((PDF, ROOT + '/reading-manifest.json'), targets)
        self.assertEqual(targets[ASSETS, before['f'][OTHER]['p']]['other_direct_owners'],
                         {'canonical': [OTHER], 'pinned_base': [OTHER], 'expected_projection': [OTHER]})
        self.assertTrue(any('cannot' in warning for warning in report['warnings']))

    def test_missing_route_is_not_a_delete_authorization(self):
        store, _ = setup_store()
        report = plan.preview(store, 'VoiceOfML/Test', '不存在.epub')
        self.assertEqual(report['direct_resources'], [])
        self.assertFalse(report['simulated_projection']['key_removed'])
        self.assertEqual(report['simulated_projection']['before_count'], report['simulated_projection']['after_count'])
        self.assertTrue(any('cannot' in warning for warning in report['warnings']))

    def test_existing_v3_withdraw_reveals_base_route_and_does_not_delete(self):
        store, base = setup_store(override=True)
        pointer, _ = publication.current(store)
        before_objects = copy.deepcopy(store.objects)
        with patch.dict(os.environ, {'GITHUB_ACTOR': 'test'}):
            publication.withdraw(store, {'source_key': KEY, 'reading_generation': 'e' * 64},
                                 pointer['generation'], apply=True)
        pointer, catalog = publication.current(store)
        projected = publication.project_sidecar(publication.pinned_base(store, catalog, {}), catalog)
        self.assertNotIn(KEY, catalog['files'])
        self.assertEqual(projected['f'][KEY], base['f'][KEY])
        self.assertTrue(before_objects.keys() <= store.objects.keys())

    def test_canonical_and_pinned_divergence_is_visible(self):
        store, base = setup_store()
        base['f'].pop(KEY)
        raw = gzip.compress(publication.encode(base), mtime=0)
        store.objects[ASSETS, plan.SIDECAR] = raw
        report = plan.preview(store, 'VoiceOfML/Test', '目录/一.pdf')
        self.assertTrue(report['canonical_differs_from_pinned'])
        self.assertEqual(report['canonical_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertIsNone(report['routes']['canonical'])
        self.assertIsNotNone(report['routes']['pinned_base'])
        self.assertTrue(report['simulated_projection']['key_removed'])
        self.assertEqual(store.writes, [])

    def test_no_v3_pointer_uses_canonical_base_without_synthesis(self):
        store, _ = setup_store()
        store.objects.pop((ASSETS, publication.POINTER))
        report = plan.preview(store, 'VoiceOfML/Test', '目录/一.pdf')
        self.assertIsNone(report['v3_generation'])
        self.assertEqual(report['routes']['expected_projection'], report['routes']['canonical'])
        self.assertEqual(store.writes, [])

    def test_unsafe_identity_or_unreadable_or_changed_roots_fail_before_mutation(self):
        store, _ = setup_store()
        for repo, path in [('owner', 'a.pdf'), ('owner/repo', '../a.pdf'), ('owner/repo', 'a\0b'),
                           ('owner/repo', 'a//b'), ('owner/repo', '/a'), ('owner/repo', '')]:
            with self.subTest(repo=repo, path=path), self.assertRaises(ValueError):
                plan.preview(store, repo, path)
        initial = store.read_bytes(ASSETS, plan.SIDECAR)
        changed = gzip.compress(publication.encode({'v': 1, 'f': {}}), mtime=0)
        real_read = store.read_bytes
        sidecar_reads = iter((initial, changed))
        def drifting(bucket, path):
            return next(sidecar_reads) if (bucket, path) == (ASSETS, plan.SIDECAR) else real_read(bucket, path)
        with patch.object(store, 'read_bytes', side_effect=drifting):
            with self.assertRaisesRegex(ValueError, 'changed during preview'):
                plan.preview(store, 'VoiceOfML/Test', '目录/一.pdf')
        self.assertEqual(store.writes, [])

    def test_source_scan_uses_exact_known_indexes_and_reports_incomplete(self):
        store, _ = setup_store()
        for bucket, path, required in plan.INDEXES:
            if required or path == 'pages/image/index-v2.json':
                value = {'version': 1, 'kind': plan.index_kind(path), 'files': []}
                if path == 'reader-index/derived_pdf_manifest.json':
                    value['files'] = [{'key': KEY, 'new_path': ROOT + '/document.pdf'}]
                store.objects[bucket, path] = publication.encode(value)
        with patch.object(store, 'list_files', side_effect=AssertionError('unexpected bucket listing'), create=True):
            report = plan.preview(store, 'VoiceOfML/Test', '目录/一.pdf', scan_sources=True)
        sources = report['source_indexes']
        self.assertTrue(sources['complete'], sources['errors'])
        self.assertEqual([s['path'] for s in sources['matched_indexes']], ['reader-index/derived_pdf_manifest.json'])
        self.assertEqual(sources['matched_indexes'][0]['entries'][0]['key'], KEY)
        self.assertEqual(store.writes, [])
        expected = {(bucket, path, required) for bucket, path, required in plan.INDEXES}
        observed = set()
        def observe_build(_fs, bucket, path):
            observed.add((bucket, path))
            try:
                return json.loads(store.read_bytes(bucket, path))
            except FileNotFoundError:
                raise
        with patch.object(plan.source_indexes, 'read_json', side_effect=observe_build):
            plan.source_indexes.build(None, ASSETS, PDF, 'reader-index/derived_pdf_manifest.json',
                                      'unused-render', 'unused-ocr')
        self.assertEqual(observed, {(bucket, path) for bucket, path, _ in expected})
        # A missing mandatory index or an invalid optional index is unknown,
        # never a claim that nothing can restore the route.
        store.objects.pop((ASSETS, 'documents/text/index.json'))
        store.objects[ASSETS, 'documents/web/html/index.json'] = publication.encode({'files': []})
        incomplete = plan.preview(store, 'VoiceOfML/Test', '目录/一.pdf', scan_sources=True)['source_indexes']
        self.assertFalse(incomplete['complete'])
        self.assertEqual({e['error'] for e in incomplete['errors']}, {'missing', 'invalid-index'})

    def test_historical_image_index_and_v2_are_both_scanned(self):
        store, _ = setup_store()
        for bucket, path, required in plan.INDEXES:
            if required or path in {'pages/image/index.json', 'pages/image/index-v2.json'}:
                kind = 'image-page-stream-index' if path == 'pages/image/index.json' else plan.index_kind(path)
                files = [{'key': KEY, 'manifest': 'historical-image'}] if path == 'pages/image/index.json' else []
                store.objects[bucket, path] = publication.encode({'version': 1, 'kind': kind, 'files': files})
        with patch.object(store, 'list_files', side_effect=AssertionError('unexpected bucket listing'), create=True):
            result = plan.inspect_source_indexes(store, KEY)
        self.assertTrue(result['complete'], result['errors'])
        self.assertEqual([row['path'] for row in result['matched_indexes']], ['pages/image/index.json'])
        self.assertEqual(store.writes, [])
        store.objects[ASSETS, 'pages/image/index.json'] = publication.encode(
            {'version': 1, 'kind': 'unknown-image-kind', 'files': []})
        result = plan.inspect_source_indexes(store, KEY)
        self.assertFalse(result['complete'])
        self.assertIn({'bucket': ASSETS, 'path': 'pages/image/index.json', 'error': 'invalid-index'}, result['errors'])

    def test_sidecar_requires_valid_data_and_limited_decompression(self):
        for raw in (b'bad', gzip.compress(b'[]'), gzip.compress(b'{"v": 2, "f": {}}')):
            with self.subTest(raw=raw), self.assertRaises((ValueError, OSError)):
                plan.decode_sidecar(raw)
        with patch.object(plan, 'MAX_EXPANDED', 3), self.assertRaisesRegex(ValueError, 'expands'):
            plan.decode_sidecar(gzip.compress(b'1234'))


if __name__ == '__main__':
    unittest.main()
