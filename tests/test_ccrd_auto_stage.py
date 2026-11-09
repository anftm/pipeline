"""Scheduled CCRD candidate work never treats staging as publication."""
import hashlib
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from scripts import ccrd_auto_stage as subject
from tests.test_ccrd_candidate_assembly import CandidateAssemblyTests
from tests.test_ccrd_metadata_patch import OLD, NEW


class AutoStageTests(CandidateAssemblyTests):
    def inputs(self):
        new = NEW.encode()
        capture = lambda _token: (new, self.source_observation)
        observe = lambda *_: self.observed
        get = lambda *_: OLD.encode()
        return {'token': 'fixture', 'read_pointer': lambda: self.pointer, 'capture': capture,
                'observe': observe, 'get': get}

    def test_unchanged_source_returns_without_build_or_bucket_credentials(self):
        identity = self.manifest['source_overrides'][str(subject.space.source.CORPUS_PATH)]
        capture = lambda _token: (OLD.encode(), {'bytes': len(OLD.encode()),
                    'sha256': hashlib.sha256(OLD.encode()).hexdigest(), 'source_stable': True})
        no_journal = Mock(side_effect=AssertionError('no writes or credentials needed'))
        result = subject.run(self.root / 'work', apply=True, token='fixture', read_pointer=lambda: self.pointer,
                             capture=capture, observe=lambda *_: self.observed, journal_factory=no_journal)
        self.assertEqual(result['status'], 'unchanged'); self.assertEqual(result['remote_writes'], 0)
        self.assertFalse((self.root / 'work').exists()); no_journal.assert_not_called()
        self.assertEqual(identity['sha256'], hashlib.sha256(OLD.encode()).hexdigest())

    def test_changed_source_is_built_then_staged_and_never_published(self):
        work = self.root / 'work'; downloads = []
        def download(key, entry, path):
            downloads.append(key); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'local fixture')
        def build(_p, _old, _new, **kwargs):
            root = kwargs['output']; root.mkdir()
            (root / 'description.json').write_text('{}')
            (root / 'staging-plan.json').write_text(json.dumps({'generation': 'ccrd-four-' + 'a' * 64,
                                                               'manifest_sha256': 'b' * 64}))
            return {'generation': 'ccrd-four-' + 'a' * 64, 'remote_writes': 0}
        store, journal = Mock(), Mock()
        store.parent_pointer.return_value = self.pointer
        with patch.object(subject.local, 'build', side_effect=build) as built, \
                patch.object(subject.tx, 'load', return_value=None), \
                patch.object(subject.objects, 'stage', return_value={'objects_verified': 70, 'objects_created': 70}) as stage, \
                patch.object(subject.manifests, 'stage_manifest', return_value={'manifest_written': True}) as manifest:
            result = subject.run(work, apply=True, download=download, store_factory=lambda **_k: store,
                                 journal_factory=lambda: journal, **self.inputs())
        self.assertEqual(result['status'], 'staged'); self.assertFalse(result['pointer_written'])
        self.assertFalse(result['consumer_switched']); self.assertFalse(result['publication_ready'])
        self.assertEqual(len(downloads), len(self.manifest['metadata']) + len(self.manifest['archives']) + 1)
        built.assert_called_once(); stage.assert_called_once(); manifest.assert_called_once()
        self.assertTrue(manifest.call_args.kwargs['apply']); self.assertTrue((work / 'report.json').exists())

    def test_changed_source_drift_or_active_transaction_stops_before_stage(self):
        work = self.root / 'work'; args = self.inputs()
        with patch.object(subject.tx, 'load', return_value=SimpleNamespace(record={'state': 'space-intent'})), \
                patch.object(subject.tx, 'audit'), patch.object(subject.local, 'build') as build:
            with self.assertRaisesRegex(ValueError, 'unfinished'):
                subject.run(work, apply=True, journal_factory=lambda: Mock(), **args)
        build.assert_not_called(); self.assertFalse(work.exists())
        broken = {**self.source_observation, 'sha256': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'capture identity'):
            subject.run(work, apply=False, **{**args, 'capture': lambda *_: (NEW.encode(), broken)})
        self.assertFalse(work.exists())

    def test_download_enforces_length_hash_and_never_overwrites(self):
        path = self.root / 'download'; raw = b'good'
        response = BytesIO(raw); response.status = 200
        identity = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        with patch.object(subject, 'urlopen', return_value=response):
            subject._download('generations/test/file', identity, path)
        self.assertEqual(path.read_bytes(), raw)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            subject._download('generations/test/file', identity, path)
        with self.assertRaises(ValueError):
            subject._download('../outside', identity, self.root / 'other')
        bad = BytesIO(raw + b'!'); bad.status = 200
        with patch.object(subject, 'urlopen', return_value=bad), self.assertRaisesRegex(ValueError, 'exceeds'):
            subject._download('generations/test/file', identity, self.root / 'bad')

    def test_workflow_stages_only_on_change_and_never_activates(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/publish-ccrd-index.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(flow['concurrency']['group'], 'publish-ccrd-index')
        self.assertIn('schedule', flow['on'])
        jobs = flow['jobs']; commands = '\n'.join(step.get('run', '') for job in jobs.values() for step in job['steps'])
        self.assertIn('scripts/ccrd_auto_stage.py', commands)
        self.assertNotIn('update_ccrd_source_txt.py', commands)
        self.assertNotIn('ccrd_publication_coordinator', commands)
        self.assertNotIn('git push', commands)
        self.assertNotIn('ccrd_rollback', commands)


if __name__ == '__main__':
    unittest.main()
