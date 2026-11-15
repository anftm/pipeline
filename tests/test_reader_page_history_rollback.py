import copy
import gzip
import unittest

from scripts import publish_reader_v3 as publication
from scripts import reader_page_history_inventory as inventory
from scripts import reader_page_history_replacement as replacement
from scripts import reader_page_history_rollback as rollback
from tests.test_reader_page_audit import OLD, NEW
from tests import test_reader_page_history_replacement as fixtures


class ReadOnlyStore:
    def __init__(self, objects):
        self.objects = objects
        self.reads = []

    def read_bytes(self, bucket, path):
        self.reads.append((bucket, path))
        try:
            return self.objects[bucket, path]
        except KeyError:
            raise FileNotFoundError(path) from None


class HistoryRollbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HistoryReplacementTests()
        self.fixture.setUp()
        original, promotion = self.fixture.history()
        self.original = original['generation']
        self.requested = promotion['resources'][0]
        plan = replacement.prepare(self.fixture.store, {OLD: NEW})
        item = plan['replacements'][0]
        self.catalog = item['catalog']
        self.ref = item['catalog_ref']
        self.objects = self.fixture.store.objects
        self.objects[publication.ASSETS, self.ref['path']] = item['catalog_raw']
        self.objects[publication.ASSETS, item['snapshot_ref']['path']] = item['packed']
        self.state = {'version': 1, 'kind': 'reader-v3-retirement',
                      'retired_paths': [self.requested['path']], 'resources': [self.ref],
                      'rollback_replacements': {self.original: self.ref}}
        self.store = ReadOnlyStore(self.objects)
        self.save_state()

    def save_state(self):
        self.objects[publication.ASSETS, inventory.RETIREMENT] = publication.encode(self.state)

    def install_catalog(self, catalog, *, path=None):
        catalog = copy.deepcopy(catalog)
        catalog.pop('generation', None)
        catalog['generation'] = publication.text.digest(catalog)
        path = path or f"{replacement.PREFIX}{self.original}/{catalog['generation']}/catalog.json"
        raw = publication.encode(catalog)
        ref = publication.metadata(publication.ASSETS, path, raw)
        self.objects[publication.ASSETS, path] = raw
        self.state['rollback_replacements'][self.original] = ref
        self.save_state()
        return ref

    def test_deleted_original_resolves_and_verifies_without_writes(self):
        del self.objects[publication.ASSETS, self.requested['path']]
        before = copy.deepcopy(self.objects)
        self.assertEqual(rollback.resolve_catalog_ref(self.store, self.requested,
            mapping={OLD: NEW}, require_alias=True), self.ref)
        report = rollback.verify(self.store, self.requested, mapping={OLD: NEW}, require_alias=True)
        self.assertTrue(report['verified'])
        self.assertTrue(report['alias_used'])
        self.assertTrue(report['resolved'])
        self.assertEqual(report['rollback_target'], self.original)
        self.assertFalse(report['applied'])
        self.assertFalse(report['publication_authorized'])
        self.assertNotIn((publication.ASSETS, self.requested['path']), self.store.reads)
        self.assertEqual(self.objects, before)

    def test_already_replacement_reference_is_validated(self):
        report = rollback.verify(self.store, self.ref, mapping={OLD: NEW})
        self.assertEqual(report['catalog_ref'], self.ref)
        self.assertFalse(report['resolved'])
        self.assertTrue(report['alias_used'])
        altered = {**self.ref, 'sha256': 'f' * 64}
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            rollback.resolve_catalog_ref(self.store, altered)

    def test_nonretired_reference_unchanged_even_with_unused_alias(self):
        self.state['retired_paths'] = []
        self.save_state()
        result = rollback.resolve_catalog_ref(self.store, self.requested,
            mapping={OLD: NEW}, require_alias=True)
        self.assertEqual(result, self.requested)
        self.assertEqual(self.store.reads, [(publication.ASSETS, inventory.RETIREMENT)])
        result['sha256'] = 'f' * 64
        self.assertNotEqual(result, self.requested)
        # An unrelated ordinary catalog is also unchanged.
        _, promotion = self.fixture.history(metadata={'unrelated': True})
        ref = promotion['resources'][0]
        self.assertEqual(rollback.resolve_catalog_ref(self.store, ref), ref)

    def test_ordinary_retirement_does_not_invent_alias(self):
        self.state.pop('rollback_replacements')
        self.save_state()
        self.assertEqual(rollback.resolve_catalog_ref(self.store, self.requested), self.requested)
        with self.assertRaisesRegex(ValueError, 'requires a rollback alias'):
            rollback.resolve_catalog_ref(self.store, self.requested, require_alias=True)
        del self.objects[publication.ASSETS, inventory.RETIREMENT]
        self.assertEqual(rollback.resolve_catalog_ref(self.store, self.requested), self.requested)

    def test_corrupt_or_missing_catalog_and_base_reject(self):
        for ref in (self.ref, self.catalog['base_sidecar']):
            key = ref['bucket'], ref['path']
            raw = self.objects[key]
            for corrupt in (b'corrupt', None):
                with self.subTest(path=ref['path'], corrupt=corrupt):
                    if corrupt is None:
                        self.objects.pop(key, None)
                    else:
                        self.objects[key] = corrupt
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        rollback.resolve_catalog_ref(self.store, self.requested)
            self.objects[key] = raw

    def test_wrong_or_missing_rollback_target_rejects_with_valid_hash(self):
        for target in ('f' * 64, None):
            with self.subTest(target=target):
                catalog = {**self.catalog, 'rollback_target': target}
                self.install_catalog(catalog)
                with self.assertRaisesRegex(ValueError, 'rollback_target'):
                    rollback.verify(self.store, self.requested)

    def test_path_generation_and_catalog_generation_are_checked(self):
        self.install_catalog(self.catalog,
            path=f"{replacement.PREFIX}{self.original}/{'f' * 64}/catalog.json")
        with self.assertRaisesRegex(ValueError, 'path/generation'):
            rollback.verify(self.store, self.requested)
        bad = {**self.catalog, 'generation': 'f' * 64}
        raw = publication.encode(bad)
        self.objects[publication.ASSETS, self.ref['path']] = raw
        self.state['rollback_replacements'][self.original] = publication.metadata(
            publication.ASSETS, self.ref['path'], raw)
        self.save_state()
        with self.assertRaisesRegex(ValueError, 'generation mismatch'):
            rollback.verify(self.store, self.requested)

    def test_selected_bundle_in_catalog_or_base_rejects(self):
        for location in ('catalog', 'base', 'key'):
            with self.subTest(location=location):
                catalog = copy.deepcopy(self.catalog)
                path = OLD.rsplit('/', 1)[0] + '/derived-manifest.json'
                if location == 'catalog':
                    catalog['unknown_metadata'] = path
                else:
                    base = {'v': 1, 'f': {}, 'unknown_metadata':
                            {path: 'value'} if location == 'key' else path}
                    raw = gzip.compress(publication.encode(base), mtime=0)
                    digest = publication.metadata('', '', raw)['sha256']
                    ref = publication.metadata(publication.ASSETS,
                        f'reader-index/snapshots/{digest}/reader_assets.json.gz', raw)
                    self.objects[ref['bucket'], ref['path']] = raw
                    catalog['base_sidecar'] = ref
                self.install_catalog(catalog)
                with self.assertRaisesRegex(ValueError, 'selected old PDF bundle'):
                    rollback.verify(self.store, self.requested, mapping={OLD: NEW})

    def test_cycles_multihop_and_shared_targets_reject_without_reading_target(self):
        initial = copy.deepcopy(self.state)
        for mode in ('self', 'retired', 'chain', 'shared'):
            with self.subTest(mode=mode):
                self.state = copy.deepcopy(initial)
                if mode == 'self':
                    self.state['rollback_replacements'][self.original] = self.requested
                elif mode == 'retired':
                    self.state['retired_paths'].append(self.ref['path'])
                else:
                    key = self.catalog['generation'] if mode == 'chain' else 'f' * 64
                    self.state['rollback_replacements'][key] = self.requested if mode == 'chain' else self.ref
                self.save_state()
                self.store.reads.clear()
                with self.assertRaisesRegex(ValueError, 'cyclic|multi-hop|ambiguous'):
                    rollback.verify(self.store, self.requested)
                self.assertEqual(self.store.reads, [(publication.ASSETS, inventory.RETIREMENT)])

    def test_composed_chm_pdf_keeps_original_target_and_suppression(self):
        fixture = fixtures.HistoryReplacementTests()
        fixture.setUp()
        original, chm, _ = fixture.composed_history()
        plan = replacement.prepare(fixture.store, {OLD: NEW})
        item = next(item for item in plan['replacements'] if item['old_generation'] == chm['generation'])
        objects = fixture.store.objects
        for ref, raw in ((item['catalog_ref'], item['catalog_raw']), (item['snapshot_ref'], item['packed'])):
            objects[ref['bucket'], ref['path']] = raw
        original_path = inventory.GENERATIONS + original['generation'] + '/catalog.json'
        original_ref = publication.metadata(publication.ASSETS, original_path,
                                            objects.pop((publication.ASSETS, original_path)))
        objects[publication.ASSETS, inventory.RETIREMENT] = publication.encode({
            'version': 1, 'kind': 'reader-v3-retirement', 'retired_paths': [original_path],
            'rollback_replacements': plan['rollback_replacements']})
        store = ReadOnlyStore(objects)
        report = rollback.verify(store, original_ref, mapping={OLD: NEW}, require_alias=True)
        self.assertEqual(report['catalog_ref'], item['catalog_ref'])
        self.assertEqual(report['rollback_target'], original['generation'])
        self.assertNotEqual(report['rollback_target'], chm['generation'])
        base = publication.pinned_base(store, item['catalog'], None)
        self.assertNotIn('VoiceOfML/Test\0unsafe.chm', base['f'])
        self.assertIn('safe.epub', base['f'])

    def test_chm_standard_alias_target_is_supported(self):
        ref = self.install_catalog(self.catalog,
            path=inventory.GENERATIONS + self.catalog['generation'] + '/catalog.json')
        self.assertEqual(rollback.resolve_catalog_ref(self.store, self.requested), ref)
        self.assertEqual(rollback.verify(self.store, ref)['rollback_target'], self.original)

    def test_unregistered_replacement_and_unretired_alias_original_reject(self):
        self.state['rollback_replacements'] = {}
        self.save_state()
        with self.assertRaisesRegex(ValueError, 'requires a rollback alias'):
            rollback.verify(self.store, self.ref)
        self.state['rollback_replacements'] = {self.original: self.ref}
        self.state['retired_paths'] = []
        self.save_state()
        with self.assertRaisesRegex(ValueError, 'original is not retired'):
            rollback.verify(self.store, self.ref)

    def test_missing_pinned_base_rejects(self):
        catalog = copy.deepcopy(self.catalog)
        del catalog['base_sidecar']
        self.install_catalog(catalog)
        with self.assertRaisesRegex(ValueError, 'requires its own pinned base'):
            rollback.verify(self.store, self.requested)

    def test_unrelated_bundle_is_preserved(self):
        catalog = {**self.catalog, 'metadata': OLD.rsplit('/', 1)[0] + '-sibling/document.pdf'}
        ref = self.install_catalog(catalog)
        self.assertEqual(rollback.resolve_catalog_ref(self.store, self.requested,
            mapping={OLD: NEW}), ref)

    def test_malformed_retirement_and_out_of_scope_target_reject(self):
        initial = copy.deepcopy(self.state)
        for change in ({'kind': 'other'}, {'retired_paths': [None]},
                       {'rollback_replacements': []}, {'rollback_replacements': {'invalid': self.ref}}):
            with self.subTest(change=change):
                self.state = {**initial, **change}
                self.save_state()
                with self.assertRaises(ValueError):
                    rollback.resolve_catalog_ref(self.store, self.requested)
        for change in ({'bucket': 'other'}, {'path': 'reader-index/v3/current.json'},
                       {'bytes': 0}, {'sha256': 'invalid'}):
            with self.subTest(change=change):
                self.state = {**initial, 'rollback_replacements': {
                    self.original: {**self.ref, **change}}}
                self.save_state()
                with self.assertRaises(ValueError):
                    rollback.resolve_catalog_ref(self.store, self.requested)

    def test_verify_unchanged_catalog_still_reads_and_checks_base(self):
        self.state['retired_paths'] = []
        self.save_state()
        report = rollback.verify(self.store, self.requested)
        self.assertTrue(report['verified'])
        self.assertFalse(report['alias_used'])
        with self.assertRaisesRegex(ValueError, 'selected old PDF bundle'):
            rollback.verify(self.store, self.requested, mapping={OLD: NEW})


if __name__ == '__main__':
    unittest.main()
