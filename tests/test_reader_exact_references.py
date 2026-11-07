import copy
import gzip
import hashlib
import json
import unittest
from types import SimpleNamespace

from scripts import reader_exact_references as exact, reader_page_atomic as atomic, reader_page_audit as audit
from scripts import reader_page_migrate as migration, reader_lifecycle as lifecycle, publish_reader_v3 as publication
from tests.test_reader_page_audit import fixture, KEY, OLD, NEW
from tests.test_reader_page_migrate import MemoryApi
from tests.test_reader_page_atomic import AtomicStore


class ExactReferenceTests(unittest.TestCase):
    def setUp(self):
        index, derived, objects, manifests, sidecar = fixture()
        self.store, self.api = AtomicStore(), MemoryApi(derived, objects)
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode(sidecar), mtime=0)
        for path, raw in manifests.items():
            self.store.objects[audit.BUCKET, path] = raw
        catalog, _ = lifecycle.catalog_generation(sidecar)
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)
        publication.unify(self.store, apply=True)
        pointer = publication.current(self.store)[0]
        plan = atomic.prepare(self.store, self.api,
            expected_index=hashlib.sha256(self.store.read_bytes(audit.BUCKET, audit.INDEX)).hexdigest(),
            expected_sidecar=hashlib.sha256(self.store.read_bytes(audit.ASSETS, audit.SIDECAR)).hexdigest(),
            expected_v3=pointer['generation'])
        atomic.apply(self.store, self.api, plan)
        self.store.writes.clear()
        class InfoApi:
            def get_bucket_paths_info(_self, bucket, paths):
                for item in self.api.get_bucket_paths_info(bucket, paths):
                    yield SimpleNamespace(type='file', path=item.path, size=item.size, xet_hash=item.xet_hash, mtime=None)
        self.info = InfoApi()

    def root(self, report, name):
        return next(item for item in report['roots'] if item['root'] == name)

    def test_old_and_new_explain_different_reference_layers_without_deletion(self):
        before = copy.deepcopy(self.store.objects)
        old = exact.explain(self.store, self.info, audit.BUCKET, OLD)
        new = exact.explain(self.store, self.info, audit.BUCKET, NEW)
        self.assertEqual(self.root(old, 'canonical-sidecar')['direct_owners'], [KEY])
        self.assertEqual(self.root(old, 'projected-sidecar')['direct_owner_count'], 0)
        self.assertEqual(self.root(old, 'legacy-catalog')['generations'][0]['references'], 1)
        self.assertEqual(self.root(old, 'v3-catalog')['redirects'][0]['role'], 'redirect-source')
        self.assertEqual(self.root(new, 'canonical-sidecar')['direct_owner_count'], 0)
        self.assertEqual(self.root(new, 'projected-sidecar')['direct_owners'], [KEY])
        self.assertEqual(self.root(new, 'v3-catalog')['redirects'][0]['role'], 'redirect-target')
        self.assertFalse(old['graph_complete']); self.assertFalse(new['deletion_authorized'])
        self.assertEqual(self.store.objects, before); self.assertEqual(self.store.writes, [])

    def test_unknown_target_is_not_claimed_safe_and_invalid_paths_fail(self):
        report = exact.explain(self.store, self.info, audit.BUCKET, 'objects/missing.bin')
        self.assertIsNone(report['object'])
        self.assertFalse(report['deletion_authorized'])
        self.assertFalse(report['graph_complete'])
        with self.assertRaises(ValueError):
            exact.explain(self.store, self.info, audit.BUCKET, '../bad')
        with self.assertRaisesRegex(ValueError, 'two Reader'):
            exact.explain(self.store, self.info, 'melsm/pdf-archive-v2', 'some/file')

    def test_changed_root_aborts(self):
        read = self.store.read_bytes
        count = 0
        def drift(bucket, path):
            nonlocal count
            if (bucket, path) == (audit.BUCKET, audit.INDEX):
                count += 1
                if count > 1:
                    return b'changed'
            return read(bucket, path)
        self.store.read_bytes = drift
        with self.assertRaisesRegex(RuntimeError, 'roots changed'):
            exact.explain(self.store, self.info, audit.BUCKET, OLD)
        self.assertEqual(self.store.writes, [])


if __name__ == '__main__':
    unittest.main()
