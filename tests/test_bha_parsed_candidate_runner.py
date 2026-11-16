import copy
from pathlib import Path
import unittest
from unittest.mock import Mock
from scripts import bha_parsed_candidate as runner


class CandidateRunnerTests(unittest.TestCase):
    def setUp(self):
        self.plan={'action':'build-candidate','archive':0,'plan_sha256':'a'*64,
            'snapshot':{'helper_revision':'b'*40,'mirror_branches':{'parsed':'c'*40},'parsed_tree':'d'*40}}
        self.candidate={'changed':True,'revisions':{'parsed':'c'*40},'helper_revision':'b'*40,
                        'before_tree':'d'*40,'remote_writes':0}
        self.inspect=Mock(side_effect=lambda:copy.deepcopy(self.plan))
        self.helper=Mock(return_value=Path('/not-created/helper'));self.build=Mock(return_value=self.candidate)
        self.bundle=Mock(return_value={'sha256':'e'*64});self.emit=Mock()

    def run_candidate(self):
        return runner.generate(self.plan,inspect_live=self.inspect,prepare_helper=self.helper,
            build_candidate=self.build,bundle=self.bundle,workspace=Path('/not-created'),emit=self.emit)

    def test_no_input_change_allocates_no_helper_or_candidate(self):
        self.plan['action']='no-input-change'
        self.assertEqual(self.run_candidate()['status'],'no-change')
        self.helper.assert_not_called();self.build.assert_not_called();self.bundle.assert_not_called()

    def test_exact_candidate_is_packaged_without_publication(self):
        result=self.run_candidate()
        self.assertEqual(result['status'],'candidate-ready');self.assertFalse(result['publication_authorized'])
        self.assertEqual(result['remote_writes'],0);self.assertEqual(self.inspect.call_count,2)
        self.bundle.assert_called_once()

    def test_source_drift_after_build_prevents_artifact_acceptance(self):
        self.inspect.side_effect=[self.plan,{**self.plan,'plan_sha256':'f'*64}]
        with self.assertRaisesRegex(ValueError,'during generation'):self.run_candidate()
        self.bundle.assert_not_called()

    def test_wrong_generated_source_and_unsupported_action_block(self):
        self.candidate['helper_revision']='f'*40
        with self.assertRaisesRegex(ValueError,'identities differ'):self.run_candidate()
        self.bundle.assert_not_called()
        self.plan['action']='inspect-drift'
        with self.assertRaisesRegex(ValueError,'separate drift'):self.run_candidate()

    def test_equal_tree_avoids_bundle(self):
        self.candidate['changed']=False
        self.assertEqual(self.run_candidate()['status'],'no-change');self.bundle.assert_not_called()
