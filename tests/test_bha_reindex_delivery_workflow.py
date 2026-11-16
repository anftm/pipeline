"""BHA delivery stays explicit, serialized and tied to the reviewed plan."""
import os
from pathlib import Path
import subprocess
import unittest
import yaml


class DeliveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(Path('.github/workflows/bha-reindex-deliver.yml').read_text())
        self.steps = {s.get('name'): s for s in self.workflow['jobs']['deliver']['steps'] if s.get('name')}

    def test_gated_schedule_shared_serialization_and_evidence(self):
        triggers = self.workflow.get('on', self.workflow.get(True))
        self.assertEqual(set(triggers), {'workflow_dispatch', 'schedule'})
        self.assertEqual(triggers['schedule'], [{'cron': '43 */6 * * *'}])
        self.assertIn("github.event_name == 'workflow_dispatch' || vars.BHA_REINDEX_AUTO_ENABLED == 'true'", self.workflow['jobs']['deliver']['if'])
        self.assertEqual(self.steps['Run bounded BHA delivery']['env']['ACTION'], "${{ github.event_name == 'schedule' && 'auto' || inputs.action }}")
        self.assertEqual(self.workflow['concurrency']['group'], 'update-bha-index')
        self.assertIs(self.workflow['concurrency']['cancel-in-progress'], False)
        self.assertEqual(self.workflow['permissions'], {'contents': 'read', 'actions': 'read'})
        self.assertEqual(self.steps['Preserve operation and acceptance evidence']['if'], 'always()')

    def test_invalid_requests_fail_before_execution(self):
        script = self.steps['Validate explicit request']['run']
        for action, run, sha, operation in [
            ('plan', '12', '', ''), ('plan', '', '', 'existing'),
            ('apply', '', 'a'*64, ''), ('apply', '12', 'invalid', ''),
            ('reconcile', '', '', ''), ('reconcile', '12', '', 'existing'),
            ('abandon', '', '', 'existing'), ('unknown', '', '', '')]:
            with self.subTest(action=action, run=run, operation=operation):
                result = subprocess.run(['bash','-c',script], capture_output=True,
                    env={**os.environ,'ACTION':action,'PLAN_RUN':run,'PLAN_SHA':sha,'OPERATION':operation,'REASON':''})
                self.assertNotEqual(result.returncode, 0)

    def test_provenance_rejects_other_sha_workflow_repo_and_failed_run(self):
        script = self.steps['Verify exact plan provenance']['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        import tempfile, json, hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'bha-reviewed-plan').mkdir()
            raw=b'{}'; (root/'bha-reviewed-plan/plan.json').write_bytes(raw)
            run={'status':'completed','conclusion':'success','event':'workflow_dispatch','head_branch':'main',
                 'path':'.github/workflows/bha-reindex-deliver.yml','head_sha':'a'*40,
                 'repository':{'full_name':'anftm/pipeline'},'run_attempt':1}
            env={**os.environ,'RUNNER_TEMP':directory,'GITHUB_SHA':'a'*40,'PLAN_SHA':hashlib.sha256(raw).hexdigest()}
            for changes in [{}, {'head_sha':'b'*40}, {'path':'.github/workflows/bha-reindex-plan.yml'},
                            {'repository':{'full_name':'other/pipeline'}}, {'conclusion':'failure'}, {'run_attempt':2}]:
                (root/'bha-plan-run.json').write_text(json.dumps({**run,**changes}))
                import sys
                result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True)
                self.assertEqual(result.returncode==0, not changes)

    def test_abandon_passes_exact_operation_and_enum_reason(self):
        script=self.steps['Run bounded BHA delivery']['run']
        result=subprocess.run(['bash','-c','mkdir() { :; }; python() { printf "%s\\0" "$@"; };\n'+script],
            env={**os.environ,'ACTION':'abandon','OPERATION':'a'*32,'REASON':'stale-plan'},capture_output=True,check=True)
        self.assertEqual(result.stdout.decode().split('\0')[:-1],
            ['-u','-m','scripts.bha_reindex_run','--action','abandon','--output','output/bha/result.json',
             '--samples','config/bha-delivery-samples.json','--operation-id','a'*32,'--reason','stale-plan'])

    def test_apply_rerun_cannot_start_runner(self):
        script=self.steps['Run bounded BHA delivery']['run']
        result=subprocess.run(['bash','-c',script],env={**os.environ,'ACTION':'apply','GITHUB_RUN_ATTEMPT':'2'},capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'Apply/auto cannot be rerun',result.stderr)
        self.assertNotIn('${{',script)
