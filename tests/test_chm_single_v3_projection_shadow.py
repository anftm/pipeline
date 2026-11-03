import ast
import copy
import gzip
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import chm_single_interrupted_preflight as fixed
from scripts import chm_single_v3_projection_shadow as shadow, publish_reader_v3 as publication
from scripts import reader_document_reset as document_reset, reader_gc_documents as document_gc


class SingleV3ProjectionShadowTests(unittest.TestCase):
    def setUp(self):
        self.base = {'v': 1, 'f': {
            fixed.SOURCE_KEY: fixed.route(fixed.OLD_ROOT),
            'repo\0book.djvu': {'s': 2, 'm': 'p', 'p': 'documents/original.djvu', 'b': fixed.ASSETS},
            'repo\0audio.aac': {'s': 2, 'm': 'a', 'p': 'media/audio/aac/object', 'b': fixed.ASSETS},
            'repo\0native.txt': {'s': 2, 'm': 't', 'p': 'documents/text/native.txt', 'b': fixed.ASSETS},
        }}
        self.raw = gzip.compress(publication.encode(self.base), compresslevel=9, mtime=0)
        sha = hashlib.sha256(self.raw).hexdigest()
        self.sha_patch = patch.object(fixed, 'SIDECAR_SHA256', sha)
        self.sha_patch.start()
        self.addCleanup(self.sha_patch.stop)
        ref = publication.metadata(fixed.ASSETS, f'reader-index/snapshots/{sha}/reader_assets.json.gz', self.raw)
        body = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {}, 'base_sidecar': ref,
                'parent_generation': '1' * 64, 'document_reset': shadow.RESET_ID}
        self.catalog = {**body, 'generation': publication.text.digest(body)}
        gen = self.catalog['generation']
        self.gen_patch = patch.object(fixed, 'PARENT_V3', gen)
        self.gen_patch.start()
        self.addCleanup(self.gen_patch.stop)
        raw_catalog = publication.encode(self.catalog)
        catalog_ref = publication.metadata(fixed.ASSETS, f'reader-index/v3/generations/{gen}/catalog.json', raw_catalog)
        self.pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': gen,
                        'catalog': catalog_ref, 'resources': [catalog_ref]}
        self.receipt = {'version': 1, 'kind': 'reader-document-reset', 'status': 'withdrawn',
                        'reset_id': shadow.RESET_ID, 'generation': gen, 'base_snapshot': ref}
        self.after = copy.deepcopy(self.base)
        self.after['f'][fixed.SOURCE_KEY] = fixed.route(fixed.ROOT)

    def simulate(self):
        return shadow.simulate(self.pointer, self.catalog, self.raw, self.after, self.receipt)

    def fixtures(self, simulation):
        pointer, catalog = simulation['pointer'], simulation['catalog']
        projection = simulation['projection']
        receipt = {'version': 1, 'active': True, 'generation': pointer['generation'],
                   'catalog_sha256': pointer['catalog']['sha256'], 'files': len(catalog['files'])}
        packed = gzip.compress(publication.encode(projection), compresslevel=9, mtime=0)
        return {'hf': {'receipt': {**receipt, 'surface': 'hf'}, 'projection': copy.deepcopy(projection)},
                'pages': {'receipt': {**receipt, 'surface': 'pages',
                                      'reader_contract': 'pdf-reading-v3-v1',
                                      'sidecar_sha256': hashlib.sha256(packed).hexdigest()},
                          'projection': copy.deepcopy(projection)}}

    def test_exact_target_route_only_and_existing_reset_receipt_remains_blocker(self):
        original = copy.deepcopy((self.pointer, self.catalog, self.base, self.receipt, self.after))
        result = self.simulate()
        self.assertEqual(result, self.simulate())
        self.assertEqual((self.pointer, self.catalog, self.base, self.receipt, self.after), original)
        changed = {key for key in self.base['f'] if self.base['f'][key] != result['projection']['f'][key]}
        self.assertEqual(changed, {fixed.SOURCE_KEY})
        self.assertEqual(result['catalog']['document_reset'], shadow.RESET_ID)
        self.assertEqual(result['catalog']['parent_generation'], self.pointer['generation'])
        self.assertEqual(result['catalog']['files'], self.catalog['files'])
        for name in ('current_reset_receipt_compatible', 'live_projection_verified',
                     'consumer_acceptance_proved', 'recovery_authorized', 'gc_authorized'):
            self.assertIs(result[name], False)

    def test_both_synthetic_fixtures_match_but_never_prove_deployment(self):
        result = self.simulate()
        check = shadow.evaluate_fixture_receipts(result, self.fixtures(result))
        self.assertTrue(check['synthetic_fixtures_match'])
        for name in ('live_projection_verified', 'consumer_acceptance_proved', 'recovery_authorized', 'gc_authorized'):
            self.assertIs(check[name], False)

    def test_incomplete_stale_and_corrupt_surface_fixtures_fail_closed(self):
        result = self.simulate()
        for mode in ('missing_pages', 'stale_generation', 'stale_hash', 'inactive', 'wrong_route',
                     'missing_unrelated', 'extra_route', 'corrupt_pages_hash', 'wrong_contract',
                     'wrong_v3_metadata', 'wrong_file_count'):
            fixtures = self.fixtures(result)
            if mode == 'missing_pages':
                del fixtures['pages']
            elif mode == 'stale_generation':
                fixtures['hf']['receipt']['generation'] = self.pointer['generation']
            elif mode == 'stale_hash':
                fixtures['pages']['receipt']['catalog_sha256'] = 'f' * 64
            elif mode == 'inactive':
                fixtures['hf']['receipt']['active'] = False
            elif mode == 'wrong_route':
                fixtures['hf']['projection']['f'][fixed.SOURCE_KEY] = fixed.route(fixed.OLD_ROOT)
            elif mode == 'missing_unrelated':
                del fixtures['pages']['projection']['f']['repo\0audio.aac']
            elif mode == 'extra_route':
                fixtures['hf']['projection']['f']['foreign'] = {'s': 1}
            elif mode == 'corrupt_pages_hash':
                fixtures['pages']['receipt']['sidecar_sha256'] = 'f' * 64
            elif mode == 'wrong_contract':
                fixtures['pages']['receipt']['reader_contract'] = 'old'
            elif mode == 'wrong_v3_metadata':
                fixtures['hf']['projection']['v3']['generation'] = 'f' * 64
            else:
                fixtures['pages']['receipt']['files'] = 1
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                shadow.evaluate_fixture_receipts(result, fixtures)

    def test_old_reset_receipt_or_v3_parent_drift_blocks_simulation(self):
        for mode in ('reset_id', 'receipt_generation', 'receipt_base', 'receipt_status',
                     'parent_generation', 'parent_catalog_hash', 'base_raw', 'unrelated_route', 'metadata'):
            pointer, catalog, raw, after, receipt = copy.deepcopy(
                (self.pointer, self.catalog, self.raw, self.after, self.receipt))
            if mode == 'reset_id':
                receipt['reset_id'] = 'foreign'
            elif mode == 'receipt_generation':
                receipt['generation'] = 'f' * 64
            elif mode == 'receipt_base':
                receipt['base_snapshot'] = {}
            elif mode == 'receipt_status':
                receipt['status'] = 'unknown'
            elif mode == 'parent_generation':
                pointer['generation'] = 'f' * 64
            elif mode == 'parent_catalog_hash':
                pointer['catalog']['sha256'] = 'f' * 64
            elif mode == 'base_raw':
                raw += b'x'
            elif mode == 'unrelated_route':
                del after['f']['repo\0native.txt']
            else:
                after['extra'] = 'foreign'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                shadow.simulate(pointer, catalog, raw, after, receipt)

    def test_unknown_v3_metadata_blocks_rather_than_silently_drops_it(self):
        catalog = copy.deepcopy(self.catalog)
        catalog['unknown_feature'] = {'preserve': True}
        body = {k: v for k, v in catalog.items() if k != 'generation'}
        generation = publication.text.digest(body)
        catalog['generation'] = generation
        raw = publication.encode(catalog)
        ref = publication.metadata(fixed.ASSETS, f'reader-index/v3/generations/{generation}/catalog.json', raw)
        pointer = {'version': 1, 'kind': 'reader-reading-pointer', 'generation': generation,
                   'catalog': ref, 'resources': [ref]}
        receipt = {**self.receipt, 'generation': generation}
        with patch.object(fixed, 'PARENT_V3', generation), self.assertRaisesRegex(ValueError, 'independent review'):
            shadow.simulate(pointer, catalog, self.raw, self.after, receipt)

    def test_oversized_old_snapshot_fails_before_json_parsing(self):
        with patch.object(shadow, 'MAX_SIDECAR_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'invalid old sidecar snapshot'):
                self.simulate()

    def test_tampered_simulation_cannot_become_fixture_acceptance(self):
        simulation = self.simulate()
        fixtures = self.fixtures(simulation)
        for mutation in ('catalog', 'pointer', 'projection'):
            candidate = copy.deepcopy(simulation)
            if mutation == 'catalog':
                candidate['catalog']['document_reset'] = 'foreign'
            elif mutation == 'pointer':
                candidate['pointer']['generation'] = 'f' * 64
            else:
                candidate['projection']['f'][fixed.SOURCE_KEY] = fixed.route(fixed.OLD_ROOT)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                shadow.evaluate_fixture_receipts(candidate, fixtures)

    def test_current_document_gc_preflight_rejects_new_v3_before_any_write(self):
        proposed = self.simulate()
        receipt_raw = publication.encode(self.receipt)
        expected_reads = {
            (document_reset.ASSETS, document_reset.RECEIPT): receipt_raw,
            (document_gc.CCRD, f'reports/ccrd-ocr-reset/{shadow.RESET_ID}/withdrawal.json'):
                publication.encode({'status': 'withdrawn'}),
            (document_reset.ASSETS, document_reset.SIDECAR): self.raw,
        }

        class ReadOnlyStore:
            def __init__(self):
                self.reads = []

            def read_bytes(self, bucket, path):
                self.reads.append((bucket, path))
                if (bucket, path) == (document_gc.CCRD, 'control/ocr-current.json'):
                    raise FileNotFoundError(path)
                if (bucket, path) not in expected_reads:
                    raise AssertionError('unexpected read after identity check')
                return expected_reads[bucket, path]

        store = ReadOnlyStore()
        with patch.object(publication, 'current', return_value=(proposed['pointer'], proposed['catalog'])):
            with self.assertRaisesRegex(ValueError, 'v3 document withdrawal changed'):
                document_gc.preflight(store, shadow.RESET_ID, object())
        self.assertEqual(len(store.reads), 4)
        self.assertEqual(self.receipt['generation'], self.pointer['generation'])

    def document_gc_fixture(self):
        """Synthetic preflight inputs only; creating these objects writes nothing."""
        from tests.test_reader_gc_ccrd import build_store
        proposed = self.simulate()
        # This made-up receipt is deliberately NOT a compatibility protocol.
        rebound = {**self.receipt, 'generation': proposed['pointer']['generation'],
                   'base_snapshot': proposed['catalog']['base_sidecar']}
        old_ccrd, _ = build_store()
        ccrd = document_reset.decode(old_ccrd.objects['current.json'])
        for value in ccrd['databases'].values():
            value['path'] = value['path'].replace('legacy-a', 'ccrd-active')
        objects = {
            (document_reset.ASSETS, document_reset.RECEIPT): publication.encode(rebound),
            (document_gc.CCRD, f'reports/ccrd-ocr-reset/{shadow.RESET_ID}/withdrawal.json'):
                publication.encode({'status': 'withdrawn'}),
            (document_reset.ASSETS, document_reset.SIDECAR): publication.encode(self.base),
            (document_reset.ASSETS, publication.POINTER): publication.encode(proposed['pointer']),
            (document_reset.PAGES, document_reset.DERIVED): publication.encode({'files': []}),
            (document_gc.CCRD, 'current.json'): publication.encode(ccrd),
        }
        for name in document_reset.REGISTRIES:
            objects[document_reset.ASSETS, 'reader-index/' + name] = publication.encode({'files': {}})

        class ReadOnlyStore:
            def __init__(self):
                self.objects = copy.deepcopy(objects)
                self.reads = []

            def read_bytes(self, bucket, path):
                self.reads.append((bucket, path))
                try:
                    return self.objects[bucket, path]
                except KeyError as error:
                    raise FileNotFoundError(path) from error

            def list_files(self, bucket, prefixes):
                return {path for target, path in self.objects if target == bucket and path.startswith(prefixes)}

        class Response:
            def __init__(self, content=None, data=None):
                self.content, self.data = content, data

            def raise_for_status(self):
                pass

            def json(self):
                return self.data

        class SyntheticConsumers:
            def __init__(self, hf, pages):
                self.hf, self.pages = hf, pages
                self.requests = []

            def get(self, url, **_kwargs):
                self.requests.append(url)
                if url == 'https://voiceofml-search.hf.space/api/reader-assets':
                    return Response(content=publication.encode(self.hf))
                if url == 'https://vomebook.github.io/search/data/reader_assets.json.gz':
                    return Response(content=publication.encode(self.pages))
                if url == 'https://vomebook-search.hf.space/api/index-status':
                    return Response(data={'ready': True, 'ocr_release': None,
                                          'loaded_sources': ['CCRD', 'CW', 'PLA', 'RMRB']})
                raise AssertionError('unexpected synthetic consumer request')

        return proposed, rebound, ReadOnlyStore(), SyntheticConsumers

    def test_hypothetical_receipt_rebind_does_not_prove_both_consumers_accepted_new_route(self):
        proposed, rebound, store, consumers = self.document_gc_fixture()
        before = copy.deepcopy(store.objects)
        # The document collector intentionally accepts either the canonical
        # sidecar OR the pinned baseline for each surface. A fabricated rebound
        # receipt plus mixed old/new synthetic projections can therefore pass
        # that narrow check without proving the CHM handoff or dual acceptance.
        client = consumers(self.base, self.after)
        with patch.object(publication, 'current', return_value=(proposed['pointer'], proposed['catalog'])), \
                patch.object(publication, 'pinned_base', return_value=copy.deepcopy(self.after)):
            receipt, _pins, _pointer, _catalog = document_gc.preflight(store, shadow.RESET_ID, client)
        self.assertEqual(receipt, rebound)
        self.assertEqual(store.objects, before)
        self.assertEqual(len(client.requests), 3)
        self.assertNotEqual(client.hf['f'][fixed.SOURCE_KEY], client.pages['f'][fixed.SOURCE_KEY])
        self.assertFalse(proposed['consumer_acceptance_proved'])
        self.assertFalse(proposed['recovery_authorized'])
        self.assertFalse(proposed['gc_authorized'])
        fixtures = self.fixtures(proposed)
        fixtures['hf']['projection'] = copy.deepcopy(self.base)
        with self.assertRaisesRegex(ValueError, 'complete projection differs'):
            shadow.evaluate_fixture_receipts(proposed, fixtures)

    def test_synthetic_rebound_receipt_does_not_bypass_remaining_document_guards(self):
        for mode in ('reset_id', 'catalog_files', 'catalog_reset', 'base_snapshot', 'registry',
                     'derived_djvu', 'republished_document', 'consumer_foreign', 'ccrd_live'):
            proposed, rebound, store, consumers = self.document_gc_fixture()
            client = consumers(self.after, self.after)
            catalog = copy.deepcopy(proposed['catalog'])
            if mode == 'reset_id':
                store.objects[document_reset.ASSETS, document_reset.RECEIPT] = publication.encode(
                    {**rebound, 'reset_id': 'wrong-reset'})
            elif mode == 'catalog_files':
                catalog['files'] = {'foreign': {}}
            elif mode == 'catalog_reset':
                catalog['document_reset'] = 'wrong-reset'
            elif mode == 'base_snapshot':
                catalog['base_sidecar'] = {}
            elif mode == 'registry':
                store.objects[document_reset.ASSETS, 'reader-index/' + document_reset.REGISTRIES[0]] = \
                    publication.encode({'files': {'foreign': {}}})
            elif mode == 'derived_djvu':
                store.objects[document_reset.PAGES, document_reset.DERIVED] = publication.encode(
                    {'files': [{'source_extension': 'djvu', 'key': 'repo\0book.djvu'}]})
            elif mode == 'republished_document':
                new = copy.deepcopy(self.base)
                new['f']['repo\0scan.pdf'] = {'b': document_reset.PAGES, 'p': 'objects/old/page-manifest.json'}
                store.objects[document_reset.ASSETS, document_reset.SIDECAR] = publication.encode(new)
            elif mode == 'consumer_foreign':
                client.hf = copy.deepcopy(self.after)
                client.hf['f']['repo\0audio.aac'] = {'s': 1}
            else:
                store.objects[document_gc.CCRD, 'control/ocr-current.json'] = b'{}'
            before = copy.deepcopy(store.objects)
            with self.subTest(mode=mode), \
                    patch.object(publication, 'current', return_value=(proposed['pointer'], catalog)), \
                    patch.object(publication, 'pinned_base', return_value=copy.deepcopy(self.after)), \
                    self.assertRaises(ValueError):
                document_gc.preflight(store, shadow.RESET_ID, client)
            self.assertEqual(store.objects, before)
            self.assertFalse(proposed['gc_authorized'])

    def test_no_workflow_or_network_writer_can_activate_simulation(self):
        tree = ast.parse(Path(shadow.__file__).read_text())
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertFalse(imports & {'os', 'sys', 'httpx', 'requests', 'boto3', 'subprocess'})
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertFalse(calls & {'put_bytes', 'put_object', 'delete', 'unify', 'commit_catalog',
                                  'acknowledge', 'observe_consumer', 'publish_catalog'})
        workflows = Path(shadow.__file__).resolve().parents[1] / '.github/workflows'
        self.assertFalse([path for path in workflows.glob('*.yml')
                          if 'scripts/chm_single_v3_projection_shadow.py' in path.read_text()])


if __name__ == '__main__':
    unittest.main()
