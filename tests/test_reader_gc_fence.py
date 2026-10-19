import unittest
from unittest.mock import patch

from scripts.reader_gc_fence import GitHubProducerFence


class ResidualRunFenceTests(unittest.TestCase):
    def api(self, endpoint):
        run = {'id': 7, 'status': 'queued', 'run_attempt': 1,
               'updated_at': '2026-10-11T00:00:00Z', 'check_suite_id': 8}
        if endpoint.endswith('/actions/permissions'):
            return [{'enabled': False}]
        if '/actions/workflows?' in endpoint:
            return [{'workflows': [{'path': '.github/workflows/pdf-account-worker.yml',
                                    'state': 'disabled_manually'}]}]
        if '/actions/runs?status=queued' in endpoint:
            return [{'workflow_runs': [{**run, 'path': '.github/workflows/pdf-account-worker.yml'}]}]
        if '/actions/runs?status=' in endpoint:
            return [{'workflow_runs': []}]
        if '/jobs?filter=latest' in endpoint:
            return [{'total_count': 0, 'jobs': []}]
        if '/jobs?filter=all' in endpoint:
            return [{'total_count': 1, 'jobs': [{'status': 'completed', 'run_attempt': 1,
                                               'completed_at': '2026-10-10T00:00:00Z'}]}]
        if '/check-suites/8/check-runs' in endpoint:
            return [{'total_count': 0, 'check_runs': []}]
        if '/attempts/1' in endpoint:
            return [{**run, 'status': 'completed', 'conclusion': 'cancelled'}]
        if endpoint.endswith('/actions/runs/7'):
            return [run]
        raise AssertionError(endpoint)

    def check(self, api):
        with patch('scripts.reader_gc_fence.producer_repositories', return_value=['owner/repo']):
            return GitHubProducerFence(api).check()

    def test_terminal_attempt_is_accepted_only_with_repository_execution_disabled(self):
        report = self.check(self.api)
        residual = report['repositories'][0]['isolated_residual_runs'][0]
        self.assertEqual(residual['attempt_conclusion'], 'cancelled')
        self.assertEqual(residual['latest_jobs'], 0)
        self.assertIs(residual['repository_actions_enabled'], False)

    def test_zero_jobs_with_enabled_actions_still_blocks(self):
        def api(endpoint):
            return [{'enabled': True}] if endpoint.endswith('/actions/permissions') else self.api(endpoint)
        with self.assertRaisesRegex(RuntimeError, 'not drained'):
            self.check(api)

    def test_cancelled_before_job_assignment_requires_the_same_isolation(self):
        def api(endpoint):
            if '/jobs?filter=all' in endpoint:
                return [{'total_count': 0, 'jobs': []}]
            return self.api(endpoint)
        report = self.check(api)
        self.assertEqual(report['repositories'][0]['isolated_residual_runs'][0]['historical_jobs'], 0)

    def test_central_terminal_residual_needs_no_repository_policy(self):
        def api(endpoint):
            if endpoint.endswith('/actions/permissions'):
                raise RuntimeError('central policy is not readable')
            return self.api(endpoint)
        with patch('scripts.reader_gc_fence.producer_repositories', return_value=['owner/repo']), \
                patch('scripts.reader_gc_fence.load_config',
                      return_value={'publisher_repository': 'owner/repo'}):
            report = GitHubProducerFence(api).check()
        residual = report['repositories'][0]['isolated_residual_runs'][0]
        self.assertEqual(residual['attempt_conclusion'], 'cancelled')
        self.assertFalse(residual['ghost_residual'])

    def test_central_old_run_without_jobs_is_a_ghost(self):
        def api(endpoint):
            if endpoint.endswith('/actions/permissions'):
                raise RuntimeError('central policy is not readable')
            if '/jobs?filter=all' in endpoint:
                return [{'total_count': 0, 'jobs': []}]
            if '/attempts/1' in endpoint:
                return [{'id': 7, 'status': 'queued', 'run_attempt': 1}]
            return self.api(endpoint)
        with patch('scripts.reader_gc_fence.producer_repositories', return_value=['owner/repo']), \
                patch('scripts.reader_gc_fence.load_config',
                      return_value={'publisher_repository': 'owner/repo'}):
            report = GitHubProducerFence(api).check()
        residual = report['repositories'][0]['isolated_residual_runs'][0]
        self.assertTrue(residual['ghost_residual'])
        self.assertEqual(residual['historical_jobs'], 0)

    def test_noncentral_run_without_jobs_still_blocks(self):
        def api(endpoint):
            if '/jobs?filter=all' in endpoint:
                return [{'total_count': 0, 'jobs': []}]
            if '/attempts/1' in endpoint:
                return [{'id': 7, 'status': 'queued', 'run_attempt': 1}]
            return self.api(endpoint)
        with self.assertRaisesRegex(RuntimeError, 'no terminal execution attempt'):
            self.check(api)

    def test_central_recent_run_without_jobs_still_blocks(self):
        def api(endpoint):
            if endpoint.endswith('/actions/permissions'):
                raise RuntimeError('central policy is not readable')
            if '/jobs?filter=all' in endpoint:
                return [{'total_count': 0, 'jobs': []}]
            if '/attempts/1' in endpoint:
                return [{'id': 7, 'status': 'queued', 'run_attempt': 1}]
            if endpoint.endswith('/actions/runs/7'):
                return [{'id': 7, 'status': 'queued', 'run_attempt': 1,
                         'updated_at': '2026-10-19T23:59:00Z', 'check_suite_id': 8}]
            return self.api(endpoint)
        with patch('scripts.reader_gc_fence.producer_repositories', return_value=['owner/repo']), \
                patch('scripts.reader_gc_fence.load_config',
                      return_value={'publisher_repository': 'owner/repo'}):
            with self.assertRaisesRegex(RuntimeError, 'no terminal execution attempt'):
                GitHubProducerFence(api).check()

    def test_unfinished_attempt_or_job_blocks(self):
        for marker, response in (
            ('/attempts/1', [{'id': 7, 'status': 'queued', 'run_attempt': 1}]),
            ('/jobs?filter=latest', [{'total_count': 1, 'jobs': [{'status': 'queued'}]}]),
            ('/jobs?filter=all', [{'jobs': [{'status': 'in_progress'}]}])):
            with self.subTest(marker=marker):
                def api(endpoint):
                    return response if marker in endpoint else self.api(endpoint)
                with self.assertRaises(RuntimeError):
                    self.check(api)

    def test_permission_or_run_changes_block(self):
        for change in ('permissions', 'run'):
            calls = 0
            def api(endpoint):
                nonlocal calls
                target = '/actions/permissions' if change == 'permissions' else '/actions/runs/7'
                if endpoint.endswith(target):
                    calls += 1
                    result = self.api(endpoint)
                    if calls == 2:
                        result[0]['enabled' if change == 'permissions' else 'run_attempt'] = (
                            True if change == 'permissions' else 2)
                    return result
                return self.api(endpoint)
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.check(api)


if __name__ == '__main__':
    unittest.main()
