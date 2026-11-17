import copy
import unittest
from unittest.mock import Mock
from scripts import search_source_watch as watch, search_source_watch_dispatch as dispatch

class SourceWatchTests(unittest.TestCase):
 def setUp(self):
  self.base={'generation':'a'*64,'source_revisions':dict.fromkeys(watch.fetch.REPOS,'b'*40)}
 def test_no_change_does_not_dispatch(self):
  r=watch.inspect(observe=lambda:copy.deepcopy(self.base),revision=lambda *_:'b'*40,token='x')
  self.assertFalse(r['update_needed']);self.assertEqual(r['remote_writes'],0)
 def test_changed_source_triggers(self):
  repo=watch.fetch.REPOS[0]
  r=watch.inspect(observe=lambda:copy.deepcopy(self.base),revision=lambda name,_:'c'*40 if name==repo else 'b'*40,token='x')
  self.assertEqual(r['changed'],[repo]);self.assertTrue(r['update_needed'])
 def test_changing_pointer_fails_closed(self):
  other=copy.deepcopy(self.base);other['generation']='c'*64
  with self.assertRaisesRegex(ValueError,'changed during'):
   watch.inspect(observe=Mock(side_effect=[self.base,other]),revision=lambda *_:'b'*40,token='x')
 def test_active_sync_coalesced(self):
  api=Mock();api.runs.return_value=[{'id':1}]
  self.assertEqual(dispatch.dispatch(api,'a'*40)['status'],'coalesced');api.dispatch.assert_not_called()
 def test_idle_sync_dispatched_once(self):
  api=Mock();api.runs.return_value=[]
  self.assertEqual(dispatch.dispatch(api,'a'*40)['status'],'dispatched');api.dispatch.assert_called_once()
 def test_unknown_dispatch_not_retried(self):
  api=Mock();api.runs.return_value=[];api.dispatch.side_effect=TimeoutError()
  with self.assertRaises(TimeoutError):dispatch.dispatch(api,'a'*40)
  api.dispatch.assert_called_once()
