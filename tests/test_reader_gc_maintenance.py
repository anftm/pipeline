import json
import os
from pathlib import Path
import tempfile
import unittest
import ast
from unittest.mock import patch

from scripts import reader_gc_maintenance as maintenance


class MaintenanceTests(unittest.TestCase):
    def test_cli_maintenance_always_preserves_queued_runs(self):
        tree = ast.parse(Path(maintenance.__file__).read_text(encoding='utf-8'))
        entry = next(node for node in tree.body if isinstance(node, ast.If)
                     and isinstance(node.test, ast.Compare)
                     and isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__')
        calls = [node for node in ast.walk(entry) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'run']
        self.assertEqual(len(calls), 1)
        keywords = {item.arg: item.value for item in calls[0].keywords}
        self.assertIsInstance(keywords.get('preserve_queued_runs'), ast.Constant)
        self.assertIs(keywords['preserve_queued_runs'].value, True)

    def setUp(self):
        # These are offline fixtures. A production Actions environment may set
        # durable maintenance globally; never let a unit test touch that bucket.
        environment = patch.dict(os.environ, {'READER_GC_DURABLE_MAINTENANCE': 'false'})
        environment.start()
        self.addCleanup(environment.stop)
        store = patch.object(maintenance, '_STORE', None)
        store.start()
        self.addCleanup(store.stop)

    def test_isolated_callback_restores_producers_and_does_not_dispatch_collector(self):
        state = {'active': True}
        events = []
        def api(repo, path, *, method='GET', body=None):
            events.append((repo, path, method))
            if path == 'actions/workflows/1':
                return {'state': 'active' if state['active'] else 'disabled_manually'}
            if path.endswith('/disable'):
                state['active'] = False
                return {}
            if path.endswith('/enable'):
                state['active'] = True
                return {}
            raise AssertionError(path)
        def pages(repo, path, field):
            if path == 'actions/workflows':
                return [{'id': 1, 'state': 'active', 'path': '.github/workflows/pdf-account-worker.yml'}]
            return []
        callback = []
        def isolated(snapshot):
            self.assertFalse(state['active'])
            callback.append(snapshot['maintenance_id'])
            return 'done'
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, 'producer_repositories', return_value=['central/repo']), \
                patch.object(maintenance, 'load_config', return_value={'publisher_repository': 'central/repo'}), \
                patch.object(maintenance, 'pages', side_effect=pages), \
                patch.object(maintenance, 'api', side_effect=api):
            journal = Path(directory) / 'journal.json'
            self.assertEqual(maintenance.run(journal, isolated_callback=isolated), 'done')
            self.assertTrue(json.loads(journal.read_text())['restored'])
        self.assertTrue(state['active'])
        self.assertEqual(len(callback), 1)
        self.assertFalse(any(path.endswith('/dispatches') for _, path, _ in events))

    def test_isolated_callback_failure_also_restores_producers(self):
        state = {'active': True}
        def api(repo, path, *, method='GET', body=None):
            if path == 'actions/workflows/1':
                return {'state': 'active' if state['active'] else 'disabled_manually'}
            if path.endswith('/disable'):state['active'] = False;return {}
            if path.endswith('/enable'):state['active'] = True;return {}
            raise AssertionError(path)
        def pages(repo, path, field):
            return ([{'id': 1, 'state': 'active', 'path': '.github/workflows/pdf-account-worker.yml'}]
                    if path == 'actions/workflows' else [])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, 'producer_repositories', return_value=['central/repo']), \
                patch.object(maintenance, 'load_config', return_value={'publisher_repository': 'central/repo'}), \
                patch.object(maintenance, 'pages', side_effect=pages), \
                patch.object(maintenance, 'api', side_effect=api):
            journal = Path(directory) / 'journal.json'
            with self.assertRaisesRegex(RuntimeError, 'callback failure'):
                maintenance.run(journal, isolated_callback=lambda _: (_ for _ in ()).throw(RuntimeError('callback failure')))
            self.assertTrue(json.loads(journal.read_text())['restored'])
        self.assertTrue(state['active'])

    def test_mutating_callback_failure_keeps_producers_isolated_until_recovery(self):
        state={'active':True}
        def api(repo,path,*,method='GET',body=None):
            if path=='actions/workflows/1':return {'state':'active' if state['active'] else 'disabled_manually'}
            if path.endswith('/disable'):state['active']=False;return {}
            if path.endswith('/enable'):state['active']=True;return {}
            raise AssertionError(path)
        def pages(repo,path,field):
            return ([{'id':1,'state':'active','path':'.github/workflows/pdf-account-worker.yml'}]
                    if path=='actions/workflows' else [])
        def partial(snapshot):
            snapshot['callback_mutation_started']=True
            raise ValueError('partial update')
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ,{'GITHUB_RUN_ID':'123'}), \
             patch.object(maintenance,'producer_repositories',return_value=['central/repo']), \
             patch.object(maintenance,'load_config',return_value={'publisher_repository':'central/repo'}), \
             patch.object(maintenance,'pages',side_effect=pages), patch.object(maintenance,'api',side_effect=api):
            path=Path(directory)/'journal.json'
            with self.assertRaisesRegex(RuntimeError,'producers remain isolated'):
                maintenance.run(path,isolated_callback=partial,require_callback_commit=True)
            journal=json.loads(path.read_text())
            self.assertTrue(journal['callback_recovery_required'])
            self.assertFalse(state['active'])
            with self.assertRaisesRegex(RuntimeError,'recovery is required'):
                maintenance.restore(path)
            self.assertFalse(state['active'])

    def test_recovery_cannot_restore_an_active_guarded_callback(self):
        class Store:
            def read_bytes(self,bucket,path):
                return json.dumps({'version':1,'kind':'reader-maintenance-journal',
                    'repositories':{},'callback_owner_run_id':'123'}).encode()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(maintenance,'durable_store',return_value=Store()), \
             patch.object(maintenance,'load_config',return_value={'publisher_repository':'central/repo'}), \
             patch.object(maintenance,'api',return_value={'status':'in_progress'}), \
             patch.object(maintenance,'restore',side_effect=AssertionError('must not restore')):
            with self.assertRaisesRegex(RuntimeError,'still active'):
                maintenance.recover(Path(directory)/'journal.json')

    def test_other_maintenance_cannot_restore_a_committed_staged_callback(self):
        state={'version':1,'kind':'reader-maintenance-journal','repositories':{},
               'callback_owner_run_id':'123','callback_mutation_started':True,
               'callback_handoff_stage':'references-retired','callback_plan_sha256':'a'*64,
               'callback_committed':True,'callback_recovery_required':False}
        class Store:
            def read_bytes(self,bucket,path):return json.dumps(state).encode()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(maintenance,'durable_store',return_value=Store()), \
             patch.object(maintenance,'restore',side_effect=AssertionError('must not restore')):
            with self.assertRaisesRegex(RuntimeError,'own verified final restoration'):
                maintenance.recover(Path(directory)/'journal.json')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'GITHUB_RUN_ID':'other'}):
            path=Path(directory)/'journal.json';path.write_text(json.dumps(state))
            with self.assertRaisesRegex(RuntimeError,'verified Actions owner'):
                maintenance.restore(path)

    def test_staged_callback_yields_without_restoring_and_persists_its_owner(self):
        active={'value':True}
        def api(repo,path,*,method='GET',body=None):
            if path=='actions/workflows/1':return {'state':'active' if active['value'] else 'disabled_manually'}
            if path.endswith('/disable'):active['value']=False;return {}
            if path.endswith('/enable'):active['value']=True;return {}
            raise AssertionError(path)
        def pages(repo,path,field):
            return ([{'id':1,'state':'active','path':'.github/workflows/pdf-account-worker.yml'}]
                    if path=='actions/workflows' else [])
        def callback(state):
            state.update(callback_mutation_started=True,callback_recovery_required=True,
                         callback_handoff_ready=True,callback_handoff_stage='references-retired',
                         callback_handoff_owner_run_id='123')
            return {'phase':'handoff-complete','handoff_complete':True}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'GITHUB_RUN_ID':'123'}), \
             patch.object(maintenance,'producer_repositories',return_value=['central/repo']), \
             patch.object(maintenance,'load_config',return_value={'publisher_repository':'central/repo'}), \
             patch.object(maintenance,'pages',side_effect=pages), patch.object(maintenance,'api',side_effect=api):
            path=Path(directory)/'journal.json'
            result=maintenance.run(path,isolated_callback=callback,require_callback_commit=True,
                                   allow_staged_handoff=True,preserve_queued_runs=True)
            self.assertEqual(result['phase'],'handoff-complete')
            self.assertFalse(active['value'])
            saved=json.loads(path.read_text())
            self.assertTrue(saved['callback_handoff_ready'])
            self.assertTrue(saved['callback_recovery_required'])
            with self.assertRaisesRegex(RuntimeError,'recovery is required'):
                maintenance.restore(path)

    def test_staged_callback_exception_never_counts_as_a_clean_handoff(self):
        active={'value':True}
        def api(repo,path,*,method='GET',body=None):
            if path=='actions/workflows/1':return {'state':'active' if active['value'] else 'disabled_manually'}
            if path.endswith('/disable'):active['value']=False;return {}
            if path.endswith('/enable'):active['value']=True;return {}
            raise AssertionError(path)
        def pages(repo,path,field):
            return ([{'id':1,'state':'active','path':'.github/workflows/pdf-account-worker.yml'}]
                    if path=='actions/workflows' else [])
        def callback(state):
            state.update(callback_mutation_started=True,callback_recovery_required=True,
                         callback_handoff_ready=True,callback_handoff_stage='references-retired',
                         callback_handoff_owner_run_id='123')
            raise ValueError('failed after handoff marker')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'GITHUB_RUN_ID':'123'}), \
             patch.object(maintenance,'producer_repositories',return_value=['central/repo']), \
             patch.object(maintenance,'load_config',return_value={'publisher_repository':'central/repo'}), \
             patch.object(maintenance,'pages',side_effect=pages), patch.object(maintenance,'api',side_effect=api):
            with self.assertRaisesRegex(RuntimeError,'producers remain isolated'):
                maintenance.run(Path(directory)/'journal.json',isolated_callback=callback,
                                require_callback_commit=True,allow_staged_handoff=True)
            self.assertFalse(active['value'])

    def test_bounded_window_preserves_queued_producer_work(self):
        state = {'active': True}
        calls = []
        def api(repo, path, *, method='GET', body=None):
            calls.append((path, method))
            if path == 'actions/workflows/1':
                return {'state': 'active' if state['active'] else 'disabled_manually'}
            if path.endswith('/disable'):
                state['active'] = False
                return {}
            if path.endswith('/enable'):
                state['active'] = True
                return {}
            raise AssertionError(path)
        def pages(repo, path, field):
            if path == 'actions/workflows':
                return [{'id': 1, 'state': 'active', 'path': '.github/workflows/pdf-account-worker.yml'}]
            if path == 'actions/runs?status=queued':
                return [{'id': 9, 'status': 'queued', 'path': '.github/workflows/reader-v3-publish.yml'}]
            return []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, 'producer_repositories', return_value=['central/repo']), \
                patch.object(maintenance, 'load_config', return_value={'publisher_repository': 'central/repo'}), \
                patch.object(maintenance, 'pages', side_effect=pages), patch.object(maintenance, 'api', side_effect=api):
            path = Path(directory) / 'journal.json'
            result = maintenance.run(path, isolated_callback=lambda _: 'done', preserve_queued_runs=True)
            self.assertEqual(result, 'done')
            self.assertTrue(json.loads(path.read_text())['restored'])
        self.assertFalse(any(path.endswith('/cancel') for path, method in calls))

    def test_preflight_only_restore_performs_no_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'preflight.json'
            journal.write_text(json.dumps({'preflight_only': True, 'repositories': {}}))
            with patch.object(maintenance, 'api', side_effect=AssertionError('remote API accessed')), \
                    patch.object(maintenance, 'durable_store', side_effect=AssertionError('durable store accessed')):
                maintenance.restore(journal)

    def test_suppressed_resume_never_dispatches_new_ocr_tasks(self):
        state = {'repositories': {'central/repo': {'workflows': [
            {'path': '.github/workflows/reader-v3-publish.yml', 'state': 'active'},
            {'path': '.github/workflows/pdf-lane-watchdog.yml', 'state': 'active'}]}},
            'resumption_dispatches': {'pdf-lane-watchdog.yml': 'suppressed', 'reader-v3-publish.yml': 'suppressed'}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, 'api', side_effect=AssertionError('unexpected dispatch')):
            maintenance.kick_restored(Path(directory) / 'journal.json', state)

    def test_restore_never_restarts_retired_ocr_matrix_without_suppression_flag(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, 'api', side_effect=AssertionError('unexpected dispatch')):
            path=Path(directory)/'journal.json'
            state={'repositories': {}}
            maintenance.kick_restored(path,state)
            self.assertEqual(state['resumption_dispatches'],
                             {'pdf-lane-watchdog.yml':'retired','reader-v3-publish.yml':'retired'})

    def test_failure_after_disabling_producers_restores_prior_permissions(self):
        permissions = {"enabled": True, "allowed_actions": "all"}
        workflow = {"state": "active"}
        events = []
        mutations = {"disable": 0}

        def api(repo, path, *, method="GET", body=None):
            events.append((path, method, body))
            if path == "actions/permissions":
                return dict(permissions)
            if path == "actions/workflows/1":
                return dict(workflow)
            if path == "actions/runs/9":
                return {"id": 9, "status": "completed", "conclusion": "cancelled", "run_attempt": 1}
            if "/attempts/" in path:
                return {"id": 9, "status": "queued", "run_attempt": 1}
            if path.endswith("/rerun"):
                return {}
            if path.endswith("/disable"):
                workflow["state"] = "disabled_manually"
                return {}
            if path.endswith("/enable"):
                workflow["state"] = "active"
                return {}
            raise AssertionError(path)

        # Producer disabling succeeds, then the queued-run cancellation fails.
        # The journaled workflow state must still be restored.
        def cancel_raises(repo, path, *, method="GET", body=None):
            if path.endswith("/cancel"):
                mutations["disable"] += 1
                raise RuntimeError("failed mutation")
            return api(repo, path, method=method, body=body)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance, "producer_repositories", return_value=["central/repo"]), \
                patch.object(maintenance, "load_config", return_value={"publisher_repository": "central/repo"}), \
                patch.object(maintenance, "pages", side_effect=self.pages), \
                patch.object(maintenance, "api", side_effect=cancel_raises):
            path = Path(directory) / "journal.json"
            with self.assertRaises(RuntimeError):
                maintenance.run(path)
            state = json.loads(path.read_text())
            self.assertTrue(state["restored"])
            self.assertIn(("actions/workflows/1/enable", "PUT", None), events)
            self.assertFalse(state["restored"] is None)

    @staticmethod
    def pages(repo, path, field):
        if path == "actions/workflows":
            return [{"id": 1, "state": "active", "path": ".github/workflows/pdf-account-worker.yml"}]
        if path == "actions/runs?status=queued":
            return [{"id": 9, "status": "queued", "path": ".github/workflows/pdf-account-worker.yml"}]
        return []

    def test_restoration_skips_a_workflow_that_is_already_active(self):
        events = []

        def api(repo, path, *, method="GET", body=None):
            events.append((path, method))
            if path == "actions/permissions":
                return {"enabled": True}
            if path == "actions/workflows/1":
                return {"state": "active"}
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            maintenance.journal(path, {"repositories": {"owner/repo": {
                "permission": {"enabled": True},
                "workflows": [{"id": 1, "state": "active"}], "cancelled": []}}})
            with patch.object(maintenance, "api", side_effect=api), \
                    patch.object(maintenance, "load_config",
                                 return_value={"publisher_repository": "central/repo"}):
                maintenance.restore(path)
            self.assertNotIn(("actions/workflows/1/enable", "PUT"), events)
            self.assertTrue(json.loads(path.read_text())["restored"])

    def test_preflight_fails_before_isolation_without_exposing_tokens(self):
        def api(repo, path, *, method="GET", body=None):
            if repo == "owner/denied":
                raise maintenance.MaintenanceAPIError(repo, path, method, 403)
            return {"enabled": True} if path == "actions/permissions" else {}

        with patch.object(maintenance, "producer_repositories",
                          return_value=["owner/ok", "owner/denied"]), \
                patch.object(maintenance, "pages",
                             return_value=[{"id": 1, "state": "active",
                                            "path": ".github/workflows/pdf-account-worker.yml"},
                                           {"id": 2, "state": "active",
                                            "path": ".github/workflows/reader-assets.yml"}]), \
                patch.object(maintenance, "api", side_effect=api):
            with self.assertRaisesRegex(RuntimeError, "preflight failed for 1") as caught:
                maintenance.preflight()
        self.assertIn("403", str(caught.exception))
        # No credential material may appear in a failure intended for logs.
        self.assertNotIn("ghp_", str(caught.exception))

    def test_disabling_repository_actions_omits_allowed_actions(self):
        state = {"enabled": True, "allowed_actions": "all"}
        writes = []

        def api(repo, path, *, method="GET", body=None):
            if path != "actions/permissions":
                raise AssertionError(path)
            if method == "GET":
                return dict(state)
            writes.append(body)
            state.clear()
            state.update(body)
            return {}

        with patch.object(maintenance, "api", side_effect=api):
            maintenance.permissions("owner/repo", {
                "enabled": False, "allowed_actions": "all"})
        self.assertEqual(writes, [{"enabled": False}])
        self.assertFalse(state["enabled"])

    def test_successful_policy_put_waits_for_stale_reads_without_repeating_mutation(self):
        reads=iter([{'enabled':True},{'enabled':True},{'enabled':True},{'enabled':False}])
        writes=[]
        def api(repo,path,*,method='GET',body=None):
            if method=='GET':return next(reads)
            writes.append(body);return {}
        with patch.object(maintenance,'api',side_effect=api),patch.object(maintenance.time,'sleep') as sleep:
            maintenance.permissions('owner/repo',{'enabled':False,'allowed_actions':'all'})
        self.assertEqual(writes,[{'enabled':False}])
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[1,2])

    def test_successful_policy_put_that_never_converges_cannot_establish_isolation(self):
        writes=[]
        def api(repo,path,*,method='GET',body=None):
            if method=='PUT':writes.append(body)
            return {'enabled':True}
        with patch.object(maintenance,'api',side_effect=api),patch.object(maintenance.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'owner/repo:actions/permissions'):
                maintenance.permissions('owner/repo',{'enabled':False})
        self.assertEqual(writes,[{'enabled':False}])

    def test_ambiguous_put_converges_by_reads_before_any_retry(self):
        reads=iter([{'enabled':True},{'enabled':True},{'enabled':False}]);writes=[]
        def api(repo,path,*,method='GET',body=None):
            if method=='GET':return next(reads)
            writes.append(body)
            raise maintenance.MaintenanceAPIError(repo,path,method,504)
        with patch.object(maintenance,'api',side_effect=api),patch.object(maintenance.time,'sleep'):
            maintenance.permissions('owner/repo',{'enabled':False})
        self.assertEqual(writes,[{'enabled':False}])

    def test_workflow_disable_and_restore_wait_for_readback_without_duplicate_put(self):
        for enabled,before,after in [(False,'active','disabled_manually'),(True,'disabled_manually','active')]:
            reads=iter([{'state':before},{'state':before},{'state':after}]);writes=[]
            def api(repo,path,*,method='GET',body=None):
                if method=='GET':return next(reads)
                writes.append(path);return {}
            with self.subTest(enabled=enabled),patch.object(maintenance,'api',side_effect=api), \
                    patch.object(maintenance.time,'sleep'):
                maintenance.workflow_state('owner/repo',{'id':1},enabled)
            self.assertEqual(writes,['actions/workflows/1/'+('enable' if enabled else 'disable')])

    def test_unconfirmed_policy_readback_restores_journal_without_dispatching_collector(self):
        writes=[]
        def api(repo,path,*,method='GET',body=None):
            if path=='actions/permissions':
                if method=='PUT':writes.append(body)
                return {'enabled':True,'allowed_actions':'all'}
            raise AssertionError('Unconfirmed isolation must never dispatch: '+path)
        inventory={'owner/repo':{'permission':{'enabled':True,'allowed_actions':'all'},
            'workflows':[],'cancelled':[]}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(maintenance,'preflight',return_value=inventory), \
                patch.object(maintenance,'pages',return_value=[]), \
                patch.object(maintenance,'load_config',return_value={'publisher_repository':'central/repo'}), \
                patch.object(maintenance,'api',side_effect=api), \
                patch.object(maintenance,'kick_restored'),patch.object(maintenance.time,'sleep'):
            path=Path(directory)/'journal.json'
            with self.assertRaisesRegex(RuntimeError,'readback mismatch'):
                maintenance.run(path,apply=False)
            state=json.loads(path.read_text())
        self.assertTrue(state['restored'])
        self.assertEqual(state['restore_errors'],[])
        self.assertNotIn('dispatch_requested',state)
        self.assertEqual(writes,[{'enabled':False}])

    def test_restore_retries_unconfirmed_idempotent_policy_and_preserves_original_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'journal.json'
            maintenance.journal(path,{'repositories':{'owner/repo':{
                'permission_changed':True,'permission':{'enabled':True},'workflows':[],'cancelled':[]}}})
            error=maintenance.MaintenanceReadbackError('owner/repo','actions/permissions','maintenance state mutation')
            with patch.object(maintenance,'permissions',side_effect=[error,None]) as permissions, \
                    patch.object(maintenance,'kick_restored') as kick,patch.object(maintenance.time,'sleep'):
                maintenance.restore(path)
            state=json.loads(path.read_text())
            self.assertEqual(permissions.call_count,2)
            self.assertTrue(state['restored'])
            self.assertEqual(state['restore_errors'],[])
            self.assertEqual(len(state['restore_attempt_errors']),1)
            self.assertEqual(state['restore_attempt_errors'][0]['stage'],'permissions')
            self.assertEqual(state['restore_attempt_errors'][0]['error'],'MaintenanceReadbackError')
            self.assertTrue(state['restore_attempt_errors'][0]['retryable'])
            kick.assert_called_once()

    def test_restore_failed_all_attempts_keeps_history_after_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'journal.json'
            maintenance.journal(path,{'repositories':{'owner/repo':{
                'permission_changed':True,'permission':{'enabled':True},'workflows':[],'cancelled':[]}}})
            error=maintenance.MaintenanceAPIError('owner/repo','actions/permissions','GET',503)
            with patch.object(maintenance,'permissions',side_effect=error) as permissions, \
                    patch.object(maintenance,'kick_restored') as kick,patch.object(maintenance.time,'sleep'):
                with self.assertRaisesRegex(RuntimeError,'restoration requires recovery'):
                    maintenance.restore(path)
            failed=json.loads(path.read_text())
            self.assertEqual(permissions.call_count,3)
            self.assertFalse(failed['restored'])
            self.assertEqual(len(failed['restore_attempt_errors']),3)
            self.assertEqual(failed['restore_errors'][0]['status'],503)
            kick.assert_not_called()
            with patch.object(maintenance,'permissions'),patch.object(maintenance,'kick_restored'):
                maintenance.restore(path)
            recovered=json.loads(path.read_text())
            self.assertTrue(recovered['restored'])
            self.assertEqual(recovered['restore_errors'],[])
            self.assertEqual(recovered['restore_attempt_errors'],failed['restore_attempt_errors'])

    def test_restore_permission_denial_is_not_retried_or_exposed_as_credential_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'journal.json'
            maintenance.journal(path,{'repositories':{'owner/repo':{
                'permission_changed':True,'permission':{'enabled':True},'workflows':[],'cancelled':[]}}})
            error=maintenance.MaintenanceAPIError('owner/repo','actions/permissions','PUT',403)
            with patch.object(maintenance,'permissions',side_effect=error) as permissions, \
                    patch.object(maintenance,'kick_restored') as kick,patch.object(maintenance.time,'sleep'):
                with self.assertRaisesRegex(RuntimeError,'restoration requires recovery'):
                    maintenance.restore(path)
            value=json.loads(path.read_text())
            self.assertEqual(permissions.call_count,1)
            self.assertFalse(value['restore_attempt_errors'][0]['retryable'])
            self.assertEqual(value['restore_errors'][0]['status'],403)
            kick.assert_not_called()

    def test_repository_permission_denial_is_best_effort(self):
        record = {"permission": {"enabled": True, "allowed_actions": "all"}}
        denied = maintenance.MaintenanceAPIError(
            "owner/repo", "actions/permissions", "PUT", 403)
        with patch.object(maintenance, "permissions", side_effect=denied):
            self.assertFalse(maintenance.disable_actions("owner/repo", record))
        self.assertEqual(record["permission_denied"], 403)
        self.assertNotIn("permission_changed", record)

    def test_repository_permission_server_error_is_not_ignored(self):
        record = {"permission": {"enabled": True}}
        failed = maintenance.MaintenanceAPIError(
            "owner/repo", "actions/permissions", "PUT", 500)
        with patch.object(maintenance, "permissions", side_effect=failed):
            with self.assertRaises(maintenance.MaintenanceAPIError):
                maintenance.disable_actions("owner/repo", record)

    def test_preflight_skips_central_repository_actions_permission(self):
        calls = []

        def api(repo, path, *, method="GET", body=None):
            calls.append((repo, path))
            raise AssertionError("central Actions policy must not be read")

        with patch.object(maintenance, "producer_repositories",
                          return_value=["central/repo"]), \
                patch.object(maintenance, "load_config",
                             return_value={"publisher_repository": "central/repo"}), \
                patch.object(maintenance, "pages",
                             return_value=[{"id": 1, "state": "active",
                                            "path": ".github/workflows/pdf-account-worker.yml"}]), \
                patch.object(maintenance, "api", side_effect=api):
            result = maintenance.preflight()
        self.assertEqual(list(result), ["central/repo"])
        self.assertEqual(result["central/repo"]["permission"], {})
        self.assertEqual(calls, [])

    def test_cancelled_run_without_jobs_is_not_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            maintenance.journal(path, {"repositories": {"owner/repo": {"workflows": [],
                "cancelled": [{"id": 42}]}}})
            with patch.object(maintenance, "api", return_value={
                    "status": "completed", "conclusion": "cancelled", "run_attempt": 1}) as api, \
                    patch.object(maintenance, "pages", return_value=[]):
                maintenance.restore(path)
            self.assertTrue(json.loads(path.read_text())["restored"])
            self.assertFalse(any(call.args[1].endswith("/rerun") for call in api.call_args_list))

    def test_ambiguous_rerun_is_never_blindly_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            maintenance.journal(path, {"repositories": {"owner/repo": {"workflows": [],
                "cancelled": [{"id": 42, "restoration_requested": True, "restoration_attempt": 1}]}}})
            with patch.object(maintenance, "api", return_value={
                    "status": "completed", "conclusion": "cancelled", "run_attempt": 1}) as api:
                maintenance.restore(path)
            state = json.loads(path.read_text())
            self.assertTrue(state["restored"])
            self.assertTrue(state["repositories"]["owner/repo"]["cancelled"][0]["rerun_skipped"])
            self.assertFalse(any(call.args[1].endswith("/rerun") for call in api.call_args_list))

    def test_not_yet_queued_cancellation_is_deferred_to_the_fence(self):
        queued = {"id": 42, "status": "queued", "path": ".github/workflows/pdf-account-worker.yml"}
        calls = []

        def api(repo, path, *, method="GET", body=None):
            calls.append((repo, path, method))
            if path == "actions/workflows":
                return [{"id": 1, "state": "active",
                         "path": ".github/workflows/pdf-account-worker.yml"}]
            if path.startswith("actions/workflows/1"):
                return {"state": "disabled_manually"} if method == "GET" else {}
            if path.startswith("actions/runs?status="):
                return [queued] if path.endswith("queued") else []
            if path.endswith("/cancel"):
                raise maintenance.MaintenanceAPIError(repo, path, "POST", 409)
            if "/attempts/" in path:
                return {"id": 42, "status": "queued", "run_attempt": 1}
            if path == "actions/runs":
                return []
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            def pages(repo, request, field):
                if request == "actions/workflows":
                    return [{"id": 1, "state": "active",
                             "path": ".github/workflows/pdf-account-worker.yml"}]
                if "status=queued" in request:
                    return [queued]
                return []
            with patch.object(maintenance, "producer_repositories", return_value=["central/repo"]), \
                    patch.object(maintenance, "load_config",
                                 return_value={"publisher_repository": "central/repo"}), \
                    patch.object(maintenance, "ACTIVE", ("queued",)), \
                    patch.object(maintenance, "pages", side_effect=pages), \
                    patch.object(maintenance, "restore") as restore, \
                    patch.object(maintenance, "api", side_effect=api):
                with self.assertRaises(Exception):
                    maintenance.run(path, apply=False)
            state = json.loads(path.read_text())
            self.assertTrue(state["repositories"]["central/repo"]["cancelled"][0].get("cancel_deferred"))
