"""Pre-pointer rollback checks actual parent and never replays an uncertain commit."""
import json
import unittest
import urllib.error
import urllib.parse
from types import SimpleNamespace

from scripts import ccrd_lifecycle as lifecycle
from scripts import ccrd_publication_coordinator as coordinator
from scripts import ccrd_rollback as rollback
from scripts import ccrd_space_activation as space
from scripts import ccrd_transaction as tx
from tests.test_ccrd_activation_recovery import RecoveryTests
from tests.test_ccrd_metadata_patch import NEW, OLD
from tests.test_ccrd_pointer_transition import PointerMemory
from tests.test_ccrd_space_activation import NEW_REV, REV
from tests.test_ccrd_transaction import MemoryStore


RESTORED = 'f' * 40


class CcrdRollbackTests(RecoveryTests):
    def prepare_journal(self, *, candidate=False):
        plan = self.activation_plan(); self.activation_plan_cache = plan
        journal, pointer = MemoryStore(), PointerMemory(self.pointer)
        coordinator.prepare(journal, parent_pointer=self.pointer, candidate_plan=self.plan_value,
                            description=self.description, activation_plan=plan,
                            pointer_store=pointer, transaction='1' * 32)
        coordinator.start_space(journal)
        if candidate:
            self.api.space_info.return_value = SimpleNamespace(sha=NEW_REV)
            self.api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': NEW_REV})
            activation = {'kind': 'ccrd-space-activation-observation', 'revision': NEW_REV,
                          'generation': self.plan_value['generation'], 'manifest_sha256': self.plan_value['manifest_sha256'],
                          'runtime_observed_twice': True}
            consumed = {'generation': self.plan_value['generation'], 'cw0': self.plan_value['identity']['new_text'],
                        'cw0_bytes_verified': True, 'status_observed_twice': True,
                        'loaded_sources': ['CCRD', 'CW', 'PLA', 'RMRB']}
            coordinator.observe_space(journal, api=self.api, get=self.candidate_get, child_of=lambda *_: True,
                                      observe_activation=lambda *_a, **_k: activation, observe_consumer=lambda *_a: consumed)
        return journal, pointer, plan

    def get(self, url, limit):
        revision = url.split('/resolve/', 1)[1].split('/', 1)[0]
        relative = urllib.parse.unquote(url.split('/resolve/' + revision + '/', 1)[1])
        if relative == 'data/ocr-release.json': raise urllib.error.HTTPError(url, 404, 'missing', {}, None)
        if revision == NEW_REV:
            return self.candidate_get(url, limit)
        if revision in {REV, RESTORED}:
            if relative == space.ACTIVATION: return self.parent_activation
            if relative == str(space.source.CORPUS_PATH): return OLD.encode()
            return (self.parent / relative).read_bytes()
        raise ValueError('unexpected revision')

    def parent_activation_receipt(self, revision):
        return {'kind': 'ccrd-space-activation-observation', 'revision': revision,
                'generation': self.manifest_parent_generation(), 'manifest_sha256': self.pointer_sha,
                'runtime_observed_twice': True}

    def test_existing_parent_is_observed_without_space_or_pointer_write(self):
        journal, pointer, _ = self.prepare_journal()
        started = rollback.start(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: False)
        self.assertEqual(started.record['state'], 'rollback-intent')
        result = rollback.observe(journal, pointer, api=self.api, get=self.get,
                                  observe_activation=lambda *_a, **_k: self.parent_activation_receipt(REV),
                                  observe_consumer=lambda *_a: self.observed)
        self.assertEqual(result['status'], 'parent'); self.assertEqual(tx.load(journal).record['state'], 'rolled-back')
        self.assertEqual(pointer.writes, 0); self.api.create_commit.assert_not_called()

    def test_candidate_rollback_unknown_response_is_not_replayed_and_parent_is_reverified(self):
        journal, pointer, _ = self.prepare_journal(candidate=True)
        rollback.start(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: True)
        def lost(**kwargs):
            self.assertEqual(kwargs['parent_commit'], NEW_REV)
            self.assertEqual(len(kwargs['operations']), 6)
            self.api.space_info.return_value = SimpleNamespace(sha=RESTORED)
            self.api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': RESTORED})
            raise TimeoutError('commit applied, response lost')
        self.api.create_commit.side_effect = lost
        with self.assertRaises(TimeoutError):
            rollback.apply_once(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: True)
        self.assertEqual(tx.load(journal).record['state'], 'rollback-claimed')
        with self.assertRaisesRegex(ValueError, 'expected state'):
            rollback.apply_once(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: True)
        self.api.create_commit.assert_called_once()
        result = rollback.observe(journal, pointer, api=self.api, get=self.get,
                                  child_of=lambda parent, child: (parent, child) == (NEW_REV, RESTORED),
                                  observe_activation=lambda *_a, **_k: self.parent_activation_receipt(RESTORED),
                                  observe_consumer=lambda *_a: self.observed)
        self.assertEqual(result['status'], 'restored-child')
        self.assertEqual(tx.load(journal).record['state'], 'rolled-back')
        self.assertEqual(pointer.raw, self.pointer); self.assertEqual(pointer.writes, 0)
        self.assertEqual(tx.audit(journal, tx.load(journal)), 6)

    def test_foreign_pointer_or_unverified_candidate_cannot_start(self):
        journal, pointer, _ = self.prepare_journal(candidate=True)
        pointer.raw = self.target_manifest
        with self.assertRaisesRegex(ValueError, 'pre-publication parent'):
            rollback.start(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: True)
        self.assertEqual(tx.load(journal).record['state'], 'space-verified')
        self.api.create_commit.assert_not_called()

    def test_unverified_candidate_cannot_start_rollback(self):
        journal, pointer, _ = self.prepare_journal()
        self.api.space_info.return_value = SimpleNamespace(sha=NEW_REV)
        self.api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': NEW_REV})
        with self.assertRaisesRegex(ValueError, 'durably observed'):
            rollback.start(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: True)
        self.assertEqual(tx.load(journal).record['state'], 'space-intent')
        self.api.create_commit.assert_not_called()

    def test_candidate_rollback_does_not_remove_active_ocr_configuration(self):
        journal, pointer, _ = self.prepare_journal(candidate=True)
        self.api.get_space_variables.return_value = {'CCRD_OCR_RELEASE_PATH': SimpleNamespace(value='configured')}
        with self.assertRaisesRegex(ValueError, 'OCR configuration'):
            rollback.start(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: True)
        self.assertEqual(tx.load(journal).record['state'], 'space-verified')
        self.api.create_commit.assert_not_called()

    def test_bad_parent_bytes_consumer_or_wrong_child_never_complete(self):
        journal, pointer, _ = self.prepare_journal(candidate=True)
        with self.assertRaisesRegex(ValueError, 'path differs'):
            rollback.start(journal, pointer, api=self.api, get=lambda *_: b'bad', child_of=lambda *_: True)
        self.assertEqual(tx.load(journal).record['state'], 'space-verified')
        rollback.start(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: True)
        with self.assertRaisesRegex(ValueError, 'without a durable write claim'):
            rollback.observe(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: True,
                             observe_activation=lambda *_a, **_k: self.parent_activation_receipt(NEW_REV),
                             observe_consumer=lambda *_a: self.observed)
        self.assertEqual(tx.load(journal).record['state'], 'rollback-intent')
        self.api.create_commit.assert_not_called()

    def test_parent_consumer_or_activation_drift_cannot_complete(self):
        journal, pointer, _ = self.prepare_journal()
        rollback.start(journal, pointer, api=self.api, get=self.get, child_of=lambda *_: False)
        with self.assertRaisesRegex(ValueError, 'consumer did not verify'):
            rollback.observe(journal, pointer, api=self.api, get=self.get,
                             observe_activation=lambda *_a, **_k: self.parent_activation_receipt(REV),
                             observe_consumer=lambda *_a: {**self.observed, 'cw0_bytes_verified': False})
        values = iter([self.parent_activation_receipt(REV), {**self.parent_activation_receipt(REV), 'revision': '0' * 40}])
        with self.assertRaisesRegex(ValueError, 'activation changed'):
            rollback.observe(journal, pointer, api=self.api, get=self.get,
                             observe_activation=lambda *_a, **_k: next(values), observe_consumer=lambda *_a: self.observed)
        self.assertEqual(tx.load(journal).record['state'], 'rollback-intent')
        self.assertEqual(pointer.writes, 0)
        self.api.create_commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
