import copy
import gzip
import hashlib
import json
import unittest
from unittest.mock import patch

from scripts import reader_page_atomic as atomic, reader_page_audit as audit
from scripts import reader_page_migrate as migration, reader_page_reconcile as reconcile
from scripts import reader_page_refresh as refresh, reader_lifecycle as lifecycle, publish_reader_v3 as publication
from tests.test_reader_page_audit import fixture, KEY, OLD, NEW
from tests.test_reader_page_atomic import AtomicStore
from tests.test_reader_page_migrate import MemoryApi
from tests.test_reader_route_control import Consumers


class PageReconciliationTests(unittest.TestCase):
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
        initial = atomic.prepare(self.store, self.api,
            expected_index=hashlib.sha256(self.store.read_bytes(audit.BUCKET, audit.INDEX)).hexdigest(),
            expected_sidecar=hashlib.sha256(self.store.read_bytes(audit.ASSETS, audit.SIDECAR)).hexdigest(),
            expected_v3=pointer['generation'])
        atomic.apply(self.store, self.api, initial)
        self.pointer = publication.current(self.store)[0]
        verifier = patch.object(publication, 'verify_generation', return_value={'complete': True})
        self.verify = verifier.start(); self.addCleanup(verifier.stop)
        self.args = {'expected_index': hashlib.sha256(self.store.read_bytes(audit.BUCKET, audit.INDEX)).hexdigest(),
                     'expected_generation': self.pointer['generation']}
        self.store.writes.clear()

    def prepare(self):
        return reconcile.prepare(self.store, self.api, **self.args)

    def test_single_index_reconciliation_preserves_history_and_existing_projection(self):
        before = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        self.assertEqual(plan['changed'], 1)
        self.assertEqual(plan['index_diff'], [{'key': KEY, 'changed_fields': ['manifest_path', 'new_path']}])
        self.assertEqual(self.store.writes, [])
        report = reconcile.apply(self.store, self.api, plan)
        self.assertTrue(report['applied'])
        self.assertEqual(report['changed_entries'], 1)
        self.assertFalse(report['objects_deleted'])
        self.assertFalse(report['deletion_authorized'])
        self.assertFalse(report['sidecar_changed'])
        self.assertFalse(report['legacy_catalog_changed'])
        self.assertFalse(report['v3_pointer_changed'])
        self.assertEqual(self.store.writes, [(audit.BUCKET, audit.INDEX)])
        for identity, raw in before.items():
            if identity != (audit.BUCKET, audit.INDEX):
                self.assertEqual(self.store.objects[identity], raw)
        index = json.loads(self.store.objects[audit.BUCKET, audit.INDEX])
        self.assertEqual(index['files'][0]['new_path'], NEW)
        self.assertEqual(index['files'][0]['manifest_path'], NEW.rsplit('/', 1)[0] + '/derived-manifest.json')
        self.assertEqual(index['files'][0]['old_path'], fixture()[0]['files'][0]['old_path'])
        self.verify.assert_called_once()
        self.assertTrue(refresh.run(self.store, self.api, apply=False)['unchanged'])
        next_args = {**self.args, 'expected_index': report['index_after_sha256']}
        repeated = reconcile.prepare(self.store, self.api, **next_args)
        self.assertEqual(repeated['changed'], 0)
        self.store.writes.clear()
        self.assertTrue(reconcile.apply(self.store, self.api, repeated)['applied'])
        self.assertEqual(self.store.writes, [])
        refreshed = refresh.run(self.store, self.api, apply=True)
        self.assertTrue(refreshed['unchanged'])
        self.assertEqual(refreshed['generation'], self.pointer['generation'])

    def test_stale_missing_copy_manifest_and_pointer_block(self):
        for field in ('expected_index', 'expected_generation'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'changed'):
                reconcile.prepare(self.store, self.api, **{**self.args, field: 'f' * 64})
        plan = self.prepare()
        self.api.files[NEW]['xet_hash'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'object changed'):
            reconcile.apply(self.store, self.api, plan)
        self.assertEqual(self.store.writes, [])

    def test_consumer_failure_or_change_after_consumer_check_stops_before_write(self):
        plan = self.prepare()
        self.verify.side_effect = RuntimeError('consumer unavailable')
        with self.assertRaisesRegex(RuntimeError, 'consumer unavailable'):
            reconcile.apply(self.store, self.api, plan)
        self.assertEqual(self.store.writes, [])
        self.verify.side_effect = lambda *_args, **_kw: self.store.objects.__setitem__(
            (audit.ASSETS, audit.SIDECAR), b'foreign')
        with self.assertRaisesRegex(ValueError, 'sidecar changed'):
            reconcile.apply(self.store, self.api, plan)
        self.assertEqual(self.store.writes, [])

    def test_conditional_conflict_and_lost_response_never_blindly_retry(self):
        plan = self.prepare()
        put = self.store.put_if_match
        def conflict(bucket, path, raw, etag):
            self.store.objects[bucket, path] = b'{"foreign":true}'
            return put(bucket, path, raw, etag)
        self.store.put_if_match = conflict
        with self.assertRaisesRegex(RuntimeError, 'changed during conditional write'):
            reconcile.apply(self.store, self.api, plan)
        self.assertEqual(self.store.objects[audit.BUCKET, audit.INDEX], b'{"foreign":true}')
        self.assertEqual(publication.current(self.store)[0], self.pointer)

    def test_response_lost_after_write_is_resolved_by_exact_after_hash(self):
        plan = self.prepare()
        put = self.store.put_if_match
        def lost(bucket, path, raw, etag):
            put(bucket, path, raw, etag)
            raise OSError('response lost')
        self.store.put_if_match = lost
        with self.assertRaisesRegex(OSError, 'response lost'):
            reconcile.apply(self.store, self.api, plan)
        self.assertEqual(hashlib.sha256(self.store.objects[audit.BUCKET, audit.INDEX]).hexdigest(),
                         hashlib.sha256(plan['after']).hexdigest())
        self.assertEqual(publication.current(self.store)[0], self.pointer)
        after = reconcile.prepare(self.store, self.api, expected_index=hashlib.sha256(plan['after']).hexdigest(),
                                  expected_generation=self.pointer['generation'])
        self.assertEqual(after['changed'], 0)

    def test_foreign_index_write_after_conditional_put_is_reported(self):
        plan = self.prepare()
        put = self.store.put_if_match
        def foreign(bucket, path, raw, etag):
            put(bucket, path, raw, etag)
            self.store.objects[audit.BUCKET, audit.INDEX] = b'{"foreign":true}'
        self.store.put_if_match = foreign
        with self.assertRaisesRegex(ValueError, 'readback differs'):
            reconcile.apply(self.store, self.api, plan)
        self.assertEqual(publication.current(self.store)[0], self.pointer)

    def test_missing_or_wrong_mapping_stops_before_write(self):
        plan = self.prepare()
        altered = copy.deepcopy(plan['snapshot'])
        altered['planned'][0]['new_path'] = 'objects/aa/' + 'a' * 64 + '/' + 'd' * 16 + '/document.pdf'
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            reconcile.validate_snapshot(altered, {OLD: NEW})
        self.assertEqual(self.store.writes, [])


if __name__ == '__main__':
    unittest.main()
