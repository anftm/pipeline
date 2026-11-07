"""Offline publication regression: unchanged OCR cannot pin stale text routing."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from scripts import publish_search_reader_index as publication


def condition(expression, *, operation='auto', changed='false', apply='true', paused='false',
              event='workflow_dispatch', repository='anftm/pipeline', build_result='success', cancelled=False):
    """Evaluate only our checked-in fixed Actions conditions, with no external data."""
    replacements = {
        'steps.operation.outputs.operation': operation, 'steps.operation.outputs.changed': changed,
        'steps.operation.outputs.apply': apply, 'vars.READER_V3_AUTO_PAUSED': paused,
        'inputs.operation': operation, 'github.event_name': event, 'github.repository': repository,
        'needs.build.result': build_result,
    }
    for key in sorted(replacements, key=len, reverse=True):
        expression = expression.replace(key, repr(replacements[key]))
    expression = expression.replace('!cancelled()', repr(not cancelled))
    expression = expression.replace('&&', ' and ').replace('||', ' or ').replace('always()', 'True')
    return bool(eval('(' + expression + ')', {'__builtins__': {}, 'fromJSON': json.loads, 'contains': lambda items, value: value in items}))


class ReaderProjectionSyncTests(unittest.TestCase):
    def workflow(self, name):
        return yaml.safe_load((Path(__file__).parents[1] / '.github/workflows' / name).read_text())

    def test_protocol_not_display_name_requests_base_refresh_after_full_writer_validation(self):
        events = []
        store = Mock(spec=['assert_serialized_writer'])
        store.assert_serialized_writer.side_effect = lambda protocol: events.append(('validate', protocol))
        with patch.dict(os.environ, {'READER_V3_WRITE_PROTOCOL': publication.PROTOCOL, 'GITHUB_WORKFLOW': 'renamed display label'}), \
                patch.object(publication, 'CentralHubStore', return_value=store), \
                patch.object(publication, 'unify', side_effect=lambda *args, **kwargs: events.append(('unify', kwargs['apply']))):
            self.assertTrue(publication.synchronize_pinned_base(dry_run=False))
        self.assertEqual(events, [('validate', publication.PROTOCOL), ('unify', True)])

    def test_noncentral_or_dry_run_cannot_promote_a_pointer(self):
        for protocol, dry_run in (('', False), (publication.PROTOCOL, True)):
            with self.subTest(protocol=protocol, dry_run=dry_run), \
                    patch.dict(os.environ, {'READER_V3_WRITE_PROTOCOL': protocol}), \
                    patch.object(publication, 'CentralHubStore') as store, patch.object(publication, 'unify') as unify:
                self.assertFalse(publication.synchronize_pinned_base(dry_run=dry_run))
                store.assert_not_called(); unify.assert_not_called()

    def test_forged_protocol_without_actual_workflow_ownership_fails_before_mutation(self):
        store = Mock(spec=['assert_serialized_writer']); store.assert_serialized_writer.side_effect = RuntimeError('not the real writer')
        with patch.dict(os.environ, {'READER_V3_WRITE_PROTOCOL': publication.PROTOCOL}), \
                patch.object(publication, 'CentralHubStore', return_value=store), patch.object(publication, 'unify') as unify:
            with self.assertRaises(RuntimeError): publication.synchronize_pinned_base(dry_run=False)
            unify.assert_not_called()

    def test_only_active_projection_changes_publish_and_ack(self):
        workflow = self.workflow('reader-v3-publish.yml')
        steps = workflow['jobs']['publish']['steps']
        publish = next(item for item in steps if item.get('name') == 'Publish the pinned projection to Pages')
        ack = next(item for item in steps if item.get('id') == 'ack')
        for step in (publish, ack):
            for operation in ('unify', 'withdraw', 'reset-documents', 'remove-route', 'restore-route', 'migrate-derived', 'resume-derived', 'redirect-derived', 'publish-generation'):
                self.assertTrue(condition(step['if'], operation=operation, changed='true'))
                self.assertFalse(condition(step['if'], operation=operation, changed='true', apply='false'))
                self.assertFalse(condition(step['if'], operation=operation, changed='false'))
            for operation in ('auto', 'correct', 'accept', 'promote', 'rollback',
                              'repair-gbk', 'retire', 'ack', 'inspect', 'verify-route', 'verify-generation'):
                self.assertFalse(condition(step['if'], operation=operation, changed='true'))
        self.assertLess(steps.index(publish), steps.index(ack))
        self.assertEqual(workflow['concurrency']['group'], 'reader-sidecar')
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        self.assertEqual(workflow['concurrency']['queue'], 'max')

    def test_pinned_only_pages_publication_does_not_advance_generation(self):
        class Api:
            def __init__(self, **_kwargs):
                pass
        store = Mock()
        pointer = {'generation': 'a' * 64}
        catalog = {'files': {}}
        base = {'v': 1, 'f': {}}
        args = Mock(dry_run=True, pinned_only=True, assets_repo='fixture')
        import gzip
        with patch.object(publication, 'parse_args', return_value=args), \
             patch.dict(os.environ, {'HF_TOKEN': 'fixture'}), \
             patch.object(publication, 'HfApi', Api), \
             patch.object(publication, 'HubBucketStore', return_value=store), \
             patch.object(publication, 'read_bucket_bytes', return_value=gzip.compress(b'{"v":1,"f":{}}')), \
             patch.object(publication, 'synchronize_pinned_base') as unify, \
             patch.object(publication, 'current_v3', return_value=(pointer, catalog)), \
             patch.object(publication, 'pinned_base', return_value=base), \
             patch.object(publication, 'project_sidecar', return_value=base), \
             patch.object(publication, 'publish_to_pages') as publish:
            self.assertEqual(publication.main(), 0)
        unify.assert_not_called()
        self.assertTrue(publish.call_args.kwargs['dry_run'])

    def test_page_migration_fence_is_checked_again_before_pages_push(self):
        workflow = self.workflow('reader-v3-publish.yml')
        publish = next(step for step in workflow['jobs']['publish']['steps']
                       if step.get('name') == 'Publish the pinned projection to Pages')
        self.assertIn('check-page-window --resource-json "$RESOURCE_JSON"', publish['run'])
        self.assertLess(publish['run'].index('check-page-window'),
                        publish['run'].index('publish_search_reader_index.py --pinned-only'))
        self.assertIn('GC_PRODUCER_TOKENS_JSON', publish['env'])

    def test_route_changes_verify_before_and_without_gc_acknowledgments(self):
        workflow = self.workflow('reader-v3-publish.yml')
        steps = workflow['jobs']['publish']['steps']
        publish = next(step for step in steps if step.get('name') == 'Publish the pinned projection to Pages')
        ack = next(step for step in steps if step.get('id') == 'ack')
        self.assertIn('--pinned-only', publish['run'])
        branch = ack['run'].split('python scripts/publish_reader_v3.py ack-all', 1)[0]
        self.assertIn('verify-route', branch)
        self.assertIn('--route-action "$OPERATION"', branch)
        self.assertIn('--wait-seconds 1200', branch)
        self.assertIn('exit 0', branch)
        self.assertNotIn('ack-all', branch)
        self.assertNotIn('acknowledge-legacy', branch)
        self.assertNotIn('compact-legacy', branch)

    def test_unify_and_retirement_have_no_remote_ocr_progress_scan(self):
        workflow = self.workflow('reader-v3-publish.yml')
        self.assertIn('retire', workflow[True]['workflow_dispatch']['inputs']['operation']['options'])
        steps = workflow['jobs']['publish']['steps']
        self.assertFalse(any('publish_reader_v3.py retire --apply' in step.get('run', '') for step in steps))
        self.assertFalse(any('pdf_progress_report.py' in item.get('run', '') for item in steps))

    def test_unchanged_apply_unify_can_still_publish_acknowledge_and_compact(self):
        """Document the current side effect; do not call this a no-op or GC permission."""
        workflow = self.workflow('reader-v3-publish.yml')
        steps = workflow['jobs']['publish']['steps']
        operation = next(item for item in steps if item.get('id') == 'operation')
        publish = next(item for item in steps if item.get('name') == 'Publish the pinned projection to Pages')
        ack = next(item for item in steps if item.get('id') == 'ack')
        self.assertIn('echo "changed=true" >> "$GITHUB_OUTPUT"', operation['run'])
        self.assertTrue(condition(publish['if'], operation='unify', changed='true', apply='true'))
        self.assertTrue(condition(ack['if'], operation='unify', changed='true', apply='true'))
        self.assertIn('ack-all --apply', ack['run'])
        self.assertIn('acknowledge-legacy --apply', ack['run'])
        self.assertIn('compact-legacy --apply', ack['run'])
        self.assertLess(ack['run'].index('ack-all --apply'), ack['run'].index('compact-legacy --apply'))

    def test_workflow_dispatch_script_preserves_active_arguments_and_rejects_retired(self):
        workflow = self.workflow('reader-v3-publish.yml')
        operation = next(item for item in workflow['jobs']['publish']['steps'] if item.get('id') == 'operation')
        script = operation['run']
        retired = ('auto', 'build-stage', 'stage', 'promote', 'rollback', 'correct',
                   'accept', 'reject', 'retry', 'repair-gbk', 'retire-ocr-progress')
        cases = {
            'inspect': [],
            'retire': [],
            'ack': ['--surface', 'pages'],
            'withdraw': ['--resource-json', '{"key":"fixture"}', '--expected-parent', 'parent'],
            'unify': ['--resource-json', '{"key":"fixture"}'],
            'reset-documents': ['--resource-json', '{"key":"fixture"}'],
            'remove-route': ['--resource-json', '{"key":"fixture"}', '--expected-parent', 'parent'],
            'restore-route': ['--resource-json', '{"key":"fixture"}', '--expected-parent', 'parent'],
            'verify-route': ['--resource-json', '{"key":"fixture"}', '--expected-parent', 'parent'],
            'migrate-derived': ['--resource-json', '{"key":"fixture"}'],
            'resume-derived': ['--resource-json', '{"key":"fixture"}'],
            'redirect-derived': ['--resource-json', '{"key":"fixture"}'],
            'publish-generation': ['--expected-parent', 'parent', '--wait-seconds', '1200'],
            'verify-generation': ['--expected-parent', 'parent', '--wait-seconds', '1200'],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_python = root / 'python'
            fake_python.write_text('#!/usr/bin/env python3\n'
                                   'import json, os, sys\n'
                                   'from pathlib import Path\n'
                                   'Path(os.environ["TEST_ARGS"]).write_text(json.dumps(sys.argv[1:]))\n')
            fake_python.chmod(0o755)
            for command in (*cases, *retired, 'unsupported'):
                for apply in ('false', 'true'):
                    with self.subTest(command=command, apply=apply):
                        argv_file, output_file = root / 'args.json', root / 'outputs.txt'
                        argv_file.unlink(missing_ok=True)
                        output_file.unlink(missing_ok=True)
                        env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                               'TEST_ARGS': str(argv_file), 'GITHUB_OUTPUT': str(output_file),
                               'OPERATION': command, 'RESOURCE_JSON': '{"key":"fixture"}',
                               'EXPECTED_PARENT': 'parent', 'SURFACE': 'pages', 'APPLY': apply,
                               'EVENT_NAME': 'workflow_dispatch', 'SCHEDULE': ''}
                        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script],
                                                cwd=root, env=env, capture_output=True, text=True)
                        if command not in cases:
                            self.assertNotEqual(result.returncode, 0)
                            self.assertFalse(argv_file.exists())
                            continue
                        self.assertEqual(result.returncode, 0, result.stderr)
                        expected = ['scripts/publish_reader_v3.py', command, '--output',
                                    'output/v3/publication.json', *cases[command]]
                        if apply == 'true':
                            expected.append('--apply')
                        self.assertEqual(json.loads(argv_file.read_text()), expected)
                        self.assertIn('operation=' + command, output_file.read_text())

    def test_pause_blocks_auto_but_not_manual_sync_and_builder_is_retired(self):
        workflow = self.workflow('reader-v3-publish.yml')
        gate = workflow['jobs']['publish']['if']
        self.assertFalse(condition(gate, operation='auto', paused='true'))
        self.assertFalse(condition(gate, paused='true', event='schedule'))
        operation = next(step for step in workflow['jobs']['publish']['steps'] if step.get('id') == 'operation')
        self.assertIn('Scheduled v3 retirement is disabled', operation['run'])
        self.assertTrue(condition(gate, operation='unify', paused='true'))
        self.assertTrue(condition(gate, operation='auto', paused='false'))
        self.assertFalse(condition(gate, operation='unify', paused='true', repository='other/repo'))
        workflows = Path(__file__).parents[1] / '.github/workflows'
        self.assertFalse((workflows / 'reader-v3-build.yml').exists())
        self.assertNotIn('reader-v3-build.yml', (workflows / 'reader-v3-publish.yml').read_text())


if __name__ == '__main__':
    unittest.main()
