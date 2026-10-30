import copy
import gzip
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import ccrd_ocr_exact_retirement as exact, ccrd_lifecycle as life
from scripts import reader_gc_graph as graph, reader_gc_stream
from scripts import reader_gc_collect as collector


class MemoryStore:
    def __init__(self):
        self.objects = {bucket: {} for bucket in graph.BUCKETS}
        self.object_sizes = {}
        self.writes = []
        self.deletions = []

    def put(self, bucket, path, value):
        raw = value if isinstance(value, bytes) else exact.encode(value)
        self.objects[bucket][path] = raw
        self.object_sizes[bucket, path] = len(raw)
        return raw

    def read_bytes(self, bucket, path):
        try:
            return self.objects[bucket][path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def read_graph_json(self, bucket, path, parser=None):
        raw = self.read_bytes(bucket, path)
        return (parser or reader_gc_stream.graph_json)([raw], path.endswith('.gz'))

    def digest(self, bucket, path):
        return exact.sha(self.read_bytes(bucket, path))

    def list_files(self, bucket, prefixes):
        return {path for path in self.objects[bucket] if path.startswith(prefixes)}

    def list_roots(self, bucket, parents):
        return {path: None for path in self.objects[bucket] if graph.is_root(path, bucket)}

    def put_bytes(self, bucket, path, raw):
        self.writes.append((bucket, path))
        self.put(bucket, path, raw)

    def delete(self, bucket, paths):
        self.deletions.extend((bucket, path) for path in paths)
        for path in paths:
            self.objects[bucket].pop(path, None)


class Fence:
    def __init__(self):self.checks = 0
    def check(self):self.checks += 1


class Response:
    def __init__(self, payload):self.payload = payload
    def raise_for_status(self):pass
    def json(self):return self.payload


class Client:
    def __init__(self, base='ccrd-four-' + 'f' * 64):
        self.status = {'ready': True, 'ocr_release': None,
                       'loaded_sources': ['CCRD', 'CW', 'RMRB', 'PLA'],
                       'bucket': {'generation': base}}
    def get(self, *args, **kwargs):return Response(self.status)


class ExactRetirementTests(unittest.TestCase):
    def make_fixture(self):
        store = MemoryStore()
        bucket = exact.BUCKET
        book, export_gen = 'a' * 64, 'b' * 64
        body = b'previous OCR text'
        text_sha = hashlib.sha256(body).hexdigest()
        text_path = f'text/objects/{text_sha[:2]}/{text_sha}.txt'
        store.put(bucket, text_path, body)
        text_ref = {'bucket': bucket, 'path': text_path, 'bytes': len(body),
                    'sha256': text_sha, 'role': 'runtime'}
        external_path = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/ocr/page-000001.json.gz'
        external_raw = gzip.compress(b'original OCR evidence')
        store.put(life.BUCKETS[1], external_path, external_raw)
        ext = {'bucket': life.BUCKETS[1], 'path': external_path, 'bytes': len(external_raw),
               'sha256': exact.sha(external_raw), 'role': 'provenance'}
        export_path = f'text/exports/{book}/{export_gen}/manifest.json'
        export = {'version': 1, 'kind': 'ccrd-ocr-text-export', 'complete': True,
                  'book_id': book, 'generation': export_gen, 'page_count': 1,
                  'pages': [{}], 'text': text_ref, 'retained_reader_resources': [ext]}
        export_raw = store.put(bucket, export_path, export)
        export_ref = {'bucket': bucket, 'path': export_path, 'bytes': len(export_raw),
                      'sha256': exact.sha(export_raw), 'role': 'provenance'}
        ident = {'version': 1, 'kind': 'ccrd-ocr-search-release', 'base_metadata_sha256': 'c' * 64,
                 'builder_sha256': 'd' * 64, 'tokenizer_version': life.TOKENIZER, 'exports': [export_ref]}
        generation = life.digest(life.canonical(ident))
        database_path = f'indexes/ocr/{generation}/OCR.sqlite3'
        database = b'database'
        store.put(bucket, database_path, database)
        db_ref = {'bucket': bucket, 'path': database_path, 'bytes': len(database),
                  'sha256': exact.sha(database), 'role': 'runtime'}
        release_path = f'releases/ocr-{generation}/manifest.json'
        release = {**ident, 'generation': generation, 'documents': 1, 'database': db_ref}
        release_raw = store.put(bucket, release_path, release)
        release_ref = {'bucket': bucket, 'path': release_path, 'bytes': len(release_raw),
                       'sha256': exact.sha(release_raw), 'role': 'runtime'}
        pointer = {'version': 1, 'kind': 'ccrd-ocr-current', 'generation': generation,
                   'release': release_ref, 'resources': [release_ref], 'parent': None}
        pointer_raw = store.put(bucket, exact.CURRENT, pointer)
        intent_path = f'control/intents/{generation}.json'
        intent = {'version': 1, 'kind': 'ccrd-ocr-intent', 'generation': generation,
                  'status': 'committed', 'expected_parent': None,
                  'resources': [release_ref, db_ref, export_ref, text_ref, ext]}
        intent_raw = store.put(bucket, intent_path, intent)
        ids = {'version': 1, 'kind': 'ccrd-ocr-id-registry',
               'ids': {'e' * 64: {'doc_id': 'OCR:1', 'readable_rel_path': 'x.txt'}},
               'resources': [release_ref], 'status': 'withdrawn'}
        ids_raw = store.put(bucket, exact.IDS, ids)
        consumer = {'version': 1, 'kind': 'ccrd-ocr-consumer', 'status': 'consumer-withdrawn',
                    'resources': [], 'previous_generation': generation, 'pointer_sha256': exact.sha(pointer_raw),
                    'base_generation': 'ccrd-four-' + 'f' * 64}
        consumer_raw = store.put(bucket, exact.CONSUMER, consumer)
        sidecar = gzip.compress(exact.encode({'v': 1, 'f': {}}))
        store.put(exact.ASSETS, 'reader-index/reader_assets.json.gz', sidecar)
        catalog_raw = store.put(exact.ASSETS, collector.CATALOG, {'version': 1, 'kind': 'reader-catalog',
            'current': {'generation': '1'}, 'generations': {'1': {'generation': '1', 'references': []}}})
        reader = {'version': 1, 'kind': 'reader-gc-consumers', 'resources': [release_ref],
                  'receipts': {'hf': {}, 'pages': {}, 'ccrd': {'generation': generation}}}
        reader_raw = store.put(exact.ASSETS, exact.READER_CONSUMERS, reader)
        rows = [{'path': ref['path'], 'bytes': ref['bytes'], 'sha256': ref['sha256']}
                for ref in (text_ref, export_ref, db_ref, release_ref)]
        controls = {exact.CURRENT: exact.sha(pointer_raw), intent_path: exact.sha(intent_raw),
                    exact.IDS: exact.sha(ids_raw), exact.CONSUMER: exact.sha(consumer_raw)}
        new_intent = {**intent, 'status': 'retired', 'resources': [],
                      'retirement_reason': 'explicit-document-reset', 'reset_id': exact.RESET_ID}
        new_ids = {**ids, 'resources': [], 'reset_id': exact.RESET_ID}
        external = {'version': 1, 'kind': 'ccrd-ocr-retention', 'status': 'committed',
                    'resources': [ext], 'generation': generation}
        new_reader = {**reader, 'resources': [], 'receipts': {'hf': {}, 'pages': {},
                       'ccrd': {'consumer_withdrawn': True}}}
        withdrawal = {'version': 1, 'kind': 'ccrd-ocr-reset', 'reset_id': exact.RESET_ID,
                      'status': 'withdrawn', 'applied': True, 'previous_pointer': pointer,
                      'consumer': Client().status,
                      'retired_control': [{'path': intent_path, 'generation': generation,
                                           'sha256': exact.sha(intent_raw), 'bytes': len(intent_raw)}]}
        plan = {'targets': rows, 'control_hashes_before': controls,
                'reader_consumer_sha256_before': exact.sha(reader_raw),
                'catalog_identity': {'digest': exact.sha(catalog_raw)},
                'reader_acknowledgments': {'hf': {}, 'pages': {}},
                'base_generation': consumer['base_generation'],
                'updates': {'intent': new_intent, 'ids': new_ids, 'external': external,
                            'reader_consumers': new_reader, 'withdrawal': withdrawal}}
        return store, plan, generation, intent_path, release_path, ext

    def helpers(self, generation, intent_path, release_path):
        return patch.multiple(exact, GENERATION=generation, INTENT=intent_path,
                              RELEASE=release_path, EXTERNAL='control/ocr-preserved-reader-' + generation + '.json')

    def acknowledgments(self, plan):
        return plan['catalog_identity'], {'receipts': {'hf': {}, 'pages': {}}}

    def test_target_configuration_is_exact_and_bounded(self):
        rows = exact.load_targets()
        self.assertEqual(len(rows), 14)
        self.assertEqual(sum(row['bytes'] for row in rows), 19137371)
        self.assertFalse(any(row['path'].endswith(('.djvu', '.pdf', '.ps')) for row in rows))

    def test_full_simulated_retirement_then_exact_delete_preserves_external_bytes(self):
        store, plan, generation, intent_path, release_path, ext = self.make_fixture()
        with self.helpers(generation, intent_path, release_path), \
                patch.object(exact.collector, 'observe_projections', return_value=self.acknowledgments(plan)):
            report = exact.simulate_retired_graph(store, plan, Fence(), deadline_minutes=1, workers=2)
            self.assertTrue(report['graph_complete'])
            self.assertEqual(report['target_references'], 0)
            before_external = store.read_bytes(ext['bucket'], ext['path'])
            exact.apply_reference_retirement(store, plan, Fence(), Client(), lambda _: None)
            self.assertEqual(store.read_bytes(ext['bucket'], ext['path']), before_external)
            exact.verify_reference_retirement(store, plan)
            result = exact.delete_exact(store, plan, Fence(), Client(), lambda _: None)
            self.assertEqual(result['confirmed_deleted_objects'], 4)
            self.assertEqual(store.read_bytes(ext['bucket'], ext['path']), before_external)
            self.assertEqual({path for b, path in store.deletions if path != exact.CURRENT},
                             {row['path'] for row in plan['targets']})
            self.assertEqual({b for b, _ in store.deletions}, {exact.BUCKET})

    def test_missing_off_bucket_provenance_blocks_before_retirement(self):
        store, plan, generation, intent_path, release_path, ext = self.make_fixture()
        store.objects[ext['bucket']].pop(ext['path'])
        with self.helpers(generation, intent_path, release_path):
            with self.assertRaisesRegex(ValueError, 'reference graph is incomplete'):
                exact.simulate_retired_graph(store, plan, Fence(), deadline_minutes=1, workers=2)
        self.assertEqual(store.deletions, [])

    def test_an_independent_reader_pin_blocks_simulation(self):
        store, plan, generation, intent_path, release_path, _ = self.make_fixture()
        for selected in (release_path, next(row['path'] for row in plan['targets'] if row['path'].endswith('.txt'))):
            with self.subTest(path=selected):
                selected_ref = next(row for row in plan['targets'] if row['path'] == selected)
                store.put(exact.ASSETS, 'reader-index/independent-pin.json', {'resources': [{
                    'bucket': exact.BUCKET, 'path': selected, 'bytes': selected_ref['bytes'],
                    'sha256': selected_ref['sha256'], 'role': 'runtime'}]})
                with self.helpers(generation, intent_path, release_path):
                    with self.assertRaisesRegex(ValueError, 'still retains'):
                        exact.simulate_retired_graph(store, plan, Fence(), deadline_minutes=1, workers=2)
                self.assertEqual(store.deletions, [])

    def test_nested_text_resource_keeps_the_ocr_target(self):
        store, plan, generation, intent_path, release_path, _ = self.make_fixture()
        selected = next(row for row in plan['targets'] if row['path'].endswith('.txt'))
        store.put(exact.ASSETS, 'reader-index/nested-resource.json', {'files': {'book': {
            'text': {'bucket': exact.BUCKET, 'path': selected['path'],
                     'bytes': selected['bytes'], 'sha256': selected['sha256'], 'role': 'runtime'}}}})
        with self.helpers(generation, intent_path, release_path):
            with self.assertRaisesRegex(ValueError, 'still retains'):
                exact.simulate_retired_graph(store, plan, Fence(), deadline_minutes=1, workers=2)
        self.assertEqual(store.deletions, [])

    def test_incomplete_graph_blocks_before_control_changes(self):
        store, plan, generation, intent_path, release_path, _ = self.make_fixture()
        store.put(exact.ASSETS, 'reader-index/unknown.json', {'unexpected': 42})
        before = copy.deepcopy(store.objects)
        with self.helpers(generation, intent_path, release_path):
            with self.assertRaisesRegex(ValueError, 'reference graph is incomplete'):
                exact.simulate_retired_graph(store, plan, Fence(), deadline_minutes=1, workers=2)
        self.assertEqual(store.objects, before)
        self.assertEqual(store.deletions, [])

    def test_target_byte_change_stops_before_retirement(self):
        store, plan, generation, intent_path, release_path, _ = self.make_fixture()
        target = plan['targets'][0]['path']
        store.put(exact.BUCKET, target, b'changed')
        with self.helpers(generation, intent_path, release_path):
            with self.assertRaisesRegex(ValueError, 'target bytes differ'):
                exact.apply_reference_retirement(store, plan, Fence(), Client(), lambda _: None)
        self.assertEqual(store.deletions, [])

    def test_partial_reference_retirement_can_resume_and_preserves_source(self):
        store, plan, generation, intent_path, release_path, ext = self.make_fixture()
        def stop(phase):
            if phase == 'ocr-intent-and-id-reference-retired':raise RuntimeError('interrupt')
        with self.helpers(generation, intent_path, release_path), \
                patch.object(exact.collector, 'observe_projections', return_value=self.acknowledgments(plan)):
            with self.assertRaisesRegex(RuntimeError, 'interrupt'):
                exact.apply_reference_retirement(store, plan, Fence(), Client(), stop)
            self.assertIn(exact.CURRENT, store.objects[exact.BUCKET])
            exact.apply_reference_retirement(store, plan, Fence(), Client(), lambda _: None)
            exact.verify_reference_retirement(store, plan)
            self.assertIn(ext['path'], store.objects[ext['bucket']])

    def test_pointer_retirement_resume_is_idempotent(self):
        store, plan, generation, intent_path, release_path, _ = self.make_fixture()
        with self.helpers(generation, intent_path, release_path), \
                patch.object(exact.collector, 'observe_projections', return_value=self.acknowledgments(plan)):
            exact.apply_reference_retirement(store, plan, Fence(), Client(), lambda _: None)
            exact.apply_reference_retirement(store, plan, Fence(), Client(), lambda _: None)
            self.assertEqual(store.deletions.count((exact.BUCKET, exact.CURRENT)), 1)

    def test_delete_rejects_unexpected_new_managed_object(self):
        store, plan, generation, intent_path, release_path, _ = self.make_fixture()
        with self.helpers(generation, intent_path, release_path), \
                patch.object(exact.collector, 'observe_projections', return_value=self.acknowledgments(plan)):
            exact.apply_reference_retirement(store, plan, Fence(), Client(), lambda _: None)
            store.put(exact.BUCKET, 'staging/ocr/unexpected', b'new')
            with self.assertRaisesRegex(ValueError, 'unexpected OCR managed objects'):
                exact.delete_exact(store, plan, Fence(), Client(), lambda _: None)
        self.assertEqual([path for _, path in store.deletions], [exact.CURRENT])

    def test_final_graph_guard_never_treats_snapshot_as_unreferenced(self):
        data = {'graph_complete': False, 'blockers': ['unfinished']}
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            exact.eligible_after_graph(data, set(), {('bucket', 'a')}, {('other', 'p')})
        data = {'graph_complete': True, 'blockers': []}
        with self.assertRaisesRegex(ValueError, 'still retains'):
            exact.eligible_after_graph(data, {('bucket', 'a'), ('other', 'p')},
                                       {('bucket', 'a')}, {('other', 'p')})
        with self.assertRaisesRegex(ValueError, 'provenance'):
            exact.eligible_after_graph(data, set(), {('bucket', 'a')}, {('other', 'p')})
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            exact.eligible_after_graph({'graph_complete': True, 'blockers': [], 'ambiguous_paths': ['x']},
                                       {('other', 'p')}, {('bucket', 'a')}, {('other', 'p')})


if __name__ == '__main__':
    unittest.main()
