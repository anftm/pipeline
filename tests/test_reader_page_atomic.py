import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from scripts import reader_page_atomic as atomic, reader_page_audit as audit
from scripts import reader_page_migrate as migration, reader_page_redirects as redirects
from scripts import reader_route_control as control, publish_reader_v3 as publication, reader_lifecycle as lifecycle
from tests.test_reader_page_audit import fixture, KEY, OLD, NEW
from tests.test_reader_page_migrate import MemoryApi, ConditionalStore
from tests.test_reader_route_control import Consumers


class AtomicStore(ConditionalStore):
    def immutable_put(self, bucket, path, raw):
        prior = self.objects.get((bucket, path))
        if prior is not None and prior != raw:
            raise ValueError('immutable object conflict')
        if prior is None:
            self.put_bytes(bucket, path, raw)


class AtomicRedirectTests(unittest.TestCase):
    def setUp(self):
        index, derived, objects, manifests, sidecar = fixture()
        self.store = AtomicStore()
        self.api = MemoryApi(derived, objects)
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode(sidecar), mtime=0)
        for path, raw in manifests.items():
            self.store.objects[audit.BUCKET, path] = raw
        catalog, _ = lifecycle.catalog_generation(sidecar)
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(catalog)
        publication.unify(self.store, apply=True)
        pointer = publication.current(self.store)[0]
        self.args = {'expected_index': hashlib.sha256(self.store.read_bytes(audit.BUCKET, audit.INDEX)).hexdigest(),
                     'expected_sidecar': hashlib.sha256(self.store.read_bytes(audit.ASSETS, audit.SIDECAR)).hexdigest(),
                     'expected_v3': pointer['generation']}
        self.store.writes.clear()

    def prepare(self):
        return atomic.prepare(self.store, self.api, **self.args)

    def test_preview_and_apply_change_only_atomic_pointer_and_new_immutable_content(self):
        before = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        self.assertFalse(atomic.report(plan, applied=False)['applied'])
        self.assertEqual(self.store.objects, before)
        result = atomic.apply(self.store, self.api, plan)
        pointer, catalog = publication.current(self.store)
        self.assertEqual(pointer, plan['pointer'])
        self.assertEqual(catalog[redirects.FIELD], {OLD: NEW})
        projected = publication.project_sidecar(publication.pinned_base(self.store, catalog, {}), catalog)
        self.assertEqual(projected['f'][KEY]['p'], NEW)
        self.assertTrue(result['applied'])
        self.assertFalse(result['canonical_indexes_changed'])
        self.assertFalse(result['objects_deleted'])
        self.assertFalse(result['deletion_authorized'])
        self.assertFalse(result['external_credentials_verified'])
        for key, value in before.items():
            if key != (audit.ASSETS, publication.POINTER):
                self.assertEqual(self.store.objects[key], value, key)
        self.assertEqual([item for item in self.store.writes if item in before], [(audit.ASSETS, publication.POINTER)])
        self.assertTrue(publication.verify_generation(self.store, Consumers(self.store), result['generation'])['complete'])

    def test_unify_refuses_unaudited_base_refresh_with_live_redirects(self):
        atomic.apply(self.store, self.api, self.prepare())
        self.store.writes.clear()
        with self.assertRaisesRegex(ValueError, 'audited base refresh'):
            publication.unify(self.store, apply=True)
        self.assertEqual(self.store.writes, [])
        _, catalog = publication.current(self.store)
        self.assertEqual(catalog[redirects.FIELD], {OLD: NEW})
        self.assertEqual(publication.pinned_base(self.store, catalog, {})['f'][KEY]['p'], NEW)

    def test_real_site_decoders_accept_redirect_catalog_and_projected_pdf(self):
        plan = self.prepare()
        root = Path(__file__).resolve().parents[2]
        hf = root / 'hf-search'
        contract = root / 'search/static/reader/core/reader-contract.js'
        if not (hf / 'backend/reader_v3_catalog.py').is_file() or not contract.is_file():
            self.skipTest('peer site worktrees are unavailable')
        code = '''import base64,json,sys
from backend.reader_v3_catalog import decode_catalog,merge_sidecar
from backend.reader_assets import decode_reader_assets
pointer=json.loads(sys.argv[1]); raw=base64.b64decode(sys.argv[2]); packed=base64.b64decode(sys.argv[3]); key=base64.b64decode(sys.argv[4]).decode()
cat=decode_catalog(pointer,raw); cat['base_sidecar']=decode_reader_assets(packed)
merged=merge_sidecar({'v':1,'f':{}},cat)
print(merged['f'][key]['p'])
'''
        import base64
        result = subprocess.run([sys.executable, '-c', code, json.dumps(plan['pointer']),
            base64.b64encode(plan['catalog_raw']).decode(), base64.b64encode(plan['packed']).decode(), base64.b64encode(KEY.encode()).decode()],
            cwd=hf, env={**os.environ, 'PYTHONPATH': str(hf)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), NEW)
        script = ('const fs=require("fs"),vm=require("vm");const c={URL,console,self:{}};'
                  'vm.createContext(c);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),c);'
                  'if(!c.self.VoiceOfMLReader.isBucketPath(process.argv[2],true,"vomebook/pdf-pages-v2"))process.exit(1)')
        pages = subprocess.run(['node', '-e', script, str(contract), NEW], capture_output=True, text=True)
        self.assertEqual(pages.returncode, 0, pages.stderr)

    def test_aliases_and_exact_bucket_fields_only(self):
        base = {'v': 1, 'f': {'one': {'s': 2, 'm': 'p', 'b': audit.BUCKET, 'p': OLD},
                              'alias': {'s': 2, 'm': 'p', 'b': audit.BUCKET, 'pd': OLD}}}
        changed, count = redirects.rewrite(base, {OLD: NEW})
        self.assertEqual(count, 2)
        self.assertEqual(changed['f']['one']['p'], NEW)
        self.assertEqual(changed['f']['alias']['pd'], NEW)
        base['f']['other'] = {'b': audit.ASSETS, 'p': OLD}
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            redirects.rewrite(base, {OLD: NEW})

    def test_unknown_or_forged_redirect_is_rejected(self):
        for mapping in ({'bad': NEW}, {OLD: 'objects/cc/' + 'a' * 64 + '/' + 'c' * 16 + '/document.pdf'},
                        {OLD: '../outside'}, {OLD: NEW, 'derived/x/' + 'd' * 32 + '/document.pdf': NEW}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                redirects.validated({redirects.FIELD: mapping})

    def test_pointer_race_or_changed_inputs_never_overwrite_foreign_data(self):
        plan = self.prepare()
        original = self.store.put_if_match
        foreign = b'{"concurrent":true}'
        def race(bucket, path, raw, etag):
            self.store.objects[audit.ASSETS, publication.POINTER] = foreign
            return original(bucket, path, raw, etag)
        self.store.put_if_match = race
        with self.assertRaisesRegex(RuntimeError, 'changed during conditional write'):
            atomic.apply(self.store, self.api, plan)
        self.assertEqual(self.store.objects[audit.ASSETS, publication.POINTER], foreign)
        self.assertEqual(self.store.objects[audit.BUCKET, audit.INDEX], audit.encode(fixture()[0]))

    def test_interrupted_staging_can_retry_same_plan_without_pointer_change(self):
        plan = self.prepare()
        original = self.store.immutable_put
        count = 0
        def fail_after_first(bucket, path, raw):
            nonlocal count
            count += 1
            original(bucket, path, raw)
            if count == 2:
                raise OSError('staging response lost')
        self.store.immutable_put = fail_after_first
        with self.assertRaisesRegex(OSError, 'response lost'):
            atomic.apply(self.store, self.api, plan)
        self.assertEqual(publication.current(self.store)[0], plan['old_pointer'])
        self.store.immutable_put = original
        result = atomic.apply(self.store, self.api, plan)
        self.assertEqual(publication.current(self.store)[0], plan['pointer'])
        self.assertTrue(result['applied'])

    def test_concurrent_input_change_stops_before_pointer(self):
        plan = self.prepare()
        before = publication.current(self.store)[0]
        self.store.objects[audit.ASSETS, audit.SIDECAR] = b'foreign'
        with self.assertRaisesRegex(ValueError, 'sidecar changed'):
            atomic.apply(self.store, self.api, plan)
        self.assertEqual(publication.current(self.store)[0], before)
        self.assertEqual(self.store.writes, [])

    def test_lost_pointer_response_can_be_resolved_by_exact_generation(self):
        plan = self.prepare()
        put = self.store.put_if_match
        def lost(bucket, path, raw, etag):
            put(bucket, path, raw, etag)
            raise OSError('response lost')
        self.store.put_if_match = lost
        with self.assertRaisesRegex(OSError, 'response lost'):
            atomic.apply(self.store, self.api, plan)
        pointer, catalog = publication.current(self.store)
        self.assertEqual(pointer, plan['pointer'])
        self.assertEqual(catalog[redirects.FIELD], {OLD: NEW})
        self.assertTrue(publication.verify_generation(self.store, Consumers(self.store), pointer['generation'])['complete'])

    def test_missing_copy_after_pointer_write_fails_before_reporting_success(self):
        plan = self.prepare()
        put = self.store.put_if_match
        def delete_after_pointer(bucket, path, raw, etag):
            put(bucket, path, raw, etag)
            del self.api.files[NEW]
        self.store.put_if_match = delete_after_pointer
        with self.assertRaisesRegex(ValueError, 'PDF object changed'):
            atomic.apply(self.store, self.api, plan)
        self.assertEqual(publication.current(self.store)[0], plan['pointer'])
        self.assertIn(OLD, self.api.files)

    def test_other_publication_cannot_drop_or_repoint_existing_redirect(self):
        atomic.apply(self.store, self.api, self.prepare())
        pointer, catalog = publication.current(self.store)
        for mapping in ({}, {OLD: 'objects/aa/' + 'a' * 64 + '/' + 'd' * 16 + '/document.pdf'}):
            candidate = {**catalog, redirects.FIELD: mapping, 'parent_generation': pointer['generation']}
            candidate.pop('generation')
            with self.subTest(mapping=mapping), self.assertRaisesRegex(ValueError, 'preserve'):
                publication.commit_catalog(self.store, candidate, pointer, apply=True)

    def test_route_change_requires_audited_refresh_with_active_redirects(self):
        result = atomic.apply(self.store, self.api, self.prepare())
        self.store.writes.clear()
        options = {'repo': 'VoiceOfML/Test', 'path': '目录/书.caj'}
        for action in ('remove', 'restore'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'audited base refresh'):
                control.change(self.store, options, result['generation'], action=action, apply=True)
        self.assertEqual(self.store.writes, [])


if __name__ == '__main__':
    unittest.main()
