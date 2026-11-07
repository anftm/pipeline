import copy
import gzip
import unittest
from unittest.mock import patch

from scripts import reader_page_refresh as refresh, reader_page_redirects as redirects
from scripts import reader_page_atomic as atomic, reader_page_audit as audit, reader_page_migrate as migration
from scripts import publish_reader_v3 as publication, reader_lifecycle as lifecycle
from tests.test_reader_page_audit import KEY, OLD, NEW
from tests.test_reader_page_atomic import AtomicStore
from tests.test_reader_page_migrate import MemoryApi
from tests.test_reader_page_audit import fixture
from tests.test_reader_route_control import Consumers


class AuditedRedirectRefreshTests(unittest.TestCase):
    def setUp(self):
        import hashlib
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
        self.original = atomic.apply(self.store, self.api, plan)
        self.store.writes.clear()

    def update_canonical(self):
        base = audit.decode_sidecar(self.store.objects[audit.ASSETS, audit.SIDECAR])
        base['f']['VoiceOfML/Test\0new.txt'] = {'s': 4}
        raw = gzip.compress(audit.encode(base), mtime=0)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = raw
        old = self.store.objects[audit.ASSETS, migration.CATALOG]
        import json
        catalog, _ = lifecycle.catalog_generation(base, json.loads(old))
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)

    def test_unchanged_unify_audits_without_writing(self):
        report = refresh.run(self.store, self.api, apply=True)
        self.assertTrue(report['unchanged'])
        self.assertTrue(report['redirect_audited'])
        self.assertEqual(report['generation'], self.original['generation'])
        self.assertEqual(self.store.writes, [])

    def test_changed_canonical_refresh_preserves_redirect_and_updates_other_format(self):
        self.update_canonical()
        plan = refresh.prepare(self.store, self.api)
        self.assertFalse(plan['unchanged'])
        preview = refresh.run(self.store, self.api, apply=False)
        self.assertFalse(preview['applied'])
        self.assertEqual(self.store.writes, [])
        report = refresh.run(self.store, self.api, apply=True)
        pointer, catalog = publication.current(self.store)
        base = publication.pinned_base(self.store, catalog, {})
        self.assertEqual(pointer['generation'], report['generation'])
        self.assertEqual(catalog[redirects.FIELD], {OLD: NEW})
        self.assertEqual(base['f'][KEY]['p'], NEW)
        self.assertIn('VoiceOfML/Test\0new.txt', base['f'])
        self.assertTrue(publication.verify_generation(self.store, Consumers(self.store), report['generation'])['complete'])
        self.assertFalse(report['objects_deleted'])
        self.assertTrue(report['redirect_audited'])

    def test_route_remove_restore_keeps_redirect_and_other_routes(self):
        options = {'repo': 'VoiceOfML/Test', 'path': '目录/书.caj'}
        removed = refresh.run(self.store, self.api, expected_parent=self.original['generation'],
                              action='remove', options=options, apply=True)
        _, catalog = publication.current(self.store)
        self.assertNotIn(KEY, publication.pinned_base(self.store, catalog, {})['f'])
        self.assertIn(KEY, catalog['suppressed_sources'])
        restored = refresh.run(self.store, self.api, expected_parent=removed['generation'],
                               action='restore', options=options, apply=True)
        pointer, catalog = publication.current(self.store)
        self.assertEqual(pointer['generation'], restored['generation'])
        self.assertEqual(catalog[redirects.FIELD], {OLD: NEW})
        self.assertEqual(publication.pinned_base(self.store, catalog, {})['f'][KEY]['p'], NEW)

    def test_sidecar_changed_before_legacy_catalog_handoff_blocks_refresh(self):
        base = audit.decode_sidecar(self.store.objects[audit.ASSETS, audit.SIDECAR])
        base['f']['VoiceOfML/Test\0pending.txt'] = {'s': 4}
        self.store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode(base), mtime=0)
        with self.assertRaisesRegex(ValueError, 'does not bind'):
            refresh.run(self.store, self.api, apply=True)
        self.assertEqual(self.store.writes, [])

    def test_missing_copy_legacy_catalog_drift_or_pointer_race_blocks_before_pointer(self):
        self.update_canonical()
        old_pointer = publication.current(self.store)[0]
        api_files = copy.deepcopy(self.api.files)
        self.api.files[NEW]['xet_hash'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            refresh.run(self.store, self.api, apply=True)
        self.api.files = api_files
        original = self.store.objects[audit.ASSETS, migration.CATALOG]
        self.store.objects[audit.ASSETS, migration.CATALOG] = b'{"version":1,"kind":"reader-catalog","current":null,"generations":{}}'
        with self.assertRaisesRegex(ValueError, 'does not bind'):
            refresh.run(self.store, self.api, apply=True)
        self.store.objects[audit.ASSETS, migration.CATALOG] = original
        plan = refresh.prepare(self.store, self.api)
        self.store.put_if_match = lambda *_: (_ for _ in ()).throw(RuntimeError('pointer changed'))
        with self.assertRaisesRegex(RuntimeError, 'pointer changed'):
            atomic.apply(self.store, self.api, plan)
        self.assertEqual(publication.current(self.store)[0], old_pointer)

    def test_unauthorized_and_wrong_generation_do_not_write(self):
        self.store.allowed = False
        with self.assertRaisesRegex(RuntimeError, 'serialized'):
            refresh.run(self.store, self.api, apply=True)
        self.store.allowed = True
        with self.assertRaisesRegex(ValueError, 'generation changed'):
            refresh.run(self.store, self.api, expected_parent='f' * 64, action='remove',
                        options={'repo': 'VoiceOfML/Test', 'path': '目录/书.caj'}, apply=True)
        self.assertEqual(self.store.writes, [])


if __name__ == '__main__':
    unittest.main()
