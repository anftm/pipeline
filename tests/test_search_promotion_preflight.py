"""Search preflight is read-only and treats missing provenance as failure."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from scripts import search_promotion_preflight as subject
from scripts.search_publication_pointer import make_pointer
from tests.test_search_adopt import SearchAdoptionTests
from tests.test_search_generation import compact_bytes


class Hub:
    def __init__(self, objects, pointer): self.objects = objects; self.pointer = pointer; self.reads = 0
    def read_bytes(self, _bucket, key):
        if key == 'published/current.json': self.reads += 1; return self.pointer
        return self.objects[key]


class SearchPromotionPreflightTests(SearchAdoptionTests):
    def setUp(self):
        super().setUp()
        self.parent = 'f' * 64; self.pointer = make_pointer(self.parent, None, '1' * 32)
        self.live = Hub(self.hub.objects, self.pointer)
        self.previous = {'generation': self.parent, 'transition': '1' * 32, 'both_consumers_observed': True}
        self.session = Mock(); self.api = Mock()
        self.patches = [patch.object(subject.fetch, 'REPOS', ['VoiceOfML/Test']),
                        patch.object(subject.daily, 'inspect_published', return_value=self.previous),
                        patch.object(subject.defaults, 'observe_pages', return_value=compact_bytes({'generation': self.parent})),
                        patch.object(subject.defaults, 'observe_space', return_value=compact_bytes({'generation': self.parent}))]
        for item in self.patches: item.start(); self.addCleanup(item.stop)

    def run_preflight(self, *, revision=lambda _repo, _token: 'a' * 40):
        return subject.inspect(self.generation, hub=self.live, session=self.session, hf_api=self.api, revision=revision)

    def test_valid_candidate_binds_parent_sources_and_objects_but_does_not_publish(self):
        result = self.run_preflight()
        self.assertEqual(result['candidate'], self.generation); self.assertEqual(result['parent'], self.parent)
        self.assertEqual(result['candidate_records'], 1); self.assertGreater(result['candidate_objects_verified'], 0)
        self.assertTrue(result['parent_consumers_observed_twice']); self.assertFalse(result['publication_ready'])
        self.assertFalse(result['external_writers_verified']); self.assertEqual(result['remote_writes'], 0)
        self.assertEqual(self.live.reads, 2)
        self.session.post.assert_not_called(); self.api.add_space_variable.assert_not_called()

    def test_workflow_is_manual_readonly_and_serialized_with_daily_writer(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/search-promotion-preflight.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['group'], 'daily-sync')
        self.assertEqual(flow['on']['workflow_dispatch']['inputs']['allow_current']['default'], 'false')
        commands = '\n'.join(step.get('run', '') for step in flow['jobs']['inspect']['steps'])
        self.assertIn('scripts/search_promotion_preflight.py', commands)
        for dangerous in ('sync_to_pages.py', 'sync_to_space.py', 'git push', '--apply'):
            self.assertNotIn(dangerous, commands)

    def test_current_generation_is_only_allowed_for_explicit_readonly_inspection(self):
        self.live.pointer = make_pointer(self.generation, None, '1' * 32)
        self.previous['generation'] = self.generation
        with patch.object(subject.defaults, 'observe_pages', return_value=compact_bytes({'generation': self.generation})), \
                patch.object(subject.defaults, 'observe_space', return_value=compact_bytes({'generation': self.generation})):
            with self.assertRaisesRegex(ValueError, 'already the desired'):
                self.run_preflight()
            result = subject.inspect(self.generation, hub=self.live, session=self.session, hf_api=self.api,
                                     revision=lambda *_: 'a' * 40, allow_current=True)
        self.assertTrue(result['candidate_already_current']); self.assertFalse(result['publication_ready'])
        self.assertEqual(result['remote_writes'], 0)

    def test_changed_or_unavailable_source_revision_never_passes(self):
        with self.assertRaisesRegex(ValueError, 'source revisions differ'):
            self.run_preflight(revision=lambda *_: '')
        values = iter(['a' * 40, 'b' * 40])
        with self.assertRaisesRegex(ValueError, 'changed during'):
            self.run_preflight(revision=lambda *_: next(values))

    def test_changed_parent_or_corrupt_object_fails_closed(self):
        with patch.object(subject.daily, 'inspect_published', return_value={**self.previous, 'transition': '2' * 32}), \
                self.assertRaisesRegex(ValueError, 'pointer changed'):
            self.run_preflight()
        key = self.manifest['objects']['pages']['search_data.json.gz']['object']
        original = self.live.objects[key]; self.live.objects[key] = b'bad'
        with self.assertRaisesRegex(ValueError, 'readback mismatch'):
            self.run_preflight()
        self.live.objects[key] = original

    def test_changed_actual_consumer_receipt_or_default_stops(self):
        changed = {**self.previous, 'transition': '2' * 32}
        with patch.object(subject.daily, 'inspect_published', side_effect=[self.previous, changed]), \
                self.assertRaisesRegex(ValueError, 'consumer evidence changed'):
            self.run_preflight()
        with patch.object(subject.defaults, 'observe_pages', side_effect=ValueError('unknown default')), \
                self.assertRaisesRegex(ValueError, 'unknown default'):
            self.run_preflight()


if __name__ == '__main__': unittest.main()
