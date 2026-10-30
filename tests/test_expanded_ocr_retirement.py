import copy
import gzip
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
