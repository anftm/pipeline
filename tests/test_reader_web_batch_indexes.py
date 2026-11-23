from contextlib import ExitStack
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from scripts import reader_web_batch_indexes as batch, reader_web_batch_plan as plan
from scripts import reader_web_index_guard as guard, reader_web_batch_publish as publish
from scripts.reader_stream_checkpoint_cas import ConditionalCheckpointStore
from tests.test_reader_web_index_guard import MemoryS3, BUCKET, encode
from unittest.mock import Mock, patch


def rows():
    parents, pairs = {}, []
    for ext,count in plan.QUOTAS.items():
        files=[]
        for i in range(count):
            path=f'{i}.{ext}'; root=f'documents/web/{ext}/'+str(i)*64
            o=dict(repo='example/books',path=path,key='example/books\0'+path,extension=ext,source_revision='b'*40,
                   source_sha256='c'*64,mode='document-stream',bucket=BUCKET,object=root+'/document.html',
                   manifest=root+'/document-manifest.json',bytes=10,sha256='d'*64,checkpointed_at='unchanged',
                   processing_roots=['reader-index/processing/stream-upload-'+'1'*32+'.json'])
            candidate_root=f'documents/web/{ext}/'+plan.digest(('c'*64+'\0'+'e'*64).encode())
            n={**o,'object':candidate_root+'/document.html','manifest':candidate_root+'/document-manifest.json','bytes':12,'sha256':'e'*64}
            for key in ('checkpointed_at','processing_roots'):n.pop(key)
            files.append(o);pairs.append((o,n))
        parents[ext]=encode({'version':1,'kind':'web-document-stream-index','files':files+[{'key':'unrelated'}],
                             'failures':[{'key':'failed','error':'existing'}]})
    return parents,pairs


class BatchIndexesTest(unittest.TestCase):
    def setUp(self):
        self.parents,self.pairs=rows(); self.targets=plan.canonical(self.parents,self.pairs,'f'*32)
        self.etags={e:f'"{e}"' for e in self.parents};self.events=[];self.remote=MemoryS3(self.events)
        self.paths={e:f'documents/web/{e}/index.json' for e in self.parents}
        self.remote.objects={self.paths[e]:(raw,self.etags[e]) for e,raw in self.parents.items()}
        self.cps=ConditionalCheckpointStore(SimpleNamespace(_location=lambda _ : ('vomebook','reader-assets-v2'),
                         _client=lambda _:self.remote,read_bytes=self.read_bytes,read_etags={}))
        self.hub=SimpleNamespace(read_bytes=lambda _,p:self.remote.objects[p][0]);self.j=guard.S3Journal(self.remote)
        orig=self.remote.put_object
        def put(**kw):
            with patch('tests.test_reader_web_index_guard.ROOT',next((p for p in self.paths.values() if p==kw['Key']),'not-root')):
                return orig(**kw)
        self.remote.put_object=put

    def read_bytes(self,bucket,path):
        response=self.remote.get_object(Bucket='reader-assets-v2',Key=path)
        self.cps.store.read_etags[bucket,path]=response['ETag']
        return response['Body'].read()

    def execute(self,**kw):
        return batch.canonical_set(**(dict(checkpoints=self.cps,journal=self.j,hub=self.hub,parents=self.parents,pairs=self.pairs,
                    operation='f'*32,etags=self.etags,prerequisites={'review':'b'*64},guard=lambda:True)|kw))

    def put_paths(self):return [x[1] for x in self.events if x[0]=='put']

    def test_exact_three_roots_once_and_observe(self):
        self.assertEqual(self.execute(),dict.fromkeys(self.paths,'written'))
        self.assertEqual([x for x in self.put_paths() if x in self.paths.values()], [self.paths[e] for e in sorted(self.paths)])
        self.assertEqual(self.execute(recover=True),dict.fromkeys(self.paths,'observed'))
        self.assertEqual(len(self.put_paths()),6)
        with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(len(self.put_paths()),6)

    def test_all_etags_preflight_before_first_claim(self):
        self.remote.objects[self.paths['mht']]=(self.parents['mht'],'"foreign"')
        with self.assertRaisesRegex(RuntimeError,'mht'):self.execute()
        self.assertEqual(self.put_paths(),[])

    def test_only_unspent_tail_can_continue(self):
        def stopped(**opts):
            if opts['path']==self.paths['html']:raise TimeoutError('before any permit for html')
            return real_apply(**opts)
        original=guard.apply
        def real_apply(**opts):return original(self.cps,self.j,**opts)
        with patch.object(guard,'apply',side_effect=lambda *a,**kw:stopped(**kw)):
            with self.assertRaises(TimeoutError):self.execute()
        self.assertEqual(self.remote.objects[self.paths['htm']][0],self.targets['htm'])
        self.assertEqual(self.execute(recover=True),{'htm':'observed','html':'written','mht':'written'})
        self.assertEqual(len(self.put_paths()),6)

    def test_spent_but_failed_index_blocks_other_pending_writes(self):
        self.remote.faults[self.paths['html']]='before'
        with self.assertRaisesRegex(RuntimeError,'unconfirmed'):self.execute()
        first=self.put_paths()
        with self.assertRaisesRegex(RuntimeError,'spent-but-unconfirmed'):self.execute(recover=True)
        self.assertEqual(self.put_paths(),first)
        self.assertNotIn(self.paths['mht'],first)

    def test_managed_payload_creates_use_scoped_guard_and_stop_on_failed_full_exit(self):
        from scripts import reader_web_managed_review, reader_web_managed_pages

        events, objects, data, reviewed = [], {}, {}, []
        cat_sha = 'a'*64
        before, after, ptr = b'before', b'after', b'parent'
        data.update({'sidecar-before.gz':before, 'sidecar-after.gz':after, 'pointer.json':ptr,
                     'retained.json':b'retention'})
        objects.update({'retention':b'retention', 'snapshot':after, publish.legacy.SIDECAR:before})
        missing = []
        for i in range(197):
            ident = str(i)
            root = 'documents/web/html/'+plan.digest(ident.encode())
            old = {'object':f'old/{i}/document.html','manifest':f'old/{i}/document-manifest.json',
                   'bytes':3,'sha256':plan.digest(b'old')}
            new = {'object':root+'/document.html','manifest':root+'/document-manifest.json'}
            reviewed.append({'id':ident,'old':old,'new':new,'old_manifest_sha256':plan.digest(b'old')})
            objects.update((name,b'old') for name in (old['object'], old['manifest']))
            for name in ('document.html','document-manifest.json'):
                raw = f'{name}-{i}'.encode()
                data[ident+'/'+name] = raw
                key = root+'/'+name
                if i<195 or (i==195 and name=='document.html'):
                    objects[key]=raw
                else:
                    missing.append(key)
        def read(layer, key):
            if key in missing:
                events.append((layer,key))
            if key not in objects:
                raise FileNotFoundError(key)
            return objects[key]
        def create(key, raw, mime):
            self.assertNotIn(key,objects)
            events.append(('create',key))
            objects[key]=raw
        def full():
            events.append(('full',None))
            return not all(key in objects for key in missing)
        def payload():
            events.append(('payload',None))
            return True
        review={'parent_generation':'parent','parent_pointer':{},'target_pointer':{},'unchanged_indexes':{},
                'legacy':{'target_sha256':'b'*64,'target_bytes':10,'parent_sha256':cat_sha,'parent_bytes':3,
                          'snapshot':{'path':'snapshot'}},'retention':{'path':'retention','sha256':'pin'},
                'canonical':{},'rows':reviewed,'timestamp':'2026-11-23T00:00:00Z'}
        proof={'version':1,'remote_writes':0,'parent_generation':'parent','pointer_sha256':plan.digest(ptr),
               'bundle_sha256':'bundle','review_sha256':'review','pages':{},
               'catalog':{'sha256':cat_sha,'size':3,'etag':'cat'},
               'sidecar':{'sha256':plan.digest(before),'size':len(before),'etag':'side'}}
        s3=Mock();s3.read_bytes.side_effect=lambda _,key:read('s3',key)
        cp=Mock(store=s3);hub=Mock();hub.read_bytes.side_effect=lambda _,key:read('resolver',key)
        roots=Mock();roots.observe.return_value=SimpleNamespace(sha256=cat_sha,size=3,etag='cat')
        journal=Mock();journal.read.return_value=None
        claims=Mock();claims._get.return_value=(None,None)
        pointers=Mock();pointers.read_for_update.return_value=(before,'side')
        mocks={'CentralHubStore':hub,'ConditionalCheckpointStore':cp,'LegacyRoots':roots,'LegacyJournal':journal,
               'S3ClaimStore':claims,'ConditionalPageStore':pointers,'CatalogParent':Mock(),
               'immutable_creator':create}
        with ExitStack() as stack:
            for target,spec in mocks.items():
                owner=publish.v3 if target=='CentralHubStore' else publish
                stack.enter_context(patch.object(owner,target,return_value=spec))
            for owner, name, result in ((reader_web_managed_review,'parse',(review,data,{},[])),
                (reader_web_managed_pages,'current',{}),(publish,'_check_pointer_phase',None),
                (publish.plan,'project',({'generation':'parent'},{},after)),
                (publish.plan,'plan_legacy',review['legacy'])):
                stack.enter_context(patch.object(owner,name,return_value=result))
            mutable=stack.enter_context(patch.object(publish.indexes,'canonical_set'))
            with self.assertRaisesRegex(RuntimeError,'publishing guard is absent'):
                publish.run(b'','bundle','review',proof,phase='apply',external_writers_stopped=False,
                            managed=True,controller_guard=full,immutable_payload_guard=payload)
        expected=[('full',None)]
        for key in missing:
            expected.extend((('s3',key),('payload',None),('create',key),('s3',key),('resolver',key)))
        self.assertEqual(events,expected+[('full',None)])
        mutable.assert_not_called()
        journal.claim.assert_not_called()

    def test_legacy_review_round_trip_preserves_plan_identity(self):
        actual={'retirement_hint':('reader-index/hint.json',{'objects':['one']}),'bytes':42}
        reviewed=json.loads(json.dumps(actual))
        publish.validate_legacy_plan(actual,reviewed)
        with self.assertRaisesRegex(ValueError,'streamed catalog plan differs'):
            publish.validate_legacy_plan(actual,{**reviewed,'bytes':41})

    def test_duplicate_wrong_source_and_wrong_scope_never_spend(self):
        newpairs=deepcopy(self.pairs);newpairs[0][1]['source_revision']='c'*40
        for pairs in (self.pairs[:-1],self.pairs[:-1]+[self.pairs[0]],newpairs):
            with self.assertRaises(ValueError):self.execute(pairs=pairs)
        self.assertEqual(self.put_paths(),[])
