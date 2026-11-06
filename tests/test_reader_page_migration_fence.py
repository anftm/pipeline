"""The migration fence must observe every repository; the current run is the only exemption."""
import os
import unittest
from unittest.mock import patch

from scripts.reader_gc_fence import GitHubProducerFence


CENTRAL = 'anftm/pipeline'
OTHER = 'external/pipeline'
WORKFLOW = '.github/workflows/reader-v3-publish.yml'


class MigrationFenceTests(unittest.TestCase):
    def setUp(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': CENTRAL, 'GITHUB_RUN_ID': '123',
               'GITHUB_WORKFLOW_REF': CENTRAL + '/' + WORKFLOW + '@refs/heads/main'}
        self.environment = patch.dict(os.environ, env)
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.scope = patch('scripts.reader_gc_fence.producer_repositories', return_value=[CENTRAL, OTHER])
        self.scope.start(); self.addCleanup(self.scope.stop)
        self.config = patch('scripts.reader_gc_fence.load_config', return_value={'publisher_repository': CENTRAL})
        self.config.start(); self.addCleanup(self.config.stop)
        self.additional = patch('scripts.reader_producer_scope_transition.ADDITIONAL', {OTHER: 1234})
        self.additional.start(); self.addCleanup(self.additional.stop)
        self.workflows = {
            CENTRAL: [{'path': '.github/workflows/pdf-account-worker.yml', 'state': 'disabled_manually'},
                      {'path': WORKFLOW, 'state': 'active'},
                      {'path': '.github/workflows/search-daily-sync.yml', 'state': 'disabled_manually'}],
            OTHER: [{'path': '.github/workflows/pdf-account-worker.yml', 'state': 'disabled_manually'}],
        }
        self.runs = {CENTRAL: [{'id': 123, 'status': 'in_progress', 'path': WORKFLOW}], OTHER: []}
        self.permissions = {OTHER: {'enabled': False}}
        self.visited = []

    def api(self, endpoint):
        parts = endpoint.split('/')
        repo = '/'.join(parts[1:3])
        self.visited.append(repo)
        if endpoint.endswith('/actions/permissions'):
            return [self.permissions[repo]]
        if '/actions/workflows?' in endpoint:
            return [{'workflows': self.workflows[repo]}]
        if '/actions/runs?status=' in endpoint:
            status = endpoint.split('status=', 1)[1].split('&', 1)[0]
            return [{'workflow_runs': [item for item in self.runs[repo] if item['status'] == status]}]
        raise AssertionError(endpoint)

    def check(self):
        return GitHubProducerFence(self.api).check(allow_current_workflow=WORKFLOW, all_workflows=True)

    def test_only_own_exact_run_is_exempt_and_external_scope_is_checked(self):
        report = self.check()
        self.assertEqual(set(self.visited), {CENTRAL, OTHER})
        self.assertFalse(report['external_bucket_credentials_verified'])
        self.assertEqual([item['repository'] for item in report['repositories']], [CENTRAL, OTHER])

    def test_prepared_additional_repositories_expand_rather_than_replace_the_legacy_scope(self):
        with patch('scripts.reader_gc_fence.producer_repositories', return_value=[CENTRAL]):
            report = self.check()
        self.assertEqual([item['repository'] for item in report['repositories']], [CENTRAL, OTHER])
        self.assertIn(OTHER, self.visited)

    def test_active_other_workflow_or_unknown_state_blocks_even_non_reader_name(self):
        for state in ('active', 'unknown'):
            with self.subTest(state=state):
                self.workflows[CENTRAL][-1]['state'] = state
                with self.assertRaisesRegex(RuntimeError, 'not paused|unknown'):
                    self.check()
        self.workflows[CENTRAL][-1]['state'] = 'disabled_manually'
        self.workflows[OTHER][0]['state'] = 'active'
        with self.assertRaisesRegex(RuntimeError, 'not paused'):
            self.check()

    def test_second_publisher_run_or_any_other_running_workflow_blocks(self):
        for item in ({'id': 124, 'status': 'in_progress', 'path': WORKFLOW},
                     {'id': 124, 'status': 'queued', 'path': WORKFLOW},
                     {'id': 124, 'status': 'in_progress', 'path': '.github/workflows/search-daily-sync.yml'}):
            with self.subTest(item=item):
                self.runs[CENTRAL].append(item)
                with self.assertRaisesRegex(RuntimeError, 'not drained'):
                    self.check()
                self.runs[CENTRAL].pop()

    def test_current_run_exemption_is_not_available_to_another_workflow_or_missing_run_id(self):
        for changes in ({'GITHUB_WORKFLOW_REF': CENTRAL + '/.github/workflows/other.yml@refs/heads/main'},
                        {'GITHUB_RUN_ID': ''}, {'GITHUB_RUN_ID': 'not-a-run'}):
            with self.subTest(changes=changes), patch.dict(os.environ, changes):
                with self.assertRaises(RuntimeError):
                    self.check()
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            GitHubProducerFence(self.api).check(allow_current_workflow='.github/workflows/other.yml', all_workflows=True)
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            GitHubProducerFence(self.api).check(allow_current_workflow=WORKFLOW)

    def test_external_repository_actions_must_also_be_disabled(self):
        self.permissions[OTHER]['enabled'] = True
        with self.assertRaisesRegex(RuntimeError, 'repository execution is not disabled'):
            self.check()
        self.permissions[OTHER].clear()
        with self.assertRaisesRegex(RuntimeError, 'repository execution is not disabled'):
            self.check()

    def test_unavailable_or_incomplete_external_inventory_fails_closed(self):
        self.workflows[OTHER].clear()
        with self.assertRaisesRegex(RuntimeError, 'inventory is incomplete'):
            self.check()
        def inaccessible(endpoint):
            if f'repos/{OTHER}/' in endpoint:
                raise RuntimeError('404 is not a fence')
            return self.api(endpoint)
        with self.assertRaisesRegex(RuntimeError, '404 is not a fence'):
            GitHubProducerFence(inaccessible).check(allow_current_workflow=WORKFLOW, all_workflows=True)


if __name__ == '__main__':
    unittest.main()
