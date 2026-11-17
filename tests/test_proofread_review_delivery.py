import unittest
from unittest.mock import Mock, patch
from scripts import proofread_review_delivery as subject, proofread_delivery as delivery
from tests.test_proofread_delivery import Store

class ReviewDeliveryTests(unittest.TestCase):
    def test_review_routes_imported_helper_and_direct_writes_through_receipts(self):
        record=delivery.Delivery(Store(),{'request_id':'review:1'},{});record.claim()
        send=Mock(return_value=(200,{}))
        def action():
            subject.submit.api_request('fixture','PUT','/repos/a/b/pulls/1/merge',{'sha':'a'*40})
            subject.review.api_request('fixture','PATCH','/repos/a/b/issues/1',{'state':'closed'})
            subject.review.delete_pull_branch('fixture','b',{})
        with patch.object(subject.submit,'api_request',send),patch.object(subject.review,'api_request',send):
            subject.execute(record,'review',action)
        self.assertEqual(send.call_count,2)
        self.assertEqual(len(record.state['effects']),2)
        self.assertFalse(record.review_merge)
    def test_revert_cannot_merge(self):
        record=delivery.Delivery(Store(),{'request_id':'revert:1'},{});record.claim()
        send=Mock()
        with patch.object(subject.revert,'api_request',send):
            with self.assertRaisesRegex(ValueError,'cannot merge'):
                subject.execute(record,'revert',lambda:subject.revert.api_request('fixture','PUT','/repos/a/b/pulls/1/merge',{'sha':'a'*40}))
        send.assert_not_called()
    def test_review_merge_without_head_rejected(self):
        record=delivery.Delivery(Store(),{'request_id':'review:2'},{});record.claim();record.review_merge=True
        with self.assertRaisesRegex(ValueError,'exact head'):
            record.call(Mock(),'fixture','PUT','/repos/a/b/pulls/1/merge',{})
    def test_helpers_restored_after_unknown_write(self):
        record=delivery.Delivery(Store(),{'request_id':'review:3'},{});record.claim()
        original=subject.submit.api_request
        send=Mock(side_effect=TimeoutError())
        with patch.object(subject.review,'api_request',send):
            with self.assertRaises(TimeoutError):
                subject.execute(record,'review',lambda:subject.review.api_request('fixture','POST','/repos/a/b/issues',{}))
        self.assertIs(subject.submit.api_request,original)
        self.assertEqual(record.state['effects'][0]['state'],'intent')
