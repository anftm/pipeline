"""One EPUB shard CAS is journaled, exact, and never blindly replayed."""
import hashlib
import json
import unittest
from unittest.mock import Mock,patch

from scripts import reader_epub_index_delivery as subject,reader_epub_index_transaction as tx
from scripts import reader_stream_checkpoint_cas as cas,reader_epub_protection,search_snapshot
from tests.test_reader_epub_index_preview import IndexPreviewTests
from tests.test_reader_epub_stage_journal import MemoryJournal,WHEN


class Checkpoints:
    def __init__(self,canonical,shard,path):
        self.path=path;self.values={subject.CANONICAL:(canonical,'"canonical"'),path:(shard,'"parent"')}
        self.puts=0;self.lose=False;self.fail=False
    def read(self,bucket,path):
        value,etag=self.values[path]
        if value is None:return cas.Snapshot(None,None,None)
        raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
        return cas.Snapshot(value,etag,hashlib.sha256(raw).hexdigest())
    def write_once(self,bucket,path,value,etag,*,operation_id):
        self.puts+=1
        if self.fail:raise TimeoutError('before update')
        if self.values[path][1]!=etag:raise ValueError('CAS conflict')
        self.values[path]=(value,'"new"')
        if self.lose:raise TimeoutError('response lost')
        return 'written'


class IndexDeliveryTests(IndexPreviewTests):
    def setUp(self):
        super().setUp();self.index_store=MemoryJournal()
        self.path=f"chapters/ebook/epub/index-{self.plan['shard']:02d}.json"
        self.checkpoints=Checkpoints(self.canonical,self.shard,self.path)
        self.validator=Mock();self.validator.after_write=Mock()
    def prepare_index(self):
        return subject.prepare(self.index_store,self.store,self.plan['operation'],checkpoints=self.checkpoints,
            protections=self.protections,objects=self.objects,revalidate=self.validator,read_bytes=self.read_bytes,
            checkpointed_at='2026-11-09T01:00:00Z')
    def apply_index(self,current):
        return subject.apply_once(self.index_store,self.store,current,checkpoints=self.checkpoints,
            protections=self.protections,objects=self.objects,revalidate=self.validator,read_bytes=self.read_bytes)
    def recover_index(self):
        return subject.reconcile(self.index_store,self.store,self.plan['operation'],checkpoints=self.checkpoints,
            protections=self.protections,objects=self.objects,revalidate=self.validator)
    def test_exact_shard_cas_records_actual_read_and_retains_parent(self):
        current=self.prepare_index();self.assertEqual(current['record']['state'],'prepared')
        done=self.apply_index(current);self.assertEqual(done['record']['state'],'observed')
        self.assertEqual(self.checkpoints.puts,1)
        self.assertEqual(done['intent']['parent_shard'],self.shard)
        self.assertEqual(done['record']['observed_raw_sha256'],self.checkpoints.read(None,self.path).raw_sha256)
        self.assertEqual(self.checkpoints.values[subject.CANONICAL][0],self.canonical)
    def test_actual_postwrite_revalidator_checks_pinned_source_and_both_consumers(self):
        from scripts import reader_epub_revalidate as live,reader_format_backlog as backlog
        current=self.prepare_index();candidate=current['intent']['candidate'];action=self.preview['actions'][0]
        source=self.plan['sources'][0];search_pointer={'desired':self.plan['search_generation']}
        raw_pointer=backlog.search_publication_pointer.make_pointer(search_pointer['desired'],None,'1'*32)
        revisions={source['repo']:source['source_revision']}
        self.plan['source_revisions_sha256']=hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest()
        # The persisted plan digest cannot be altered in production. This
        # test exercises the read-only adapter with an isolated validated plan.
        self.plan['plan_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in self.plan.items() if k!='plan_sha256'})).hexdigest()
        self.preview['plan_sha256']=self.plan['plan_sha256']
        self.preview['staging_sha256']=hashlib.sha256(search_snapshot.canonical({k:v for k,v in self.preview.items() if k!='staging_sha256'})).hexdigest()
        api=Mock();api.repo_info.return_value=Mock(sha=source['source_revision'])
        hub=Mock();hub.read_bytes.side_effect=lambda bucket,path: raw_pointer if path==backlog.CURRENT else b'manifest'
        live_validator=live.LivePlanRevalidator(hub=hub,api=api,fs=Mock(),client=Mock())
        receipt={'generation':self.plan['reader_generation'],'projection_verified':True}
        metadata={(source['repo'],source['source_revision'],source['path']):
                  {'path':source['path'],'lfs':{'sha256':action['source_sha256']}}}
        route={'s':2,'m':'e','b':'vomebook/reader-assets-v2','p':source['previous_manifest']}
        with patch.object(backlog,'capture_search',return_value=(raw_pointer,b'manifest',{},revisions,{})), \
                patch.object(backlog.publication,'current',return_value=({'generation':self.plan['reader_generation']}, {'base_sidecar':{'sha256':'x'}})), \
                patch.object(backlog.publication,'pinned_base',return_value={'f':{source['key']:route}}), \
                patch.object(backlog.publication,'project_sidecar',return_value={'f':{source['key']:route}}), \
                patch.object(backlog.publication,'observe_consumer',return_value=receipt) as observe, \
                patch.object(backlog.matrix,'read_json',side_effect=lambda _f,_b,path:self.canonical if path.endswith('/index.json') else candidate), \
                patch.object(backlog,'_metadata',return_value=metadata):
            live_validator.after_write(self.plan,self.preview,candidate)
        self.assertEqual(observe.call_count,2)
        self.assertEqual(self.checkpoints.puts,0)

    def test_actual_s3_checkpoint_adapter_lost_response_is_reconciled(self):
        import io
        from scripts.reader_bucket_store import S3BucketStore
        from tests.test_reader_epub_stage_integration import failure
        values={}
        def encode(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
        values[subject.CANONICAL]=encode(self.canonical);values[self.path]=encode(self.shard)
        client=Mock();raw=S3BucketStore.__new__(S3BucketStore)
        raw._location=Mock(return_value=('vomebook','reader-assets-v2'));raw._client=Mock(return_value=client)
        def get(*,Bucket,Key):
            if Key not in values:raise failure('NoSuchKey',404)
            body=values[Key];return {'Body':io.BytesIO(body),'ContentLength':len(body),
                                     'ETag':'"'+hashlib.sha256(body).hexdigest()+'"'}
        calls=[]
        def put(*,Bucket,Key,Body,ContentType,IfMatch=None,IfNoneMatch=None):
            calls.append(Key);before=values.get(Key)
            if IfMatch!='"'+hashlib.sha256(before).hexdigest()+'"':raise failure('PreconditionFailed',412)
            values[Key]=Body
            raise TimeoutError('response lost after commit')
        client.get_object.side_effect=get;client.put_object.side_effect=put
        self.checkpoints=cas.ConditionalCheckpointStore(raw)
        current=self.prepare_index();done=self.apply_index(current)
        self.assertEqual(done['record']['state'],'observed')
        self.assertEqual(calls,[self.path])
        self.assertEqual(self.checkpoints.read('vomebook/reader-assets-v2',self.path).value,done['intent']['candidate'])

    def test_raw_parent_drift_even_with_same_parsed_json_and_etag_is_rejected(self):
        current=self.prepare_index();original=self.checkpoints.read
        def altered(bucket,path):
            snap=original(bucket,path)
            if path==self.path:return cas.Snapshot(snap.value,snap.etag,'f'*64)
            return snap
        with patch.object(self.checkpoints,'read',side_effect=altered):
            with self.assertRaisesRegex(ValueError,'exact bytes changed'):
                self.apply_index(current)
        self.assertEqual(self.checkpoints.puts,0)
        self.assertEqual(tx.load(self.index_store,self.plan['operation'])['record']['state'],'prepared')

    def test_response_lost_reconciles_without_second_shard_write(self):
        current=self.prepare_index();self.checkpoints.lose=True
        with self.assertRaises(TimeoutError):self.apply_index(current)
        self.assertEqual(self.checkpoints.puts,1)
        self.assertEqual(tx.load(self.index_store,self.plan['operation'])['record']['state'],'write-intent')
        done=self.recover_index();self.assertEqual(done['record']['state'],'observed')
        self.assertEqual(self.checkpoints.puts,1)
        with self.assertRaisesRegex(ValueError,'not prepared'):self.apply_index(done)
    def test_unknown_before_write_cannot_be_replayed_or_marked_observed(self):
        current=self.prepare_index();self.checkpoints.fail=True
        with self.assertRaises(TimeoutError):self.apply_index(current)
        with self.assertRaisesRegex(ValueError,'recovery remains unresolved'):self.recover_index()
        self.assertEqual(self.checkpoints.puts,1)
        pending=tx.load(self.index_store,self.plan['operation'])
        with self.assertRaisesRegex(ValueError,'not prepared'):self.apply_index(pending)
        self.assertEqual(pending['record']['state'],'write-intent')
    def test_parent_change_and_unprotected_object_block_before_index_write(self):
        current=self.prepare_index();self.checkpoints.values[self.path]=(self.shard,'"foreign"')
        with self.assertRaisesRegex(ValueError,'validator or exact bytes changed'):self.apply_index(current)
        self.assertEqual(self.checkpoints.puts,0)
        self.checkpoints.values[self.path]=(self.shard,'"parent"')
        action=self.preview['actions'][0];self.protections.values.pop(action['protection_path'])
        with self.assertRaisesRegex(ValueError,'protection is missing'):self.apply_index(current)
        self.assertEqual(self.checkpoints.puts,0)
    def test_changed_canonical_or_object_stops_recovery(self):
        current=self.prepare_index();self.checkpoints.lose=True
        with self.assertRaises(TimeoutError):self.apply_index(current)
        action=self.preview['actions'][0];item=action['files'][0];self.objects.values[item['remote']]=b'foreign'
        with self.assertRaisesRegex(ValueError,'object changed'):self.recover_index()
        self.assertEqual(self.checkpoints.puts,1)


if __name__=='__main__':unittest.main()
