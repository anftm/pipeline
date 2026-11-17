import copy
import unittest
from unittest.mock import Mock,patch
from scripts import proofread_delivery as d

class Store:
    def __init__(self):self.values={};self.writes=0;self.lose=False
    def read(self,key):
        row=self.values.get(key);return (copy.deepcopy(row),str(self.writes)) if row else (None,None)
    def put(self,key,value,etag):
        if self.read(key)[1]!=etag:raise RuntimeError('CAS conflict')
        self.writes+=1;self.values[key]=copy.deepcopy(value)
        if self.lose:raise TimeoutError('lost response')

class DeliveryTests(unittest.TestCase):
    def test_claim_precedes_effect_and_result_precedes_complete(self):
        s=Store();request={'request_id':'one','archive_id':0,'kind':'proofread','auto_merge':True}
        def execute(delivery,payload):
            self.assertFalse(payload['auto_merge'])
            def send(token,method,path,body):
                state=s.read(delivery.key)[0]
                self.assertEqual(state['state'],'claimed');self.assertEqual(state['effects'][-1]['state'],'intent')
                self.assertEqual(body['author'],d.IDENTITY)
                return 201,{'sha':'a'*40}
            delivery.call(send,'fixture','PUT','/repos/anftm/test/contents/a',{'content':'fixture'})
        result=d.submit(s,request,run={'id':'1'},execute=execute)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(s.read(result['receipt'])[0]['effects'][0]['state'],'responded')
        again=Mock();result=d.submit(s,request,run={'id':'2'},execute=again)
        self.assertEqual(result['status'],'already-complete');again.assert_not_called()
    def test_unknown_effect_blocks_replay(self):
        s=Store();request={'request_id':'x'};send=Mock(side_effect=TimeoutError('lost'))
        def execute(delivery,payload):delivery.call(send,'fixture','POST','/repos/anftm/test/pulls',{})
        with self.assertRaises(TimeoutError):d.submit(s,request,run={},execute=execute)
        with self.assertRaisesRegex(RuntimeError,'explicit recovery'):d.submit(s,request,run={},execute=execute)
        send.assert_called_once()
    def test_caught_unknown_effect_cannot_continue_other_writes(self):
        delivery=d.Delivery(Store(),{'request_id':'x'},{});delivery.claim()
        send=Mock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):delivery.call(send,'fixture','POST','/repos/a/b/pulls',{})
        with self.assertRaisesRegex(RuntimeError,'unresolved effect'):
            delivery.call(send,'fixture','POST','/repos/a/b/issues',{})
        send.assert_called_once()
    def test_unknown_claim_never_sends_effect(self):
        s=Store();s.lose=True;execute=Mock()
        with self.assertRaises(TimeoutError):d.submit(s,{'request_id':'x'},run={},execute=execute)
        execute.assert_not_called()
    def test_receipt_lost_response_poison_cannot_be_swallowed_and_completed(self):
        s=Store();delivery=d.Delivery(s,{'request_id':'x'},{});delivery.claim()
        def send(*args):s.lose=True;return 201,{'number':1}
        with self.assertRaises(TimeoutError):delivery.call(send,'fixture','POST','/repos/a/b/pulls',{})
        s.lose=False
        with self.assertRaisesRegex(RuntimeError,'unknown'):delivery.save()
        self.assertNotEqual(s.read(delivery.key)[0]['state'],'complete')

    def test_request_identity_reuse_rejected(self):
        s=Store();d.submit(s,{'request_id':'x','content':'one'},run={},execute=lambda *_:None)
        with self.assertRaisesRegex(ValueError,'reused'):d.submit(s,{'request_id':'x','content':'two'},run={},execute=lambda *_:None)
    def test_no_merge_delete_or_unknown_method(self):
        delivery=d.Delivery(Store(),{'request_id':'x'},{});delivery.claim();send=Mock()
        for method,path in [('DELETE','/repos/a/b/git/refs'),('PUT','/repos/a/b/pulls/1/merge'),('TRACE','/repos/a/b')]:
            with self.assertRaises(ValueError):delivery.call(send,'fixture',method,path,{})
        send.assert_not_called()
    def test_legacy_entry_stays_paused(self):
        with self.assertRaisesRegex(RuntimeError,'paused'):d.legacy.main()
