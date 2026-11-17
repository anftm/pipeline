import copy
import unittest
from scripts.ccrd_stage_provenance import validate


class StageProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.run={'id':42,'status':'completed','conclusion':'failure','head_branch':'main','head_sha':'a'*40,
                  'event':'schedule','path':'.github/workflows/publish-ccrd-index.yml','run_attempt':1}
        self.jobs={'total_count':1,'jobs':[{'run_id':42,'head_sha':'a'*40,'name':'stage','steps':[
            {'name':'Build and stage only if the upstream source changed','number':6,'status':'completed','conclusion':'success'},
            {'name':'Preserve exact staged candidate for recovery','number':7,'status':'completed','conclusion':'success'},
            {'name':'Publish verified staged update when automation is enabled','number':8,'status':'completed','conclusion':'failure'}]}]}
    def test_later_activation_failure_keeps_exact_staging_recoverable(self):
        self.assertTrue(validate(self.run,self.jobs,run_id=42,sha='a'*40)['staging_verified'])
    def test_bad_stage_or_missing_artifact_never_recoverable(self):
        for number in [0,1]:
            jobs=copy.deepcopy(self.jobs);jobs['jobs'][0]['steps'][number]['conclusion']='failure'
            with self.assertRaises(ValueError):validate(self.run,jobs,run_id=42,sha='a'*40)
    def test_rerun_foreign_sha_job_and_incomplete_inventory_rejected(self):
        for field,value in [('run_attempt',2),('head_sha','b'*40),('event','pull_request')]:
            with self.assertRaises(ValueError):validate({**self.run,field:value},self.jobs,run_id=42,sha='a'*40)
        for change in [{'total_count':2},{'jobs':[]}]:
            with self.assertRaises(ValueError):validate(self.run,{**self.jobs,**change},run_id=42,sha='a'*40)
        jobs=copy.deepcopy(self.jobs);jobs['jobs'][0]['head_sha']='b'*40
        with self.assertRaises(ValueError):validate(self.run,jobs,run_id=42,sha='a'*40)
