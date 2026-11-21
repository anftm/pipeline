"""Exercise the real all-workflows fence against offline GitHub and bucket reads."""
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from scripts import reader_image_history_fence as window, reader_image_history_retired as retired


OTHER = 'retired/pipeline'
BHA = '.github/workflows/bha-fork-sync.yml'
WORKER = '.github/workflows/pdf-account-worker.yml'
OPERATION = '1' * 32
REVIEWED = '2' * 64
HEAD = '3' * 40


class WindowGuardTests(unittest.TestCase):
    def setUp(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': window.CENTRAL,
               'GITHUB_WORKFLOW_REF': window.WORKFLOW_REF, 'GITHUB_WORKFLOW': window.WORKFLOW_NAME,
               'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_SHA': HEAD, 'GITHUB_WORKFLOW_SHA': HEAD,
               'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2',
               'READER_V3_WRITE_PROTOCOL': window.PROTOCOL}
        stack = (patch.dict(os.environ, env, clear=True),
                 patch('scripts.reader_gc_fence.producer_repositories', return_value=[window.CENTRAL]),
                 patch('scripts.reader_producer_scope_transition.ADDITIONAL', {OTHER: 1}))
        for p in stack:
            p.start()
            self.addCleanup(p.stop)
        self.workflows = {
            window.CENTRAL: {**dict.fromkeys(window.REQUIRED_PAUSED | {WORKER, BHA}, 'disabled_manually'),
                             window.WORKFLOW: 'active'},
            OTHER: {WORKER: 'disabled_manually'},
        }
        self.owner = {'id': 123, 'run_attempt': 2, 'head_branch': 'main', 'head_sha': HEAD,
                      'path': window.WORKFLOW, 'status': 'in_progress', 'event': 'workflow_dispatch'}
        self.runs = {window.CENTRAL: [self.owner], OTHER: []}
        self.receipt = {'version': 1, 'kind': window.RECEIPT_KIND, 'key': window.HPNG_KEY,
                        'operation': OPERATION, 'reviewed_sha256': REVIEWED, 'head_sha': HEAD,
                        'workflow_ref': window.WORKFLOW_REF, 'owner_run_id': '123', 'owner_run_attempt': '2'}
        self.permission = {'enabled': False}
        self.calls = []
        self.reads = []
        self.receipt_raw = None
        self.receipt_path = None

    def api(self, endpoint):
        self.calls.append(endpoint)
        _, org, name, route = endpoint.split('/', 3)
        repo = org + '/' + name
        if route == 'actions/runs/123' and repo == window.CENTRAL:
            return [self.owner.copy()]
        if route == 'actions/workflows?per_page=100':
            return [{'workflows': [{'path': path, 'state': state} for path, state in self.workflows[repo].items()]}]
        if route == 'actions/permissions' and repo == OTHER:
            return [self.permission]
        if route.startswith('actions/runs?status='):
            status = route.split('=', 1)[1].split('&', 1)[0]
            return [{'workflow_runs': [run for run in self.runs[repo] if run['status'] == status]}]
        raise AssertionError(endpoint)

    def read_bytes(self, bucket, path):
        self.reads.append((bucket, path))
        self.assertEqual((bucket, path), (window.ASSETS, self.receipt_path))
        if self.receipt_raw is None:
            raise FileNotFoundError(path)
        return self.receipt_raw

    def guard(self, *, raw=None, **kwargs):
        self.receipt_raw = raw if raw is not None else json.dumps(self.receipt).encode()
        digest = hashlib.sha256(self.receipt_raw).hexdigest()
        self.receipt_path = window.RECEIPT_PREFIX + digest + '.json'
        opts = dict(key=window.HPNG_KEY, operation=OPERATION, reviewed_sha256=REVIEWED,
                    receipt_sha256=digest, external_writers_stopped=True, api=self.api)
        return window.WindowGuard(self, **{**opts, **kwargs})

    def test_guard_rereads_window_and_every_scope_at_each_runtime_boundary(self):
        guard = self.guard()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reads, [])
        self.assertIs(guard('prepare'), True)
        self.assertIs(guard(), True)
        self.assertEqual(len(self.reads), 4)
        self.assertEqual(self.calls.count(f'repos/{window.CENTRAL}/actions/runs/123'), 4)
        self.assertEqual(self.calls.count(f'repos/{OTHER}/actions/permissions'), 2)
        self.workflows[window.CENTRAL][BHA] = 'active'
        with self.assertRaisesRegex(RuntimeError, 'not paused'):
            guard('legacy:put-sidecar')

    def test_no_flag_or_receipt_can_replace_external_writer_confirmation(self):
        for value in (None, False, 1, 'true'):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'separate confirmation'):
                self.guard(external_writers_stopped=value)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reads, [])

    def test_exact_identity_inputs(self):
        for changes in ({'key': 'Other/Repo\0archive/h.PNG'}, {'operation': '1' * 31},
                        {'reviewed_sha256': True}, {'receipt_sha256': 'B' * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.guard(**changes)

    def test_context_mismatch_fails_before_reads(self):
        guard = self.guard()
        for field, wrong in {
            'GITHUB_ACTIONS': 'false', 'GITHUB_REPOSITORY': OTHER,
            'GITHUB_WORKFLOW_REF': window.WORKFLOW_REF.replace('main', 'test'),
            'GITHUB_WORKFLOW': 'Another workflow', 'READER_V3_WRITE_PROTOCOL': 'different',
            'GITHUB_REF': 'refs/heads/test', 'GITHUB_EVENT_NAME': 'schedule',
            'GITHUB_WORKFLOW_SHA': 'f' * 40, 'GITHUB_SHA': 'main',
            'GITHUB_RUN_ID': '0123', 'GITHUB_RUN_ATTEMPT': '0',
        }.items():
            with self.subTest(field=field), patch.dict(os.environ, {field: wrong}):
                with self.assertRaises(RuntimeError):
                    guard('prepare')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reads, [])

    def test_receipt_binds_review_exact_owner_and_commit(self):
        for field, value in {'owner_run_id': '124', 'owner_run_attempt': '1', 'head_sha': '4' * 40,
                             'workflow_ref': 'main', 'version': True, 'key': 'Other/Repo\0h.PNG',
                             'reviewed_sha256': '5' * 64, 'operation': '6' * 32,
                             'external_writers_stopped': True}.items():
            with self.subTest(field=field), patch.dict(self.receipt, {field: value}):
                with self.assertRaisesRegex(RuntimeError, 'does not bind'):
                    self.guard()()
        self.assertEqual(self.calls, [])

    def test_missing_changed_malformed_and_ambiguous_receipts(self):
        for replacement in (None, b'other bytes'):
            guard = self.guard()
            self.receipt_raw = replacement
            with self.assertRaisesRegex(RuntimeError, 'missing|differs from its pin'):
                guard()
        for raw in (b'{', b'0' * (window.MAX_RECEIPT + 1),
                    b'{"owner_run_id":"124",' + json.dumps(self.receipt).encode()[1:]):
            with self.subTest(size=len(raw)), self.assertRaisesRegex(RuntimeError, 'invalid|differs from its pin'):
                self.guard(raw=raw)()
        self.assertEqual(self.calls, [])

    def test_environment_and_receipt_do_not_prove_the_run_identity(self):
        guard = self.guard()
        for field, value in {'id': 124, 'run_attempt': 1, 'path': '.github/workflows/reader-maintenance.yml',
                             'status': 'completed', 'head_branch': 'branch', 'event': 'schedule',
                             'head_sha': '5' * 40}.items():
            with self.subTest(field=field), patch.dict(self.owner, {field: value}):
                with self.assertRaisesRegex(RuntimeError, 'attempt is not active'):
                    guard('v3:observe')

    def test_known_paths_must_be_present_paused_and_drained(self):
        guard = self.guard()
        for path in sorted(window.REQUIRED_PAUSED):
            with self.subTest(path=path):
                states = self.workflows[window.CENTRAL]
                states[path] = 'active'
                with self.assertRaisesRegex(RuntimeError, 'not paused'):
                    guard()
                del states[path]
                with self.assertRaisesRegex(RuntimeError, 'missing from paused inventory'):
                    guard()
                states[path] = 'disabled_manually'
                self.runs[window.CENTRAL].append({'path': path, 'id': 124, 'status': 'waiting'})
                with self.assertRaisesRegex(RuntimeError, 'not drained'):
                    guard()
                self.runs[window.CENTRAL].pop()

    def test_neither_second_publisher_nor_disabled_own_workflow_is_allowed(self):
        self.runs[window.CENTRAL].append({'path': window.WORKFLOW, 'id': 124, 'status': 'queued'})
        with self.assertRaisesRegex(RuntimeError, 'another publisher run'):
            self.guard()()
        self.runs[window.CENTRAL].pop()
        self.workflows[window.CENTRAL][window.WORKFLOW] = 'disabled_manually'
        with self.assertRaisesRegex(RuntimeError, 'current publisher workflow is not active'):
            self.guard()()

    def test_prepared_additional_repos_cannot_be_skipped(self):
        self.permission['enabled'] = True
        with self.assertRaisesRegex(RuntimeError, 'repository execution is not disabled'):
            self.guard()()

        def inaccessible(endpoint):
            if endpoint.startswith(f'repos/{OTHER}/'):
                raise RuntimeError('repository returned 404')
            return self.api(endpoint)

        with self.assertRaisesRegex(RuntimeError, '404'):
            self.guard(api=inaccessible)()

    def test_exact_removed_addons_need_no_actions_queries_but_other_safety_checks_continue(self):
        removed = {name: pin[0] for name, pin in retired.PINS.items()}
        with patch('scripts.reader_producer_scope_transition.ADDITIONAL', removed):
            self.assertTrue(self.guard()())
            self.assertFalse(any(any(name in call for name in removed) for call in self.calls))
            self.workflows[window.CENTRAL][window.COLLECTOR] = 'active'
            with self.assertRaisesRegex(RuntimeError, 'not paused'):
                self.guard()()

    def test_wrong_retired_identity_or_missing_receipt_never_passes(self):
        removed = {name: pin[0] for name, pin in retired.PINS.items()}
        wrong = dict(removed)
        wrong[next(iter(wrong))] += 1
        with patch('scripts.reader_producer_scope_transition.ADDITIONAL', wrong):
            with self.assertRaisesRegex(ValueError, 'ID changed'):
                self.guard()()
        with patch('scripts.reader_producer_scope_transition.ADDITIONAL', removed), \
                patch.object(retired, 'DIRECTORY', retired.DIRECTORY / 'nonexistent'):
            with self.assertRaises(FileNotFoundError):
                self.guard()()

    def test_receipts_do_not_override_active_scope(self):
        active = next(iter(retired.PINS))
        self.assertEqual(retired.verified_deletions({active: retired.PINS[active][0]}, [active.upper()]), [])

    def test_owner_or_receipt_change_during_fence_blocks(self):
        def change(endpoint):
            if f'repos/{OTHER}/actions/workflows' in endpoint:
                if target == 'owner':
                    self.owner['status'] = 'completed'
                else:
                    self.receipt_raw = None
            return self.api(endpoint)

        for target in ('owner', 'receipt'):
            self.owner['status'] = 'in_progress'
            with self.subTest(target=target), self.assertRaisesRegex(RuntimeError, 'not active|receipt is missing'):
                self.guard(api=change)()


if __name__ == '__main__':
    unittest.main()
