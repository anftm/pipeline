import gzip
import unittest

from scripts import expanded_ocr_retirement as expanded
from tests.test_ccrd_ocr_exact_retirement import MemoryStore


class RetiredInputExternalSafetyTests(unittest.TestCase):
    def test_retired_input_still_blocks_when_a_live_reader_points_at_it(self):
        store = MemoryStore()
        store.put(expanded.ASSETS, 'reader-index/reader_assets.json.gz',
                  gzip.compress(expanded.encode({'v': 1, 'f': {}})))
        source = 'a' * 64
        root = 'objects/aa/' + source + '/' + 'b' * 16 + '/ocr-input'
        path = root + '/page-000001.png'
        store.put(expanded.INPUT, path, b'image')
        store.put(expanded.ASSETS, 'reader-index/v3/retirement.json', {
            'version': 1, 'kind': 'reader-v3-retirement', 'resources': [], 'retired_paths': [],
            'replacement_acks': {'hf': True, 'pages': True}, 'retired_input_roots': {root: {
                'bucket': expanded.INPUT, 'root': root, 'source_sha256': source, 'pages': 1}}})
        store.put(expanded.ASSETS, 'reader-index/external-user.json', {'resources': [{
            'bucket': expanded.INPUT, 'path': path, 'role': 'runtime'}]})
        graph = expanded.OriginReferenceGraph(store, workers=2)
        report = graph.build()
        self.assertTrue(report['graph_complete'], report['blockers'])
        self.assertTrue(expanded.external_target_origins(graph, [{'bucket': expanded.INPUT, 'path': path}]))
        self.assertIn(((expanded.ASSETS, 'reader-index/external-user.json'),
                       (expanded.INPUT, path)), graph.omitted_retired_input_pins)
