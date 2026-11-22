"""Seal a single-format batch from a reviewed immutable candidate archive.

The approval is separate from production apply. Reuses the central publisher's
managed ZIP validator and current dual transport authority on its output.
"""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryFile
from stat import S_IFMT,S_IFREG
from zipfile import ZipFile,ZIP_DEFLATED

from . import publish_reader_v3 as v3, reader_web_batch_plan as plan, reader_web_exact_plan as web
from . import reader_web_managed_review as schema
from .reader_image_history_publish_s3 import CATALOG,SIDECAR
from .reader_image_history_parent import CatalogParent


def prepare(blob,expected,hub,legacy,client):
    if (not isinstance(expected,str) or not web._is_sha(expected) or not 0<len(blob)<=schema.MAX_PACKED
            or sha256(blob).hexdigest()!=expected):raise ValueError('candidate archive differs from approval')
    with ZipFile(BytesIO(blob)) as z:
        infos=z.infolist(); files={x.filename:x for x in infos}
        if (len(files)!=len(infos) or not 3<=len(infos)<=1+2*plan.MAX_MANAGED_ROWS
                or sum(r.file_size for r in infos)>schema.MAX_CONTENT+schema.MAX_REVIEW
                or any(r.is_dir() or S_IFMT(r.external_attr>>16) not in (0,S_IFREG) for r in infos)):
            raise ValueError('candidate archive outside bounded set')
        def member(p,n):
            i=files[p]
            if not 0<i.file_size<=n:raise ValueError('candidate member outside byte bound')
            with z.open(i) as f:raw=f.read(n+1)
            if len(raw)!=i.file_size:raise ValueError('candidate member size drift')
            return raw
        request=web._json(member('approval.json',schema.MAX_REVIEW),'managed candidate approval')
        if (not isinstance(request,dict) or set(request)!={'version','rows','expected_generation','timestamp'}
                or type(request['version']) is not int or request['version']!=1 or not web._is_sha(request['expected_generation'])):
            raise ValueError('invalid managed candidate approval')
        rows=request['rows']; n=len(rows)
        if not 1<=n<=plan.MAX_MANAGED_ROWS or len(infos)!=1+2*n:raise ValueError('invalid approved row count')
        ids=[r['id'] for r in rows]
        if (len(set(ids))!=n or any(not isinstance(i,str) or len(i)!=4 or not i.isascii() or not i.isdecimal() for i in ids)
                or set(files)!={'approval.json'}|{f'{i}/{name}' for i in ids for name in ('document.html','document-manifest.json')}):
            raise ValueError('invalid bounded approved candidate paths')
        ext=rows[0]['new']['extension'];
        if ext not in web.EXTENSIONS:raise ValueError('managed canonical extension outside allowlist')
        before=hub.read_bytes(web.BUCKET,SIDECAR);ptr=hub.read_bytes(web.BUCKET,v3.POINTER)
        if web._json(ptr,'current pointer')['generation']!=request['expected_generation']:
            raise ValueError('approved live Reader parent changed')
        path=f'documents/web/{ext}/index.json'; index=hub.read_bytes(web.BUCKET,path)
        current=web._rows(web._json(index,'canonical index'),'files'); objects={}; reviewed=[]
        for r in rows:
            if set(r)!={'id','new','expected_old_object'}:raise ValueError('invalid approved row fields')
            i=r['id']; new=r['new'];old=current[new['key']]
            web._web_row(old,old=True);web._web_row(new,old=False)
            if old['object']!=r['expected_old_object']:raise ValueError('approved old object moved')
            html=member(i+'/document.html',schema.MAX_DOC); new_manifest=member(i+'/document-manifest.json',web.MAX_MANIFEST)
            old_manifest=hub.read_bytes(web.BUCKET,old['manifest']);j=web._json(old_manifest,'old web manifest')
            if (len(old_manifest)>web.MAX_MANIFEST or (j['path'],j['sha256'],j['bytes'])!=('document.html',old['sha256'],old['bytes'])):
                raise ValueError('old manifest differs from exact old route')
            objects[i+'/document.html']=html;objects[i+'/document-manifest.json']=new_manifest
            reviewed.append({'id':i,'old':old,'new':new,'old_manifest_sha256':plan.digest(old_manifest),'new_manifest_sha256':plan.digest(new_manifest)})
    pairs=[(x['old'],x['new']) for x in reviewed];op=plan.digest(v3.encode([plan.digest(ptr),plan.digest(index),expected]))[:32]
    target=plan.canonical({ext:index},pairs,op,managed=True)[ext]; parent,successor,packed=plan.project(hub,before,pairs)
    if v3.encode(parent)!=v3.encode(web._json(ptr,'prior pointer')):raise ValueError('current pointer drift')
    observed=legacy.observe(CATALOG); epoch=request['timestamp']
    with TemporaryFile() as src,TemporaryFile() as out:
        CatalogParent(client,digest=observed.sha256,size=observed.size,etag=observed.etag)._download(CATALOG,src,condition=observed.etag)
        summary=plan.plan_legacy(src,out,before=before,after=packed,pairs=pairs,timestamp=epoch,parent_sha=observed.sha256)
    if (hub.digest(web.BUCKET,CATALOG)!=observed.sha256 or hub.read_bytes(web.BUCKET,path)!=index
            or hub.read_bytes(web.BUCKET,v3.POINTER)!=ptr or hub.read_bytes(web.BUCKET,SIDECAR)!=before):
        raise ValueError('managed upstream roots changed during preparation')
    refs={r['manifest'] for pair in pairs for r in pair}; pin=v3.encode({'version':1,'kind':'reader-web-retained-objects',
                                                   'resources':[{'bucket':web.BUCKET,'path':p} for p in sorted(refs)]})
    h=plan.digest(pin)
    # Keep an independently traversable manifest root outside reader-index/gc/.
    review=v3.encode({'kind':'reader-web-managed-v1','publication_ready':False,'remote_writes':0,'operation':op,'selected':n,
        'parent_pointer':parent,'target_pointer':successor,'timestamp':epoch,'canonical':{'path':path,'before_sha256':plan.digest(index),'after_sha256':plan.digest(target)},
        'unchanged_indexes':{},'sidecar':{'before_sha256':plan.digest(before),'after_sha256':plan.digest(packed)},'legacy':summary,'pages_parent':{},
        'rows':reviewed,'producer_bytes':sum(r['new']['bytes'] for r in reviewed),'retention':{'path':f'reader-index/retained-web-managed-{h}.json','manifest_count':2*n,'sha256':h}})
    envelope={'review.json':review,'retention.json':pin,'pointer.json':ptr,'sidecar-before.gz':before,'sidecar-after.gz':packed,
              'canonical-before.json':index,'canonical-after.json':target,**objects}
    out=BytesIO()
    with ZipFile(out,'w',ZIP_DEFLATED) as z:
        for p,raw in sorted(envelope.items()):z.writestr(p,raw)
    bundle=out.getvalue(); schema.parse(bundle,plan.digest(bundle),plan.digest(review))
    return bundle,{'bundle_sha256':plan.digest(bundle),'review_sha256':plan.digest(review),'count':n,'target_generation':successor['generation'],'publication_ready':False,'remote_writes':0}


def main():
    import argparse,os
    from .reader_stream_checkpoint_cas import ConditionalCheckpointStore
    from .reader_image_history_publish_s3 import LegacyRoots
    from .reader_image_history_store import S3ClaimStore
    from .reader_page_cas import ConditionalPageStore
    from . import reader_web_batch_review, reader_web_managed_pages
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--candidate-sha256',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if (os.environ.get('GITHUB_REPOSITORY')!='anftm/pipeline' or os.environ.get('GITHUB_EVENT_NAME')!='workflow_dispatch'
            or os.environ.get('GITHUB_REF')!='refs/heads/main' or os.environ.get('GITHUB_RUN_ATTEMPT')!='1' or not web._is_sha(a.candidate_sha256)):
        raise ValueError('requires exact hosted managed preparation')
    cps=ConditionalCheckpointStore();store=cps.store;client=store._client('vomebook');claims=S3ClaimStore(client)
    val=claims._get(f'reader-index/gc/web-managed-approvals/v1/{a.candidate_sha256}.zip',maximum=schema.MAX_PACKED)[0]
    if val is None:raise ValueError('approved immutable portable input does not exist')
    hub=v3.CentralHubStore();legacy=LegacyRoots(client)
    bundle,receipt=prepare(val,a.candidate_sha256,hub,legacy,client)
    r,data,parents,_=schema.parse(bundle,receipt['bundle_sha256'],receipt['review_sha256'])
    authority=reader_web_batch_review.authority(r,data,parents,checkpoints=cps,pointers=ConditionalPageStore(hub,s3=store),legacy=legacy,hub=hub,s3=store)
    authority.update(bundle_sha256=receipt['bundle_sha256'],review_sha256=receipt['review_sha256'],pages=reader_web_managed_pages.current(r['parent_pointer']))
    a.output.mkdir(exist_ok=True,parents=True)
    with (a.output/'managed-review.zip').open('xb') as f:f.write(bundle)
    with (a.output/'managed-review-receipt.json').open('xb') as f:f.write(v3.encode(receipt))
    with (a.output/'managed-authority.json').open('xb') as f:f.write(v3.encode(authority))

if __name__=='__main__':main()
