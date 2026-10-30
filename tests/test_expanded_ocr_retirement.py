import copy
import gzip
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from scripts import expanded_ocr_retirement as expanded
from scripts import ccrd_ocr_exact_retirement as exact
from tests.test_ccrd_ocr_exact_retirement import MemoryStore, Fence


class ExpandedRetirementSimulationTests(unittest.TestCase):
    def fixture(self):
        store = MemoryStore()
        source = 'a' * 64
        root = f'objects/aa/{source}/' + 'b' * 16
        retained = f'derived/migration-20261007/{"c" * 32}/derived-manifest.json'
        text = root + '/text/page-000001.json.gz'
        old = b'ocr text'
        store.put(expanded.PAGES, text, old)
        provenance = exact.encode({'kind': 'pdf-derived-source', 'resources': []})
        store.put(expanded.PAGES, retained, provenance)
        current = {'version': 1, 'kind': 'ccrd-ocr-current', 'generation': 'd' * 64,
                   'resources': [], 'release': {}}
        store.put(expanded.CCRD, exact.CURRENT, current)
        intent = {'version': 1, 'kind': 'ccrd-ocr-intent', 'generation': 'd' * 64,
                  'status': 'committed', 'resources': [{'bucket': expanded.PAGES, 'path': text,
                      'sha256': exact.sha(old), 'bytes': len(old), 'role': 'runtime'}]}
        store.put(expanded.CCRD, exact.INTENT, intent)
        ids = {'version': 1, 'kind': 'ccrd-ocr-id-registry', 'ids': {}, 'resources': []}
        store.put(expanded.CCRD, exact.IDS, ids)
        consumer = {'version': 1, 'kind': 'ccrd-ocr-consumer', 'resources': []}
        store.put(expanded.CCRD, exact.CONSUMER, consumer)
        store.put(expanded.ASSETS, 'reader-index/reader_assets.json.gz', gzip.compress(exact.encode({'v': 1, 'f': {}})))
        old_consumer = {'version': 1, 'kind': 'reader-gc-consumers', 'resources': []}
        store.put(expanded.ASSETS, exact.READER_CONSUMERS, old_consumer)
        rows = [{'bucket': expanded.PAGES, 'path': text, 'bytes': len(old), 'sha256': exact.sha(old)}]
        external = {'version': 1, 'kind': 'ccrd-ocr-retention', 'resources': [{
            'bucket': expanded.PAGES, 'path': retained, 'bytes': len(provenance),
            'sha256': exact.sha(provenance), 'role': 'provenance'}]}
        plan = {'expanded_targets': rows,
                'reader_consumer_sha256_before': store.digest(expanded.ASSETS, exact.READER_CONSUMERS),
                'control_hashes_before': {path: store.digest(expanded.CCRD, path) for path in
                                          (exact.CURRENT, exact.INTENT, exact.IDS, exact.CONSUMER)},
                'updates': {'external': external, 'intent': {**intent, 'status': 'retired', 'resources': []},
                            'ids': ids, 'reader_consumers': old_consumer}}
        return store, plan, text

    def test_simulation_reports_an_independent_retained_reference(self):
        store, plan, path = self.fixture()
        store.put(expanded.ASSETS, 'reader-index/independent-pin.json', {'resources': [{
            'bucket': expanded.PAGES, 'path': path}]})
        before = copy.deepcopy(store.objects)
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            result = expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1)
        self.assertTrue(result['graph_complete'])
        self.assertEqual(result['retained_target_count'], 1)
        self.assertFalse(result['deletion_authorized'])
        self.assertEqual(store.objects, before)
        self.assertEqual(store.deletions, [])

    def test_unreadable_or_unknown_root_blocks_simulation(self):
        store, plan, path = self.fixture()
        store.put(expanded.ASSETS, 'reader-index/unknown.json', {'unexpected': True})
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1)
        self.assertEqual(store.deletions, [])

    def test_source_document_path_never_matches_external_target_pattern(self):
        self.assertFalse(expanded.ALLOWED[expanded.PAGES].fullmatch(
            'derived/migration-20261007/' + 'c' * 32 + '/document.pdf'))
        self.assertFalse(expanded.ALLOWED[expanded.PAGES].fullmatch(
            'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/document.djvu'))
        self.assertFalse(expanded.ALLOWED[expanded.ASSETS].fullmatch(
            'documents/pdf/ps/' + 'a' * 64 + '/document.pdf'))

    def test_external_sibling_bundle_pin_blocks_target_retirement(self):
        bucket = expanded.PAGES
        parent = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/ocr/'
        target = parent + 'page-000001.json.gz'
        unrelated_sibling = parent + 'page-000002.json.gz'
        origin = (expanded.ASSETS, 'reader-index/live-consumer.json')
        graph = SimpleNamespace(direct={origin: {(bucket, unrelated_sibling)}}, trees=set(), tree_origins={})
        hits = expanded.external_target_origins(graph, [{'bucket': bucket, 'path': target}])
        self.assertEqual(hits[origin], {(bucket, target)})

    def test_target_internal_origin_does_not_imply_external_ownership(self):
        bucket = expanded.PAGES
        origin = (bucket, 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/text-layer-manifest.json')
        target = (bucket, 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/text/page-000001.json.gz')
        graph = SimpleNamespace(direct={origin: {target}}, trees=set(), tree_origins={})
        rows = [{'bucket': b, 'path': p} for b, p in (origin, target)]
        self.assertEqual(expanded.external_target_origins(graph, rows), {})

    def test_unknown_tree_owner_blocks_conservatively(self):
        bucket = expanded.PAGES
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        target = root + '/text/page-000001.json.gz'
        graph = SimpleNamespace(direct={}, trees={(bucket, root)}, tree_origins={})
        hits = expanded.external_target_origins(graph, [{'bucket': bucket, 'path': target}])
        self.assertEqual(hits[(bucket, root + '/<unknown-tree-owner>')], {(bucket, target)})

    def test_internal_tree_owner_is_excluded_but_independent_owner_is_not(self):
        bucket = expanded.PAGES
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        target = root + '/text/page-000001.json.gz'
        origin = (bucket, root + '/text-layer-manifest.json')
        rows = [{'bucket': b, 'path': p} for b, p in (origin, (bucket, target))]
        graph = SimpleNamespace(direct={}, trees={(bucket, root)},
                                tree_origins={(bucket, root): {origin}})
        self.assertEqual(expanded.external_target_origins(graph, rows), {})
        external = (expanded.ASSETS, 'reader-index/audit.json')
        graph.tree_origins[bucket, root].add(external)
        self.assertEqual(expanded.external_target_origins(graph, rows)[external],
                         {origin, (bucket, target)})

    def test_same_tree_records_every_owner_even_after_first_expansion(self):
        store = MemoryStore()
        bucket = expanded.PAGES
        root = 'objects/aa/' + 'a' * 64 + '/' + 'b' * 16
        one, two = root + '/ocr/page-000001.json.gz', root + '/ocr/page-000002.json.gz'
        store.put(bucket, one, b'one')
        store.put(bucket, two, b'two')
        graph = expanded.OriginReferenceGraph(store)
        for domain in expanded.graph.BUCKETS:
            graph.set_inventory(domain, set(store.objects[domain]))
        first = (bucket, root + '/text-review-manifest.json')
        second = (expanded.ASSETS, 'reader-index/live-consumer.json')
        graph.origin = first
        graph.mark(bucket, one)
        graph.origin = second
        graph.mark(bucket, two)
        self.assertEqual(graph.tree_origins[bucket, root + '/ocr'], {first, second})
        self.assertIn(second, expanded.external_target_origins(
            graph, [{'bucket': bucket, 'path': one}, {'bucket': first[0], 'path': first[1]}]))

    def test_missing_tree_tracking_never_silently_grants_authority(self):
        with self.assertRaisesRegex(ValueError, 'origins were not recorded'):
            expanded.external_target_origins(SimpleNamespace(direct={}, trees=set()), [])

    def test_attestation_rejects_unread_or_missing_bucket_roots(self):
        fake = SimpleNamespace(files={b: set() for b in expanded.graph.BUCKETS},
                               root_hashes={}, retired_roots=set())
        fake.files[expanded.ASSETS].add('reader-index/a.json')
        report = {'graph_complete': True, 'blockers': [], 'ambiguous_paths': []}
        with self.assertRaisesRegex(ValueError, 'did not read every active root'):
            expanded.attest_root_fingerprints(report, fake)
        fake.root_hashes[(expanded.ASSETS, 'reader-index/a.json')] = 'a' * 64
        proof = expanded.attest_root_fingerprints(report, fake)
        self.assertEqual(len(proof['root_members']), 1)
        self.assertEqual(proof['root_sha256'][0]['sha256'], 'a' * 64)
        fake.files.pop(expanded.CCRD)
        with self.assertRaisesRegex(ValueError, 'complete four-bucket'):
            expanded.attest_root_fingerprints(report, fake)

    def test_target_internal_manifest_cycle_is_not_a_live_root(self):
        store, plan, path = self.fixture()
        root = path.rsplit('/text/', 1)[0]
        manifest = root + '/text-layer-manifest.json'
        doc = {'version': 1, 'kind': 'pdf-text-layer-index', 'resources': [
            {'bucket': expanded.PAGES, 'path': path}]}
        store.put(expanded.PAGES, manifest, doc)
        raw = store.read_bytes(expanded.PAGES, manifest)
        plan['expanded_targets'].append({'bucket': expanded.PAGES, 'path': manifest,
                                        'bytes': len(raw), 'sha256': exact.sha(raw)})
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            report = expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1,
                                       omit_internal_target_manifests=True)
        self.assertTrue(report['target_exclusivity_proven'])
        self.assertEqual(report['retained_target_count'], 0)
        self.assertEqual(store.deletions, [])

    def test_external_root_pin_still_blocks_without_reading_target_manifest(self):
        store, plan, path = self.fixture()
        root = path.rsplit('/text/', 1)[0]
        manifest = root + '/text-layer-manifest.json'
        store.put(expanded.PAGES, manifest, {'kind': 'pdf-text-layer-index', 'resources': []})
        raw = store.read_bytes(expanded.PAGES, manifest)
        plan['expanded_targets'].append({'bucket': expanded.PAGES, 'path': manifest,
                                        'bytes': len(raw), 'sha256': exact.sha(raw)})
        store.put(expanded.ASSETS, 'reader-index/independent-pin.json', {'resources': [{
            'bucket': expanded.PAGES, 'path': manifest}]})
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            report = expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1,
                                       omit_internal_target_manifests=True)
        self.assertFalse(report['target_exclusivity_proven'])
        self.assertGreater(report['retained_target_count'], 0)
        self.assertTrue(report['non_target_origins'])
        self.assertEqual(store.deletions, [])

    def test_retained_directory_root_still_blocks_internal_scc_retirement(self):
        store, plan, path = self.fixture()
        directory = path.rsplit('/text/', 1)[0]
        store.put(expanded.ASSETS, 'reader-index/independent-tree.json', {'resources': [{
            'bucket': expanded.PAGES, 'root': directory}]})
        with patch.object(exact, 'inspect_roots', return_value=(plan['control_hashes_before'], {})):
            report = expanded.simulate(store, plan, Fence(), workers=2, deadline_minutes=1,
                                       omit_internal_target_manifests=True)
        self.assertFalse(report['target_exclusivity_proven'])
        self.assertEqual(report['retained_target_count'], 1)
        self.assertTrue(report['non_target_origins'])
        self.assertEqual(store.deletions, [])


if __name__ == '__main__':
    unittest.main()
