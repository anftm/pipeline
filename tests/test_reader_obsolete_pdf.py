import copy
import gzip
import json
import unittest

from scripts import reader_obsolete_pdf as obsolete
from scripts import publish_reader_v3 as p, reader_lifecycle as lifecycle
from tests.test_publish_reader_v3 import Store
from tests.test_reader_route_control import Consumers


def encode(value):
    # Deliberately retain unusual whitespace, Unicode and a trailing newline.
    return (json.dumps(value, ensure_ascii=False, indent=3) + '\n').encode()


class ObsoletePDFTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.inventory, self.copies, self.manifests, owners = [], [], [], []
        self.sidecar = {'v': 1, 'f': {}}
        self.meta = {}
        for number in range(8):
            caj = number == 7
            source = f'{number + 1:064x}'
            root = f'objects/{source[:2]}/{source}'
            old_path = f'derived/migration-20261007/{number:032x}/document.pdf'
            copy_path = f'{root}/{number + 10:016x}/document.pdf'
            new_path = f'{root}/{number + 20:016x}/document.pdf'
            old_body, new_body = f'%PDF-old-{number}'.encode(), f'%PDF-new-{number}'.encode()
            identity = obsolete.proof(old_body)
            profile = 'caj-family-pdf-v1' if caj else 'gbk-font-repair-v1'
            source_path = f'目录/{number}.' + ('caj' if caj else 'pdf')
            old = {'version': 1, 'kind': 'pdf-derived-source', 'key': 'VoiceOfML/Test\0' + source_path,
                   'repo': 'VoiceOfML/Test', 'source_path': source_path, 'source_revision': 'a' * 40,
                   'source_extension': 'caj' if caj else 'pdf', 'source_sha256': source,
                   'profile': profile, 'generation': 'migration-20261007', 'bucket': obsolete.BUCKET,
                   'new_path': old_path, 'manifest_path': old_path.replace('document.pdf', 'derived-manifest.json'),
                   'old_path': f'{root}/{profile}/document.pdf', 'source_uri': 'hf://original',
                   'derived_bytes': identity['bytes'], 'derived_sha256': identity['sha256']}
            exact = {'old_pdf': old_path, 'old_manifest': old['manifest_path'], 'new_pdf': copy_path,
                     'new_manifest': copy_path.replace('document.pdf', 'derived-manifest.json'),
                     **identity, 'xet_hash': 'b' * 64}
            copied = {**old, 'new_path': copy_path, 'manifest_path': exact['new_manifest']}
            self.put(old['manifest_path'], encode(old))
            self.put(exact['new_manifest'], encode(copied))
            for path, body in ((old_path, old_body), (copy_path, old_body), (new_path, new_body)):
                self.put(path, body)
                self.meta[path] = {'bucket': obsolete.BUCKET, 'path': path, 'size': len(body), 'xet_hash': 'b' * 64}
            previous = {'bucket': obsolete.BUCKET, 'path': old_path, **identity, 'role': 'provenance'}
            current = {k: v for k, v in old.items() if k not in ('version', 'kind')}
            current.update(profile='gbk-font-repair-v2', generation='gbk-font-repair-v2',
                           new_path=new_path, path=new_path, previous_pdf=previous,
                           manifest_path=new_path.replace('document.pdf', 'derived-manifest.json'),
                           derived_bytes=len(new_body), derived_sha256=obsolete.proof(new_body)['sha256'])
            row = {'manifest_path': old['manifest_path'], 'manifest': old, 'canonical_owners': [] if caj else [current]}
            if not caj:
                manifest = {**current, 'version': 1, 'kind': 'pdf-derived-source'}
                owners.append(current)
                self.manifests.append({'path': current['manifest_path'], 'document': manifest})
                self.put(current['manifest_path'], encode(manifest))
            self.inventory.append(row)
            self.copies.append(exact)
            self.sidecar['f'][old['key']] = {'s': 2, 'm': 'p', 'p': copy_path if caj else new_path, 'b': obsolete.BUCKET}
        self.index = {'version': 1, 'kind': 'pdf-derived-source-index', 'generation': 'unchanged',
                      'files': owners + [{'key': 'unrelated', 'keep': {'count': 123}}], 'extra': ['keep']}
        self.put(obsolete.INDEX, encode(self.index))
        caj = self.inventory[-1]['manifest']
        self.ccrd_root = ('vomebook/ccrd', 'control/retained/preserved-reader.json')
        self.original = {'version': 1, 'kind': 'ccrd-ocr-retention', 'status': 'committed', 'generation': 'c' * 64,
                         'resources': [{'bucket': obsolete.BUCKET, 'path': caj['manifest_path'],
                                        **obsolete.proof(self.store.read_bytes(obsolete.BUCKET, caj['manifest_path'])),
                                        'role': 'provenance'}]}
        self.unrelated = {'bucket': 'vomebook/ccrd', 'path': 'keep/original.caj', 'role': 'provenance'}
        root = copy.deepcopy(self.original)
        root['resources'] = [self.unrelated, *root['resources'], {'bucket': 'other/bucket', 'path': 'keep'}]
        self.store.objects[self.ccrd_root] = encode(root)
        self.publish()

    def put(self, path, raw):
        self.store.objects[obsolete.BUCKET, path] = raw

    def publish(self):
        self.store.objects[obsolete.audit.ASSETS, obsolete.audit.SIDECAR] = gzip.compress(p.encode(self.sidecar))
        catalog, _ = lifecycle.catalog_generation(self.sidecar)
        self.store.objects[p.ASSETS, 'reader-index/reader_catalog.json'] = p.encode(catalog)
        p.unify(self.store, apply=True)
        _, catalog = p.current(self.store)
        self.projection = p.project_sidecar(p.pinned_base(self.store, catalog, {}), catalog)
        self.observations = {site: p.observe_consumer(self.store, site, Consumers(self.store)) for site in ('hf', 'pages')}
        self.store.writes.clear()

    def prepare(self):
        return obsolete.prepare(self.store, inventory=self.inventory, exact_copies=self.copies,
                                manifest_inventory=self.manifests, ccrd_original=self.original,
                                ccrd_root=self.ccrd_root, observations=self.observations,
                                projection=self.projection, metadata=lambda b, path: self.meta.get(path))

    def test_exact_mutations_and_allowlist_preserve_active_caj_and_originals(self):
        before = copy.deepcopy(self.store.objects)
        result = self.prepare()
        self.assertEqual(self.store.objects, before)
        self.assertEqual(self.store.writes, [])
        self.assertEqual(result['blockers'], [])
        expected = {row[field] for row in self.copies for field in ('old_pdf', 'old_manifest')}
        expected.update(row[field] for row in self.copies[:-1] for field in ('new_pdf', 'new_manifest'))
        self.assertEqual(set(result['delete_allowlist']), expected)
        self.assertEqual(len(expected), 30)
        self.assertEqual(len(result['mutations']), 9)
        self.assertFalse(result['deletion_authorized'])
        for item in result['mutations']:
            self.assertEqual(item['before'], before[item['bucket'], item['path']])
            self.assertEqual(item['before_identity'], obsolete.proof(item['before']))
            self.assertEqual(item['after_identity'], obsolete.proof(item['after']))
            expected_value = json.loads(item['before'])
            if item['path'] == obsolete.INDEX:
                for entry in expected_value['files'][:-1]:
                    del entry['previous_pdf']
            elif (item['bucket'], item['path']) == self.ccrd_root:
                expected_value['resources'].remove(self.original['resources'][0])
                self.assertIn(self.unrelated, json.loads(item['after'])['resources'])
            else:
                del expected_value['previous_pdf']
            self.assertEqual(json.loads(item['after']), expected_value)
            self.assertTrue(item['after'].endswith(b'\n'))
            self.assertIn(b'\n   ', item['after'])
        self.assertEqual(len(result['source_proofs']), 8)
        self.assertEqual(len(result['candidates']), 30)
        for field in ('new_pdf', 'new_manifest'):
            self.assertNotIn(self.copies[-1][field], expected)

    def test_new_source_and_profile_mismatch_fail_even_with_matching_inventory(self):
        for field, value, message in [('source_revision', 'f' * 40, 'source identity'),
                                      ('profile', 'gbk-font-repair-v1', 'profile')]:
            with self.subTest(field=field):
                self.setUp()
                current = self.inventory[0]['canonical_owners'][0]
                current[field] = value
                self.put(obsolete.INDEX, encode(self.index))
                with self.assertRaisesRegex(ValueError, message):
                    self.prepare()

    def test_v2_manifest_and_pdf_metadata_or_content_mismatch_fail(self):
        for mode in ('manifest', 'size', 'content', 'xet'):
            with self.subTest(mode=mode):
                self.setUp()
                current = self.inventory[0]['canonical_owners'][0]
                if mode == 'manifest':
                    self.put(current['manifest_path'], encode({**self.manifests[0]['document'], 'source_sha256': 'f' * 64}))
                elif mode == 'size':
                    self.meta[current['new_path']]['size'] += 1
                elif mode == 'xet':
                    self.meta[current['new_path']]['xet_hash'] = None
                else:
                    self.put(current['new_path'], b'%PDF-corrupt')
                with self.assertRaisesRegex(ValueError, 'mismatch'):
                    self.prepare()

    def test_current_route_mismatch_fails(self):
        key = self.inventory[0]['manifest']['key']
        self.sidecar['f'][key]['p'] = self.copies[0]['new_pdf']
        self.publish()
        with self.assertRaisesRegex(ValueError, 'current route mismatch'):
            self.prepare()

    def test_missing_caj_route_is_explicit_blocker_without_ccrd_mutation(self):
        del self.sidecar['f'][self.inventory[-1]['manifest']['key']]
        self.publish()
        result = self.prepare()
        self.assertEqual(len(result['blockers']), 1)
        self.assertEqual(result['delete_allowlist'], [])
        self.assertEqual(len(result['target_delete_paths']), 30)
        self.assertNotIn(self.ccrd_root, [(m['bucket'], m['path']) for m in result['mutations']])

    def test_stale_or_missing_actual_consumer_and_projection_fail(self):
        for mode in ('missing', 'stale', 'projection'):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'missing':
                    del self.observations['pages']
                elif mode == 'stale':
                    self.observations['hf']['generation'] = 'e' * 64
                else:
                    self.projection['f'].clear()
                with self.assertRaises(ValueError):
                    self.prepare()

    def test_reference_byte_splices_preserve_unrelated_bytes(self):
        for raw, expected in [(b'{"previous_pdf": {"x":1},  "other" : 1e2}\n', b'{  "other" : 1e2}\n'),
                              (b'{"other" : 1e2, "previous_pdf": {"x":1}}', b'{"other" : 1e2}')]:
            self.assertEqual(obsolete.remove_reference(raw, 'previous_pdf', {'x': 1}), expected)
        raw = b'{"resources":[ {"x":1} ], "other": "keep"}'
        self.assertEqual(obsolete.remove_reference(raw, 'resources', {'x': 1}, array=True),
                         b'{"resources":[  ], "other": "keep"}')


if __name__ == '__main__':
    unittest.main()
