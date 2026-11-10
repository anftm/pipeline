"""Canonical CAS is bounded to a single durable claim and exact readback."""
import copy
import unittest
from unittest.mock import Mock

from scripts import reader_epub_canonical_delivery as subject,reader_epub_canonical_transaction as tx
from tests.test_reader_epub_canonical_preview import CanonicalPreviewTests
from tests.test_reader_epub_stage_journal import MemoryJournal


class CanonicalDeliveryTests(CanonicalPreviewTests):
    def setUp(self):
        super().setUp();self.target=MemoryJournal();self.validator.after_canonical=Mock()
    def prepare(self):
        return subject.prepare(self.target,self.store,self.index_store,self.plan['operation'],checkpoints=self.checkpoints,
            protections=self.protections,objects=self.objects,revalidate=self.validator)
    def apply(self,current):
        return subject.apply_once(self.target,self.store,self.index_store,current,checkpoints=self.checkpoints,
            protections=self.protections,objects=self.objects,revalidate=self.validator)
    def recover(self):
        return subject.reconcile(self.target,self.store,self.index_store,self.plan['operation'],checkpoints=self.checkpoints,
            protections=self.protections,objects=self.objects,revalidate=self.validator)
    def test_one_canonical_write_and_exact_observation(self):
        before=self.checkpoints.puts;current=self.prepare();done=self.apply(current)
        self.assertEqual(done['record']['state'],'observed');self.assertEqual(self.checkpoints.puts,before+1)
        self.assertEqual(self.checkpoints.read(None,tx.CANONICAL).value,done['intent']['candidate'])
        self.assertEqual(done['intent']['previous_rows'][self.old['key']],self.old)
        self.validator.after_canonical.assert_called_once()
        with self.assertRaisesRegex(ValueError,'not prepared'):self.apply(done)
    def test_lost_response_reads_candidate_without_replay(self):
        current=self.prepare();self.checkpoints.lose=True;before=self.checkpoints.puts
        with self.assertRaises(TimeoutError):self.apply(current)
        self.assertEqual(tx.load(self.target,self.plan['operation'])['record']['state'],'write-intent')
        done=self.recover();self.assertEqual(done['record']['state'],'observed');self.assertEqual(self.checkpoints.puts,before+1)
    def test_unknown_before_write_remains_unresolved(self):
        current=self.prepare();self.checkpoints.fail=True;before=self.checkpoints.puts
        with self.assertRaises(TimeoutError):self.apply(current)
        with self.assertRaisesRegex(ValueError,'recovery remains unresolved'):self.recover()
        pending=tx.load(self.target,self.plan['operation'])
        with self.assertRaisesRegex(ValueError,'not prepared'):self.apply(pending)
        self.assertEqual(self.checkpoints.puts,before+1)
    def test_parent_and_shard_drift_fail_before_claim(self):
        current=self.prepare();before=self.checkpoints.puts
        self.checkpoints.values[tx.CANONICAL]=(self.canonical,'"foreign"')
        with self.assertRaisesRegex(ValueError,'exact parent or validator changed'):self.apply(current)
        self.checkpoints.values[tx.CANONICAL]=(self.canonical,'"canonical"')
        shard=self.checkpoints.values[self.path][0];self.checkpoints.values[self.path]=(shard,'"foreign"')
        with self.assertRaisesRegex(ValueError,'shard bytes or validator changed'):self.apply(current)
        self.assertEqual(self.checkpoints.puts,before)
        self.assertEqual(tx.load(self.target,self.plan['operation'])['record']['state'],'prepared')
    def test_after_write_revalidator_must_exist_before_claim(self):
        current=self.prepare();self.validator.after_canonical=None;before=self.checkpoints.puts
        with self.assertRaisesRegex(ValueError,'required before write'):self.apply(current)
        self.assertEqual(self.checkpoints.puts,before)
    def test_live_canonical_verifier_checks_old_route_and_current_source(self):
        from scripts import reader_epub_revalidate as live,reader_format_backlog as backlog,search_snapshot
        from unittest.mock import patch
        import hashlib
        current=self.prepare();shard=self.checkpoints.values[self.path][0];candidate=current['intent']['candidate']
        source=self.plan['sources'][0];action=self.preview['actions'][0];revisions={source['repo']:source['source_revision']}
        plan=copy.deepcopy(self.plan);preview=copy.deepcopy(self.preview)
        plan['source_revisions_sha256']=hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest()
        plan['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in plan.items() if k!='plan_sha256'})).hexdigest()
        preview['plan_sha256']=plan['plan_sha256'];preview['staging_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in preview.items() if k!='staging_sha256'})).hexdigest()
        raw=backlog.search_publication_pointer.make_pointer(plan['search_generation'],None,'1'*32)
        api=Mock();api.repo_info.return_value=Mock(sha=source['source_revision'])
        hub=Mock();hub.read_bytes.side_effect=lambda _b,p:raw if p==backlog.CURRENT else b'manifest'
        validator=live.LivePlanRevalidator(hub=hub,api=api,fs=Mock(),client=Mock())
        route={'s':2,'m':'e','b':backlog.READER_BUCKET,'p':source['previous_manifest']}
        metadata={(source['repo'],source['source_revision'],source['path']):{'path':source['path'],'lfs':{'sha256':action['source_sha256']}}}
        with patch.object(backlog,'capture_search',return_value=(raw,b'manifest',{},revisions,{})), \
                patch.object(backlog.publication,'current',return_value=({'generation':plan['reader_generation']},{'base_sidecar':{'sha256':'x'}})), \
                patch.object(backlog.publication,'pinned_base',return_value={'f':{source['key']:route}}), \
                patch.object(backlog.publication,'project_sidecar',return_value={'f':{source['key']:route}}), \
                patch.object(backlog.publication,'observe_consumer',return_value={'generation':plan['reader_generation'],'projection_verified':True}) as observe, \
                patch.object(backlog.matrix,'read_json',side_effect=lambda _f,_b,path:candidate if path.endswith('/index.json') else shard), \
                patch.object(backlog,'_metadata',return_value=metadata):
            self.assertIsNone(validator.after_canonical(plan,preview,candidate,shard))
            self.assertEqual(observe.call_count,2)
            with patch.object(backlog.publication,'project_sidecar',return_value={'f':{}}):
                with self.assertRaisesRegex(ValueError,'old Reader route changed'):
                    validator.after_canonical(plan,preview,candidate,shard)

    def test_missing_protection_during_recovery_keeps_claim_pending(self):
        current=self.prepare();self.checkpoints.lose=True
        with self.assertRaises(TimeoutError):self.apply(current)
        self.protections.values.pop(self.preview['actions'][0]['protection_path'])
        with self.assertRaisesRegex(ValueError,'protection changed'):self.recover()
        self.assertEqual(tx.load(self.target,self.plan['operation'])['record']['state'],'write-intent')


if __name__=='__main__':unittest.main()
