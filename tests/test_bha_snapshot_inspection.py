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
        commands = '\n'.join(x.get('run', '') for x in flow['jobs']['inspect']['steps'])
        self.assertIn('scripts/bha_snapshot_inspection.py', commands)
        self.assertNotIn('git push', commands)
        self.assertNotIn('HF_S3_SECRET_ACCESS_KEY', str(flow))


if __name__ == '__main__':
    unittest.main()
