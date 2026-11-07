import base64
import copy
import gzip
import hashlib
import json
import unittest
from unittest.mock import patch

from scripts import reader_legacy_compact as compact
from scripts import reader_page_audit as audit, reader_page_migrate as migration
from scripts import publish_reader_v3 as publication
from scripts.reader_gc_stream import graph_json
from tests import test_reader_sidecar_reconcile as sidecar_fixture


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class LegacyCompactionTests(unittest.TestCase):
    def setUp(self):
        # Reuse the sidecar fixture, then apply that reconciliation so the
        # legacy catalog holds superseded generations to compact.
        base = sidecar_fixture.SidecarReconciliationTests(
            'test_preview_and_apply_preserve_all_history_and_extra_references')
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.base, self.store, self.pointer = base, base.store, base.pointer
        from scripts import reader_sidecar_reconcile as sidecar_reconcile
        sidecar_reconcile.apply(self.store, base.api, base.prepare())
        store = self.store
        store.read_graph_json = lambda bucket, path, parser=None: (parser or graph_json)(
            [store.objects[bucket, path]], path.endswith('.gz'))
        store.digest = lambda bucket, path: sha(store.objects[bucket, path])
        writer = patch.object(publication, 'assert_writer')
        writer.start()
        self.addCleanup(writer.stop)
        store.writes.clear()

    def prepare(self):
        return compact.prepare(self.store, expected_generation=self.pointer['generation'],
                               expected_catalog=sha(self.store.read_bytes(audit.ASSETS, migration.CATALOG)))

    def catalog(self):
        return json.loads(self.store.objects[audit.ASSETS, migration.CATALOG])

    def test_apply_retires_history_with_one_catalog_write_and_keeps_the_original_bytes(self):
        before = copy.deepcopy(self.store.objects)
        old = self.catalog()
        value = self.prepare()
        self.assertEqual(self.store.writes, [])
        report = compact.preview(value)
        self.assertFalse(report['applied'])
        self.assertFalse(report['deletion_authorized'])
        self.assertGreaterEqual(len(report['generations_dropped']), 1)
        result = compact.apply(self.store, value)
        tx = compact.identity(value)
        self.assertTrue(result['applied'])
        self.assertTrue(result['catalog_written'])
        self.assertFalse(result['objects_deleted'])
        current = old['current']['generation']
        new = self.catalog()
        self.assertEqual(list(new['generations']), [current])
        self.assertEqual(new['generations'][current], old['generations'][current])
        self.assertTrue(set(old['generations']) - {current} <= set(new['retired_generations']))
        self.assertTrue(set(old.get('retired_generations', {})) <= set(new['retired_generations']))
        journal = (audit.ASSETS, compact.PREFIX + tx + '.json.gz')
        changed = {key for key, raw in self.store.objects.items() if before.get(key) != raw}
        self.assertEqual(changed, {(audit.ASSETS, migration.CATALOG),
                                   (audit.ASSETS, publication.ACKNOWLEDGMENTS), journal})
        self.assertTrue(before.keys() <= self.store.objects.keys())
        saved = json.loads(gzip.decompress(self.store.objects[journal]))
        self.assertEqual(base64.b64decode(saved['catalog_before_base64']), before[audit.ASSETS, migration.CATALOG])
        receipt = json.loads(self.store.objects[audit.ASSETS, publication.ACKNOWLEDGMENTS])
        self.assertEqual(receipt['current']['catalog_sha256'],
                         sha(self.store.objects[audit.ASSETS, migration.CATALOG]))
        self.assertEqual(publication.current(self.store)[0], self.pointer)
        self.assertEqual(sum(1 for item in self.store.writes if migration.CATALOG in item), 1)

    def test_refuses_to_release_a_reference_that_only_a_dropped_generation_protects(self):
        catalog = self.catalog()
        stale = next(name for name in catalog['generations'] if name != catalog['current']['generation'])
        catalog['generations'][stale]['references'].append(
            {'bucket': audit.BUCKET, 'path': 'objects/only-in-history/required.bin'})
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)
        self.store.writes.clear()
        with self.assertRaisesRegex(ValueError, 'nothing else pins'):
            self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_refuses_a_plan_that_would_drop_a_snapshot_identity(self):
        catalog = self.catalog()
        stale = next(name for name in catalog['generations'] if name != catalog['current']['generation'])
        catalog['generations'][stale]['snapshot'] = {
            'bucket': audit.ASSETS, 'bytes': 1, 'sha256': 'a' * 64,
            'path': 'reader-index/snapshots/' + 'a' * 64 + '/reader_assets.json.gz'}
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)
        self.store.writes.clear()
        with self.assertRaisesRegex(ValueError, 'snapshot identity'):
            self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_stale_identities_and_unready_catalog_stop_before_any_write(self):
        good = self.prepare()['catalog_before_sha256']
        generation = self.pointer['generation']
        with self.assertRaisesRegex(ValueError, 'changed after review'):
            compact.prepare(self.store, expected_generation=generation, expected_catalog='f' * 64)
        with self.assertRaisesRegex(ValueError, 'v3 generation changed'):
            compact.prepare(self.store, expected_generation='f' * 64, expected_catalog=good)
        with self.assertRaisesRegex(ValueError, 'identities are required'):
            compact.prepare(self.store, expected_generation='nope', expected_catalog=good)
        catalog = self.catalog()
        catalog['redirect_sidecar_pending'] = {'kind': 'pending'}
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)
        with self.assertRaisesRegex(ValueError, 'not acknowledged'):
            self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_failed_writer_window_prevents_every_write(self):
        value = self.prepare()
        self.base.check.side_effect = RuntimeError('external isolation not established')
        with self.assertRaisesRegex(RuntimeError, 'external isolation'):
            compact.apply(self.store, value)
        self.assertEqual(self.store.writes, [])

    def test_lost_receipt_response_resumes_without_a_second_catalog_write(self):
        value = self.prepare()
        real = publication.record_legacy_acknowledgments

        def lost(store, apply=False):
            if apply:
                raise OSError('receipt response lost')
            return real(store, apply=apply)
        with patch.object(publication, 'record_legacy_acknowledgments', side_effect=lost):
            with self.assertRaisesRegex(OSError, 'response lost'):
                compact.apply(self.store, value)
        tx = compact.identity(value)
        self.assertEqual(sha(self.store.objects[audit.ASSETS, migration.CATALOG]), value['catalog_after_sha256'])
        self.store.writes.clear()
        result = compact.resume(self.store, tx)
        self.assertTrue(result['applied'])
        self.assertFalse(result['catalog_written'])
        self.assertEqual(sum(1 for item in self.store.writes if migration.CATALOG in item), 0)
        self.assertTrue(compact.resume(self.store, tx)['applied'])

    def test_diverged_catalog_or_forged_journal_is_rejected_without_writes(self):
        value = self.prepare()
        compact.persist(self.store, value)
        self.store.writes.clear()
        self.store.objects[audit.ASSETS, migration.CATALOG] = b'{"foreign": true}'
        with self.assertRaisesRegex(ValueError, 'diverged'):
            compact.run(self.store, value)
        self.assertEqual(self.store.writes, [])
        bad = copy.deepcopy(value)
        bad['catalog_after_sha256'] = 'e' * 64
        with self.assertRaises(ValueError):
            compact.identity(bad)
        with self.assertRaisesRegex(ValueError, 'identity'):
            compact.resume(self.store, '../bad')


if __name__ == '__main__':
    unittest.main()
