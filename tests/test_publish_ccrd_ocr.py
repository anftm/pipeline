"""Bounded in-memory publication protocol; no uploads, model calls or full indexes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import ccrd_ocr_release as contract, publish_ccrd_ocr as publisher, publish_ccrd_index as legacy


class Store:
    def __init__(self):
        self.objects, self.writes = {}, []
    def read_bytes(self, bucket, path):
        if (bucket, path) not in self.objects: raise FileNotFoundError(path)
        return self.objects[bucket, path]
    def put_bytes(self, bucket, path, raw):
        self.writes.append((bucket, path)); self.objects[bucket, path] = raw


class PublishCcrdOcrTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.body = b'complete independent OCR TXT'
        self.text_path = 'text/objects/aa/' + 'a' * 64 + '.txt'
        self.provenance = {'bucket': 'vomebook/pdf-pages-v2', 'path': 'objects/aa/book/text/page.json.gz',
                           'sha256': contract.digest(b'accepted source'), 'bytes': len(b'accepted source'), 'role': 'provenance'}
        self.store.objects[self.provenance['bucket'], self.provenance['path']] = b'accepted source'
        self.export = {'book_id': 'a' * 64, 'doc_id': 'OCR:1', 'readable_rel_path': 'VoiceOfML/Example/dir/book.txt',
                       'retained_reader_resources': [self.provenance]}
        self.export_path = 'text/exports/' + 'a' * 64 + '/' + 'b' * 64 + '/manifest.json'
        self.export_raw = contract.canonical(self.export)
        self.export_ref = publisher.resource(self.export_path, self.export_raw)
        identity = {'version': 1, 'kind': 'ccrd-ocr-search-release', 'base_metadata_sha256': 'c' * 64,
                    'builder_sha256': 'd' * 64, 'tokenizer_version': contract.TOKENIZER, 'exports': [self.export_ref]}
        generation = contract.digest(contract.canonical(identity))
        self.release = {**identity, 'generation': generation, 'documents': 1,
                        'database': publisher.resource(f'indexes/ocr/{generation}/OCR.sqlite3', b'fixture SQLite')}
        self.path = f'releases/ocr-{generation}/manifest.json'
        self.raw = contract.canonical(self.release)
        self.files = {self.path: self.raw, self.export_path: self.export_raw, self.text_path: self.body,
                      self.release['database']['path']: b'fixture SQLite'}
        self.candidate = {'manifest': self.release, 'records': [{'doc_id': 'OCR:1'}], 'exports': [self.export]}

    def publish(self, **kwargs):
        with patch.object(publisher, 'plan', return_value=(self.candidate, self.files)), \
                patch.object(publisher, 'assert_writer'):
            return publisher.publish(self.store, Path('.'), self.path, contract.digest(self.raw), 'c' * 64,
                                     kwargs.pop('expected_parent', None), **kwargs)

    def test_protection_precedes_upload_and_complete_readback_precedes_pointer(self):
        report = self.publish(apply=True)
        paths = [path for bucket, path in self.store.writes]
        self.assertTrue(paths[0].startswith('control/intents/'))
        self.assertLess(paths.index(self.path), paths.index(publisher.CURRENT))
        self.assertEqual(report['deleted_objects'], 0)
        pointer = json.loads(self.store.read_bytes(contract.BUCKET, publisher.CURRENT))
        self.assertEqual(pointer['release']['sha256'], contract.digest(self.raw))
        registry = json.loads(self.store.read_bytes(contract.BUCKET, publisher.IDS))
        self.assertEqual(registry['ids']['a' * 64]['doc_id'], 'OCR:1')
        self.assertIn(self.provenance, json.loads(self.store.objects[contract.BUCKET, paths[0]])['resources'])
        self.assertTrue(self.publish(apply=True)['unchanged'])

    def test_corrupt_or_missing_provenance_never_advances_the_pointer(self):
        self.store.objects[self.provenance['bucket'], self.provenance['path']] = b'bad evidence'
        with self.assertRaisesRegex(ValueError, 'provenance'):
            self.publish(apply=True)
        self.assertNotIn((contract.BUCKET, publisher.CURRENT), self.store.objects)

    def test_stale_parent_and_stable_id_collision_reject_before_writing(self):
        with self.assertRaisesRegex(ValueError, 'parent'):
            self.publish(apply=True, expected_parent='f' * 64)
        self.assertEqual(self.store.writes, [])
        registry = {'version': 1, 'kind': 'ccrd-ocr-id-registry', 'resources': [], 'ids': {
            'e' * 64: {'doc_id': 'OCR:1', 'readable_rel_path': 'VoiceOfML/Other/book.txt'}}}
        self.store.objects[contract.BUCKET, publisher.IDS] = contract.canonical(registry)
        with self.assertRaisesRegex(ValueError, 'another book'):
            self.publish(apply=True)
        self.assertEqual(self.store.writes, [])

    def test_dry_run_does_not_write_or_call_writer_mutation(self):
        self.assertFalse(self.publish(apply=False)['applied'])
        self.assertEqual(self.store.writes, [])

    def test_immutable_object_conflict_cannot_replace_existing_text(self):
        self.store.objects[contract.BUCKET, self.text_path] = b'different'
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.publish(apply=True)
        self.assertEqual(self.store.objects[contract.BUCKET, self.text_path], b'different')
        self.assertNotIn((contract.BUCKET, publisher.CURRENT), self.store.objects)

    def test_interrupted_unpromoted_generation_may_be_completed_but_not_published_history(self):
        self.publish(apply=True)
        previous, _ = publisher.optional_json(self.store, publisher.CURRENT)
        generation = 'e' * 64
        intent = {'version': 1, 'kind': 'ccrd-ocr-intent', 'generation': generation,
                  'status': 'uploading', 'expected_parent': previous['generation'], 'resources': []}
        self.store.objects[contract.BUCKET, f'control/intents/{generation}.json'] = contract.canonical(intent)
        db_path = f'indexes/ocr/{generation}/OCR.sqlite3'
        release_path = f'releases/ocr-{generation}/manifest.json'
        self.store.objects[contract.BUCKET, db_path] = b'partial database'
        self.store.objects[contract.BUCKET, release_path] = b'partial manifest'
        self.files[db_path] = b'complete database'
        self.files[release_path] = b'complete manifest'
        self.candidate['manifest']['generation'] = generation
        report = self.publish(apply=True, expected_parent=previous['generation'])
        self.assertEqual(report['generation'], generation)
        self.assertEqual(self.store.objects[contract.BUCKET, db_path], b'complete database')
        self.assertEqual(self.store.objects[contract.BUCKET, release_path], b'complete manifest')
        self.assertEqual(self.store.objects[contract.BUCKET, self.text_path], self.body)
        self.assertEqual(json.loads(self.store.read_bytes(contract.BUCKET, publisher.CURRENT))['generation'], generation)

    def test_unpromoted_candidate_resume_is_limited_to_its_generation_namespace(self):
        previous = {'generation': 'a' * 64}
        generation = 'e' * 64
        intent = {'version': 1, 'kind': 'ccrd-ocr-intent', 'generation': generation,
                  'status': 'uploading', 'expected_parent': previous['generation'], 'resources': []}
        intent_path = f'control/intents/{generation}.json'
        self.store.objects[contract.BUCKET, intent_path] = contract.canonical(intent)
        db_path = f'indexes/ocr/{generation}/OCR.sqlite3'
        release_path = f'releases/ocr-{generation}/manifest.json'
        self.assertEqual(publisher.unpromoted_candidate_paths(
            self.store, {'generation': generation}, previous), {db_path, release_path})
        # A committed or absent intent, a different parent, or the live generation
        # never grants replacement.
        for changed, parent in (({**intent, 'status': 'committed'}, previous),
                                ({**intent, 'expected_parent': 'b' * 64}, previous),
                                (intent, {'generation': generation})):
            self.store.objects[contract.BUCKET, intent_path] = contract.canonical(changed)
            self.assertEqual(publisher.unpromoted_candidate_paths(
                self.store, {'generation': generation}, parent), set())
        self.store.objects.pop((contract.BUCKET, intent_path))
        self.assertEqual(publisher.unpromoted_candidate_paths(
            self.store, {'generation': generation}, previous), set())

    def test_resume_preserves_protection_for_objects_it_did_not_replace(self):
        generation = 'e' * 64
        intent_path = f'control/intents/{generation}.json'
        old = {'version': 1, 'kind': 'ccrd-ocr-intent', 'generation': generation,
               'status': 'uploading', 'expected_parent': None,
               'resources': [{'bucket': contract.BUCKET, 'path': 'text/objects/old.txt',
                              'sha256': 'a' * 64, 'bytes': 1, 'role': 'runtime'},
                             {'bucket': contract.BUCKET, 'path': 'indexes/ocr/' + generation + '/OCR.sqlite3',
                              'sha256': 'b' * 64, 'bytes': 1, 'role': 'runtime'}]}
        self.store.objects[contract.BUCKET, intent_path] = contract.canonical(old)
        intent = {'version': 1, 'kind': 'ccrd-ocr-intent', 'generation': generation,
                  'status': 'uploading', 'expected_parent': None,
                  'resources': [{'bucket': contract.BUCKET,
                                 'path': 'indexes/ocr/' + generation + '/OCR.sqlite3',
                                 'sha256': 'c' * 64, 'bytes': 2, 'role': 'runtime'}]}
        merged = publisher.merge_unpromoted_intent(
            self.store, intent_path, intent, {'indexes/ocr/' + generation + '/OCR.sqlite3': b'x'})
        paths = {ref['path']: ref for ref in merged['resources']}
        self.assertIn('text/objects/old.txt', paths)
        self.assertEqual(paths['indexes/ocr/' + generation + '/OCR.sqlite3']['sha256'], 'c' * 64)

    def test_resume_replaces_only_the_named_unpromoted_objects(self):
        self.store.objects[contract.BUCKET, self.text_path] = b'partial text'
        publisher.put_checked(self.store, self.text_path, b'complete text', immutable=True,
                              replace_unpromoted=True)
        self.assertEqual(self.store.objects[contract.BUCKET, self.text_path], b'complete text')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            publisher.put_checked(self.store, self.text_path, b'other text', immutable=True)

    def test_additive_update_never_silently_drops_a_previous_book(self):
        self.publish(apply=True)
        pointer, _ = publisher.optional_json(self.store, publisher.CURRENT)
        candidate = {'manifest': {**self.release, 'exports': []}, 'exports': []}
        writes = list(self.store.writes)
        with self.assertRaisesRegex(ValueError, 'drop'):
            publisher.verify_additive_update(self.store, pointer, candidate)
        self.assertEqual(writes, self.store.writes)

    def test_actual_workflow_ownership_is_required_for_mutation(self):
        environment = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': 'anftm/pipeline', 'GITHUB_RUN_ID': '123',
                       'GITHUB_WORKFLOW_REF': publisher.WORKFLOW, 'CCRD_OCR_WRITE_PROTOCOL': publisher.PROTOCOL}
        with patch.dict('os.environ', environment):
            publisher.assert_writer()
            for key, value in (('GITHUB_WORKFLOW_REF', publisher.WORKFLOW.replace('main', 'feature')),
                               ('GITHUB_REPOSITORY', 'other/repository'), ('CCRD_OCR_WRITE_PROTOCOL', 'fake')):
                with self.subTest(key=key), patch.dict('os.environ', {key: value}), self.assertRaises(RuntimeError):
                    publisher.assert_writer()

    def test_legacy_parent_read_never_overwrites_new_manifest_or_hides_network_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'current.json').write_text('{"candidate":true}')
            def download(bucket, files, token):
                self.assertEqual(Path(files[0][1]).name, 'previous-current.json')
                Path(files[0][1]).write_text('{"previous":true}')
            with patch.object(legacy, 'download_bucket_files', side_effect=download):
                self.assertEqual(legacy.read_current(root, 'token'), {'previous': True})
            self.assertEqual(json.loads((root / 'current.json').read_text()), {'candidate': True})
            with patch.object(legacy, 'download_bucket_files', side_effect=TimeoutError):
                with self.assertRaisesRegex(RuntimeError, 'prior CCRD'):
                    legacy.read_current(root, 'token')


if __name__ == '__main__':
    unittest.main()
