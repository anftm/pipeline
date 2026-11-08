"""Uncertain Space commit recovery reads exact state and never replays writes."""
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch

from scripts import ccrd_activation_recovery as recovery
from scripts import ccrd_lifecycle
from scripts import ccrd_space_activation as space
from tests.test_ccrd_metadata_patch import NEW
from tests.test_ccrd_space_activation import NEW_REV, REV, CcrdSpaceActivationTests


class RecoveryTests(CcrdSpaceActivationTests):
    def candidate_get(self, url, _limit):
        relative = urllib.parse.unquote(url.split('/resolve/' + NEW_REV + '/', 1)[1])
        if relative == space.ACTIVATION:
            return ccrd_lifecycle.canonical(self.activation_plan_cache['activation'])
        if relative == str(space.source.CORPUS_PATH): return NEW.encode()
        return (self.output / relative).read_bytes()

    def test_no_commit_observes_exact_parent_without_writes(self):
        plan = self.activation_plan()
        result = recovery.observe(plan, api=self.api, get=self.get)
        self.assertEqual(result['status'], 'parent'); self.assertEqual(result['revision'], REV)
        self.assertEqual(result['files_verified'], 6); self.assertEqual(result['remote_writes'], 0)
        self.api.create_commit.assert_not_called()

    def test_exact_direct_child_and_all_six_candidate_files_required(self):
        self.activation_plan_cache = self.activation_plan()
        self.api.space_info.return_value = SimpleNamespace(sha=NEW_REV)
        self.api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': NEW_REV})
        result = recovery.observe(self.activation_plan_cache, api=self.api, get=self.candidate_get,
                                  child_of=lambda parent, child: (parent, child) == (REV, NEW_REV))
        self.assertEqual(result['status'], 'candidate'); self.assertEqual(result['files_verified'], 6)
        self.assertFalse(result['consumer_content_verified']); self.api.create_commit.assert_not_called()

    def test_foreign_child_missing_bytes_or_building_stops(self):
        self.activation_plan_cache = self.activation_plan()
        self.api.space_info.return_value = SimpleNamespace(sha=NEW_REV)
        self.api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': NEW_REV})
        with self.assertRaisesRegex(ValueError, 'direct child'):
            recovery.observe(self.activation_plan_cache, api=self.api, get=self.candidate_get, child_of=lambda *_: False)
        with self.assertRaisesRegex(ValueError, 'path differs'):
            recovery.observe(self.activation_plan_cache, api=self.api, get=lambda *_: b'wrong', child_of=lambda *_: True)
        self.api.get_space_runtime.return_value.stage = 'RUNNING_BUILDING'
        with self.assertRaisesRegex(ValueError, 'runtime does not match'):
            recovery.observe(self.activation_plan_cache, api=self.api, get=self.candidate_get, child_of=lambda *_: True)
        self.api.create_commit.assert_not_called()

    def test_bad_intent_or_moving_head_fails_before_recovery(self):
        plan = self.activation_plan(); bad = {**plan, 'candidate_generation': 'foreign'}
        with self.assertRaisesRegex(ValueError, 'intent hash differs'):
            recovery.observe(bad, api=self.api, get=self.get)
        self.api.space_info.side_effect = [SimpleNamespace(sha=REV), SimpleNamespace(sha=NEW_REV)]
        with self.assertRaisesRegex(RuntimeError, 'changed during'):
            recovery.observe(plan, api=self.api, get=self.get)
        self.api.create_commit.assert_not_called()

    def test_git_parent_check_requires_exact_single_parent(self):
        import subprocess
        calls = []
        def run(command, **_kwargs):
            calls.append(command)
            return SimpleNamespace(stdout=f'{NEW_REV} {REV}\n' if 'rev-list' in command else '')
        with patch.object(recovery.subprocess, 'run', side_effect=run):
            self.assertTrue(recovery.direct_child(REV, NEW_REV))
        self.assertEqual(len(calls), 3)
        with patch.object(recovery.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 1)), \
                self.assertRaises(subprocess.TimeoutExpired):
            recovery.direct_child(REV, NEW_REV)


if __name__ == '__main__':
    unittest.main()
