"""Staged CCRD readback is bounded and cannot mutate any remote object."""
import hashlib
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import Mock

from scripts import ccrd_stage_readonly as subject
from tests.test_ccrd_stage_manifest import CcrdStageManifestTests


class FakeResponse(BytesIO):
    status = 200


class CcrdStageReadOnlyTests(CcrdStageManifestTests):
    def test_exact_candidate_manifest_and_all_references_required(self):
        plan, store, capture, observe = self.prepared()
        self.run_manifest(plan, store, capture, observe, apply=True)
        result = subject.inspect(plan, self.description, store=store, capture=capture, observe=observe)
        self.assertEqual(result['references_verified'], len(plan['objects']) + 3)
        self.assertFalse(result['publication_ready']); self.assertFalse(result['external_writers_verified'])
        self.assertEqual(result['remote_writes'], 0); self.assertEqual(store.manifest_creates, 1)
        store.raw_manifest = None
        with self.assertRaisesRegex(ValueError, 'not present'):
            subject.inspect(plan, self.description, store=store, capture=capture, observe=observe)
        self.assertEqual(store.manifest_creates, 1)

    def test_readonly_adapter_hashes_and_confines_paths(self):
        gen = 'ccrd-four-' + 'a' * 64
        calls = []
        def open_url(request, timeout):
            calls.append((request.full_url, timeout))
            return FakeResponse(b'abc')
        store = subject.ReadOnlyStore(gen, open_url=open_url)
        key = f'generations/{gen}/data/file.json'
        self.assertEqual(store.inspect(key), {'bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()})
        self.assertEqual(store.read_manifest(), b'abc')
        self.assertEqual(len(calls), 2)
        for key in ('current.json', f'generations/{gen}/manifest.json', f'generations/{gen}/../escape'):
            with self.subTest(key=key), self.assertRaises(ValueError): store.inspect(key)
        with self.assertRaisesRegex(RuntimeError, 'read-only'):
            store.create_manifest(b'{}')

    def test_404_is_absence_but_other_errors_and_oversize_fail(self):
        gen = 'ccrd-four-' + 'a' * 64; key = f'generations/{gen}/data/file.json'
        def not_found(request, **_kw): raise urllib.error.HTTPError(request.full_url, 404, 'missing', {}, None)
        store = subject.ReadOnlyStore(gen, open_url=not_found)
        self.assertIsNone(store.inspect(key)); self.assertIsNone(store.read_manifest())
        def denied(request, **_kw): raise urllib.error.HTTPError(request.full_url, 403, 'denied', {}, None)
        with self.assertRaises(urllib.error.HTTPError): subject.ReadOnlyStore(gen, open_url=denied).inspect(key)
        with unittest.mock.patch.object(subject.objects, 'MAX_BYTES', 2):
            with self.assertRaisesRegex(ValueError, 'bound'):
                subject.ReadOnlyStore(gen, open_url=lambda *_a, **_k: FakeResponse(b'abc')).inspect(key)

    def test_workflow_has_no_mutation_credentials_or_dispatch_path(self):
        import yaml
        from pathlib import Path
        flow = yaml.load((Path(__file__).resolve().parents[1] / '.github/workflows/ccrd-verify-staged.yml').read_text(),
                         Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'publish-ccrd-index')
        steps = flow['jobs']['inspect']['steps']
        commands = '\n'.join(step.get('run', '') for step in steps)
        self.assertIn('scripts/ccrd_stage_readonly.py', commands)
        self.assertIn("run.get('head_sha') != os.environ['STAGE_SHA']", commands)
        for forbidden in ('HF_S3_ACCESS_KEY_ID', 'HF_S3_SECRET_ACCESS_KEY', '--known-writers-coordinated', 'git push'):
            self.assertNotIn(forbidden, str(steps))

    def test_preserved_allowlist_and_larger_database_limit(self):
        gen = 'ccrd-four-' + 'a' * 64
        store = subject.ReadOnlyStore(gen, open_url=lambda *_a, **_k: FakeResponse(b'abc'))
        path = 'indexes/newspapers/RMRB/' + 'f' * 64 + '.sqlite3'
        with unittest.mock.patch.object(subject.objects, 'MAX_BYTES', 2), \
                unittest.mock.patch.object(subject.objects, 'MAX_PRESERVED_DATABASE_BYTES', 4):
            self.assertEqual(store.inspect_preserved(path)['bytes'], 3)
            with self.assertRaisesRegex(ValueError, 'bound'):
                store.inspect_preserved('generations/ccrd-old/CCRD.sqlite3')
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            store.inspect_preserved('control/secret.json')


if __name__ == '__main__':
    unittest.main()
