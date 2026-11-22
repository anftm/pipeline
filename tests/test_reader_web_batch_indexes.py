from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from scripts import reader_web_batch_indexes as batch, reader_web_batch_plan as plan
from scripts import reader_web_index_guard as guard, reader_web_batch_publish as publish
from scripts.reader_stream_checkpoint_cas import ConditionalCheckpointStore
from tests.test_reader_web_index_guard import MemoryS3, BUCKET, encode
from unittest.mock import patch


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
