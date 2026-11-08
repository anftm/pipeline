"""The four-source diagnostic never invokes a writer or silently falls back."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from scripts import ccrd_lifecycle as lifecycle, ccrd_source_override as source
from scripts import check_ccrd_four_source as checker
from tests.test_ccrd_lifecycle import CcrdLifecycleTests


class ReadOnlyStore:
    def __init__(self, first, second=None):
        self.first = json.dumps(first, ensure_ascii=False).encode()
        self.second = self.first if second is None else json.dumps(second, ensure_ascii=False).encode()
        self.reads = 0

    def read_bytes(self, bucket, path):
        assert (bucket, path) == (lifecycle.BUCKET, 'current.json')
        self.reads += 1
        return self.first if self.reads == 1 else self.second

    def __getattr__(self, name):
        raise AssertionError('unexpected store access: ' + name)


class FourSourceCheckTests(unittest.TestCase):
    def setUp(self):
        fixture = CcrdLifecycleTests('test_four_source_pointer_retains_all_databases_metadata_and_txt_archives')
        fixture.setUp()
        self.manifest = fixture.four_source_pointer()
        self.raw = 'source text\n'.encode()
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.manifest['source_overrides'] = {str(source.CORPUS_PATH): {'sha256': self.digest, 'bytes': len(self.raw)}}

    def download(self, target):
        target.write_bytes(self.raw)
        return {'sha256': self.digest, 'bytes': len(self.raw)}

    def observe(self, generation, recorded):
        return {'generation': generation, 'cw0': recorded, 'cw0_bytes_verified': True}

    def test_unchanged_and_changed_are_read_only_and_never_authorize_publish(self):
        store = ReadOnlyStore(self.manifest)
        result = checker.check(store, self.download, self.observe)
        self.assertEqual(store.reads, 2)
        self.assertFalse(result['source_changed'])
        self.assertEqual(result['sources'], ['CCRD', 'CW', 'PLA', 'RMRB'])
        self.assertEqual(result['declared_resources'], 7)
        self.assertTrue(result['consumer']['cw0_bytes_verified'])
        self.assertFalse(result['publication_ready'])
        self.assertEqual(result['remote_writes'], 0)
        changed = copy.deepcopy(self.manifest)
        changed['source_overrides'][str(source.CORPUS_PATH)]['sha256'] = 'f' * 64
        result = checker.check(ReadOnlyStore(changed), self.download, self.observe)
        self.assertTrue(result['source_changed'])
        self.assertFalse(result['publication_ready'])

    def test_rejects_malformed_legacy_unreadable_or_moving_manifest(self):
        for mutation in ('legacy', 'missing-source', 'bad-hash', 'invalid-archives'):
            value = copy.deepcopy(self.manifest)
            if mutation == 'legacy': value['format'] = 1
            elif mutation == 'missing-source': value['source_overrides'] = {}
            elif mutation == 'bad-hash': value['source_overrides'][str(source.CORPUS_PATH)]['sha256'] = 'bad'
            else: value['archives'].pop()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                checker.check(ReadOnlyStore(value), self.download, self.observe)
        moved = copy.deepcopy(self.manifest); moved['generation'] = 'ccrd-new'
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            checker.check(ReadOnlyStore(self.manifest, moved), self.download, self.observe)
        class Unreadable(ReadOnlyStore):
            def read_bytes(self, *_args): raise PermissionError('denied')
        with self.assertRaises(PermissionError):
            checker.check(Unreadable(self.manifest), self.download, self.observe)

    def test_rejects_download_identity_errors_and_invalid_utf8(self):
        def bad_identity(path):
            path.write_bytes(b'text')
            return {'sha256': 'a' * 64, 'bytes': 4}
        with self.assertRaisesRegex(ValueError, 'inconsistent'):
            checker.check(ReadOnlyStore(self.manifest), bad_identity, self.observe)
        def invalid(path):
            raw = b'\xff'; path.write_bytes(raw)
            return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        with self.assertRaises(UnicodeDecodeError):
            checker.check(ReadOnlyStore(self.manifest), invalid, self.observe)

    def test_consumer_failure_does_not_produce_a_report(self):
        def failed(*_args):
            raise RuntimeError('consumer unavailable')
        with self.assertRaisesRegex(RuntimeError, 'consumer unavailable'):
            checker.check(ReadOnlyStore(self.manifest), self.download, failed)

    def test_workflow_only_runs_the_readonly_checker(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/publish-ccrd-index.yml'
        flow = yaml.safe_load(path.read_text())
        triggers = flow.get('on', flow.get(True))
        self.assertEqual(set(triggers), {'workflow_dispatch', 'schedule'})
        self.assertEqual(triggers['schedule'], [{'cron': '41 */6 * * *'}])
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'publish-ccrd-index')
        commands = '\n'.join(step.get('run', '') for job in flow['jobs'].values() for step in job['steps'])
        self.assertIn('scripts/check_ccrd_four_source.py', commands)
        for dangerous in ('publish_ccrd_index.py', 'update_ccrd_source_txt.py', 'promote_ccrd_generation.py'):
            self.assertNotIn(dangerous, commands)


if __name__ == '__main__':
    unittest.main()
