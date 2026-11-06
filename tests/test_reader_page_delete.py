import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import yaml
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import os

from scripts import reader_page_delete as deletion, reader_gc_graph as graph


PATHS = ['derived/generation/' + 'a' * 32 + '/document.pdf',
         'objects/aa/' + 'a' * 64 + '/' + 'b' * 16 + '/document.pdf']


class FakeApi:
    def __init__(self):
        self.files = {p: SimpleNamespace(path=p, size=10, xet_hash='a' * 64) for p in PATHS}
    def get_bucket_paths_info(self, bucket, paths, **kwargs):
        for path in paths:
            if path in self.files:
                yield self.files[path]


class DeleteTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'HF_TOKEN': 'fixture'})
        env.start(); self.addCleanup(env.stop)
        self.api = FakeApi()
        self.ref = SimpleNamespace(files={b: set() for b in graph.BUCKETS}, references=set(),
                                   root_hashes={('bucket', 'root'): 'a' * 64}, loaded_digests={}, fingerprints={})
        self.ref.files[deletion.BUCKET] = set(PATHS)
        self.report = {'graph_complete': True, 'blockers': [], 'buckets': {
            deletion.BUCKET: {'candidates': list(PATHS)}}}
        self.now = datetime.now(timezone.utc)
        self.plan = deletion.candidate_plan(self.ref, self.report, self.api, PATHS, clock=self.now)
        self.fence, self.consumers, self.chm, self.intent = Mock(), Mock(), Mock(), Mock()

    def apply(self, plan=None, expected=None, confirm=True):
        plan = plan or self.plan
        return deletion.apply_exact(Mock(), self.api, self.ref, self.report, plan,
            expected or deletion.plan_id(plan), fence=self.fence, check_consumers=self.consumers,
            check_chm_window=self.chm, persist_intent=self.intent, confirm_external_writers=confirm)

    def test_plan_binds_exact_candidates_and_versions_not_permission(self):
        self.assertFalse(self.plan['deletion_authorized'])
        self.assertEqual([item['path'] for item in self.plan['targets']], sorted(PATHS))
        self.assertEqual(self.plan['total_bytes'], 20)
        self.assertEqual(deletion.check_plan(self.plan, deletion.plan_id(self.plan), now=self.now), sorted(PATHS))

    def test_unreferenced_candidates_not_in_complete_graph_are_rejected(self):
        self.ref.references.add((deletion.BUCKET, PATHS[0]))
        with self.assertRaisesRegex(ValueError, 'not an unreferenced'):
            deletion.candidate_plan(self.ref, self.report, self.api, PATHS)
        self.ref.references.clear()
        self.ref.files.pop(graph.ccrd_lifecycle.BUCKET)
        with self.assertRaisesRegex(RuntimeError, 'four-bucket'):
            deletion.candidate_plan(self.ref, self.report, self.api, PATHS)

    def test_plan_expiry_tamper_and_invalid_paths_stop(self):
        with self.assertRaisesRegex(ValueError, 'expired'):
            deletion.check_plan(self.plan, deletion.plan_id(self.plan), now=self.now + timedelta(hours=2))
        for paths in ([], ['../etc'], ['objects/../x'], ['derived/x/', 'objects/a'], PATHS * 2):
            with self.subTest(paths=paths), self.assertRaises(ValueError): deletion.validate_paths(paths)
        with self.assertRaisesRegex(ValueError, 'digest'):
            deletion.check_plan(self.plan, 'f' * 64)
        wrong = {**self.plan, 'total_bytes': 0}
        with self.assertRaisesRegex(ValueError, 'total'):
            deletion.check_plan(wrong, deletion.plan_id(wrong), now=self.now)

    def test_changed_version_or_reference_never_sends_delete(self):
        self.api.files[PATHS[0]].xet_hash = 'changed'
        with patch('huggingface_hub.utils.get_session') as session:
            with self.assertRaisesRegex(RuntimeError, 'version changed'):
                self.apply()
            session.assert_not_called()
        self.api.files[PATHS[0]].xet_hash = 'a' * 64
        self.ref.references.add((deletion.BUCKET, PATHS[0]))
        with patch('huggingface_hub.utils.get_session') as session:
            with self.assertRaisesRegex(RuntimeError, 'newly referenced'):
                self.apply()
            session.assert_not_called()

    def test_candidate_with_referenced_sibling_is_never_deleted(self):
        sibling = PATHS[0].rsplit('/', 1)[0] + '/derived-manifest.json'
        self.ref.references.add((deletion.BUCKET, sibling))
        with patch('huggingface_hub.utils.get_session') as session:
            with self.assertRaisesRegex(RuntimeError, 'referenced sibling'):
                self.apply()
            session.assert_not_called()

    def test_missing_external_confirmation_or_changed_roots_block(self):
        with self.assertRaisesRegex(ValueError, 'external writers'):
            self.apply(confirm=False)
        with patch('scripts.reader_page_delete.collector.verify_roots', side_effect=RuntimeError('root changed')):
            with self.assertRaisesRegex(RuntimeError, 'root changed'):
                self.apply()
        self.intent.assert_not_called()

    def test_success_is_one_exact_post_and_absence_verification(self):
        session = Mock(); response = Mock(); response.json.return_value = {}
        def delete(*args, **kwargs):
            payload = [json.loads(row) for row in kwargs['content'].splitlines()]
            self.assertEqual(payload, [{'type': 'deleteFile', 'path': path} for path in sorted(PATHS)])
            self.api.files.clear()
            return response
        session.post.side_effect = delete
        with patch('huggingface_hub.utils.get_session', return_value=session), \
             patch('scripts.reader_page_delete.collector.verify_roots') as verify, \
             patch('huggingface_hub.utils.build_hf_headers', return_value={}):
            result = self.apply()
        self.assertTrue(result['verified_absent'])
        self.assertEqual(result['objects'], 2)
        session.post.assert_called_once()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(self.chm.call_count, 3)
        self.assertEqual(self.fence.check.call_count, 3)
        self.consumers.assert_called_once()
        self.intent.assert_called_once()
        self.assertEqual(self.intent.call_args.args[0]['plan_sha256'], deletion.plan_id(self.plan))

    def test_changed_nested_manifest_after_intent_never_sends_delete(self):
        manifest = (deletion.BUCKET, 'objects/live/reading-manifest.json')
        original = b'{"version":1}'
        self.ref.loaded_digests[manifest] = __import__('hashlib').sha256(original).hexdigest()
        store = Mock(spec=['read_bytes', 'token'])
        store.read_bytes.return_value = original
        store.token.return_value = 'fixture'
        self.intent.side_effect = lambda *_args: setattr(store.read_bytes, 'return_value', b'{"version":2}')
        with patch('huggingface_hub.utils.get_session') as session, \
             patch('scripts.reader_page_delete.collector.verify_roots'):
            with self.assertRaisesRegex(RuntimeError, 'nested graph document changed'):
                deletion.apply_exact(store, self.api, self.ref, self.report, self.plan,
                    deletion.plan_id(self.plan), fence=self.fence, check_consumers=self.consumers,
                    check_chm_window=self.chm, persist_intent=self.intent, confirm_external_writers=True)
            session.assert_not_called()
        self.intent.assert_called_once()

    def test_changed_version_after_intent_never_sends_delete(self):
        self.intent.side_effect = lambda *_args: setattr(self.api.files[PATHS[0]], 'xet_hash', 'changed')
        with patch('huggingface_hub.utils.get_session') as session, \
             patch('scripts.reader_page_delete.collector.verify_roots'):
            with self.assertRaisesRegex(RuntimeError, 'changed before deletion'):
                self.apply()
            session.assert_not_called()
        self.intent.assert_called_once()

    def test_lost_response_or_partial_failure_is_never_retried(self):
        for failure in ('lost', 'partial', 'remaining', 'malformed'):
            session, response = Mock(), Mock()
            if failure == 'lost': session.post.side_effect = OSError('lost')
            else:
                response.json.return_value = ({'failed': [{'path': PATHS[0]}]} if failure == 'partial'
                                              else {'failed': 'invalid'} if failure == 'malformed' else {'failed': []})
                session.post.return_value = response
            with self.subTest(failure=failure), patch('huggingface_hub.utils.get_session', return_value=session), \
                 patch('scripts.reader_page_delete.collector.verify_roots'), \
                 patch('huggingface_hub.utils.build_hf_headers', return_value={}):
                with self.assertRaises((RuntimeError, OSError)): self.apply()
                session.post.assert_called_once()

    def test_collector_shell_routes_page_plan_and_apply_without_network(self):
        workflow = yaml.load((Path(__file__).resolve().parents[1] / '.github/workflows/reader-gc-collect.yml').read_text(),
                             Loader=yaml.BaseLoader)
        script = next(x for x in workflow['jobs']['collect']['steps'] if x.get('name') ==
                      'Verify maintenance window and collect unreferenced objects')['run']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'python'
            fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
                            'from pathlib import Path\n'
                            'if sys.argv[1:2]==["-c"]: exec(sys.argv[2])\n'
                            'else: Path(os.environ["TEST_ARGS"]).write_text(json.dumps(sys.argv[1:]))\n')
            fake.chmod(0o755)
            common = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                      'TEST_ARGS': str(root / 'args.json'), 'GRACE_DAYS': '0', 'LIMIT': '0',
                      'STORAGE': 'hub', 'SCOPE': 'page-plan', 'APPLY': 'false',
                      'RESUME_RUN': json.dumps({'paths': [PATHS[0]]})}
            import subprocess
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=root,
                                    env=common, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / 'output/gc/page-plan/paths.json').read_text()), [PATHS[0]])
            self.assertIn('plan', json.loads((root / 'args.json').read_text()))
            config = {'plan_run': '123', 'plan_sha256': 'a' * 64, 'external_writers_stopped': True}
            env = {**common, 'SCOPE': 'page-apply', 'APPLY': 'true', 'RESUME_RUN': json.dumps(config)}
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=root,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads((root / 'args.json').read_text())
            self.assertIn('apply', args)
            self.assertIn('--confirm-external-writers-stopped', args)
            self.assertIn(config['plan_sha256'], args)
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=root,
                                    env={**env, 'APPLY': 'false'}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_existing_collector_runs_page_phases_directly_without_nested_workflow_lock(self):
        root = Path(__file__).resolve().parents[1] / '.github/workflows'
        workflow = yaml.load((root / 'reader-gc-collect.yml').read_text(), Loader=yaml.BaseLoader)
        inputs = workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(len(inputs), 10)
        self.assertIn('page-plan', inputs['scope']['options'])
        self.assertIn('page-apply', inputs['scope']['options'])
        self.assertEqual(workflow['concurrency']['group'], 'reader-sidecar')
        self.assertNotIn('page', workflow['jobs'])
        step = next(x for x in workflow['jobs']['collect']['steps'] if x.get('name') ==
                    'Verify maintenance window and collect unreferenced objects')
        self.assertIn('reader_page_delete.py plan', step['run'])
        self.assertIn('reader_page_delete.py apply', step['run'])
        self.assertIn('v["external_writers_stopped"] is True', step['run'])
        self.assertNotIn('${{ inputs.', step['run'])
        self.assertFalse((root / 'reader-page-delete.yml').exists())

    def test_cli_plan_apply_contract_end_to_end_with_fake_store(self):
        self.ref.root_hashes = {}
        self.ref.unreadable = {}
        store = Mock()
        store.api = self.api
        store.token.return_value = 'fixture'
        fake_session, response = Mock(), Mock()
        response.json.return_value = {}
        def delete(*_args, **_kwargs):
            self.api.files.clear()
            return response
        fake_session.post.side_effect = delete
        control_objects = {}
        def read(bucket, path):
            if (bucket, path) not in control_objects:
                raise FileNotFoundError(path)
            return control_objects[bucket, path]
        store.read_bytes.side_effect = read
        store.put_bytes.side_effect = lambda bucket, path, raw: control_objects.__setitem__((bucket, path), raw)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
             patch('scripts.reader_page_delete.HubBucketStore', return_value=store), \
             patch('scripts.reader_page_delete.GitHubProducerFence', return_value=self.fence), \
             patch('scripts.reader_page_delete.assert_workflow_context'), \
             patch('scripts.reader_page_delete.graph.ReferenceGraph', return_value=self.ref), \
             patch('scripts.reader_page_delete.reader_gc_chm_window.require_clear'), \
             patch('scripts.reader_page_delete.collector.observe_projections'), \
             patch('scripts.reader_page_delete.collector.verify_roots'), \
             patch('huggingface_hub.utils.get_session', return_value=fake_session), \
             patch('huggingface_hub.utils.build_hf_headers', return_value={}), \
             redirect_stdout(output):
            self.ref.build = Mock(return_value=self.report)
            root = Path(directory)
            paths, plan = root / 'paths.json', root / 'plan.json'
            paths.write_text(json.dumps(PATHS))
            self.assertEqual(deletion.main(['plan', '--paths', str(paths), '--plan', str(plan), '--storage', 'hub']), 0)
            data = json.loads(plan.read_text())
            identity = deletion.plan_id(data)
            self.assertEqual(deletion.main(['apply', '--plan', str(plan), '--expected-sha256', identity,
                                            '--confirm-external-writers-stopped', '--storage', 'hub']), 0)
            self.assertEqual(self.api.files, {})
            self.assertEqual(fake_session.post.call_count, 1)
            self.assertEqual({path.rsplit('/', 1)[-1] for _, path in control_objects}, {'intent.json', 'result.json'})
            # A replay has no candidates and cannot send a second POST.
            self.assertEqual(deletion.main(['apply', '--plan', str(plan), '--expected-sha256', identity,
                                            '--confirm-external-writers-stopped', '--storage', 'hub']), 1)
            self.assertEqual(fake_session.post.call_count, 1)

    def test_guard_or_consumer_failure_stops_before_delete(self):
        for guard in (self.chm, self.consumers, self.intent):
            guard.side_effect = RuntimeError('blocked')
            with patch('huggingface_hub.utils.get_session') as session, \
                 patch('scripts.reader_page_delete.collector.verify_roots'):
                with self.assertRaisesRegex(RuntimeError, 'blocked'): self.apply()
                session.assert_not_called()
            guard.side_effect = None


if __name__ == '__main__':
    unittest.main()
