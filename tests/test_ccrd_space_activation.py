"""CCRD Space activation commits only six exact allowlisted files."""
import json
import unittest
import urllib.error
import urllib.parse
from types import SimpleNamespace
from unittest.mock import Mock

from scripts import ccrd_lifecycle, ccrd_staging_plan
from scripts import ccrd_space_activation as subject
from tests.test_ccrd_candidate_assembly import CandidateAssemblyTests
from tests.test_ccrd_metadata_patch import NEW, OLD
from tests.test_ccrd_stage_manifest import ManifestMemoryStore

REV, NEW_REV = 'c' * 40, 'e' * 40


class CcrdSpaceActivationTests(CandidateAssemblyTests):
    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        with patch.object(ccrd_staging_plan.assembly, '_cw_database',
                          return_value=({'sha256': 'c' * 64, 'bytes': 42}, {'seconds': 0.1})):
            self.description = self.make()
        self.plan_value = ccrd_staging_plan.make(self.pointer, self.description)
        self.target_manifest = ccrd_lifecycle.canonical(self.plan_value['manifest'])
        self.store = ManifestMemoryStore(self.pointer); self.store.raw_manifest = self.target_manifest
        for entry in self.plan_value['objects']:
            expected = {'bytes': entry['bytes'], 'sha256': entry['sha256']}
            self.store.objects[entry['target']] = expected
            if entry['source'] == 'parent': self.store.preserved[entry['source_path']] = expected
        for entry in self.plan_value['identity']['preserved_databases'].values():
            self.store.preserved[entry['path']] = {key: entry[key] for key in ('bytes', 'sha256')}
        self.capture = lambda: {**self.plan_value['identity']['new_text'],
                                'revision': self.plan_value['identity']['source_revision'],
                                'url': self.plan_value['identity']['source_url'], 'source_stable': True}
        self.api = Mock(); self.api.space_info.return_value = SimpleNamespace(sha=REV)
        self.api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': REV})
        self.api.get_space_variables.return_value = {}
        self.parent_activation = json.dumps({'version': 1, 'kind': 'ccrd-index-activation',
                                             'bucket': 'vomebook/ccrd-index',
                                             'path': f"generations/{self.manifest_parent_generation()}/manifest.json",
                                             'sha256': self.pointer_sha}).encode()

    def manifest_parent_generation(self):
        return json.loads(self.pointer)['generation']

    def get(self, url, _limit):
        relative = urllib.parse.unquote(url.split('/resolve/' + REV + '/', 1)[1])
        if relative == subject.ACTIVATION: return self.parent_activation
        if relative == 'data/ocr-release.json': raise urllib.error.HTTPError(url, 404, 'not found', {}, None)
        if relative == str(subject.source.CORPUS_PATH): return OLD.encode()
        return (self.parent / relative).read_bytes()

    def activation_plan(self):
        return subject.plan(self.pointer, self.plan_value, self.description, api=self.api, space_revision=REV,
                            store=self.store, capture=self.capture, observe=lambda: self.observed, get=self.get,
                            current_manifest=self.pointer, candidate_manifest=self.target_manifest)

    def test_exact_plan_rechecks_parent_staging_and_space_files(self):
        result = self.activation_plan()
        self.assertEqual(result['candidate_generation'], self.plan_value['generation'])
        self.assertEqual(len(result['parent_files']), 5)
        self.assertEqual(result['parent_revision'], REV)
        self.assertFalse(result['publication_ready']); self.assertEqual(result['remote_writes'], 0)
        self.assertEqual(result, self.activation_plan())

    def test_existing_ocr_activation_is_not_silently_dropped(self):
        self.api.get_space_variables.return_value = {'CCRD_OCR_RELEASE_PATH': SimpleNamespace(value='release')}
        with self.assertRaisesRegex(ValueError, 'OCR configuration'):
            self.activation_plan()
        self.api.get_space_variables.return_value = {}
        def get_with_ocr(url, limit):
            if url.endswith('/data/ocr-release.json'): return b'{}'
            return self.get(url, limit)
        with self.assertRaisesRegex(ValueError, 'OCR activation file'):
            subject.plan(self.pointer, self.plan_value, self.description, api=self.api, space_revision=REV,
                         store=self.store, capture=self.capture, observe=lambda: self.observed, get=get_with_ocr,
                         current_manifest=self.pointer, candidate_manifest=self.target_manifest)
        self.api.create_commit.assert_not_called()

    def test_wrong_manifest_parent_or_staged_object_fails_closed(self):
        key = self.plan_value['objects'][0]['target']; del self.store.objects[key]
        with self.assertRaisesRegex(ValueError, 'staged member'):
            self.activation_plan()
        self.assertIsNone(self.store.objects.get(key))
        self.api.create_commit.assert_not_called()

    def test_apply_is_one_parent_guarded_commit_of_exact_paths(self):
        planned = self.activation_plan()
        self.api.create_commit.return_value = SimpleNamespace(oid=NEW_REV)
        self.api.space_info.side_effect = [SimpleNamespace(sha=REV), SimpleNamespace(sha=REV),
                                           SimpleNamespace(sha=REV), SimpleNamespace(sha=NEW_REV)]
        result = subject.apply(planned, self.pointer, self.plan_value, self.description, api=self.api, store=self.store,
                               capture=self.capture, observe=lambda: self.observed, candidate_root=self.output,
                               new_text=NEW.encode(), current_manifest=self.pointer, candidate_manifest=self.target_manifest,
                               confirm_plan_sha256=planned['plan_sha256'], get=self.get)
        self.assertEqual(result['revision'], NEW_REV); self.assertFalse(result['consumer_verified'])
        self.assertFalse(result['pointer_written']); self.assertFalse(result['external_writers_verified'])
        self.assertEqual(self.api.create_commit.call_count, 1)
        kwargs = self.api.create_commit.call_args.kwargs
        self.assertEqual(kwargs['parent_commit'], REV)
        self.assertEqual({x.path_in_repo for x in kwargs['operations']}, set(result['files_written']))
        self.assertEqual(len(result['files_written']), 6)
        self.assertTrue(all(isinstance(x.path_or_fileobj, bytes) for x in kwargs['operations']))

    def test_source_or_parent_drift_at_final_boundary_never_commits(self):
        planned = self.activation_plan()
        count = []
        def capture():
            count.append(True)
            value = self.capture()
            return {**value, 'revision': 'f' * 40} if len(count) == 3 else value
        with self.assertRaisesRegex(ValueError, 'source does not match'):
            subject.apply(planned, self.pointer, self.plan_value, self.description, api=self.api, store=self.store,
                          capture=capture, observe=lambda: self.observed, candidate_root=self.output,
                          new_text=NEW.encode(), current_manifest=self.pointer, candidate_manifest=self.target_manifest,
                          confirm_plan_sha256=planned['plan_sha256'], get=self.get)
        self.api.create_commit.assert_not_called()

    def test_unknown_commit_response_is_never_retried(self):
        planned = self.activation_plan(); self.api.create_commit.side_effect = TimeoutError('uncertain')
        with self.assertRaises(TimeoutError):
            subject.apply(planned, self.pointer, self.plan_value, self.description, api=self.api, store=self.store,
                          capture=self.capture, observe=lambda: self.observed, candidate_root=self.output,
                          new_text=NEW.encode(), current_manifest=self.pointer, candidate_manifest=self.target_manifest,
                          confirm_plan_sha256=planned['plan_sha256'], get=self.get)
        self.assertEqual(self.api.create_commit.call_count, 1)

    def test_corrupt_metadata_or_plan_never_commits(self):
        planned = self.activation_plan(); (self.output / subject.metadata.PATHS[0]).write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            subject.apply(planned, self.pointer, self.plan_value, self.description, api=self.api, store=self.store,
                          capture=self.capture, observe=lambda: self.observed, candidate_root=self.output,
                          new_text=NEW.encode(), current_manifest=self.pointer, candidate_manifest=self.target_manifest,
                          confirm_plan_sha256=planned['plan_sha256'], get=self.get)
        self.api.create_commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
