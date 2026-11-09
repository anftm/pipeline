"""BHA snapshot evidence never infers a restore from its manifest."""
import hashlib
import json
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import yaml
from scripts import bha_snapshot_inspection as subject


class Response(BytesIO):
    status = 200


class BhaSnapshotInspectionTests(unittest.TestCase):
    def setUp(self):
        self.trees = {str(i): 'a' * 40 for i in range(32)}
        self.docs, self.facets = b'docs', b'facets'
        self.value = {'schema': 3, 'index_name': 'article', 'physical_index': 'article-123',
                      'generation': 'bha-123-abc', 'snapshot': 'bha-123-abc', 'document_count': 2,
                      'archive_commits': {str(i): 'b' * 40 for i in range(32)},
                      'index_metadata': {'archive_revisions': self.trees, 'archive_start': 0, 'archive_end': 31,
                                         'version': '1', 'source_fingerprint': 'c' * 64, 'build_fingerprint': 'd' * 64},
                      'sidecars': {'docs': {'filename': 'docs.sqlite', 'bytes': len(self.docs),
                                            'sha256': hashlib.sha256(self.docs).hexdigest()},
                                   'facets': {'filename': 'facets.sqlite', 'bytes': len(self.facets),
                                              'sha256': hashlib.sha256(self.facets).hexdigest()}}}
        self.raw = json.dumps(self.value).encode(); self.get = Mock(return_value=self.raw)
        self.hash = lambda key, _size: hashlib.sha256(self.docs if key.endswith('/docs.sqlite') else self.facets).hexdigest()

    def test_manifest_only_never_claims_bytes_or_restore(self):
        hashed = Mock(side_effect=AssertionError('must not hash'))
        report = subject.inspect(self.trees, get=self.get, hash_object=hashed)
        self.assertTrue(report['manifest_verified']); self.assertFalse(report['sidecars_verified'])
        self.assertFalse(report['restore_verified']); self.assertFalse(report['publication_ready'])
        self.assertEqual(report['remote_writes'], 0); self.assertEqual(self.get.call_count, 2)
        hashed.assert_not_called()

    def test_full_sidecar_hashes_still_do_not_claim_restore(self):
        report = subject.inspect(self.trees, full_sidecars=True, get=self.get, hash_object=self.hash)
        self.assertTrue(report['sidecars_verified']); self.assertFalse(report['restore_verified'])
        self.assertFalse(report['external_writers_verified'])

    def test_expected_generation_and_creation_time_reject_stale_same_tree_snapshot(self):
        self.value['created_at'] = 100
        raw = json.dumps(self.value).encode()
        good = subject.inspect(self.trees, get=lambda *_: raw, expected_generation='bha-123-abc', not_before=100)
        self.assertTrue(good['manifest_verified'])
        with self.assertRaisesRegex(ValueError, 'identity is malformed'):
            subject.inspect(self.trees, get=lambda *_: raw, expected_generation='bha-123-abc', not_before=101)
        with self.assertRaisesRegex(ValueError, 'identity is malformed'):
            subject.inspect(self.trees, get=lambda *_: raw, expected_generation='bha-other', not_before=100)
        with self.assertRaisesRegex(ValueError, 'minimum time'):
            subject.inspect(self.trees, get=lambda *_: raw, not_before=True)

    def test_live_restored_snapshot_is_observation_not_a_new_restore(self):
        health = {'ok': True, 'index_ready': True, 'index_status': 'ready', 'es': True, 'document_count': 2,
                  'bucket_snapshot': {'state': 'restored', 'stage': 'complete', 'snapshot': 'bha-123-abc',
                                      'source_stale': False}}
        result = subject.inspect_live(get=self.get, fetch=lambda *_: json.dumps(health).encode())
        self.assertTrue(result['manifest_verified']); self.assertTrue(result['reported_restore_complete'])
        self.assertFalse(result['sidecars_verified']); self.assertFalse(result['restore_executed'])
        self.assertFalse(result['publication_ready']); self.assertEqual(result['remote_writes'], 0)
        self.assertEqual(self.get.call_count, 3)

    def test_live_snapshot_rejects_stale_unknown_or_mismatched_status(self):
        base = {'ok': True, 'index_ready': True, 'index_status': 'ready', 'es': True, 'document_count': 2,
                'bucket_snapshot': {'state': 'restored', 'stage': 'complete', 'snapshot': 'bha-123-abc',
                                    'source_stale': False}}
        for snapshot in ({**base['bucket_snapshot'], 'source_stale': True},
                         {k: v for k, v in base['bucket_snapshot'].items() if k != 'source_stale'},
                         {**base['bucket_snapshot'], 'snapshot': 'bha-other'},
                         {**base['bucket_snapshot'], 'state': 'pending_publish'}):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                subject.inspect_live(get=lambda *_: self.raw,
                                     fetch=lambda *_: json.dumps({**base, 'bucket_snapshot': snapshot}).encode())
        values = iter([base, {**base, 'bucket_snapshot': {**base['bucket_snapshot'], 'snapshot': 'bha-other'}}])
        with self.assertRaisesRegex(ValueError, 'status changed'):
            subject.inspect_live(get=lambda *_: self.raw, fetch=lambda *_: json.dumps(next(values)).encode())

    def test_foreign_revisions_sidecars_or_moving_pointer_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'sidecar bytes differ'):
            subject.inspect(self.trees, full_sidecars=True, get=self.get, hash_object=lambda *_: '0' * 64)
        other = dict(self.trees); other['0'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'target revisions'):
            subject.inspect(other, get=self.get, hash_object=self.hash)
        with self.assertRaisesRegex(ValueError, 'changed during'):
            subject.inspect(self.trees, get=Mock(side_effect=[self.raw, self.raw + b' ']))
        self.value['sidecars']['docs']['filename'] = '../foreign'
        with self.assertRaisesRegex(ValueError, 'sidecar identity'):
            subject.inspect(self.trees, get=lambda *_: json.dumps(self.value).encode())

    def test_stream_hash_rejects_truncation_and_oversize(self):
        self.assertEqual(subject._hash('generations/bha-1-a/docs.sqlite', 3,
                                       opener=lambda *_a, **_k: Response(b'abc')), hashlib.sha256(b'abc').hexdigest())
        with self.assertRaisesRegex(ValueError, 'truncated'):
            subject._hash('generations/bha-1-a/docs.sqlite', 4, opener=lambda *_a, **_k: Response(b'abc'))
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            subject._hash('generations/bha-1-a/docs.sqlite', 2, opener=lambda *_a, **_k: Response(b'abc'))
        with self.assertRaisesRegex(ValueError, 'invalid'):
            subject._hash('generations/bha-1-a/docs.sqlite', True, opener=lambda *_a, **_k: Response(b'abc'))

    def test_workflow_is_manual_and_has_no_mutating_credentials(self):
        flow = yaml.load((Path(__file__).resolve().parents[1] / '.github/workflows/bha-snapshot-inspect.yml').read_text(),
                         Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'update-bha-index')
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['live_health']['default'], 'false')
        commands = '\n'.join(x.get('run', '') for x in flow['jobs']['inspect']['steps'])
        self.assertIn('scripts/bha_snapshot_inspection.py', commands)
        self.assertIn('--live', commands)
        self.assertNotIn('git push', commands)
        self.assertNotIn('HF_S3_SECRET_ACCESS_KEY', str(flow))


if __name__ == '__main__':
    unittest.main()
