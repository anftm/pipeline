import copy
from pathlib import Path
import unittest
from unittest.mock import Mock
from scripts import bha_parsed_publish as publisher


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.plan={'archive':0,'plan_sha256':'a'*64,'current_inputs':{'helper':'b'*40},
                   'recorded_inputs':{'helper':'a'*40},'recorded_parsed_tree':'e'*40,'snapshot':{}}
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

    def test_reviewed_bundle_and_plan_hashes_are_independent(self):
        import tempfile, hashlib, json
        from scripts import search_snapshot
        with tempfile.TemporaryDirectory(prefix='bha-receipt-contract-') as directory:
            root=Path(directory);bundle=root/'parsed.bundle';bundle.write_bytes(b'opaque-bundle-bytes')
            plan={'kind':'bha-parsed-build-readonly-plan','action':'build-candidate','archive':0,
                  'snapshot':{'mirror_branches':{'parsed':'c'*40},'helper_revision':'b'*40,'parsed_tree':'d'*40}}
            plan['plan_sha256']=hashlib.sha256(search_snapshot.canonical(plan)).hexdigest()
            value={'kind':'bha-parsed-candidate-result','status':'candidate-ready','remote_writes':0,
                   'publication_authorized':False,'plan':plan,'plan_sha256':plan['plan_sha256'],
                   'candidate':{'archive':0,'changed':True,'revisions':plan['snapshot']['mirror_branches'],
                                'helper_revision':'b'*40,'before_tree':'d'*40},
                   'bundle':{'filename':bundle.name,'bytes':bundle.stat().st_size,
                             'sha256':hashlib.sha256(bundle.read_bytes()).hexdigest()}}
            receipt=root/'result.json';raw=json.dumps(value).encode();receipt.write_bytes(raw)
            digest=hashlib.sha256(raw).hexdigest()
            self.assertEqual(publisher.read_candidate(receipt,digest,bundle),value)
            bundle.write_bytes(b'corrupted-bundle-x!!')
            with self.assertRaises(ValueError):publisher.read_candidate(receipt,digest,bundle)
            bundle.write_bytes(b'opaque-bundle-bytes');value['plan']['action']='inspect-drift'
            raw=json.dumps(value).encode();receipt.write_bytes(raw)
            with self.assertRaisesRegex(ValueError,'plan identity'):
                publisher.read_candidate(receipt,hashlib.sha256(raw).hexdigest(),bundle)

    def test_unknown_publish_error_is_never_retried(self):
        self.publish.side_effect=RuntimeError('unknown')
        with self.assertRaisesRegex(RuntimeError,'unknown'):self.run_publish()
        self.publish.assert_called_once()
