"""A search promotion intent cannot itself deploy or claim completed rollout."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import search_promotion_intent as intent
from scripts import search_snapshot
from scripts.search_publication_pointer import make_pointer
from tests.test_search_transition import MemoryStore, receipt

A, B, OP = 'a' * 64, 'b' * 64, '1' * 32


def default(site, generation):
    return search_snapshot.canonical({'version': 1, 'kind': 'search-default-observation', 'site': site,
                                      'generation': generation, 'observed_at': '2026-11-08T00:00:00Z', 'details': {}})


class Hub:
    def __init__(self): self.pointer = make_pointer(A, None, '0' * 32)
    def read_bytes(self, _bucket, key):
        if key == 'published/current.json': return self.pointer
        raise AssertionError(key)


class PromotionIntentTests(unittest.TestCase):
    def setUp(self):
        self.hub, self.store = Hub(), MemoryStore()
        self.store.pointer = self.hub.pointer
        revisions = {'VoiceOfML/Test': 'c' * 40}
        import hashlib
        self.check = {'parent': A, 'parent_transition': '0' * 32, 'source_revisions': revisions,
                      'source_revisions_sha256': hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest()}
        self.session, self.api = Mock(), Mock()
        self.patches = [patch.object(intent.preflight, 'inspect', return_value=self.check),
                        patch.object(intent.adoption, 'validate_generation', return_value={'generation': A}),
                        patch.object(intent.receipts, 'observe', side_effect=lambda site, *_a, **_k: receipt(site, A)),
                        patch.object(intent.defaults, 'observe_pages', return_value=default('pages', A)),
                        patch.object(intent.defaults, 'observe_space', return_value=default('space', A))]
        for item in self.patches: item.start(); self.addCleanup(item.stop)

    def run_prepare(self):
        return intent.prepare(B, hub=self.hub, session=self.session, api=self.api, store=self.store,
                              get=lambda *_: b'', revision=lambda *_: '', transaction_id=OP)

    def test_workflow_is_manual_dry_by_default_and_requires_writer_declaration(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/search-promotion-intent.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        inputs = flow['on']['workflow_dispatch']['inputs']
        self.assertEqual(inputs['apply']['default'], 'false')
        self.assertEqual(inputs['known_writers_coordinated']['default'], 'false')
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'daily-sync')
        commands = '\n'.join(step.get('run', '') for step in flow['jobs']['prepare']['steps'])
        self.assertIn('--known-writers-coordinated', commands)
        self.assertNotIn('sync_to_pages.py', commands); self.assertNotIn('sync_to_space.py', commands)

    def test_verified_parent_prepares_only_a_durable_journal(self):
        result = self.run_prepare()
        self.assertEqual(result['state'], 'prepared'); self.assertEqual(result['transaction'], OP)
        self.assertTrue(result['journal_written']); self.assertFalse(result['pointer_written'])
        self.assertFalse(result['sites_changed']); self.assertFalse(result['publication_ready'])
        self.assertFalse(result['external_writers_verified']); self.assertEqual(self.store.pointer, self.hub.pointer)
        self.session.post.assert_not_called(); self.api.add_space_variable.assert_not_called()

    def test_preflight_drift_or_stale_parent_never_writes_journal(self):
        with patch.object(intent.preflight, 'inspect', side_effect=[self.check, {**self.check, 'parent': 'f' * 64}]), \
                self.assertRaisesRegex(ValueError, 'changed before durable intent'):
            self.run_prepare()
        self.assertIsNone(self.store.head)
        self.hub.pointer = make_pointer(A, None, '2' * 32)
        with self.assertRaisesRegex(ValueError, 'parent pointer changed'):
            self.run_prepare()
        self.assertIsNone(self.store.head)

    def test_unknown_default_or_existing_transition_never_switches_sites(self):
        with patch.object(intent.defaults, 'observe_pages', side_effect=ValueError('unknown default')), \
                self.assertRaisesRegex(ValueError, 'unknown default'):
            self.run_prepare()
        self.assertIsNone(self.store.head)
        self.run_prepare()
        with self.assertRaisesRegex(ValueError, 'unfinished work'): self.run_prepare()
        self.session.post.assert_not_called(); self.api.add_space_variable.assert_not_called()


if __name__ == '__main__': unittest.main()
