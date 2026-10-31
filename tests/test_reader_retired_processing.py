import base64
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from scripts import build_reader_assets_v2_sidecar as sidecar
from scripts import reader_gc_retirement as retirement
from scripts import reader_lifecycle as lifecycle
from scripts import reader_retired_processing as history


class HistoricalProcessingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.root = 'chapters/ebook/epub/' + 'a' * 64 + '/' + 'b' * 16
        key, source = 'repo\0book.epub', 'c' * 64
        identity = hashlib.sha256((key + '\0' + source + '\0' + self.root).encode()).hexdigest()[:32]
        self.path = 'reader-index/processing/office-chapters-' + identity + '.json'
        self.entry = {'key': key, 'source_sha256': source, 'root': self.root,
                      'manifest': self.root + '/chapter-manifest.json', 'processing_roots': [self.path]}
        self.sidecar = {'v': 1, 'f': {self.entry['key']: {'s': 2, 'm': 'e', 'b': history.ASSETS,
                         'p': self.entry['manifest'], 'c': self.entry['manifest'], 'cb': history.ASSETS}}}
        record = lifecycle.processing_record([{'bucket': history.ASSETS, 'root': self.root}],
                                             'office-chapter-upload', {'run_id': '123'})
        record = lifecycle.release_processing(record, 'd' * 32)
        raw = json.dumps(record, sort_keys=True).encode()
        digest = hashlib.sha256(raw).hexdigest()
        receipts = {surface: {'surface': surface, 'generation': history.OLD_V3_GENERATION,
                    'base_sidecar_sha256': history.OLD_BASE_DIGEST, 'projection_verified': True}
                    for surface in ('hf', 'pages')}
        self.row = {'path': self.path, 'bucket': history.ASSETS, 'sha256': digest,
                    'generation': record['generation'], 'operation': record['operation'],
                    'run': record['run'], 'released_at': record['released_at'],
                    'supersession': {'version': 1, 'kind': 'reader-processing-supersession',
                        'original_generation': record['generation'], 'record_sha256': digest,
                        'replacement_generation': history.OLD_BASE_GENERATION,
                        'sidecar_sha256': history.OLD_BASE_DIGEST, 'resources': record['resources'],
                        'original_record_base64': base64.b64encode(raw).decode(), 'consumer_receipts': receipts}}

    def test_transaction_requires_exact_hash_plan_and_verified_batches(self):
        targets = [(history.ASSETS, self.path)]
        plan = hashlib.sha256(retirement.encode(targets)).hexdigest()
        body = {'applied': True, 'complete': True, 'reader_only': True, 'scope': 'unreferenced',
                'plan_sha256': plan, 'targets': [{'bucket': history.ASSETS, 'path': self.path}],
                'batches': [{'bucket': history.ASSETS, 'paths': [self.path], 'status': 'verified-deleted'}],
                'planned_objects': 1, 'confirmed_deleted_objects': 1, 'processing_retirements': [self.row]}
        raw = json.dumps(body).encode()
        self.assertEqual(history.load_receipt(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                                              expected_plan=plan)[self.path], self.row)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            history.load_receipt(raw + b' ', expected_sha256=hashlib.sha256(raw).hexdigest(), expected_plan=plan)
        body['batches'][0]['status'] = 'accepted'
        raw = json.dumps(body).encode()
        with self.assertRaisesRegex(ValueError, 'batches'):
            history.load_receipt(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), expected_plan=plan)

    def test_archived_original_requires_exact_owner_and_both_sidecars(self):
        self.assertEqual(history.verify_owner(self.path, self.entry, self.row, self.sidecar, self.sidecar),
                         'archived-original')
        raw = base64.b64decode(self.row['supersession']['original_record_base64'])
        self.assertEqual(history.validate_archived_record(self.path, self.row, json.loads(raw), raw)['status'],
                         'released')
        with self.assertRaisesRegex(ValueError, 'original record identity'):
            history.validate_archived_record(self.path, self.row, json.loads(raw), raw + b' ')
        bad = copy.deepcopy(self.row)
        bad['supersession']['consumer_receipts']['pages']['projection_verified'] = False
        with self.assertRaisesRegex(ValueError, 'both consumer'):
            history.verify_owner(self.path, self.entry, bad, self.sidecar, self.sidecar)
        bad = copy.deepcopy(self.row)
        bad['supersession']['original_record_base64'] = base64.b64encode(b'{}').decode()
        with self.assertRaisesRegex(ValueError, 'original record identity'):
            history.verify_owner(self.path, self.entry, bad, self.sidecar, self.sidecar)
        other = copy.deepcopy(self.sidecar)
        other['f'][self.entry['key']]['p'] = 'chapters/ebook/epub/foreign/chapter-manifest.json'
        other['f'][self.entry['key']].pop('c')
        with self.assertRaisesRegex(ValueError, 'not covered'):
            history.verify_owner(self.path, self.entry, self.row, self.sidecar, other)

    def test_digest_only_stream_proof_requires_all_routes_and_exact_identity(self):
        native = 'native/ebook/chm/' + 'a' * 64 + '/document.epub'
        key = 'repo\0book.chm'
        roots = sorted([self.root, native.rsplit('/', 1)[0]])
        identity = hashlib.sha256((key + '\0' + '\0'.join(roots)).encode()).hexdigest()[:32]
        path = f'reader-index/processing/stream-upload-{identity}.json'
        entry = {'key': key, 'root': self.root, 'manifest': self.root + '/chapter-manifest.json',
                 'object': native, 'fallback': native, 'processing_roots': [path]}
        row = {'path': path, 'bucket': history.ASSETS, 'sha256': 'e' * 64,
               'generation': history.OLD_BASE_GENERATION, 'operation': 'reader-stream-upload',
               'run': {}, 'released_at': '2026-10-25T14:56:07+00:00'}
        routes = {'v': 1, 'f': {key: {'b': history.ASSETS, 'p': entry['manifest'], 'f': native}}}
        self.assertEqual(history.verify_owner(path, entry, row, routes, routes), 'verified-retirement-digest')
        newer = copy.deepcopy(routes)
        newer['f'][key].pop('f')
        with self.assertRaisesRegex(ValueError, 'not covered'):
            history.verify_owner(path, entry, row, routes, newer)
        wrong = {**entry, 'root': 'chapters/ebook/epub/' + 'c' * 64 + '/' + 'd' * 16}
        with self.assertRaisesRegex(ValueError, 'identity'):
            history.verify_owner(path, wrong, row, routes, routes)

    def test_unverified_absence_and_present_new_upload_are_never_swallowed(self):
        seen = []
        def missing(_path):
            raise FileNotFoundError(self.path)
        with self.assertRaises(FileNotFoundError):
            lifecycle.processing_handoffs([self.entry], 'f' * 32, missing)
        updates = lifecycle.processing_handoffs([self.entry], 'f' * 32, missing,
                                                retired=lambda path, owners: seen.append((path, owners)))
        self.assertEqual(updates, {})
        self.assertEqual(seen, [(self.path, [self.entry])])
        seen.clear()
        updates = lifecycle.processing_handoffs([self.entry], 'f' * 32,
            lambda _: self.fail('known missing root fetched per-item'), absent={self.path},
            retired=lambda path, owners: seen.append((path, owners)))
        self.assertEqual(updates, {})
        self.assertEqual(seen, [(self.path, [self.entry])])
        active = lifecycle.processing_record([{'bucket': history.ASSETS, 'root': self.root}],
                                             'new-upload', {'run_id': 'new'})
        updates = lifecycle.processing_handoffs([self.entry], 'f' * 32, lambda _: active,
                                                retired=lambda *_: self.fail('present record was ignored'))
        self.assertEqual(updates[self.path]['status'], 'released')
        self.assertEqual(updates[self.path]['operation'], 'new-upload')
        with self.assertRaisesRegex(ValueError, 'invalid processing record'):
            lifecycle.processing_handoffs([self.entry], 'f' * 32, lambda _: None,
                                          retired=lambda *_: self.fail('not a 404'))

    def test_missing_path_recheck_fails_before_any_release_is_published(self):
        other_root = 'media/audio/aac/' + 'd' * 64
        other_path = 'reader-index/processing/stream-upload-' + 'e' * 32 + '.json'
        other = {'key': 'repo\0a.aac', 'object': other_root + '/audio.mp3',
                 'processing_roots': [other_path]}
        active = lifecycle.processing_record([{'bucket': history.ASSETS, 'root': other_root}],
                                             'new-upload', {'run_id': 'active'})
        written = []
        def appeared(_paths, _owners):
            raise RuntimeError('historically absent processing root appeared during handoff')
        with self.assertRaisesRegex(RuntimeError, 'appeared'):
            sidecar.release_processing_roots([{'files': [self.entry, other]}], 'f' * 32,
                lambda _: active, lambda path, value: written.append((path, value)),
                absent={self.path}, retired_many=appeared)
        self.assertEqual(written, [])

    def test_guard_rejects_unknown_absence_and_unchanged_current_projection(self):
        class Store:
            def list_files(self, *_):
                return set()
            def read_bytes(self, *_):
                return b'proof'
        pointer = {'generation': 'g'}
        catalog = {'base_sidecar': {'x': 1}}
        with patch.object(history, 'load_receipt', return_value={}), \
             patch('scripts.publish_reader_v3.current', return_value=(pointer, catalog)):
            with self.assertRaisesRegex(ValueError, 'lack exact'):
                sidecar.historical_processing_guard([{'files': [self.entry]}], self.sidecar, Store(), None)
        with patch.object(history, 'load_receipt', return_value={self.path: self.row}), \
             patch('scripts.publish_reader_v3.current', return_value=(pointer, catalog)), \
             patch('scripts.publish_reader_v3.pinned_base', return_value=self.sidecar), \
             patch('scripts.publish_reader_v3.project_sidecar', return_value=self.sidecar), \
             patch('scripts.publish_reader_v3.observe_consumer', side_effect=[
                 {'generation': 'g', 'projection_verified': True},
                 {'generation': 'wrong', 'projection_verified': True}]):
            with self.assertRaisesRegex(ValueError, 'both deployed'):
                sidecar.historical_processing_guard([{'files': [self.entry]}], self.sidecar, Store(), None)

    def test_persistently_missing_record_does_not_skip_unproven_new_absence(self):
        class Store:
            def list_files(self, *_):
                return set()
            def read_bytes(self, *_):
                return b'proof'
        pointer, catalog = {'generation': 'g'}, {'base_sidecar': {'x': 1}}
        with patch.object(history, 'load_receipt', return_value={self.path: self.row}), \
             patch('scripts.publish_reader_v3.current', return_value=(pointer, catalog)), \
             patch('scripts.publish_reader_v3.pinned_base', return_value=self.sidecar), \
             patch('scripts.publish_reader_v3.project_sidecar', return_value=self.sidecar), \
             patch('scripts.publish_reader_v3.observe_consumer', return_value={
                 'generation': 'g', 'projection_verified': True}):
            guard, stats, missing = sidecar.historical_processing_guard(
                [{'files': [self.entry]}], self.sidecar, Store(), None)
        self.assertEqual(missing, {self.path})
        self.assertEqual(stats['verified_retirements'], 1)
        guard(self.path, [self.entry])
        with self.assertRaisesRegex(FileNotFoundError, 'newly missing'):
            guard('reader-index/processing/stream-upload-' + 'f' * 32 + '.json', [self.entry])
        changed = {**self.entry, 'source_sha256': 'f' * 64}
        with self.assertRaisesRegex(FileNotFoundError, 'newly missing'):
            guard(self.path, [changed])

    def test_archived_protection_is_used_without_writing_a_synthetic_record(self):
        class Store:
            def list_files(self, *_):
                return set()
            def read_bytes(self, *_):
                return b'proof'
        pointer, catalog = {'generation': 'g'}, {'base_sidecar': {'x': 1}}
        new = {'v': 1, 'f': {self.entry['key']: {'b': history.ASSETS, 'p': 'documents/web/html/other'}}}
        with patch.object(history, 'load_receipt', return_value={self.path: self.row}), \
             patch('scripts.publish_reader_v3.current', return_value=(pointer, catalog)), \
             patch('scripts.publish_reader_v3.pinned_base', return_value=self.sidecar), \
             patch('scripts.publish_reader_v3.project_sidecar', return_value=self.sidecar), \
             patch('scripts.publish_reader_v3.observe_consumer', return_value={
                 'generation': 'g', 'projection_verified': True}):
            guard, stats, missing = sidecar.historical_processing_guard(
                [{'files': [self.entry]}], new, Store(), None)
        self.assertEqual(stats['verified_retirements'], 0)
        self.assertEqual(stats['evidence'], {'archived-protection-preserved': 1})
        record = guard(self.path, [self.entry])
        self.assertEqual(record['status'], 'released')
        writes = []
        count = sidecar.release_processing_roots([{'files': [self.entry]}], 'f' * 32,
            lambda _: self.fail('archived record was read from storage'),
            lambda path, value: writes.append((path, value)), absent=missing, retired=guard,
            retired_many=lambda paths, owners: {p: guard(p, owners[p]) for p in paths})
        self.assertEqual(count, 0)
        self.assertEqual(writes, [])

    def test_stream_owner_identity_handles_archived_removed_fallback(self):
        native = 'native/ebook/chm/' + 'a' * 64
        key = 'repo\0book.chm'
        resources = [{'bucket': history.ASSETS, 'root': self.root},
                     {'bucket': history.ASSETS, 'root': native}]
        identity = hashlib.sha256((key + '\0' + '\0'.join(sorted([self.root, native]))).encode()).hexdigest()[:32]
        path = f'reader-index/processing/stream-upload-{identity}.json'
        entry = {'key': key, 'root': self.root, 'manifest': self.root + '/chapter-manifest.json',
                 'processing_roots': [path]}
        row = {'operation': 'reader-stream-upload'}
        history.verify_owner_identity(path, entry, row, resources)
        with self.assertRaisesRegex(ValueError, 'identity'):
            history.verify_owner_identity(path, entry, row)


if __name__ == '__main__':
    unittest.main()
