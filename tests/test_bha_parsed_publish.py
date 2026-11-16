import copy
from pathlib import Path
import unittest
from unittest.mock import Mock
from scripts import bha_parsed_publish as publisher


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.plan={'archive':0,'plan_sha256':'a'*64,'current_inputs':{'helper':'b'*40}}
        self.value={'plan':self.plan,'candidate':{'commit':'c'*40,'tree':'d'*40}}
        self.inspect=Mock(side_effect=lambda:copy.deepcopy(self.plan))
        self.restore=Mock();self.checkpoint=Mock()
        def publish(root,candidate,record):
            record({'status':'push-intent'});return True
        self.publish=Mock(side_effect=publish)

    def run_publish(self):
        return publisher.publish_reviewed(self.value,inspect_live=self.inspect,restore=self.restore,
            publish=self.publish,workspace=Path('/not-created'),checkpoint=self.checkpoint)

    def test_exact_reviewed_candidate_published_without_claiming_consumer_or_baseline(self):
        result=self.run_publish()
        self.assertEqual(result['commit'],'c'*40);self.assertFalse(result['consumer_verified'])
        self.assertFalse(result['baseline_updated']);self.restore.assert_called_once();self.publish.assert_called_once()
        self.assertEqual(self.inspect.call_count,3)

    def test_moved_source_before_or_after_restore_blocks_publish(self):
        for values in [[{'changed':True}],[self.plan,{'changed':True}]]:
            self.inspect.side_effect=values
            with self.assertRaisesRegex(ValueError,'identity changed'):self.run_publish()
            self.publish.assert_not_called()

    def test_checkpoint_drift_prevents_underlying_push(self):
        self.inspect.side_effect=[self.plan,self.plan,{'changed':True}]
        pushed=[]
        def publish(root,candidate,record):
            record({'status':'push-intent'});pushed.append(True);return True
        self.publish.side_effect=publish
        with self.assertRaisesRegex(ValueError,'checkpoint'):self.run_publish()
        self.assertEqual(pushed,[])

    def test_unknown_publish_error_is_never_retried(self):
        self.publish.side_effect=RuntimeError('unknown')
        with self.assertRaisesRegex(RuntimeError,'unknown'):self.run_publish()
        self.publish.assert_called_once()
