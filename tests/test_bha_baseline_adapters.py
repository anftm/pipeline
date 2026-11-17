import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import bha_upstream_auto as auto, bha_runtime_baseline as overlays
from tests.test_bha_upstream_auto import fixtures


class BaselineAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        _,_,_,self.plan,_=fixtures()
        self.inputs={'version':1,'archives':{str(i):self.plan['recorded_inputs'] for i in range(32)}}
        self.trees={str(i):self.plan['recorded_parsed_tree'] for i in range(32)}
        self.ip=self.root/'inputs.json';self.tp=self.root/'trees.json';self.sp=self.root/'samples.json'
        self.ip.write_text(json.dumps(self.inputs));self.tp.write_text(json.dumps(self.trees));self.sp.write_text(json.dumps({'samples':[{'archive':0,'path':'x/y/z/a.json'}]}))
        out=self.root/'out';out.mkdir();self.ops=auto.Operations('fixture',out,self.ip,self.tp,self.sp);self.ops.overlay=overlays.empty()
        self.value={'status':'no-change','build_requested':True,'plan':self.plan,'candidate':{
            'changed':False,'before_tree':self.trees['0'],'tree':self.trees['0'],
            'commit':self.plan['snapshot']['mirror_branches']['parsed']}}
        self.handoff={'built_inputs':self.plan['current_inputs'],'tree':self.trees['0']}
        self.observed={'changed_archives':[],'archive_revisions':self.trees,'consumer_fingerprint':'a'*64,'document_count':42}
        self.snapshot={'sidecars_verified':True,'manifest_verified':True,'archive_revisions':self.trees,
            'build_fingerprint':'a'*64,'document_count':42,'generation':'bha-test','manifest_sha256':'b'*64}

    def invoke(self):
        with patch.object(self.ops,'plan',return_value=self.plan), \
             patch.object(auto.delivery,'inspect_plan_with_samples',return_value=self.observed), \
             patch('scripts.bha_snapshot_inspection.inspect',return_value=self.snapshot) as snapshot:
            result=self.ops.advance_equal_baseline(self.handoff,self.value)
            snapshot.assert_called_once_with(self.trees,full_sidecars=True)
            return result

    def test_equal_tree_verified_snapshot_adopts_only_inputs(self):
        result=self.invoke();new_inputs,new_trees=overlays.apply(self.inputs,self.trees,result)
        self.assertEqual(new_trees,self.trees)
        self.assertEqual(new_inputs['archives']['0'],self.plan['current_inputs'])
        self.assertEqual(json.loads(self.ip.read_text()),self.inputs)

    def test_equal_tree_rejects_stale_snapshot_identity_and_partial_hash(self):
        for key,value in [('sidecars_verified',False),('document_count',41),('build_fingerprint','c'*64),('archive_revisions',{})]:
            prior=copy.deepcopy(self.snapshot);self.snapshot[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.invoke()
            self.snapshot=prior

    def test_equal_tree_requires_exact_completed_build(self):
        self.value['candidate']['changed']=True
        with self.assertRaisesRegex(ValueError,'completed build'):self.invoke()
