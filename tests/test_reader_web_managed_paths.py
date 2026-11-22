"""Managed preparation admits bounded cohorts without extending old gates."""
import copy,hashlib,io,json,unittest
from zipfile import ZipFile,ZIP_DEFLATED
from scripts import reader_web_batch_plan as plan,reader_web_managed_review as schema,reader_web_managed_pages as pages
from scripts import publish_reader_v3 as v3,reader_web_exact_plan as web,reader_gc_graph as gc
from tests.test_reader_gc_graph import MemoryStore,ASSETS

S=lambda b:hashlib.sha256(b).hexdigest()


def fixture(n,ext='mhtml'):
    rows=[]; members={}; kept={'key':'unrelated','source_sha256':'a'*64}
    for j in range(n):
        path=f'entry-{j}.{ext}'; raw=f'<!doctype html><p>new {j}</p>'.encode(); h=S(raw); src=S(path.encode()); name=f'{j:04d}'
        def root(d):return f'documents/web/{ext}/{d}'
        old_base=root(S(('old-root:'+path).encode())); new_base=root(S(f'{src}\0{h}'.encode()))
        old={'key':'org/books\0'+path,'repo':'org/books','path':path,'extension':ext,'source_revision':'3'*40,
            'source_sha256':src,'mode':'document-stream','bucket':web.BUCKET,'object':old_base+'/document.html',
            'manifest':old_base+'/document-manifest.json','bytes':2,'sha256':S(('old:'+path).encode()),'checkpointed_at':'old'}
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
