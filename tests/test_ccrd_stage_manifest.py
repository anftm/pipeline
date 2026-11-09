"""A staged CCRD manifest requires every exact object and does not publish."""
import unittest

from scripts import ccrd_stage_manifest as subject
from tests.test_ccrd_stage_objects import MemoryStore, StageObjectTests


class ManifestMemoryStore(MemoryStore):
    def __init__(self, pointer):
        super().__init__(pointer)
        self.raw_manifest = None
        self.manifest_creates = 0
        self.lost_response = False
        self.preserved = {}

    def inspect_preserved(self, key):
        return self.preserved.get(key)

    def read_manifest(self):
        return self.raw_manifest

    def create_manifest(self, raw):
        self.manifest_creates += 1
        if self.raw_manifest is not None:
            raise RuntimeError('conditional conflict')
        self.raw_manifest = raw
        if self.lost_response:
            raise TimeoutError('response lost')


class CcrdStageManifestTests(StageObjectTests):
    def prepared(self):
        plan = self.prepare(); store = ManifestMemoryStore(self.pointer)
        for item in plan['objects']:
            value = {'bytes': item['bytes'], 'sha256': item['sha256']}
            store.objects[item['target']] = value
            if item['source'] == 'parent': store.preserved[item['source_path']] = value
        for value in plan['identity']['preserved_databases'].values():
            store.preserved[value['path']] = {'bytes': value['bytes'], 'sha256': value['sha256']}
        observation = {**plan['identity']['new_text'], 'revision': plan['identity']['source_revision'],
                       'url': plan['identity']['source_url'], 'source_stable': True}
        return plan, store, lambda: observation, lambda: self.observed

    def run_manifest(self, plan, store, capture, observe, *, apply=False):
        return subject.stage_manifest(plan, approved_description=self.description, store=store,
                                      capture=capture, observe=observe, apply=apply, before_create=lambda: None)

    def test_preview_and_conditional_manifest_never_write_pointer(self):
        plan, store, capture, observe = self.prepared()
        result = self.run_manifest(plan, store, capture, observe)
        self.assertFalse(result['manifest_present']); self.assertFalse(result['manifest_written'])
        self.assertEqual(store.manifest_creates, 0)
        result = self.run_manifest(plan, store, capture, observe, apply=True)
        self.assertTrue(result['manifest_present']); self.assertTrue(result['manifest_written'])
        self.assertFalse(result['manifest_write_response_unknown'])
        self.assertFalse(result['pointer_written']); self.assertFalse(result['consumer_switched'])
        self.assertFalse(result['publication_ready']); self.assertFalse(result['external_writers_verified'])
        self.assertEqual(result['references_verified'], len(plan['objects']) + 3)
        self.assertEqual(store.manifest_creates, 1)
        again = self.run_manifest(plan, store, capture, observe, apply=True)
        self.assertTrue(again['manifest_present']); self.assertFalse(again['manifest_written'])
        self.assertEqual(store.manifest_creates, 1)

    def test_manifest_guard_prevents_creation_after_source_or_journal_drift(self):
        plan, store, capture, observe = self.prepared()
        with self.assertRaisesRegex(ValueError, 'requires a fresh publication guard'):
            subject.stage_manifest(plan, approved_description=self.description, store=store,
                                   capture=capture, observe=observe, apply=True)
        self.assertEqual(store.manifest_creates, 0)
        def stopped(): raise ValueError('journal changed')
        with self.assertRaisesRegex(ValueError, 'journal changed'):
            subject.stage_manifest(plan, approved_description=self.description, store=store,
                                   capture=capture, observe=observe, apply=True, before_create=stopped)
        self.assertEqual(store.manifest_creates, 0)
        self.assertIsNone(store.raw_manifest)

    def test_missing_target_preserved_database_or_parent_member_blocks_manifest(self):
        for mode in ('target', 'database', 'parent'):
            with self.subTest(mode=mode):
                plan, store, capture, observe = self.prepared()
                if mode == 'target': store.objects.pop(plan['objects'][0]['target'])
                elif mode == 'database': store.preserved.pop(next(iter(plan['identity']['preserved_databases'].values()))['path'])
                else: store.preserved.pop(next(x['source_path'] for x in plan['objects'] if x['source'] == 'parent'))
                with self.assertRaisesRegex(ValueError, 'missing or mismatched'):
                    self.run_manifest(plan, store, capture, observe, apply=True)
                self.assertEqual(store.manifest_creates, 0)

    def test_source_consumer_or_parent_drift_prevents_write(self):
        plan, store, capture, observe = self.prepared()
        source = capture()
        with self.assertRaisesRegex(ValueError, 'source does not match'):
            self.run_manifest(plan, store, lambda: {**source, 'revision': 'f' * 40}, observe, apply=True)
        with self.assertRaisesRegex(ValueError, 'consumer has not verified'):
            self.run_manifest(plan, store, capture, lambda: {**self.observed, 'cw0_bytes_verified': False}, apply=True)
        store.pointer += b' '
        with self.assertRaises(ValueError): self.run_manifest(plan, store, capture, observe, apply=True)
        self.assertEqual(store.manifest_creates, 0)

    def test_lost_response_reads_exact_manifest_once_and_never_replays(self):
        plan, store, capture, observe = self.prepared(); store.lost_response = True
        result = self.run_manifest(plan, store, capture, observe, apply=True)
        self.assertTrue(result['manifest_written']); self.assertEqual(store.manifest_creates, 1)
        self.assertTrue(result['manifest_write_response_unknown'])
        self.assertFalse(result['pointer_written'])

    def test_existing_foreign_manifest_never_overwrites(self):
        plan, store, capture, observe = self.prepared(); store.raw_manifest = b'foreign'
        with self.assertRaisesRegex(ValueError, 'different bytes'):
            self.run_manifest(plan, store, capture, observe, apply=True)
        self.assertEqual(store.manifest_creates, 0)

    def test_source_changes_between_first_and_second_capture(self):
        plan, store, capture, observe = self.prepared(); first = capture()
        observations = iter([first, {**first, 'sha256': 'f' * 64}])
        with self.assertRaisesRegex(ValueError, 'source does not match'):
            self.run_manifest(plan, store, lambda: next(observations), observe, apply=True)
        self.assertEqual(store.manifest_creates, 0)


if __name__ == '__main__':
    unittest.main()
