import copy
import gzip
import hashlib
import json
import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import reader_page_migrate as migration, reader_page_audit as audit
from scripts import publish_reader_v3 as publication, reader_lifecycle as lifecycle
from tests.test_reader_page_audit import fixture, KEY, OLD, NEW
from tests.test_publish_reader_v3 import Store
from tests.test_reader_route_control import Consumers


class MemoryApi:
    def __init__(self, derived, objects):
        self.files = {**derived, **objects}
    def list_bucket_tree(self, bucket, prefix, recursive):
        for path, meta in self.files.items():
            if path.startswith(prefix):
                yield SimpleNamespace(type='file', path=path, size=meta['size'], xet_hash=meta['xet_hash'])
    def get_bucket_paths_info(self, bucket, paths):
        for path in paths:
            if path in self.files:
                yield SimpleNamespace(path=path, **self.files[path])


class ConditionalStore(Store):
    """In-memory stand-in with ETags and an atomic conditional update."""
    def read_for_update(self, bucket, path):
        raw = self.read_bytes(bucket, path)
        return raw, '"' + hashlib.sha256(raw).hexdigest() + '"'

    def put_if_match(self, bucket, path, raw, etag):
        if self.read_for_update(bucket, path)[1] != etag:
            raise RuntimeError('page root changed during conditional write')
        self.put_bytes(bucket, path, raw)


class PageMigrationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'READER_PAGE_EXTERNAL_WRITERS_STOPPED': 'true'})
        env.start(); self.addCleanup(env.stop)
        self.fence = patch('scripts.reader_page_migrate.GitHubProducerFence', autospec=True)
        self.fence_class = self.fence.start(); self.addCleanup(self.fence.stop)
        self.index, self.derived, self.objects, self.manifests, self.sidecar = fixture()
        self.store = ConditionalStore(); self.api = MemoryApi(self.derived, self.objects)
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(self.index)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode(self.sidecar), mtime=0)
        for path, raw in self.manifests.items(): self.store.objects[audit.BUCKET, path] = raw
        catalog, generation = lifecycle.catalog_generation(self.sidecar)
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)
        publication.unify(self.store, apply=True)
        self.pointer = publication.current(self.store)[0]
        self.store.writes.clear()
        self.args = {'expected_index': hashlib.sha256(self.store.read_bytes(audit.BUCKET, audit.INDEX)).hexdigest(),
                     'expected_sidecar': hashlib.sha256(self.store.read_bytes(audit.ASSETS, audit.SIDECAR)).hexdigest(),
                     'expected_v3': self.pointer['generation']}

    def prepare(self):
        return migration.prepare(self.store, self.api, **self.args)

    def test_new_pdf_paths_are_accepted_by_both_site_contracts_when_available(self):
        root = Path(__file__).resolve().parents[2]
        hf = root / 'hf-search'
        page = root / 'search/static/reader/core/reader-contract.js'
        if not (hf / 'backend/reader_assets.py').is_file() or not page.is_file():
            self.skipTest('peer site worktrees are unavailable')
        plan = self.prepare()
        import sys
        sys.path.insert(0, str(hf))
        try:
            from backend.reader_assets import decode_reader_assets
            actual = decode_reader_assets(plan['packed'])
        finally:
            sys.path.remove(str(hf))
        self.assertEqual(actual['f'][KEY]['p'], NEW)
        script = ('const fs=require("fs"),vm=require("vm");'
                  'const c={URL,console,self:{}};vm.createContext(c);'
                  'vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),c);'
                  'if(!c.self.VoiceOfMLReader.isBucketPath(process.argv[2],true,"vomebook/pdf-pages-v2"))process.exit(1)')
        result = subprocess.run(['node', '-e', script, str(page), NEW], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.writes, [])

    def test_prepare_and_apply_update_only_indexes_and_leave_all_files(self):
        original = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        self.assertEqual(self.store.objects, original)
        self.assertEqual(self.store.writes, [])
        self.assertEqual(plan['index']['files'][0]['new_path'], NEW)
        self.assertEqual(plan['sidecar']['f'][KEY]['p'], NEW)
        old_catalog = json.loads(original[audit.ASSETS, migration.CATALOG])
        result = migration.apply(self.store, self.api, plan)
        self.assertEqual(result['migrated_entries'], 1)
        self.assertFalse(result['objects_deleted'])
        self.assertTrue(original.keys() <= self.store.objects.keys())
        pointer, catalog = publication.current(self.store)
        projected = publication.project_sidecar(publication.pinned_base(self.store, catalog, {}), catalog)
        self.assertEqual(projected['f'][KEY]['p'], NEW)
        legacy = json.loads(self.store.read_bytes(audit.ASSETS, migration.CATALOG))
        old_id = old_catalog['current']['generation']
        self.assertEqual(legacy['generations'][old_id]['references'], old_catalog['generations'][old_id]['references'])
        self.assertEqual(legacy['generations'][old_id]['status'], 'superseded')
        self.assertEqual(legacy['current']['generation'], result['base_generation'])
        self.assertEqual(pointer['generation'], result['v3_generation'])
        client = Consumers(self.store)
        for site in ('hf', 'pages'):
            self.assertTrue(publication.observe_consumer(self.store, site, client)['projection_verified'])

    def test_extra_current_catalog_references_are_carried_forward(self):
        catalog = json.loads(self.store.read_bytes(audit.ASSETS, migration.CATALOG))
        generation = catalog['current']['generation']
        extra = {'bucket': audit.BUCKET, 'path': 'objects/extra/required.bin'}
        catalog['generations'][generation]['references'].append(extra)
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)
        plan = self.prepare()
        refs = plan['catalog']['generations'][plan['generation']]['references']
        self.assertIn(extra, refs)
        self.assertIn({'bucket': audit.BUCKET, 'path': NEW}, refs)
        self.assertNotIn({'bucket': audit.BUCKET, 'path': OLD}, refs)
        self.assertIn({'bucket': audit.BUCKET, 'path': OLD},
                      plan['catalog']['generations'][generation]['references'])

    def test_forged_cached_audit_cannot_redirect_to_another_source(self):
        snapshot = audit.audit(self.store, self.api)
        snapshot['planned'][0]['new_path'] = 'objects/bb/' + 'b' * 64 + '/' + 'c' * 16 + '/document.pdf'
        snapshot['planned'][0]['new_manifest'] = snapshot['planned'][0]['new_path'].rsplit('/', 1)[0] + '/derived-manifest.json'
        with self.assertRaisesRegex(ValueError, 'object identity'):
            migration.prepare_from_audit(self.store, snapshot, **self.args)
        self.assertEqual(self.store.writes, [])

    def test_stale_input_and_incomplete_copy_cannot_mutate(self):
        for field in ('expected_index', 'expected_sidecar', 'expected_v3'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'stale'):
                migration.prepare(self.store, self.api, **{**self.args, field: 'f' * 64})
        self.api.files[NEW]['xet_hash'] = 'different'
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_producer_fence_failure_or_missing_confirmation_stops_before_write(self):
        plan = self.prepare()
        with patch.dict(os.environ, {'READER_PAGE_EXTERNAL_WRITERS_STOPPED': 'false'}):
            with self.assertRaisesRegex(RuntimeError, 'independent bucket writers'):
                migration.apply(self.store, self.api, plan)
        self.fence_class.assert_not_called()
        self.fence_class.return_value.check.side_effect = RuntimeError('fence unavailable')
        with self.assertRaisesRegex(RuntimeError, 'fence unavailable'):
            migration.apply(self.store, self.api, plan)
        self.assertEqual(self.store.writes, [])
        self.fence_class.return_value.check.assert_called_with(
            allow_current_workflow=migration.WORKFLOW, all_workflows=True)

    def test_fence_is_rechecked_after_interruption_and_during_resume(self):
        plan = self.prepare()
        check = self.fence_class.return_value.check
        check.side_effect = [None, None, None, None, None, RuntimeError('producer resumed')]
        with self.assertRaisesRegex(RuntimeError, 'producer resumed'):
            migration.apply(self.store, self.api, plan)
        self.assertEqual(self.store.read_bytes(audit.BUCKET, audit.INDEX), audit.encode(plan['index']))
        self.assertEqual(self.store.read_bytes(audit.ASSETS, audit.SIDECAR), plan['before_sidecar'])
        transactions = [path for bucket, path in self.store.objects
                        if bucket == audit.ASSETS and path.startswith(migration.TRANSACTION_PREFIX)]
        self.assertEqual(len(transactions), 1)
        identity = transactions[0].rsplit('/', 1)[-1].removesuffix('.json.gz')
        before = copy.deepcopy(self.store.objects)
        check.side_effect = RuntimeError('producer still active')
        with self.assertRaisesRegex(RuntimeError, 'producer still active'):
            migration.resume(self.store, self.api, identity)
        self.assertEqual(self.store.objects, before)
        check.side_effect = None
        self.assertTrue(migration.resume(self.store, self.api, identity)['applied'])

    def test_writer_and_object_change_are_rechecked_before_writes(self):
        plan = self.prepare()
        self.store.allowed = False
        with self.assertRaisesRegex(RuntimeError, 'serialized'):
            migration.apply(self.store, self.api, plan)
        self.store.allowed = True
        self.api.files[NEW]['xet_hash'] = 'different'
        with self.assertRaisesRegex(ValueError, 'object changed'):
            migration.apply(self.store, self.api, plan)
        self.assertEqual(self.store.writes, [])

    def test_moving_catalog_or_sidecar_stops_before_write(self):
        for bucket, path in ((audit.ASSETS, audit.SIDECAR), (audit.ASSETS, migration.CATALOG), (audit.BUCKET, audit.INDEX)):
            with self.subTest(path=path):
                plan = self.prepare()
                original = self.store.objects[bucket, path]
                self.store.objects[bucket, path] = b'changed'
                with self.assertRaisesRegex(ValueError, 'root changed'):
                    migration.apply(self.store, self.api, plan)
                self.store.objects[bucket, path] = original
                self.assertEqual(self.store.writes, [])

    def test_alias_is_updated_but_unsupported_field_blocks(self):
        other = 'VoiceOfML/Test\0别名.caj'
        self.sidecar['f'][other] = {'s': 2, 'm': 'p', 'b': audit.BUCKET, 'p': OLD}
        self.store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode(self.sidecar), mtime=0)
        catalog, _ = lifecycle.catalog_generation(self.sidecar)
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)
        publication.unify(self.store, apply=True)
        self.args['expected_sidecar'] = migration.digest(self.store.read_bytes(audit.ASSETS, audit.SIDECAR))
        self.args['expected_v3'] = publication.current(self.store)[0]['generation']
        plan = self.prepare()
        self.assertEqual(plan['sidecar']['f'][other]['p'], NEW)
        self.assertEqual(plan['changed_routes'], 2)
        self.assertEqual(self.store.read_bytes(audit.BUCKET, audit.INDEX), plan['before_index'])

    def test_missing_deployed_site_or_wrong_generation_cannot_confirm(self):
        plan = self.prepare()
        result = migration.apply(self.store, self.api, plan)
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            publication.verify_generation(self.store, Consumers(self.store, stale='vomebook.github.io'),
                                          result['generation'])
        with self.assertRaisesRegex(ValueError, 'changed'):
            publication.verify_generation(self.store, Consumers(self.store), '0' * 64)
        self.assertTrue(publication.verify_generation(self.store, Consumers(self.store),
                                                       result['generation'])['complete'])

    def test_resume_after_every_write_or_lost_response_preserves_single_generation(self):
        original_objects = copy.deepcopy(self.store.objects)
        targets = [(audit.BUCKET, audit.INDEX), (audit.ASSETS, audit.SIDECAR),
                   (audit.ASSETS, migration.CATALOG), (audit.ASSETS, publication.POINTER)]
        for fail_target in targets:
            for lost_after in (False, True):
                with self.subTest(target=fail_target, lost_after=lost_after):
                    self.store.objects = copy.deepcopy(original_objects)
                    self.store.writes.clear()
                    self.store.put_bytes = Store.put_bytes.__get__(self.store)
                    plan = self.prepare()
                    actual = self.store.put_bytes
                    def fail(bucket, path, raw):
                        if (bucket, path) == fail_target:
                            if lost_after:
                                actual(bucket, path, raw)
                            raise OSError('simulated interruption')
                        return actual(bucket, path, raw)
                    self.store.put_bytes = fail
                    with self.assertRaises(OSError):
                        migration.apply(self.store, self.api, plan)
                    transactions = [p for b, p in self.store.objects if b == audit.ASSETS and p.startswith(migration.TRANSACTION_PREFIX)]
                    self.assertEqual(len(transactions), 1)
                    identity = transactions[0].rsplit('/', 1)[-1].removesuffix('.json.gz')
                    self.store.put_bytes = Store.put_bytes.__get__(self.store)
                    recovered = migration.resume(self.store, self.api, identity)
                    self.assertFalse(recovered['objects_deleted'])
                    self.assertEqual(publication.current(self.store)[0]['generation'], recovered['generation'])
                    catalog = json.loads(self.store.read_bytes(audit.ASSETS, migration.CATALOG))
                    self.assertEqual(catalog['current']['generation'], recovered['base_generation'])
                    self.assertEqual(len(catalog['generations']), 2)
                    repeated = migration.resume(self.store, self.api, identity)
                    self.assertEqual(repeated['generation'], recovered['generation'])
                    self.assertTrue(original_objects.keys() <= self.store.objects.keys())

    def test_root_concurrent_write_between_s3_read_and_put_is_not_overwritten(self):
        plan = self.prepare()
        original = self.store.put_if_match
        foreign = b'{"concurrent":true}'
        target = (audit.BUCKET, audit.INDEX)
        def race(bucket, path, raw, etag):
            if (bucket, path) == target:
                self.store.objects[target] = foreign
            return original(bucket, path, raw, etag)
        self.store.put_if_match = race
        with self.assertRaisesRegex(RuntimeError, 'changed during conditional write'):
            migration.apply(self.store, self.api, plan)
        self.assertEqual(self.store.objects[target], foreign)
        self.assertEqual(self.store.objects[audit.ASSETS, audit.SIDECAR], plan['before_sidecar'])
        self.assertEqual(publication.current(self.store)[0], self.pointer)

    def test_pointer_concurrent_write_between_s3_read_and_put_is_not_overwritten(self):
        plan = self.prepare()
        original = self.store.put_if_match
        target = (audit.ASSETS, publication.POINTER)
        foreign = b'{"concurrent":true}'
        def race(bucket, path, raw, etag):
            if (bucket, path) == target:
                self.store.objects[target] = foreign
            return original(bucket, path, raw, etag)
        self.store.put_if_match = race
        with self.assertRaisesRegex(RuntimeError, 'changed during conditional write'):
            migration.apply(self.store, self.api, plan)
        self.assertEqual(self.store.objects[target], foreign)
        self.assertEqual(self.store.objects[audit.BUCKET, audit.INDEX], audit.encode(plan['index']))
        self.assertTrue(any(path.startswith(migration.TRANSACTION_PREFIX) for _, path in self.store.objects))

    def test_promotion_rechecks_all_prior_roots_after_later_writes(self):
        plan = self.prepare()
        original = self.store.put_if_match
        foreign = b'{"concurrent":true}'
        def race(bucket, path, raw, etag):
            original(bucket, path, raw, etag)
            if (bucket, path) == (audit.ASSETS, migration.CATALOG):
                self.store.objects[audit.BUCKET, audit.INDEX] = foreign
        self.store.put_if_match = race
        with self.assertRaisesRegex(ValueError, 'root changed before promotion'):
            migration.apply(self.store, self.api, plan)
        self.assertEqual(self.store.objects[audit.BUCKET, audit.INDEX], foreign)
        self.assertEqual(publication.current(self.store)[0], self.pointer)

    def test_promotion_refuses_a_changed_canonical_after_root_readback(self):
        plan = self.prepare()
        original = publication.unify
        foreign = b'{"concurrent":true}'
        def race(store, **kwargs):
            self.store.objects[audit.ASSETS, audit.SIDECAR] = foreign
            return original(store, **kwargs)
        with patch('scripts.reader_page_migrate.publication.unify', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'canonical sidecar changed'):
                migration.apply(self.store, self.api, plan)
        self.assertEqual(self.store.objects[audit.ASSETS, audit.SIDECAR], foreign)
        self.assertEqual(publication.current(self.store)[0], self.pointer)

    def test_resume_accepts_semantically_unchanged_json_wire_serialization(self):
        plan = self.prepare()
        value = migration.transaction(plan)
        identity = migration.persist_transaction(self.store, value)
        raw = self.store.objects[audit.ASSETS, migration.CATALOG]
        self.store.objects[audit.ASSETS, migration.CATALOG] = json.dumps(
            json.loads(raw), ensure_ascii=False, indent=4).encode()
        result = migration.resume(self.store, self.api, identity)
        self.assertEqual(result['base_generation'], plan['generation'])
        self.assertEqual(publication.current(self.store)[0]['generation'], result['generation'])

    def test_resume_rejects_foreign_write_without_mutating_other_roots(self):
        plan = self.prepare()
        value = migration.transaction(plan)
        identity = migration.persist_transaction(self.store, value)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = b'foreign'
        self.store.writes.clear()
        with self.assertRaisesRegex(ValueError, 'root changed'):
            migration.resume(self.store, self.api, identity)
        self.assertEqual(self.store.writes, [])
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            migration.resume(self.store, self.api, '../bad')

    def test_old_v3_snapshot_does_not_move_if_index_write_fails(self):
        plan = self.prepare()
        original_put = self.store.put_bytes
        def fail(bucket, path, raw):
            if (bucket, path) == (audit.ASSETS, audit.SIDECAR):
                raise OSError('interrupted')
            return original_put(bucket, path, raw)
        self.store.put_bytes = fail
        with self.assertRaises(OSError):
            migration.apply(self.store, self.api, plan)
        pointer, catalog = publication.current(self.store)
        self.assertEqual(pointer, self.pointer)
        self.assertEqual(publication.pinned_base(self.store, catalog, {})['f'][KEY]['p'], OLD)
        self.assertIn(OLD, self.api.files)
        self.assertIn(NEW, self.api.files)


if __name__ == '__main__':
    unittest.main()
