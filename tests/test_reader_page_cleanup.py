import unittest

from scripts import reader_gc_graph as graph, reader_page_cleanup as cleanup


class PageCleanupTests(unittest.TestCase):
    def report(self):
        return {'version': 1, 'mode': 'report-only', 'graph_complete': True,
                'buckets': {bucket: {'candidates': []} for bucket in graph.BUCKETS},
                'blockers': [], 'scanned_at': '2026-01-01'}

    def test_complete_graph_is_partitioned_but_never_authorizes_delete(self):
        value = self.report()
        value['buckets'][cleanup.BUCKET]['candidates'] = ['derived/g/old/document.pdf',
            'objects/aa/x/v/document.pdf', 'reader-index/not-a-target.json']
        sizes = {(cleanup.BUCKET, 'derived/g/old/document.pdf'): 123}
        report = cleanup.report_from_graph(value, sizes)
        self.assertTrue(report['graph_complete'])
        self.assertFalse(report['deletion_authorized'])
        self.assertEqual(report['groups']['derived/']['files'], 1)
        self.assertEqual(report['groups']['derived/']['known_bytes'], 123)
        self.assertTrue(report['groups']['derived/']['sizes_complete'])
        self.assertFalse(report['groups']['objects/']['sizes_complete'])

    def test_incomplete_or_missing_bucket_never_exposes_candidates(self):
        for mutate in ('incomplete', 'missing'):
            value = self.report()
            value['buckets'][cleanup.BUCKET]['candidates'] = ['objects/aa/x/v/document.pdf']
            if mutate == 'incomplete': value['graph_complete'] = False
            else: value['buckets'].pop(graph.ccrd_lifecycle.BUCKET)
            report = cleanup.report_from_graph(value)
            self.assertFalse(report['graph_complete'])
            self.assertEqual(report['groups']['objects/']['paths'], [])
            self.assertFalse(report['deletion_authorized'])

    def test_malformed_candidates_fail(self):
        value = self.report()
        value['buckets'][cleanup.BUCKET]['candidates'] = ['ok', None]
        with self.assertRaises(ValueError): cleanup.report_from_graph(value)


if __name__ == '__main__':
    unittest.main()
