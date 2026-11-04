import json
import unittest
from unittest.mock import Mock, patch

from scripts import chm_subset_locked_preflight as preflight, reader_gc_credentials as credentials
from scripts import reader_gc_fence as fence, reader_gc_maintenance as maintenance


class TokenSelectionTests(unittest.TestCase):
    def setUp(self):
        self.target = credentials.OVERRIDE_REPOSITORY
        self.env = {'GC_PRODUCER_TOKENS_JSON': json.dumps({self.target: 'old-target', 'other/repo': 'other-token'}),
                    credentials.OVERRIDE_VARIABLE: 'new-target'}

    def test_override_is_bound_to_only_one_repository(self):
        with patch.dict('os.environ', self.env, clear=True):
            self.assertEqual(credentials.token_for(self.target), 'new-target')
            self.assertEqual(credentials.token_for('other/repo'), 'other-token')
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                credentials.token_for('foreign/repo')

    def test_missing_override_uses_existing_exact_mapping(self):
        with patch.dict('os.environ', {'GC_PRODUCER_TOKENS_JSON': self.env['GC_PRODUCER_TOKENS_JSON']}, clear=True):
            self.assertEqual(credentials.token_for(self.target), 'old-target')

    def test_invalid_mapping_does_not_succeed_even_with_override(self):
        for raw in ('', '[]', '{'):
            with self.subTest(raw=raw), patch.dict('os.environ', {
                    'GC_PRODUCER_TOKENS_JSON': raw, credentials.OVERRIDE_VARIABLE: 'new-target'}, clear=True):
                with self.assertRaises(RuntimeError):
                    credentials.token_for(self.target)

    def test_maintenance_get_and_mutation_use_exact_same_override(self):
        result = Mock(returncode=0, stdout='{}', stderr='')
        with patch.dict('os.environ', self.env, clear=True), patch.object(maintenance.subprocess, 'run', return_value=result) as run:
            maintenance.api(self.target, 'actions/permissions')
            maintenance.api(self.target, 'actions/workflows/1/disable', method='PUT')
            maintenance.api('other/repo', 'actions/permissions')
        self.assertEqual([call.kwargs['env']['GH_TOKEN'] for call in run.call_args_list],
                         ['new-target', 'new-target', 'other-token'])

    def test_maintenance_failure_never_prints_token_or_retries_mutation(self):
        result = Mock(returncode=1, stdout='', stderr='HTTP 401 new-target')
        with patch.dict('os.environ', self.env, clear=True), patch.object(maintenance.subprocess, 'run', return_value=result) as run:
            with self.assertRaises(maintenance.MaintenanceAPIError) as error:
                maintenance.api(self.target, 'actions/workflows/1/disable', method='PUT')
        self.assertNotIn('new-target', str(error.exception))
        self.assertEqual(run.call_count, 1)

    def test_fence_and_readonly_preflight_use_same_override(self):
        response = Mock(returncode=0, stdout='[{}]', stderr='')
        get = Mock(status_code=200, links={})
        get.json.return_value = {'workflows': []}
        with patch.dict('os.environ', self.env, clear=True), patch.object(fence.subprocess, 'run', return_value=response) as run, \
             patch.object(preflight.httpx, 'get', return_value=get) as request:
            fence.GitHubProducerFence.github_api('repos/' + self.target + '/actions/permissions')
            preflight.github_get('repos/' + self.target + '/actions/workflows')
        self.assertEqual(run.call_args.kwargs['env']['GH_TOKEN'], 'new-target')
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer new-target')

    def test_workflows_pass_override_to_maintenance_and_collector(self):
        from pathlib import Path
        import yaml
        root = Path(__file__).resolve().parents[1] / '.github/workflows'
        maintenance_flow = yaml.load((root / 'reader-maintenance.yml').read_text(), Loader=yaml.BaseLoader)
        release_flow = yaml.load((root / 'publish-chm-gc-release-inner.yml').read_text(), Loader=yaml.BaseLoader)
        collect_flow = yaml.load((root / 'reader-gc-collect.yml').read_text(), Loader=yaml.BaseLoader)
        secret = '${{ secrets.GC_AMBROSSEE_MAINTENANCE_20261102 }}'
        self.assertEqual(maintenance_flow['jobs']['maintain']['env'][credentials.OVERRIDE_VARIABLE], secret)
        self.assertEqual(release_flow['jobs']['release']['env'][credentials.OVERRIDE_VARIABLE], secret)
        target = next(step for step in collect_flow['jobs']['collect']['steps']
                      if step.get('name') == 'Verify maintenance window and collect unreferenced objects')
        self.assertEqual(target['env'][credentials.OVERRIDE_VARIABLE], secret)

    def test_v3_document_fence_receives_the_same_exact_repository_override(self):
        from pathlib import Path
        import yaml
        root = Path(__file__).resolve().parents[1] / '.github/workflows'
        workflow = yaml.load((root / 'reader-v3-publish.yml').read_text(), Loader=yaml.BaseLoader)
        operation = next(step for step in workflow['jobs']['publish']['steps'] if step.get('id') == 'operation')
        self.assertEqual(operation['env'][credentials.OVERRIDE_VARIABLE],
                         '${{ secrets.GC_AMBROSSEE_MAINTENANCE_20261102 }}')
        self.assertEqual(operation['env']['GC_PRODUCER_TOKENS_JSON'], '${{ secrets.GC_PRODUCER_TOKENS_JSON }}')

    def test_document_fence_selects_override_for_target_reads_only(self):
        from scripts import reader_document_reset
        target, other = self.target, 'other/repo'
        def api_response(command, **kwargs):
            endpoint = command[-1]
            repo = '/'.join(endpoint.split('/')[1:3])
            expected_token = 'new-target' if repo == target else 'other-token'
            self.assertEqual(kwargs['env']['GH_TOKEN'], expected_token)
            if '/actions/workflows?' in endpoint:
                data = [{'workflows': [{'id': 1, 'path': '.github/workflows/pdf-account-worker.yml',
                                        'state': 'disabled_manually'}]}]
            elif '/actions/runs?' in endpoint:
                data = [{'workflow_runs': []}]
            else:
                raise AssertionError('unexpected fence endpoint: ' + endpoint)
            return Mock(returncode=0, stdout=json.dumps(data), stderr='')
        with patch.dict('os.environ', self.env, clear=True), \
             patch.object(fence, 'producer_repositories', return_value=[target, other]), \
             patch.object(fence.subprocess, 'run', side_effect=api_response) as run:
            report = reader_document_reset.DocumentProducerFence().check()
        self.assertEqual([entry['repository'] for entry in report], [target, other])
        self.assertEqual(run.call_count, 12)

    def test_removed_publisher_run_still_blocks_the_document_fence(self):
        from scripts import reader_document_reset
        path = '.github/workflows/pdf-account-publish.yml'
        for status in ('in_progress', 'queued', 'pending'):
            with self.subTest(status=status):
                def api(endpoint):
                    if '/actions/workflows?' in endpoint:
                        return [{'workflows': [{'path': '.github/workflows/pdf-account-worker.yml',
                                                'state': 'disabled_manually'}]}]
                    if f'/actions/runs?status={status}&' in endpoint:
                        return [{'workflow_runs': [{'id': 7, 'status': status, 'path': path}]}]
                    if '/actions/runs?status=' in endpoint:
                        return [{'workflow_runs': []}]
                    raise AssertionError('unexpected document fence endpoint')
                with patch.object(fence, 'producer_repositories', return_value=['owner/repo']), \
                        patch.object(fence.GitHubProducerFence, 'isolated_residual',
                                     side_effect=RuntimeError('residual is executable')):
                    with self.assertRaisesRegex(RuntimeError, 'still active|residual is executable'):
                        reader_document_reset.DocumentProducerFence(api).check()


if __name__ == '__main__':
    unittest.main()
