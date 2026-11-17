import copy
import json
from pathlib import Path
import tempfile
import unittest
from scripts import bha_runtime_baseline as b, bha_upstream_auto as auto
from tests.test_bha_upstream_auto import OrchestrationTests


def inputs(letter):return {**dict.fromkeys(['main','config','ocr_cache','ocr_patch','helper'],letter*40),'patch_layout':'1'}


class RuntimeBaselineTests(unittest.TestCase):
    def setUp(self):
        self.inputs={'version':1,'archives':{str(i):inputs('a') for i in range(32)}}
        self.trees={str(i):'b'*40 for i in range(32)}
        self.accept={'operation':'c'*32,'job_id':'d'*32,'snapshot_generation':'bha-test','commit':'e'*40,'tree':'f'*40}
    def test_verified_overlay_survives_repo_baseline_without_main_write(self):
        overlay=b.advance(b.empty(),self.inputs,self.trees,archive=6,after_inputs=inputs('c'),after_tree='f'*40,acceptance=self.accept)
        effective,trees=b.apply(self.inputs,self.trees,overlay)
        self.assertEqual(effective['archives']['6'],inputs('c'));self.assertEqual(trees['6'],'f'*40)
        self.assertEqual(self.inputs['archives']['6'],inputs('a'))
        self.assertEqual(b.apply(effective,trees,overlay),(effective,trees))
        second=b.advance(overlay,effective,trees,archive=6,after_inputs=inputs('d'),after_tree='e'*40,acceptance=self.accept)
        self.assertEqual(second['archives']['6']['before_inputs'],inputs('a'))
        self.assertEqual(b.apply(self.inputs,self.trees,second)[0]['archives']['6'],inputs('d'))
    def test_unrelated_baseline_change_blocks_overlay(self):
        overlay=b.advance(b.empty(),self.inputs,self.trees,archive=6,after_inputs=inputs('c'),after_tree='f'*40,acceptance=self.accept)
        bad=copy.deepcopy(self.inputs);bad['archives']['6']=inputs('d')
        with self.assertRaisesRegex(ValueError,'conflicts'):b.apply(bad,self.trees,overlay)
    def test_materialization_never_changes_repository_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);ip=root/'inputs.json';tp=root/'trees.json';ip.write_text(json.dumps(self.inputs));tp.write_text(json.dumps(self.trees))
            out=root/'out';out.mkdir();ops=auto.Operations('fixture',out,ip,tp,root/'samples.json')
            class Receipts:state=None
            ops.use_baseline(Receipts())
            self.assertEqual(json.loads(ip.read_text()),self.inputs)
            self.assertEqual(json.loads(ops.inputs.read_text()),self.inputs)


class AutomaticAdoptionTests(OrchestrationTests):
    def test_verified_handoff_auto_adopts_and_next_run_uses_complete(self):
        overlay=b.empty();self.ops.advance_baseline.return_value=overlay
        result=auto.run(action='auto',receipts=auto.Receipts(self.store),ops=self.ops,provenance={},adopt_baseline=True)
        self.assertEqual(result['status'],'complete');self.assertTrue(result['runtime_baseline_adopted'])
        state=auto.Receipts(self.store).state;self.assertEqual(state['stage'],'complete')
        self.assertEqual(auto.Receipts(self.store).read(state['baseline_overlay']),overlay)
        self.assertEqual(self.ops.baseline.call_count,2)
    def test_equal_tree_automatic_adoption_uses_verified_receipt(self):
        value={'status':'no-change','plan':self.final,'candidate':self.value['candidate']}
        self.ops.build.return_value=value
        self.ops.advance_equal_baseline.return_value=b.empty()
        result=auto.run(action='auto',receipts=auto.Receipts(self.store),ops=self.ops,provenance={},adopt_baseline=True)
        self.assertEqual(result['status'],'complete')
        self.ops.advance_equal_baseline.assert_called_once()
        self.ops.publish.assert_not_called();self.ops.deliver.assert_not_called()
        self.assertEqual(auto.Receipts(self.store).state['stage'],'complete')

    def test_baseline_head_lost_response_does_not_repeat_publication(self):
        self.ops.advance_baseline.return_value=b.empty();self.store.lose=auto.HEAD
        result=auto.run(action='auto',receipts=auto.Receipts(self.store),ops=self.ops,provenance={},adopt_baseline=True)
        self.assertEqual(result['status'],'complete');self.ops.publish.assert_called_once()
        self.assertEqual(auto.Receipts(self.store).state['stage'],'complete')

    def test_deferred_handoff_revalidated_before_adoption(self):
        self.run_auto();self.ops.baseline.return_value={'different':True}
        with self.assertRaisesRegex(ValueError,'acceptance changed'):
            auto.run(action='auto',receipts=auto.Receipts(self.store),ops=self.ops,provenance={},adopt_baseline=True)
        self.ops.advance_baseline.assert_not_called()
        self.assertEqual(auto.Receipts(self.store).state['stage'],'baseline-deferred')
