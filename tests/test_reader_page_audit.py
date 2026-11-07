import copy
import gzip
import json
import unittest
from types import SimpleNamespace

from scripts import reader_page_audit as audit
from tests.test_publish_reader_v3 import Store


SOURCE = 'a' * 64
OLD = 'derived/migration-20261007/' + 'b' * 32 + '/document.pdf'
NEW = 'objects/aa/' + SOURCE + '/' + 'c' * 16 + '/document.pdf'
SECOND = 'objects/aa/' + SOURCE + '/' + 'd' * 16 + '/document.pdf'
KEY = 'VoiceOfML/Test\0目录/书.caj'


def fixture():
    entry = {'key': KEY, 'repo': 'VoiceOfML/Test', 'source_path': '目录/书.caj',
             'source_extension': 'caj', 'profile': 'caj-family-pdf-v1', 'source_revision': '1' * 40,
             'source_sha256': SOURCE, 'derived_sha256': '2' * 64, 'derived_bytes': 55,
             'old_path': 'objects/old/document.pdf', 'source_uri': 'hf://datasets/fixture',
             'generation': 'migration-20261007', 'bucket': audit.BUCKET, 'new_path': OLD,
             'manifest_path': OLD.rsplit('/', 1)[0] + '/derived-manifest.json'}
    index = {'version': 1, 'kind': 'pdf-derived-source-index', 'files': [entry]}
    derived = {OLD: {'size': 55, 'xet_hash': 'xet-a'}}
    manifest = {**entry, 'kind': 'pdf-derived-source', 'version': 1, 'new_path': NEW,
                'manifest_path': NEW.rsplit('/', 1)[0] + '/derived-manifest.json'}
    raw = audit.encode(manifest)
    objects = {NEW: {'size': 55, 'xet_hash': 'xet-a'}, manifest['manifest_path']: {'size': len(raw), 'xet_hash': 'm'}}
    sidecar = {'v': 1, 'f': {KEY: {'s': 2, 'm': 'p', 'b': audit.BUCKET, 'p': OLD}}}
    return index, derived, objects, {manifest['manifest_path']: raw}, sidecar


class AuditTests(unittest.TestCase):
    def test_exact_source_and_content_match_is_proposed_not_deleted(self):
        index, derived, objects, manifests, sidecar = fixture()
        result = audit.build_plan(index, derived, objects, manifests.__getitem__, sidecar=sidecar)
        self.assertFalse(result['deletion_authorized'])
        self.assertEqual(len(result['planned']), 1)
        self.assertEqual(result['planned'][0]['new_path'], NEW)
        self.assertEqual(result['planned'][0]['old_path'], OLD)
        self.assertTrue(result['planned'][0]['currently_direct_route'])
        self.assertFalse(result['unresolved'])
        self.assertFalse(result['unclassified_object_pdfs'])

    def test_same_content_two_books_are_disambiguated_by_manifest_key(self):
        index, derived, objects, manifests, sidecar = fixture()
        candidate = SECOND.rsplit('/', 1)[0] + '/derived-manifest.json'
        raw = json.loads(next(iter(manifests.values())))
        raw.update(key='VoiceOfML/Test\0别名.caj', new_path=SECOND, manifest_path=candidate)
        payload = audit.encode(raw)
        objects[SECOND] = {'size': 55, 'xet_hash': 'xet-a'}
        objects[candidate] = {'size': len(payload), 'xet_hash': 'm2'}
        manifests[candidate] = payload
        result = audit.build_plan(index, derived, objects, manifests.__getitem__, sidecar=sidecar)
        self.assertEqual([x['new_path'] for x in result['planned']], [NEW])
        self.assertEqual(result['unclassified_object_pdfs'], [SECOND])

    def test_ambiguous_missing_or_mismatched_manifest_is_never_a_match(self):
        index, derived, objects, manifests, sidecar = fixture()
        candidate = SECOND.rsplit('/', 1)[0] + '/derived-manifest.json'
        value = json.loads(next(iter(manifests.values())))
        value.update(new_path=SECOND, manifest_path=candidate)
        raw = audit.encode(value)
        objects[SECOND] = {'size': 55, 'xet_hash': 'xet-a'}
        objects[candidate] = {'size': len(raw), 'xet_hash': 'm2'}
        manifests[candidate] = raw
        result = audit.build_plan(index, derived, objects, manifests.__getitem__, sidecar=sidecar)
        self.assertFalse(result['planned'])
        self.assertEqual(result['unresolved'][0]['reason'], 'no-unique-matching-manifest')
        objects.pop(candidate)
        result = audit.build_plan(index, derived, objects, manifests.__getitem__)
        self.assertFalse(result['planned'])
        self.assertEqual(result['unresolved'][0]['reason'], 'manifest-unreadable')
        objects.pop(SECOND)
        original_path = next(iter(manifests))
        broken = json.loads(manifests[original_path]); broken['source_revision'] = '3' * 40
        encoded = audit.encode(broken)
        objects[original_path]['size'] = len(encoded)
        manifests[original_path] = encoded
        result = audit.build_plan(index, derived, objects, manifests.__getitem__)
        self.assertFalse(result['planned'])
        self.assertEqual(result['unresolved'][0]['matching_manifests'], 0)

    def test_unknown_versions_and_unindexed_orphans_are_only_reported(self):
        index, derived, objects, manifests, _ = fixture()
        derived[OLD]['xet_hash'] = 'different'
        result = audit.build_plan(index, derived, objects, manifests.__getitem__)
        self.assertFalse(result['planned'])
        self.assertEqual(result['unclassified_object_pdfs'], [NEW])
        self.assertFalse(result['deletion_authorized'])
        index['files'][0]['derived_bytes'] += 1
        result = audit.build_plan(index, derived, objects, manifests.__getitem__)
        self.assertEqual(result['unresolved'][0]['reason'], 'legacy-object-missing-or-mismatched')

    def test_live_audit_mock_uses_readonly_io_and_detects_moving_root(self):
        index, derived, objects, manifests, sidecar = fixture()
        store = Store()
        store.objects[audit.BUCKET, audit.INDEX] = audit.encode(index)
        store.objects[audit.ASSETS, audit.SIDECAR] = gzip.compress(audit.encode(sidecar), mtime=0)
        store.objects[audit.ASSETS, audit.POINTER] = audit.encode({'generation': 'f' * 64})
        for path, raw in manifests.items(): store.objects[audit.BUCKET, path] = raw
        class Api:
            def list_bucket_tree(self, bucket, prefix, recursive):
                self.test.assertEqual((bucket, recursive), (audit.BUCKET, True))
                for path, meta in (derived if prefix == 'derived/' else objects).items():
                    yield SimpleNamespace(type='file', path=path, size=meta['size'], xet_hash=meta['xet_hash'])
        api = Api(); api.test = self
        report = audit.audit(store, api)
        self.assertEqual(report['v3_generation'], 'f' * 64)
        self.assertEqual(store.writes, [])
        self.assertEqual(len(report['planned']), 1)
        original = store.read_bytes; reads = 0
        def drift(bucket, path):
            nonlocal reads
            if (bucket, path) == (audit.BUCKET, audit.INDEX):
                reads += 1
                if reads > 1: return b'changed'
            return original(bucket, path)
        store.read_bytes = drift
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            audit.audit(store, api)
        self.assertEqual(store.writes, [])

    def test_reconciled_index_still_audits_exact_old_and_new_identity(self):
        index, derived, objects, manifests, sidecar = fixture()
        index['files'][0]['new_path'] = NEW
        index['files'][0]['manifest_path'] = NEW.rsplit('/', 1)[0] + '/derived-manifest.json'
        mapping = {OLD: NEW}
        paths = audit.candidate_manifest_paths(index, derived, objects, active_redirects=mapping)
        self.assertEqual(paths, [index['files'][0]['manifest_path']])
        report = audit.build_plan(index, derived, objects, manifests.__getitem__, sidecar=sidecar,
                                  active_redirects=mapping)
        self.assertEqual(len(report['planned']), 1)
        self.assertEqual(report['planned'][0]['index_location'], 'objects')
        self.assertEqual(report['planned'][0]['old_path'], OLD)
        self.assertEqual(report['planned'][0]['new_path'], NEW)
        self.assertEqual(report['unindexed_legacy_pdfs'], [])
        self.assertEqual(report['unresolved'], [])
        altered = copy.deepcopy(index)
        altered['files'][0]['source_revision'] = '3' * 40
        report = audit.build_plan(altered, derived, objects, manifests.__getitem__, active_redirects=mapping)
        self.assertEqual(report['unresolved'][0]['reason'], 'no-unique-matching-manifest')
        altered = copy.deepcopy(index)
        altered['files'][0]['manifest_path'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'manifest path'):
            audit.build_plan(altered, derived, objects, manifests.__getitem__, active_redirects=mapping)

    def test_bad_identity_and_inventory_are_rejected(self):
        index, derived, objects, manifests, _ = fixture()
        for field, value in [('source_sha256', '../bad'), ('manifest_path', 'other'), ('key', 'bad')]:
            altered = copy.deepcopy(index); altered['files'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                audit.build_plan(altered, derived, objects, manifests.__getitem__)
        class BadApi:
            def list_bucket_tree(self, *args, **kwargs):
                return [SimpleNamespace(type='file', path='outside', size=1, xet_hash='hash')]
        with self.assertRaises(ValueError):
            audit.hub_inventory(BadApi(), 'objects/')


if __name__ == '__main__':
    unittest.main()
