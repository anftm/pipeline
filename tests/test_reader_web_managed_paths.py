"""Managed preparation admits bounded cohorts without extending old gates."""
import copy,hashlib,io,json,unittest
from collections import Counter
from threading import Barrier, Lock
from unittest.mock import Mock, patch
from zipfile import ZipFile,ZIP_DEFLATED
from scripts import reader_web_batch_plan as plan,reader_web_managed_review as schema,reader_web_managed_pages as pages
from scripts import reader_web_batch_publish as publish,reader_web_managed_prepare as prepare
import httpx
from scripts import publish_reader_v3 as v3,reader_web_exact_plan as web,reader_gc_graph as gc
from scripts.reader_web_batch_cli import MAX_PUBLIC_WEB_BYTES, live
from tests.test_reader_gc_graph import MemoryStore,ASSETS

S=lambda b:hashlib.sha256(b).hexdigest()


def fixture(n,ext='mhtml',*,sizes=None,old_size=2):
    rows=[]; members={}; kept={'key':'unrelated','source_sha256':'a'*64}
    for j in range(n):
        path=f'entry-{j}.{ext}'; raw=f'<!doctype html><p>new {j}</p>'.encode()
        if sizes is not None:raw=raw.ljust(sizes[j],b' ')
        h=S(raw); src=S(path.encode()); name=f'{j:04d}'
        def root(d):return f'documents/web/{ext}/{d}'
        old_base=root(S(('old-root:'+path).encode())); new_base=root(S(f'{src}\0{h}'.encode()))
        old={'key':'org/books\0'+path,'repo':'org/books','path':path,'extension':ext,'source_revision':'3'*40,
            'source_sha256':src,'mode':'document-stream','bucket':web.BUCKET,'object':old_base+'/document.html',
            'manifest':old_base+'/document-manifest.json','bytes':old_size,'sha256':S(('old:'+path).encode()),'checkpointed_at':'old'}
        new={k:v for k,v in old.items() if k!='checkpointed_at'}|{'object':new_base+'/document.html','manifest':new_base+'/document-manifest.json','bytes':len(raw),'sha256':h}
        doc=v3.encode({'kind':'web-document-stream','version':1,'path':'document.html','source_extension':ext,'source_sha256':src,'bytes':len(raw),'sha256':h})
        members[name+'/document.html']=raw;members[name+'/document-manifest.json']=doc
        rows.append({'id':name,'old':old,'new':new,'old_manifest_sha256':'f'*64,'new_manifest_sha256':S(doc)})
    parent=v3.encode({'version':1,'kind':web.KIND,'files':[r['old'] for r in rows]+[kept],'failures':[{'key':'failure'}]})
    pairs=[(r['old'],r['new']) for r in rows]; op='b'*32; target=plan.canonical({ext:parent},pairs,op,managed=True)[ext]
    pin=v3.encode({'version':1,'kind':'reader-web-retained-objects','resources':[{'bucket':web.BUCKET,'path':path} for path in sorted({r['manifest'] for pair in pairs for r in pair})]}); h=S(pin)
    ptr={'generation':'c'*64}; before=b'fake-side-before';after=b'fake-side-after'
    review={'kind':'reader-web-managed-v1','publication_ready':False,'remote_writes':0,'operation':op,'selected':n,
        'parent_pointer':ptr,'target_pointer':{'generation':'d'*64},'timestamp':'2026-01-01T00:00:00+00:00',
        'canonical':{'path':f'documents/web/{ext}/index.json','before_sha256':S(parent),'after_sha256':S(target)},'unchanged_indexes':{},
        'sidecar':{'before_sha256':S(before),'after_sha256':S(after)},'legacy':{},'pages_parent':{},'rows':rows,
        'producer_bytes':sum(r['new']['bytes'] for r in rows),'retention':{'path':f'reader-index/retained-web-managed-{h}.json','manifest_count':2*n,'sha256':h}}
    members.update({'canonical-before.json':parent,'canonical-after.json':target,'retention.json':pin,'pointer.json':v3.encode(ptr),
                    'sidecar-before.gz':before,'sidecar-after.gz':after})
    return pairs,members,review


def packed(members,review):
    out=io.BytesIO();r=v3.encode(review)
    with ZipFile(out,'w',ZIP_DEFLATED) as z:
        for p,b in {**members,'review.json':r}.items():z.writestr(p,b)
    value=out.getvalue();return value,S(value),S(r)


class ManagedPathsTests(unittest.TestCase):
    def test_bounds_and_all_four_extensions(self):
        for n,ext in [(1,'mhtml'),(16,'htm'),(128,'html'),(200,'mht')]:
            with self.subTest(n=n,ext=ext):
                pairs,m,r=fixture(n,ext);p,s,parents,p2=schema.parse(*packed(m,r));self.assertEqual((p['selected'],p2),(n,pairs))
                with self.assertRaises(ValueError):plan.canonical(parents,pairs,r['operation'])
        with self.assertRaises(ValueError):fixture(201)

    def test_managed_new_and_aggregate_size_boundaries(self):
        mib=1024**2
        self.assertEqual((schema.MAX_DOC,MAX_PUBLIC_WEB_BYTES,schema.MAX_CONTENT,schema.MAX_PACKED,plan.MAX_MANAGED_ROWS),
                         (8*mib,8*mib,12*mib,16*mib,200))
        _, m, r=fixture(2,sizes=[8*mib,4*mib],old_size=8*mib)
        self.assertEqual(schema.parse(*packed(m,r))[0]['producer_bytes'],12*mib)
        for sizes, message in (([8*mib+1],'member exceeds limit'),([8*mib,4*mib+1],'invalid reviewed HTML')):
            with self.subTest(sizes=sizes), self.assertRaisesRegex(ValueError,message):
                _, m, r=fixture(len(sizes),sizes=sizes)
                schema.parse(*packed(m,r))

    def test_oversize_managed_old_is_rejected_before_authority_or_writes(self):
        _, m, r=fixture(1,'html')
        old=r['rows'][0]['old']; old['bytes']=MAX_PUBLIC_WEB_BYTES+1
        path=r['canonical']['path']; parent=json.loads(m['canonical-before.json']); parent['files'][0]=old
        m['canonical-before.json']=v3.encode(parent)
        error='managed old web document exceeds public readback cap'
        with patch.object(publish.v3,'CentralHubStore') as writer, self.assertRaisesRegex(ValueError,error):
            publish.run(*packed(m,r),{},phase='apply',external_writers_stopped=True,managed=True)
        writer.assert_not_called()

        row=r['rows'][0]; generation=r['parent_pointer']['generation']
        approval={'version':1,'expected_generation':generation,'timestamp':r['timestamp'],
                  'rows':[{'id':row['id'],'new':row['new'],'expected_old_object':old['object']}]}
        out=io.BytesIO()
        with ZipFile(out,'w',ZIP_DEFLATED) as archive:
            archive.writestr('approval.json',v3.encode(approval))
            for name in ('0000/document.html','0000/document-manifest.json'):
                archive.writestr(name,m[name])
        blob=out.getvalue()
        hub=Mock(); hub.read_bytes.side_effect=[m['sidecar-before.gz'],m['pointer.json'],m['canonical-before.json']]
        legacy=Mock()
        with self.assertRaisesRegex(ValueError,error):
            prepare.prepare(blob,S(blob),hub,legacy,None)
        self.assertEqual([call.args[1] for call in hub.read_bytes.call_args_list],[prepare.SIDECAR,v3.POINTER,path])
        legacy.observe.assert_not_called()

    def test_scope_and_extra_zip_member(self):
        pairs,m,r=fixture(4); bad=copy.deepcopy(r);bad['rows'][0]['new']['source_revision']='f'*40
        with self.assertRaisesRegex(ValueError,'exact same-source'):schema.parse(*packed(m,bad))
        bad=copy.deepcopy(r);bad['rows'][1]['id']=bad['rows'][0]['id']
        with self.assertRaises(ValueError):schema.parse(*packed(m,bad))
        with self.assertRaises(ValueError):schema.parse(*packed(m|{'../extra':b'x'},r))
        with self.assertRaises(ValueError):schema.parse(*packed(m|{'0000/document.html':b'(wrong)'},r))

    def test_gc_pin_reaches_old_and_new_without_a_catalog(self):
        pairs,m,r=fixture(8,'html'); st=MemoryStore(); root=r['retention']['path'];st.put(ASSETS,root,m['retention.json'])
        for pair in pairs:
            for row in pair:
                st.put(ASSETS,row['manifest'],{'kind':'web-document-stream','version':1,'path':'document.html'});st.put(ASSETS,row['object'],b'file')
        graph=gc.ReferenceGraph(st);self.assertTrue(gc.is_root(root,ASSETS))
        for b in gc.BUCKETS:graph.set_inventory(b,set(st.objects[b]))
        graph.process((ASSETS,root),json.loads(m['retention.json']))
        for pair in pairs:
            for row in pair:
                ref=ASSETS,row['manifest'];self.assertIn(ref,graph.references)
                graph.process(ref,json.loads(st.read_bytes(*ref)));self.assertIn((ASSETS,row['object']),graph.references)
        self.assertFalse(graph.blockers)

    def test_old_readback_uses_bucket_bytes_not_the_compatibility_alias(self):
        old='documents/web/html/'+'1'*64
        new='documents/web/html/'+'2'*64
        row={k:{'object':p+'/document.html','manifest':p+'/document-manifest.json','sha256':S(k.encode()),'bytes':3}
             for k,p in [('old',old),('new',new)]}
        row.update(old_manifest_sha256=S(b'm'),new_manifest_sha256=S(b'm'))
        def serve(req):
            if req.url.host=='huggingface.co':
                if req.url.path.endswith('/document-manifest.json'):
                    return httpx.Response(200,content=b'm')
                self.assertTrue(req.url.path.endswith('/'+row['old']['object']))
                return httpx.Response(200,content=b'old')
            self.assertEqual(req.url.host,'voiceofml-search.hf.space')
            self.assertEqual(req.url.params['path'],row['new']['object'])
            return httpx.Response(200,content=b'new')
        g='a'*64
        receipt={'generation':g,'complete':True,'consumers':{k:{'generation':g,'projection_verified':True} for k in ('hf','pages')}}
        sealed={'target_pointer':{'generation':g},'rows':[row]}
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            self.assertEqual(live(sealed,receipt,{'generation':g},client)['old_and_new_verified'],1)
        for failed, message in (([502], None), ([502,503,504], httpx.HTTPStatusError),
                                ([404], httpx.HTTPStatusError), ([200], ValueError)):
            with self.subTest(failed=failed):
                attempts=iter(failed); seen=[]; waits=[]
                def transient(req):
                    if req.url.host!='voiceofml-search.hf.space':return serve(req)
                    status=next(attempts,None);seen.append(status)
                    return serve(req) if status is None else httpx.Response(status,content=b'bad')
                with httpx.Client(transport=httpx.MockTransport(transient)) as client:
                    if message is None:
                        self.assertEqual(live(sealed,receipt,{'generation':g},client,sleep=waits.append)['old_and_new_verified'],1)
                        self.assertEqual((seen,waits),([502,None],[1]))
                    else:
                        with self.assertRaises(message):live(sealed,receipt,{'generation':g},client,sleep=waits.append)
                        self.assertEqual((seen,waits),(failed,[1,2] if failed[0]==502 else []))

    def test_parallel_public_readback_is_bounded_and_checks_every_member(self):
        g='a'*64
        proof={'generation':g,'complete':True,'consumers':{k:{'generation':g,'projection_verified':True}
                                                               for k in ('hf','pages')}}
        rows=[]; expected={}
        for i in range(8):
            row={}
            for kind in ('old','new'):
                digest=S(f'{i}/{kind}'.encode())
                prefix=f'documents/web/html/{digest}'
                obj,manifest=prefix+'/document.html',prefix+'/document-manifest.json'
                body=f'{kind}{i}'.encode(); manifest_body=f'manifest{i}{kind}'.encode()
                row[kind]={'object':obj,'manifest':manifest,'bytes':len(body),'sha256':S(body)}
                row[kind+'_manifest_sha256']=S(manifest_body)
                expected[obj]=body; expected[manifest]=manifest_body
            rows.append(row)
        wave=Barrier(4, timeout=5); lock=Lock(); calls=Counter()
        def serve(req):
            key=req.url.params.get('path') or req.url.path.split('/resolve/',1)[1]
            with lock: calls[key]+=1
            if key in {r['old']['object'] for r in rows[:4]}:wave.wait()
            return httpx.Response(200,content=expected[key])
        scope={'target_pointer':{'generation':g},'rows':rows}
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            self.assertEqual(live(scope,proof,{'generation':g},client,workers=4)['old_and_new_verified'],8)
            self.assertEqual(calls,Counter({key:1 for key in expected}))
            with self.assertRaisesRegex(ValueError,'concurrency'):
                live(scope,proof,{'generation':g},client,workers=5)
            calls.clear(); expected[rows[0]['old']['object']]=b'BAD!'
            with self.assertRaisesRegex(ValueError,'differs from sealed bytes'):
                live(scope,proof,{'generation':g},client,workers=4)
            self.assertTrue(all(r['old']['object'] not in calls for r in rows[4:]))

    def test_pages_gate_rejects_wrong_parent_and_attempt(self):
        c='a'*40; g='f'*64; ptr={'generation':g,'catalog':{'sha256':'1'*64}}
        state={'status':'completed','conclusion':'success'}
        receipt={'code_sha':c,'workflow_sha':c,'search_generation':'2'*64,'reader_generation':g,'reader_catalog_sha256':'1'*64,
                 'run_id':41,'run_attempt':1,'code_proof_run_id':41,'code_proof_run_attempt':1,'reader_refresh_id':None}
        run=dict(state,id=41,head_sha=c,run_attempt=1,head_branch='main',event='push',display_title='Deploy static content to Pages',path='.github/workflows/static.yml')
        jobs=[dict(state,id=i,name=k,head_sha=c,run_id=41,run_attempt=1,steps=[dict(state,name=n) for n in sorted(w)])
              for i,(k,w) in enumerate((('prepare',pages.PREPARE),('deploy',pages.DEPLOY)),1)]
        routes={'/git/ref/heads/main':{'object':{'sha':c}},'/deployments':[{'id':9,'sha':c,'environment':'github-pages'}],
                '/deployments/9/statuses':[{'state':'success','environment':'github-pages','log_url':'https://github.com/vomebook/search/actions/runs/41/attempts/1'}],
                '/actions/runs/41/attempts/1':run,'/actions/runs/41/attempts/1/jobs':{'total_count':2,'jobs':jobs}}
        api=lambda path,params=None:(routes[path],False)
        public=lambda path:receipt if path.endswith('receipt.json') else {'version':1,'kind':'search-snapshot-local','generation':'2'*64}
        proof=pages.verify(api,public,ptr);self.assertEqual(proof['full_attempt'],1)
        with self.assertRaises(ValueError):pages.verify(api,public,ptr,proof|{'full_run':12})
        with self.assertRaises(ValueError):pages.verify(api,public,ptr|{'generation':'0'*64})
        receipt['code_proof_run_attempt']=2
        with self.assertRaisesRegex(ValueError,'first-attempt'):pages.verify(api,public,ptr)
