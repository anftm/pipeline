import copy
import gzip
import unittest
from unittest.mock import patch

from scripts import publish_reader_v3 as publication, reader_page_audit as audit
from scripts import reader_page_history_inventory as inventory
from scripts import reader_page_migrate as migration
from tests.test_reader_page_audit import fixture, KEY, OLD, NEW


class ReadOnlyStore:
    """Deliberately offers no write, delete, API or publication methods."""
    def __init__(self):
        self.objects = {}

    def read_bytes(self, bucket, path):
        try:
            return self.objects[bucket, path]
        except KeyError:
            raise FileNotFoundError(path) from None

    def list_files(self, bucket, prefixes):
        return [path for b, path in self.objects if b == bucket and path.startswith(prefixes)]


class HistoryInventoryTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadOnlyStore()
        self.base = fixture()[-1]
        self.put(migration.CATALOG, {'version': 1, 'kind': 'reader-catalog',
                                    'current': None, 'generations': {}})

    def put(self, path, value):
        raw = publication.encode(value)
        self.store.objects[audit.ASSETS, path] = raw
        return publication.metadata(audit.ASSETS, path, raw)

    def history(self, *, base=None, promotion=False, **fields):
        packed = gzip.compress(publication.encode(self.base if base is None else base), mtime=0)
        digest = publication.metadata(audit.ASSETS, '', packed)['sha256']
        path = f'reader-index/snapshots/{digest}/reader_assets.json.gz'
        self.store.objects[audit.ASSETS, path] = packed
        catalog = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
                   'base_sidecar': publication.metadata(audit.ASSETS, path, packed), **fields}
        catalog['generation'] = publication.text.digest(catalog)
        ref = self.put(inventory.GENERATIONS + catalog['generation'] + '/catalog.json', catalog)
        if promotion:
            self.put(inventory.PROMOTIONS + catalog['generation'] + '.json', {
                'version': 1, 'kind': 'reader-reading-promotion',
                'generation': catalog['generation'], 'resources': [ref]})
        return catalog, ref

    def retirement(self, resources=None, aliases=None):
        return self.put(inventory.RETIREMENT, {
            'version': 1, 'kind': 'reader-v3-retirement', 'retired_paths': [],
            'resources': resources or [], 'rollback_replacements': aliases or {}})

    def scan(self):
        before = copy.deepcopy(self.store.objects)
        result = inventory.inventory(self.store, {OLD: NEW})
        self.assertEqual(self.store.objects, before)
        for flag in ('graph_complete', 'fully_unreferenced', 'apply_allowed', 'deletion_authorized'):
            self.assertIs(result[flag], False)
        self.assertEqual(result['remote_mutations'], 0)
        return result

    def test_alias_only_history_discovered_with_no_promotion_or_catalog_listing(self):
        original = 'a' * 64
        catalog, ref = self.history(rollback_target=original)
        journal = self.retirement(aliases={original: ref})
        with patch.object(self.store, 'list_files', return_value=[]):
            result = self.scan()
        self.assertEqual(result['blockers'], [])
        self.assertTrue(result['scope_complete'])
        self.assertEqual(len(result['histories']), 1)
        history = result['histories'][0]
        self.assertEqual(history['classification'], 'needs_rewrite')
        self.assertEqual(history['rollback_target'], original)
        self.assertEqual(history['catalog_ref'], ref)
        self.assertEqual(history['base_sidecar'], catalog['base_sidecar'])
        self.assertEqual(result['aliases_needing_preserve'][0]['reference'], ref)
        self.assertTrue(result['aliases_needing_preserve'][0]['must_preserve'])
        self.assertIn(journal, result['evidence_resources'])
        self.assertIn(catalog['base_sidecar'], result['evidence_resources'])

    def test_sanitized_catalog_discovered_by_listing_without_promotion_or_alias(self):
        _, ref = self.history(rollback_target='a' * 64)
        result = self.scan()
        self.assertEqual(result['blockers'], [])
        self.assertEqual(result['histories'][0]['catalog_ref'], ref)
        self.assertEqual(result['histories'][0]['classification'], 'needs_rewrite')

    def test_duplicate_consistent_references_coalesce_and_retain_all_sources(self):
        catalog, ref = self.history(promotion=True, rollback_target='a' * 64)
        self.retirement(resources=[ref, ref], aliases={'a' * 64: ref})
        result = self.scan()
        self.assertEqual(result, self.scan())
        self.assertEqual(result['blockers'], [])
        self.assertEqual(len(result['histories']), 1)
        self.assertEqual(len(result['histories'][0]['sources']), 5)
        self.assertEqual(result['evidence_resources'].count(ref), 1)
        self.assertEqual(result['evidence_resources'].count(catalog['base_sidecar']), 1)

    def test_digest_and_size_conflicts_block_with_exact_source(self):
        for field, wrong in (('sha256', 'f' * 64), ('bytes', 999)):
            with self.subTest(field=field):
                _, ref = self.history(promotion=True, rollback_target='a' * 64)
                bad = {**ref, field: wrong}
                self.retirement(resources=[ref], aliases={'a' * 64: bad})
                result = self.scan()
                conflict = next(b for b in result['blockers'] if b['code'] == 'digest_conflict')
                self.assertEqual(conflict['source'], inventory.RETIREMENT)
                self.assertEqual(conflict['location'], '/rollback_replacements/' + 'a' * 64)
                self.assertEqual(conflict['reference'], bad)
                self.assertEqual(conflict['previous_reference'], ref)
                self.assertFalse(result['scope_complete'])
                self.assertEqual(result['histories'][0]['classification'], 'blocked')

    def test_legacy_selected_roots_reported_and_unrelated_references_preserved(self):
        sibling = OLD.rsplit('/', 1)[0] + '-sibling/document.pdf'
        refs = [{'bucket': audit.BUCKET, 'path': OLD, 'sha256': 'b' * 64, 'bytes': 123},
                {'bucket': audit.BUCKET, 'path': OLD.rsplit('/', 1)[0] + '/derived-manifest.json'},
                {'bucket': audit.BUCKET, 'path': sibling},
                {'bucket': audit.BUCKET, 'path': 'derived/other/' + 'e' * 32 + '/document.pdf'}]
        entry = {'generation': 'legacy-one', 'status': 'superseded', 'references': refs,
                 'sidecar_sha256': 'c' * 64}
        self.put(migration.CATALOG, {'version': 1, 'kind': 'reader-catalog',
            'current': None, 'generations': {'legacy-one': entry}})
        base = copy.deepcopy(self.base)
        base['f']['unrelated'] = {'s': 2, 'b': audit.BUCKET, 'p': sibling}
        self.history(base=base, metadata={'previous_pdf': refs[-1]})
        result = self.scan()
        self.assertEqual(result['blockers'], [])
        legacy = result['legacy_generations'][0]
        self.assertEqual(legacy['entry'], entry)
        self.assertEqual([hit['reference'] for hit in legacy['selected_root_references']], refs[:2])
        self.assertEqual(len(result['histories'][0]['selected_references']), 1)

    def test_unsupported_selected_reference_has_exact_json_pointer_and_path(self):
        root = OLD.rsplit('/', 1)[0]
        base = copy.deepcopy(self.base)
        base['metadata'] = {'previous_pdf': {'path': root + '/derived-manifest.json'}}
        catalog, _ = self.history(base=base)
        result = self.scan()
        blocker = next(b for b in result['blockers'] if b['code'] == 'unsupported_selected_reference')
        self.assertEqual(blocker, {'code': 'unsupported_selected_reference',
            'source': catalog['base_sidecar']['path'], 'location': '/metadata/previous_pdf/path',
            'path': root + '/derived-manifest.json', 'bundle_root': root})
        self.assertEqual(result['histories'][0]['classification'], 'blocked')

    def test_unknown_ref_and_wrong_bucket_are_blockers(self):
        bad = {'bucket': audit.BUCKET, 'path': OLD, 'sha256': 'a' * 64, 'bytes': 12}
        self.retirement(resources=[bad])
        result = self.scan()
        blocker = next(b for b in result['blockers'] if b['code'] == 'unsupported_reference')
        self.assertEqual(blocker['location'], '/resources/0')
        self.assertEqual(blocker['reference'], bad)
        self.assertFalse(result['scope_complete'])

    def test_alias_identity_and_pinned_checksum_are_verified(self):
        catalog, ref = self.history(rollback_target='b' * 64)
        self.retirement(aliases={'a' * 64: ref})
        self.assertIn('alias_target_mismatch', [b['code'] for b in self.scan()['blockers']])
        self.store.objects[audit.ASSETS, catalog['base_sidecar']['path']] = b'corrupt'
        result = self.scan()
        self.assertTrue(any('sidecar snapshot checksum mismatch' in b.get('error', '')
                            for b in result['blockers']))

    def test_catalog_checksum_is_verified(self):
        _, ref = self.history()
        self.retirement(resources=[ref])
        self.store.objects[audit.ASSETS, ref['path']] += b' '
        with patch.object(self.store, 'list_files', return_value=[]):
            result = self.scan()
        self.assertTrue(any('v3 catalog checksum mismatch' in b.get('error', '')
                            for b in result['blockers']))

    def test_current_catalog_is_identified_and_empty_scope_never_proves_no_refs(self):
        catalog, ref = self.history(base={'v': 1, 'f': {}})
        self.put(publication.POINTER, {'version': 1, 'kind': 'reader-reading-pointer',
            'generation': catalog['generation'], 'catalog': ref, 'resources': [ref]})
        result = self.scan()
        self.assertEqual(result['blockers'], [])
        self.assertTrue(result['histories'][0]['is_current'])
        self.assertEqual(result['histories'][0]['classification'], 'no_selected_references_in_catalog_and_base')
        self.assertTrue(result['scope_complete'])

    def test_selected_redirects_and_wrong_bucket_routes_are_classified(self):
        catalog, ref = self.history(base={'v': 1, 'f': {}}, derived_pdf_redirects={OLD: NEW})
        result = self.scan()
        self.assertEqual(result['blockers'], [])
        self.assertEqual(result['histories'][0]['classification'], 'needs_rewrite')
        base = copy.deepcopy(self.base)
        base['f'][KEY]['b'] = audit.ASSETS
        self.history(base=base)
        result = self.scan()
        self.assertTrue(any(b['code'] == 'unsupported_selected_reference' and b['path'] == OLD
                            for b in result['blockers']))

    def test_evidence_change_blocks_affected_history(self):
        _, ref = self.history()
        read = self.store.read_bytes
        reads = 0

        def changing(bucket, path):
            nonlocal reads
            raw = read(bucket, path)
            if path == ref['path']:
                reads += 1
                if reads > 2:  # listing, verified catalog read, final evidence check
                    return raw + b' '
            return raw

        with patch.object(self.store, 'read_bytes', side_effect=changing):
            result = self.scan()
        self.assertIn('evidence_changed', [b['code'] for b in result['blockers']])
        self.assertEqual(result['histories'][0]['classification'], 'blocked')

    def test_invalid_mapping_rejects_before_reading(self):
        dot_old = 'derived/../' + OLD.split('/')[2] + '/document.pdf'
        for mapping in ({}, {dot_old: NEW}):
            with self.subTest(mapping=mapping), patch.object(self.store, 'read_bytes') as read:
                with self.assertRaises(ValueError):
                    inventory.inventory(self.store, mapping)
                read.assert_not_called()

    def test_missing_legacy_catalog_and_bad_journal_are_blockers(self):
        del self.store.objects[audit.ASSETS, migration.CATALOG]
        self.put(inventory.RETIREMENT, {'version': 1})
        result = self.scan()
        self.assertEqual({b['code'] for b in result['blockers']}, {'missing_document', 'invalid_retirement'})


if __name__ == '__main__':
    unittest.main()
