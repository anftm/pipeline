import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from scripts import bha_parsed_baseline as baseline, build_fork_parsed as builder


class BaselineTests(unittest.TestCase):
    def setUp(self):
        branches={k:'a'*40 for k in (*builder.INPUT_BRANCHES,'parsed')}
        snapshot={'archive':0,'mirror_repository_id':1,'upstream_repository_id':2,
                  'mirror_branches':branches,'upstream_branches':dict(branches),
                  'helper_revision':'b'*40,'parsed_tree':'c'*40}
        built=builder.source_snapshot(branches,'b'*40)
        self.inputs={'version':1,'archives':{str(i):dict(built) for i in range(32)}}
        self.trees={str(i):'c'*40 for i in range(32)}
        self.publication={'kind':'bha-parsed-publication-result','status':'published','archive':0,
            'commit':'d'*40,'tree':'e'*40,'previous_tree':'c'*40,'source_snapshot':snapshot,
            'previous_inputs':dict(built),'built_inputs':dict(built),'baseline_updated':False,'consumer_verified':False}
        expected=copy.deepcopy(snapshot);expected['mirror_branches']['parsed']='d'*40;expected['parsed_tree']='e'*40
        self.inspect=Mock(return_value=expected)
        record={'state':'verified','archive_commits':{str(i):'d'*40 for i in range(32)},
                'archive_revisions':{str(i):'e'*40 for i in range(32)},'operation':'f'*32,'job_id':'a'*32,
                'result':{'snapshot_sidecars_verified':True,'consumer_revisions_verified':True,'snapshot_generation':'snapshot'}}
        self.current=SimpleNamespace(record=record,head_raw=b'head',etag='"a"')
        self.observe=Mock(return_value={'index_status':'ready','switch_pending':False,
            'archive_revisions':record['archive_revisions'],'job':{'job_id':'a'*32,'operation_id':'f'*32,'state':'succeeded'}})

    def prepare(self):
        with patch.object(baseline.journal,'load',return_value=self.current),patch.object(baseline.journal,'audit'):
            return baseline.prepare(self.publication,self.inputs,self.trees,Mock(),inspect_source=self.inspect,observe_consumer=self.observe)

    def test_exact_index_acceptance_updates_only_selected_archive_locally(self):
        old=copy.deepcopy(self.inputs);result=self.prepare()
        self.assertEqual(self.inputs,old);self.assertEqual(self.trees['0'],'c'*40)
        self.assertEqual(result['trees']['0'],'e'*40);self.assertEqual(result['trees']['1'],'c'*40)
        self.assertEqual(result['remote_writes'],0);self.assertFalse(result['baseline_published'])

    def test_unverified_or_mismatched_index_never_updates_baseline(self):
        for field,value in [('state','accepted'),('archive_revisions',{'0':'c'*40})]:
            prior=self.current.record[field];self.current.record[field]=value
            with self.assertRaisesRegex(ValueError,'acceptance'):self.prepare()
            self.current.record[field]=prior
        self.current.record['result']['snapshot_sidecars_verified']=False
        with self.assertRaises(ValueError):self.prepare()

    def test_source_or_baseline_drift_rejected(self):
        self.inspect.return_value={}
        with self.assertRaisesRegex(ValueError,'source changed'):self.prepare()
        self.trees['0']='f'*40
        with self.assertRaisesRegex(ValueError,'baseline changed'):self.prepare()

    def test_journal_drift_during_observation_rejected(self):
        later=SimpleNamespace(record=self.current.record,head_raw=b'other',etag='"b"')
        with patch.object(baseline.journal,'load',side_effect=[self.current,later]),patch.object(baseline.journal,'audit'):
            with self.assertRaisesRegex(ValueError,'during baseline'):
                baseline.prepare(self.publication,self.inputs,self.trees,Mock(),inspect_source=self.inspect,observe_consumer=self.observe)
