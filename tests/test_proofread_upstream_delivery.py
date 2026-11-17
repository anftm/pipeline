import copy
import unittest
from unittest.mock import Mock
from scripts import proofread_upstream_delivery as subject
from tests.test_proofread_delivery import Store

class UpstreamDeliveryTests(unittest.TestCase):
    def setUp(self):self.state={'version':1,'baseline':[],'published':[],'claimed':{'repo#1':{'upstream_number':2}}}
    def test_inspect_never_mutates(self):
        store=Store();execute=Mock();r=subject.run(store,self.state,run_id='1',execute=execute)
        self.assertEqual(r['remote_writes'],0);self.assertEqual(store.writes,0);execute.assert_not_called()
    def test_state_persisted_without_git_commit(self):
        store=Store()
        def execute(record,state,save):
            value=copy.deepcopy(state);value['published']=['repo#1'];save(value)
        subject.run(store,self.state,run_id='1',execute=execute,apply=True)
        self.assertEqual(store.read(subject.KEY)[0]['state'],'complete')
        self.assertEqual(store.read(subject.KEY)[0]['publisher_state']['published'],['repo#1'])
    def test_unknown_effect_stops_later_run(self):
        store=Store();send=Mock(side_effect=TimeoutError())
        def execute(record,state,save):record.call(send,'fixture','POST','/repos/a/b/pulls',{})
        with self.assertRaises(TimeoutError):subject.run(store,self.state,run_id='1',execute=execute,apply=True)
        with self.assertRaisesRegex(RuntimeError,'no replay'):subject.run(store,self.state,run_id='2',execute=execute,apply=True)
        send.assert_called_once()
