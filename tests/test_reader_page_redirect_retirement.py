import copy
import gzip
import json
import unittest
from unittest.mock import patch

from scripts import reader_page_atomic as atomic, reader_page_audit as audit
from scripts import reader_page_migrate as migration, reader_page_redirects as redirects
from scripts import reader_page_redirect_retirement as retirement
from scripts import reader_lifecycle as lifecycle, publish_reader_v3 as publication
from scripts import reader_route_control as control
from tests.test_reader_page_audit import fixture, KEY, OLD, NEW
from tests.test_reader_page_atomic import AtomicStore
from tests.test_reader_page_migrate import MemoryApi


class RedirectRetirementTests(unittest.TestCase):
    def setUp(self):
        index, derived, objects, manifests, sidecar = fixture()
        self.store, self.api = AtomicStore(), MemoryApi(derived, objects)
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode(sidecar), mtime=0)
        for path, raw in manifests.items():
            self.store.objects[audit.BUCKET, path] = raw
        legacy, _ = lifecycle.catalog_generation(sidecar)
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(legacy)
        publication.unify(self.store, apply=True)
        plan = atomic.prepare(self.store, self.api,
            expected_index=retirement._sha(self.store.read_bytes(audit.BUCKET, audit.INDEX)),
            expected_sidecar=retirement._sha(self.store.read_bytes(audit.ASSETS, audit.SIDECAR)),
            expected_v3=publication.current(self.store)[0]['generation'])
        atomic.apply(self.store, self.api, plan)
        # Model completed canonical reconciliation, retaining the old history.
        index['files'][0]['new_path'] = NEW
        index['files'][0]['manifest_path'] = NEW.rsplit('/', 1)[0] + '/derived-manifest.json'
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        sidecar['f'][KEY]['p'] = NEW
        packed = gzip.compress(audit.encode(sidecar), mtime=0)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = packed
        legacy, generation = lifecycle.catalog_generation(sidecar, legacy)
        snapshot_path = f'reader-index/generations/{generation}/reader_assets.json.gz'
        legacy['generations'][generation]['snapshot'] = publication.metadata(audit.ASSETS, snapshot_path, packed)
        self.store.objects[audit.ASSETS, snapshot_path] = packed
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(legacy)
        self.args = self.identities()
        self.store.writes.clear()

    def identities(self):
        return dict(expected_index=retirement._sha(self.store.read_bytes(audit.BUCKET, audit.INDEX)),
                    expected_sidecar=retirement._sha(self.store.read_bytes(audit.ASSETS, audit.SIDECAR)),
                    expected_catalog=retirement._sha(self.store.read_bytes(audit.ASSETS, migration.CATALOG)),
                    expected_generation=publication.current(self.store)[0]['generation'])

    def prepare(self):
        return retirement.prepare(self.store, self.api, **self.args)

    def test_only_new_catalog_history_and_pointer_no_deletion_or_acks(self):
        before = copy.deepcopy(self.store.objects)
        old_pointer, old_catalog = publication.current(self.store)
        plan = self.prepare()
        self.assertEqual(self.store.objects, before)
        self.assertFalse(retirement.report(plan, applied=False)['current_generation_redirect_protection_ended'])
        expected = copy.deepcopy(old_catalog)
        del expected[redirects.FIELD]
        expected.update(parent_generation=old_pointer['generation'], generation=plan['pointer']['generation'])
        self.assertEqual(plan['catalog'], expected)
        result = retirement.apply(self.store, self.api, plan)
        pointer, catalog = publication.current(self.store)
        self.assertEqual(pointer, plan['pointer'])
        self.assertNotIn(redirects.FIELD, catalog)
        self.assertEqual(catalog['base_sidecar'], old_catalog['base_sidecar'])
        self.assertEqual(retirement._projection(publication.pinned_base(self.store, catalog, {}), catalog),
                         retirement._projection(publication.pinned_base(self.store, old_catalog, {}), old_catalog))
        self.assertTrue(result['current_generation_redirect_protection_ended'])
        self.assertTrue(result['historical_generations_require_retirement'])
        for flag in ('objects_deleted', 'deletion_authorized', 'consumer_acks_created', 'canonical_indexes_changed'):
            self.assertFalse(result[flag])
        for key, value in before.items():
            if key != (audit.ASSETS, publication.POINTER):
                self.assertEqual(self.store.objects[key], value, key)
        self.assertEqual([key for key in self.store.writes if key in before], [(audit.ASSETS, publication.POINTER)])
        history = json.loads(self.store.read_bytes(audit.ASSETS,
            f"reader-index/v3/promotions/{pointer['generation']}.json"))
        self.assertEqual(history['acks'], {'hf': False, 'pages': False})
        # General publication still cannot remove redirects.
        with self.assertRaisesRegex(ValueError, 'preserve'):
            redirects.preserve(old_catalog, catalog)

    def test_suppression_overrides_and_extra_catalog_fields_are_preserved(self):
        pointer, catalog = publication.current(self.store)
        catalog['suppressed_sources'] = [KEY]
        other_key = 'VoiceOfML/Test\0other.pdf'
        catalog['files'][other_key] = {
            'resource': publication.metadata(audit.BUCKET,
                NEW.rsplit('/', 1)[0] + '/reading-manifest.json', b'fixture'),
            'source_sha256': 'a' * 64, 'reading_generation': 'e' * 64}
        catalog['extra_metadata'] = {'label': 'retained'}
        packed, ref = control.prepare_base(catalog, self.store.read_bytes(audit.ASSETS, audit.SIDECAR))
        catalog['base_sidecar'] = ref
        catalog.pop('generation')
        catalog['generation'] = publication.text.digest(catalog)
        raw = publication.encode(catalog)
        catalog_ref = publication.metadata(audit.ASSETS,
            f"reader-index/v3/generations/{catalog['generation']}/catalog.json", raw)
        self.store.objects[audit.ASSETS, ref['path']] = packed
        self.store.objects[audit.ASSETS, catalog_ref['path']] = raw
        self.store.objects[audit.ASSETS, publication.POINTER] = publication.encode(
            {**pointer, 'generation': catalog['generation'], 'catalog': catalog_ref, 'resources': [catalog_ref]})
        self.args = self.identities()
        plan = self.prepare()
        retirement.apply(self.store, self.api, plan)
        _, actual = publication.current(self.store)
        for field in ('suppressed_sources', 'files', 'extra_metadata', 'base_sidecar'):
            self.assertEqual(actual[field], catalog[field])
        projected = publication.project_sidecar(publication.pinned_base(self.store, actual, {}), actual)
        self.assertNotIn(KEY, projected['f'])
        self.assertEqual(projected['f'][other_key]['p'], catalog['files'][other_key]['resource']['path'])

    def test_expected_identities_are_required_and_exact(self):
        for field in self.args:
            for value in ('', 'f' * 64):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    retirement.prepare(self.store, self.api, **{**self.args, field: value})
        self.assertEqual(self.store.writes, [])

    def test_unmigrated_index_and_selected_bundle_references_block(self):
        original = copy.deepcopy(self.store.objects)
        for root in ('index', 'sidecar', 'legacy', 'index-unknown'):
            with self.subTest(root=root):
                self.store.objects = copy.deepcopy(original)
                if root == 'index':
                    self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(fixture()[0])
                elif root == 'index-unknown':
                    index = json.loads(self.store.read_bytes(audit.BUCKET, audit.INDEX))
                    index['unexpected'] = {'resource': OLD.rsplit('/', 1)[0] + '/derived-manifest.json'}
                    self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
                elif root == 'sidecar':
                    self.store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode(fixture()[4]), mtime=0)
                else:
                    legacy = json.loads(self.store.read_bytes(audit.ASSETS, migration.CATALOG))
                    legacy['generations'][legacy['current']['generation']]['references'].append(
                        {'bucket': audit.BUCKET, 'path': OLD})
                    self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(legacy)
                with self.assertRaises(ValueError):
                    retirement.prepare(self.store, self.api, **self.identities())
        self.assertEqual(self.store.writes, [])

    def test_seven_unrelated_previous_pdfs_and_other_derived_references_survive(self):
        old_root = OLD.rsplit('/', 1)[0]
        # Include a similarly prefixed sibling to enforce a directory boundary.
        unrelated = [old_root + '-other/document.pdf'] + [
            'derived/unindexed/' + f'{i:032x}' + '/document.pdf' for i in range(6)]
        index = json.loads(self.store.read_bytes(audit.BUCKET, audit.INDEX))
        template = copy.deepcopy(index['files'][0])
        for i, path in enumerate(unrelated):
            entry = index['files'][0] if i == 0 else copy.deepcopy(template)
            if i:
                entry['key'] = f'VoiceOfML/Test\0unrelated-{i}.pdf'
                entry['new_path'] = NEW.replace('/' + 'c' * 16 + '/', '/' + f'{i:016x}' + '/')
                entry['manifest_path'] = entry['new_path'].rsplit('/', 1)[0] + '/derived-manifest.json'
                index['files'].append(entry)
            entry['previous_pdf'] = {'bucket': audit.BUCKET, 'path': path}
            self.api.files[path] = {'size': 55, 'xet_hash': f'unindexed-{i}'}
        self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        base = audit.decode_sidecar(self.store.read_bytes(audit.ASSETS, audit.SIDECAR))
        base['f']['VoiceOfML/Test\0unrelated-route.pdf'] = {'s': 2, 'm': 'p', 'b': audit.BUCKET, 'p': unrelated[0]}
        packed = gzip.compress(audit.encode(base), mtime=0)
        self.store.objects[audit.ASSETS, audit.SIDECAR] = packed
        legacy = json.loads(self.store.read_bytes(audit.ASSETS, migration.CATALOG))
        legacy, generation = lifecycle.catalog_generation(base, legacy)
        legacy_path = f'reader-index/generations/{generation}/reader_assets.json.gz'
        legacy['generations'][generation]['snapshot'] = publication.metadata(audit.ASSETS, legacy_path, packed)
        self.store.objects[audit.ASSETS, legacy_path] = packed
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(legacy)
        pointer, catalog = publication.current(self.store)
        packed, ref = control.prepare_base(catalog, packed)
        catalog['base_sidecar'] = ref
        catalog['extra_metadata'] = {'previous_pdf': {'path': unrelated[-1]}}
        catalog.pop('generation')
        catalog['generation'] = publication.text.digest(catalog)
        raw = publication.encode(catalog)
        catalog_ref = publication.metadata(audit.ASSETS,
            f"reader-index/v3/generations/{catalog['generation']}/catalog.json", raw)
        self.store.objects[audit.ASSETS, ref['path']] = packed
        self.store.objects[audit.ASSETS, catalog_ref['path']] = raw
        self.store.objects[audit.ASSETS, publication.POINTER] = publication.encode(
            {**pointer, 'generation': catalog['generation'], 'catalog': catalog_ref, 'resources': [catalog_ref]})
        self.args = self.identities()
        before = copy.deepcopy(self.store.objects)
        plan = self.prepare()
        self.assertEqual(plan['audit']['unindexed_legacy_pdfs'], sorted(unrelated))
        self.assertEqual(plan['finalized_redirects'], 1)
        self.assertEqual(plan['finalized_old_bundles'], [old_root])
        self.assertEqual(self.store.objects, before)
        preview = retirement.report(plan, applied=False)
        result = retirement.apply(self.store, self.api, plan)
        for report in (preview, result):
            self.assertEqual(report['finalization_scope'], 'active-redirect-old-bundles')
            self.assertEqual(report['finalized_redirects'], 1)
            self.assertEqual(report['finalized_old_bundles'], [old_root])
            self.assertTrue(report['unrelated_derived_references_preserved'])
            self.assertFalse(report['deletion_authorized'])
        for key, value in before.items():
            if key != (audit.ASSETS, publication.POINTER):
                self.assertEqual(self.store.objects[key], value, key)
        _, actual = publication.current(self.store)
        self.assertEqual(actual['extra_metadata'], catalog['extra_metadata'])
        self.assertEqual(actual['base_sidecar'], catalog['base_sidecar'])
        self.assertEqual(publication.pinned_base(self.store, actual, {}), base)
        self.assertTrue(all(path in self.api.files for path in unrelated))

    def test_selected_old_bundle_root_manifest_and_other_files_block(self):
        original = self.store.read_bytes(audit.BUCKET, audit.INDEX)
        root = OLD.rsplit('/', 1)[0]
        for path in (root, OLD, root + '/derived-manifest.json', root + '/pages/page-000001.png'):
            with self.subTest(path=path):
                index = json.loads(original)
                index['files'][0]['previous_pdf'] = {'bucket': audit.BUCKET, 'path': path}
                self.store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
                with self.assertRaisesRegex(ValueError, 'canonical PDF index still references selected old derived bundle'):
                    retirement.prepare(self.store, self.api, **self.identities())
        self.assertEqual(self.store.writes, [])

    def test_pending_legacy_reconciliation_blocks(self):
        legacy = json.loads(self.store.read_bytes(audit.ASSETS, migration.CATALOG))
        legacy['redirect_sidecar_pending'] = {}
        self.store.objects[audit.ASSETS, migration.CATALOG] = audit.encode(legacy)
        with self.assertRaisesRegex(ValueError, 'pending'):
            retirement.prepare(self.store, self.api, **self.identities())
        self.assertEqual(self.store.writes, [])

    def test_all_targets_and_copies_are_required(self):
        for mode in ('missing', 'changed', 'manifest'):
            with self.subTest(mode=mode):
                files, stored = copy.deepcopy(self.api.files), copy.deepcopy(self.store.objects)
                if mode == 'missing':
                    del self.api.files[NEW]
                elif mode == 'changed':
                    self.api.files[OLD]['xet_hash'] = 'foreign'
                else:
                    self.store.objects[audit.BUCKET, NEW.rsplit('/', 1)[0] + '/derived-manifest.json'] = b'{}'
                with self.assertRaises(ValueError):
                    self.prepare()
                self.api.files, self.store.objects = files, stored
        self.assertEqual(self.store.writes, [])

    def test_modified_plan_rejected_before_any_write(self):
        plan = self.prepare()
        for change in ('candidate', 'audit', 'pointer', 'evidence'):
            altered = copy.deepcopy(plan)
            if change == 'candidate':
                altered['catalog']['files']['foreign'] = {}
            elif change == 'audit':
                altered['audit']['planned'] = []
            elif change == 'pointer':
                altered['pointer']['catalog']['path'] = plan['old_pointer']['catalog']['path']
            else:
                altered['evidence_resources'] = []
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'fresh evidence'):
                retirement.apply(self.store, self.api, altered)
        self.assertEqual(self.store.writes, [])

    def test_full_projection_including_top_level_metadata_is_checked(self):
        project = publication.project_sidecar
        def changed(base, catalog):
            value = project(base, catalog)
            if redirects.FIELD not in catalog:
                value['extra'] = 'changed'
            return value
        with patch.object(publication, 'project_sidecar', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'full Reader projection'):
                self.prepare()
        self.assertEqual(self.store.writes, [])

    def test_root_or_pinned_change_during_staging_blocks_cas(self):
        original = copy.deepcopy(self.store.objects)
        put = self.store.immutable_put
        for root in ('legacy', 'pinned'):
            with self.subTest(root=root):
                self.store.objects = copy.deepcopy(original)
                plan = self.prepare()
                def change(bucket, path, raw):
                    put(bucket, path, raw)
                    if path == plan['pointer']['catalog']['path']:
                        target = migration.CATALOG if root == 'legacy' else plan['snapshot_ref']['path']
                        self.store.objects[audit.ASSETS, target] = b'foreign'
                self.store.immutable_put = change
                with self.assertRaises(ValueError):
                    retirement.apply(self.store, self.api, plan)
                self.assertEqual(publication.current(self.store)[0], plan['old_pointer'])
                self.store.immutable_put = put

    def test_conflicting_staged_catalog_or_promotion_never_overwritten(self):
        before = copy.deepcopy(self.store.objects)
        for conflict in ('catalog', 'promotion'):
            with self.subTest(conflict=conflict):
                self.store.objects = copy.deepcopy(before)
                plan = self.prepare()
                if conflict == 'catalog':
                    path = plan['pointer']['catalog']['path']
                    raw = b'{"foreign":true}'
                else:
                    path = f"reader-index/v3/promotions/{plan['pointer']['generation']}.json"
                    raw = publication.encode({'acks': {'hf': True, 'pages': True}})
                self.store.objects[audit.ASSETS, path] = raw
                with self.assertRaises(ValueError):
                    retirement.apply(self.store, self.api, plan)
                self.assertEqual(self.store.objects[audit.ASSETS, path], raw)
                self.assertEqual(publication.current(self.store)[0], plan['old_pointer'])

    def test_copy_or_legacy_snapshot_changes_after_prepare_prevent_staging(self):
        plan = self.prepare()
        self.api.files[NEW]['size'] += 1
        with self.assertRaises(ValueError):
            retirement.apply(self.store, self.api, plan)
        self.api.files[NEW]['size'] -= 1
        ref = plan['evidence_resources'][1]
        self.store.objects[ref['bucket'], ref['path']] = gzip.compress(audit.encode({'v': 1, 'f': {}}), mtime=0)
        with self.assertRaisesRegex(ValueError, 'legacy snapshot differs'):
            retirement.apply(self.store, self.api, plan)
        self.assertEqual(self.store.writes, [])

    def test_cas_conflict_preserves_foreign_pointer(self):
        plan = self.prepare()
        put = self.store.put_if_match
        def race(bucket, path, raw, etag):
            self.store.objects[bucket, path] = b'{"foreign":true}'
            put(bucket, path, raw, etag)
        self.store.put_if_match = race
        with self.assertRaisesRegex(RuntimeError, 'changed during conditional write'):
            retirement.apply(self.store, self.api, plan)
        self.assertEqual(self.store.read_bytes(audit.ASSETS, publication.POINTER), b'{"foreign":true}')

    def test_interrupted_staging_retries_without_overwriting_catalogs(self):
        plan = self.prepare()
        put = self.store.immutable_put
        def interrupted(bucket, path, raw):
            put(bucket, path, raw)
            if path == plan['pointer']['catalog']['path']:
                raise OSError('response lost')
        self.store.immutable_put = interrupted
        with self.assertRaisesRegex(OSError, 'response lost'):
            retirement.apply(self.store, self.api, plan)
        self.assertEqual(publication.current(self.store)[0], plan['old_pointer'])
        self.store.immutable_put = put
        self.assertTrue(retirement.apply(self.store, self.api, plan)['applied'])

    def test_post_cas_copy_loss_is_failure_and_history_is_retained(self):
        plan = self.prepare()
        put = self.store.put_if_match
        def lost(bucket, path, raw, etag):
            put(bucket, path, raw, etag)
            del self.api.files[NEW]
        self.store.put_if_match = lost
        with self.assertRaisesRegex(ValueError, 'PDF object changed'):
            retirement.apply(self.store, self.api, plan)
        self.assertEqual(publication.current(self.store)[0], plan['pointer'])
        self.assertIn((audit.ASSETS, plan['old_pointer']['catalog']['path']), self.store.objects)


if __name__ == '__main__':
    unittest.main()
