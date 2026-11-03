import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
import yaml

from scripts import exact_stale_run_delete as cleanup


class FakeClient:
    def __init__(self):
        self.deleted = []
        self.responses = [204, 204]
        self.status = 'queued'
        self.jobs_count = 0
        self.wrong_repo = False

    def get(self, url, params=None):
        path = url.removeprefix(cleanup.BASE)
        if not path:
            value = {'id': cleanup.REPO_ID, 'full_name': cleanup.CENTRAL}
        elif '/check-suites/' in path:
            value = {'total_count': 0, 'check_runs': []}
        else:
            run_id = int(path.split('/')[3])
            if run_id in self.deleted:
                return Mock(status_code=404)
            run = {'id': run_id, 'repository': {'id': cleanup.REPO_ID if not self.wrong_repo else 1},
                   'path': cleanup.WORKFLOW_PATH, 'workflow_id': cleanup.WORKFLOW_ID,
                   'event': 'workflow_dispatch', 'head_sha': cleanup.TARGETS[run_id], 'status': self.status,
                   'conclusion': None, 'run_attempt': 1, 'check_suite_id': 123,
                   'created_at': '2020-01-01T00:00:00Z'}
            if path.endswith('/attempts/1'):
                value = copy.deepcopy(run)
            elif path.endswith('/jobs'):
                value = {'total_count': self.jobs_count, 'jobs': [] if not self.jobs_count else [{'id': 8}]}
            elif path.endswith('/artifacts'):
                value = {'total_count': 0, 'artifacts': []}
            else:
                value = run
        response = Mock(status_code=200)
        response.json.return_value = value
        return response

    def delete(self, url):
        run_id = int(url.rsplit('/', 1)[-1])
        status = self.responses.pop(0)
        if status == 204:
            self.deleted.append(run_id)
        response = Mock(status_code=status)
        response.json.return_value = {'message': 'Resource not accessible by integration'}
        return response


class ExactRunCleanupTests(unittest.TestCase):
    def setUp(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': cleanup.CENTRAL,
               'GITHUB_WORKFLOW_REF': cleanup.OWNER_WORKFLOW, 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '999'}
        self.context = patch.dict(os.environ, env, clear=True)
        self.context.start()
        self.addCleanup(self.context.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakeClient()

    def test_two_exact_runs_only_after_durable_local_intent(self):
        def delete(url):
            self.assertTrue((self.root / 'before.json').is_file())
            self.assertTrue((self.root / 'intent.json').is_file())
            return FakeClient.delete(self.client, url)
        with patch.object(self.client, 'delete', side_effect=delete):
            result = cleanup.run(self.client, self.root)
        self.assertEqual(self.client.deleted, list(cleanup.TARGETS))
        self.assertEqual(result['bucket_deletions'], 0)
        self.assertFalse(result['gc_authorized'])

    def test_denial_stops_before_second_delete(self):
        self.client.responses = [403]
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
            cleanup.run(self.client, self.root)
        self.assertEqual(self.client.deleted, [])
        self.assertTrue((self.root / '39305430594-delete.json').exists())
        self.assertFalse((self.root / '39261848246-delete.json').exists())

    def test_nonempty_jobs_or_wrong_identity_stop_before_any_delete(self):
        for field, value in (('status', 'in_progress'), ('jobs_count', 1), ('wrong_repo', True)):
            client = FakeClient()
            setattr(client, field, value)
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                cleanup.run(client, self.root)
            self.assertEqual(client.deleted, [])
            self.assertFalse((self.root / 'intent.json').exists())

    def test_ambiguous_request_is_never_retried_or_followed_by_second(self):
        error = httpx.ConnectError('network')
        with patch.object(self.client, 'delete', side_effect=error) as delete:
            with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
                cleanup.run(self.client, self.root)
        self.assertEqual(delete.call_count, 1)
        self.assertFalse((self.root / '39261848246-delete.json').exists())

    def test_foreign_context_stops_before_network(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'other/repo'}), \
                patch.object(self.client, 'get') as get:
            with self.assertRaisesRegex(RuntimeError, 'central manual workflow'):
                cleanup.run(self.client, self.root)
        get.assert_not_called()

    def test_manual_workflow_has_no_shared_locks_or_gc(self):
        path = Path(cleanup.__file__).resolve().parents[1] / '.github/workflows/exact-stale-run-delete.yml'
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertNotIn('concurrency', workflow)
        job = workflow['jobs']['delete']
        self.assertNotIn('concurrency', job)
        runs = ' '.join(step.get('run', '') for step in job['steps'])
        self.assertNotIn('reader-gc-collect', runs)
        self.assertNotIn('reader_gc_maintenance', runs)
        self.assertEqual(workflow['permissions']['actions'], 'write')
        step = next(s for s in job['steps'] if s.get('name') == 'Delete only the two fixed stale run records')
        self.assertEqual(step['env']['EXACT_RUN_GITHUB_TOKEN'], '${{ github.token }}')


if __name__ == '__main__':
    unittest.main()
