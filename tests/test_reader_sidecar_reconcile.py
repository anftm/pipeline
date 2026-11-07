import base64
import copy
import gzip
import hashlib
import json
import unittest
from unittest.mock import patch

from scripts import reader_sidecar_reconcile as sidecar_reconcile
from scripts import reader_page_atomic as atomic, reader_page_audit as audit
from scripts import reader_page_migrate as migration, reader_page_reconcile as reconcile
from scripts import reader_lifecycle as lifecycle, publish_reader_v3 as publication
from tests.test_reader_page_audit import fixture, KEY, OLD, NEW
from tests.test_reader_page_atomic import AtomicStore
from tests.test_reader_page_migrate import MemoryApi
from tests.test_reader_route_control import Consumers


class SidecarReconciliationTests(unittest.TestCase):
    def setUp(self):
        index, derived, objects, manifests, sidecar = fixture()
        self.store, self.api = AtomicStore(), MemoryApi(derived, objects)
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode(sidecar), mtime=0)
        for path, raw in manifests.items():
            self.store.objects[audit.BUCKET, path] = raw
        catalog, _ = lifecycle.catalog_generation(sidecar)
        # An extra protection not present in the sidecar must survive.
        current = catalog['current']['generation']
        catalog['generations'][current]['references'].append({'bucket': audit.BUCKET, 'path': 'objects/extra/required.bin'})
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)
        publication.unify(self.store, apply=True)
        pointer = publication.current(self.store)[0]
        plan = atomic.prepare(self.store, self.api,
            expected_index=hashlib.sha256(self.store.read_bytes(audit.BUCKET, audit.INDEX)).hexdigest(),
            expected_sidecar=hashlib.sha256(self.store.read_bytes(audit.ASSETS, audit.SIDECAR)).hexdigest(),
            expected_v3=pointer['generation'])
        atomic.apply(self.store, self.api, plan)
        pointer = publication.current(self.store)[0]
        plan = reconcile.prepare(self.store, self.api,
            expected_index=hashlib.sha256(self.store.read_bytes(audit.BUCKET, audit.INDEX)).hexdigest(),
            expected_generation=pointer['generation'])
        with patch.object(publication, 'verify_generation', return_value={'complete': True}):
            reconcile.apply(self.store, self.api, plan)
        self.pointer = publication.current(self.store)[0]
        self.store.stat = lambda bucket, path: len(self.store.read_bytes(bucket, path))
        self.verifier = patch.object(publication, 'verify_generation', return_value={'complete': True})
        self.verifier.start(); self.addCleanup(self.verifier.stop)
        self.fence = patch.object(migration, 'require_writer_window')
        self.check = self.fence.start(); self.addCleanup(self.fence.stop)
        self.store.writes.clear()

    def prepare(self):
        return sidecar_reconcile.prepare(self.store, self.api,
            expected_index=hashlib.sha256(self.store.read_bytes(audit.BUCKET, audit.INDEX)).hexdigest(),
            expected_sidecar=hashlib.sha256(self.store.read_bytes(audit.ASSETS, audit.SIDECAR)).hexdigest(),
            expected_catalog=hashlib.sha256(self.store.read_bytes(audit.ASSETS, migration.CATALOG)).hexdigest(),
            expected_generation=self.pointer['generation'])

    def test_preview_and_apply_preserve_all_history_and_extra_references(self):
        before = copy.deepcopy(self.store.objects)
        value = self.prepare()
        self.assertEqual(self.store.writes, [])
        report = sidecar_reconcile.preview(value)
        self.assertFalse(report['applied']); self.assertFalse(report['deletion_authorized'])
        result = sidecar_reconcile.apply(self.store, self.api, value)
        self.assertTrue(result['applied']); self.assertFalse(result['objects_deleted'])
        self.assertTrue(before.keys() <= self.store.objects.keys())
        for key, raw in before.items():
            if key not in {(audit.ASSETS, audit.SIDECAR), (audit.ASSETS, migration.CATALOG)}:
                self.assertEqual(self.store.objects[key], raw)
        old = json.loads(before[audit.ASSETS, migration.CATALOG])
        current = json.loads(self.store.objects[audit.ASSETS, migration.CATALOG])
        self.assertNotIn('redirect_sidecar_pending', current)
        for gen, item in old['generations'].items():
            self.assertEqual(current['generations'][gen]['references'], item['references'])
        refs = current['generations'][value['base_generation']]['references']
        self.assertIn({'bucket': audit.BUCKET, 'path': NEW}, refs)
        self.assertIn({'bucket': audit.BUCKET, 'path': 'objects/extra/required.bin'}, refs)
        self.assertNotIn({'bucket': audit.BUCKET, 'path': OLD}, refs)
        self.assertEqual(audit.decode_sidecar(self.store.read_bytes(audit.ASSETS, audit.SIDECAR))['f'][KEY]['p'], NEW)
        self.assertTrue(publication.verify_generation(self.store, Consumers(self.store), self.pointer['generation'])['complete'])
        self.assertGreaterEqual(self.check.call_count, 4)

    def test_unpersisted_journal_and_oversized_catalog_fail_before_writes(self):
        value = self.prepare()
        with self.assertRaises(FileNotFoundError):
            sidecar_reconcile.run(self.store, self.api, value)
        self.assertEqual(self.store.writes, [])
        self.store.stat = lambda *_: 49 * 1024 * 1024
        with self.assertRaisesRegex(ValueError, 'bounded'):
            self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_same_snapshot_under_legacy_generation_cannot_be_replaced(self):
        value = self.prepare()
        self.store.objects[audit.ASSETS, value['snapshot_path']] = b'foreign'
        before = copy.deepcopy(self.store.objects)
        with self.assertRaisesRegex(ValueError, 'immutable object conflict'):
            sidecar_reconcile.apply(self.store, self.api, value)
        self.assertEqual(self.store.objects[audit.ASSETS, value['snapshot_path']], b'foreign')
        self.assertEqual(self.store.objects[audit.ASSETS, audit.SIDECAR], before[audit.ASSETS, audit.SIDECAR])
        self.assertEqual(self.store.objects[audit.ASSETS, migration.CATALOG], before[audit.ASSETS, migration.CATALOG])

    def test_fence_failure_before_journal_prevents_writes(self):
        value = self.prepare()
        self.check.side_effect = RuntimeError('external isolation not established')
        with self.assertRaisesRegex(RuntimeError, 'external isolation'):
            sidecar_reconcile.apply(self.store, self.api, value)
        self.assertEqual(self.store.writes, [])

    def test_interruption_after_catalog_is_resumable_only_with_fence(self):
        value = self.prepare()
        put = self.store.put_if_match
        def fail(bucket, path, raw, etag):
            if path == audit.SIDECAR:
                raise OSError('sidecar response lost')
            return put(bucket, path, raw, etag)
        self.store.put_if_match = fail
        with self.assertRaisesRegex(OSError, 'response lost'):
            sidecar_reconcile.apply(self.store, self.api, value)
        tx = sidecar_reconcile.identity(value)
        self.assertEqual(sidecar_reconcile.sha(self.store.read_bytes(audit.ASSETS, migration.CATALOG)), value['catalog_after_sha256'])
        self.assertIn('redirect_sidecar_pending', json.loads(self.store.read_bytes(audit.ASSETS, migration.CATALOG)))
        self.assertEqual(sidecar_reconcile.sha(self.store.read_bytes(audit.ASSETS, audit.SIDECAR)), value['sidecar_before_sha256'])
        before = copy.deepcopy(self.store.objects)
        self.check.side_effect = RuntimeError('writer resumed')
        with self.assertRaisesRegex(RuntimeError, 'writer resumed'):
            sidecar_reconcile.resume(self.store, self.api, tx)
        self.assertEqual(self.store.objects, before)
        self.check.side_effect = None
        self.store.put_if_match = put
        self.assertTrue(sidecar_reconcile.resume(self.store, self.api, tx)['applied'])
        self.assertNotIn('redirect_sidecar_pending', json.loads(self.store.read_bytes(audit.ASSETS, migration.CATALOG)))
        self.assertTrue(sidecar_reconcile.resume(self.store, self.api, tx)['applied'])
        self.assertEqual(publication.current(self.store)[0], self.pointer)

    def test_divergent_root_or_impossible_order_fails_without_other_writes(self):
        value = self.prepare()
        sidecar_reconcile.persist(self.store, value)
        self.store.writes.clear()
        self.store.objects[audit.ASSETS, audit.SIDECAR] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'diverged'):
            sidecar_reconcile.run(self.store, self.api, value)
        self.assertEqual(self.store.writes, [])
        self.store.objects[audit.ASSETS, audit.SIDECAR] = base64.b64decode(value['sidecar_after_base64'])
        # Catalog is still before, while sidecar is after: impossible order.
        with self.assertRaisesRegex(ValueError, 'impossible order'):
            sidecar_reconcile.run(self.store, self.api, value)

    def test_forged_journal_content_or_hash_is_rejected(self):
        value = self.prepare()
        bad = copy.deepcopy(value)
        bad['base_generation'] = 'e' * 32
        with self.assertRaises(ValueError):
            sidecar_reconcile.identity(bad)
        with self.assertRaisesRegex(ValueError, 'identity'):
            sidecar_reconcile.resume(self.store, self.api, '../bad')


if __name__ == '__main__':
    unittest.main()
