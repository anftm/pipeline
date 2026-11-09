"""Manual CCRD activation requires exact staged bytes and never retries a write."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import ccrd_auto_activate as subject
from scripts import ccrd_publication_coordinator as coordinator
from scripts import ccrd_transaction as tx
from tests.test_ccrd_metadata_patch import NEW
from tests.test_ccrd_pointer_transition import PointerMemory
from tests.test_ccrd_publication_coordinator import CoordinatorTests
from tests.test_ccrd_transaction import MemoryStore


class AutoActivateTests(CoordinatorTests):
    def bundle(self):
        path = self.root / 'bundle'; path.mkdir()
        (path / 'parent-pointer.json').write_bytes(self.pointer)
        (path / 'description.json').write_bytes(subject.lifecycle.canonical(self.description))
        (path / 'staging-plan.json').write_bytes(subject.lifecycle.canonical(self.plan_value))
        (path / 'stage-result.json').write_bytes(subject.lifecycle.canonical({
            'generation': self.plan_value['generation'], 'manifest_sha256': self.plan_value['manifest_sha256'],
            'candidate_description_sha256': self.description['candidate_identity']}))
        (path / 'new-source.txt').write_bytes(NEW.encode())
        return path

    def download_metadata(self, key, _entry, target):
        prefix = f"generations/{self.plan_value['generation']}/"
        if not key.startswith(prefix):
            raise AssertionError('unexpected staged metadata path')
        source = self.output / key[len(prefix):]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    def start(self, bundle, journal, pointer, *, metadata_root=None, download=None):
        return subject.start(bundle, token='fixture', api=self.api, journal=journal, pointer_store=pointer,
                             store_factory=lambda **_k: self.store, capture=lambda *_: (NEW.encode(), self.capture()),
                             get=self.get, download=download or self.download_metadata,
                             metadata_root=metadata_root or self.root / 'activation-metadata')

    def test_one_activation_persists_intent_then_commits_once(self):
        bundle = self.bundle(); journal = MemoryStore(); pointer = PointerMemory(self.pointer)
        self.store.generation = self.plan_value['generation']; self.api.create_commit.side_effect = TimeoutError('lost')
        with patch.object(subject.stage, '_read_pointer', return_value=self.pointer), \
                patch.object(subject.stage.consumer, 'observe', return_value=self.observed):
            with self.assertRaises(TimeoutError):
                self.start(bundle, journal, pointer)
            self.assertEqual(tx.load(journal).record['state'], 'space-claimed')
            with self.assertRaisesRegex(ValueError, 'unfinished work'):
                self.start(bundle, journal, pointer, metadata_root=self.root / 'second-metadata')
        self.api.create_commit.assert_called_once(); self.assertEqual(pointer.writes, 0)

    def test_missing_manifest_or_foreign_pointer_cannot_create_intent(self):
        bundle = self.bundle(); journal = MemoryStore(); pointer = PointerMemory(b'foreign')
        with self.assertRaisesRegex(ValueError, 'live pointer differs'):
            self.start(bundle, journal, pointer)
        self.assertIsNone(journal.head); self.api.create_commit.assert_not_called()
        pointer.raw = self.pointer; self.store.raw_manifest = b'foreign'
        with self.assertRaisesRegex(ValueError, 'immutable manifest'):
            self.start(bundle, journal, pointer)
        self.assertIsNone(journal.head); self.api.create_commit.assert_not_called()

    def test_corrupt_staged_metadata_cannot_create_a_durable_intent(self):
        bundle = self.bundle(); journal = MemoryStore(); pointer = PointerMemory(self.pointer)
        def corrupt(_key, _entry, target):
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b'corrupt')
        with patch.object(subject.stage, '_read_pointer', return_value=self.pointer), \
                patch.object(subject.stage.consumer, 'observe', return_value=self.observed):
            with self.assertRaisesRegex(ValueError, 'metadata readback differs'):
                self.start(bundle, journal, pointer, download=corrupt)
        self.assertIsNone(journal.head); self.api.create_commit.assert_not_called()

    def test_bundle_rejects_foreign_or_altered_files_before_network_writes(self):
        bundle = self.bundle()
        item = bundle / 'new-source.txt'
        original = item.read_bytes()
        item.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'source does not match'):
            subject._bundle(bundle)
        item.write_bytes(original)
        (bundle / 'unexpected').write_text('foreign')
        with self.assertRaisesRegex(ValueError, 'too many entries|exact approved files'):
            subject._bundle(bundle)
        (bundle / 'unexpected').unlink()
        (bundle / 'foreign-dir').mkdir()
        with self.assertRaisesRegex(ValueError, 'too many entries|unexpected directory'):
            subject._bundle(bundle)
        self.api.create_commit.assert_not_called()

    def test_activation_workflow_requires_reviewed_stage_and_writer_coordination(self):
        import yaml
        flow = yaml.load((Path(__file__).resolve().parents[1] / '.github/workflows/ccrd-activate-staged.yml').read_text(),
                         Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'publish-ccrd-index')
        inputs = flow['on']['workflow_dispatch']['inputs']
        self.assertEqual(inputs['known_writers_coordinated']['default'], 'false')
        commands = '\n'.join(step.get('run', '') for step in flow['jobs']['activate']['steps'])
        self.assertIn("run.get('head_sha') != os.environ['GITHUB_SHA']", commands)
        self.assertIn('scripts/ccrd_auto_activate.py', commands)
        self.assertNotIn('git push', commands)

    def test_refuses_symlinked_or_oversized_bundle_input(self):
        bundle = self.bundle(); path = bundle / 'new-source.txt'
        path.unlink(); path.symlink_to(self.root / 'outside')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            subject._path(bundle, 'new-source.txt', 1024)


if __name__ == '__main__':
    unittest.main()
