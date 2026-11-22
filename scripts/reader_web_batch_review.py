"""Validate a digest-bound, flat-allowlisted ten-route input and read authority.

This module has no write entry point. Its authority receipt is a prerequisite
for a separately reviewed writer operation, not permission to mutate.
"""
from dataclasses import asdict
from io import BytesIO
from zipfile import ZipFile
from . import reader_web_batch_plan as plan, reader_web_exact_plan as web
from . import publish_reader_v3 as v3
from .reader_image_history_publish_s3 import CATALOG, SIDECAR
from .reader_stream_checkpoint_cas import strong_etag

MAX_PACKED=8*1024*1024
MAX_EXPANDED=24*1024*1024
IDS={'02','05','07','11','14','15','16','18','19','20'}
REVIEW_FIELDS={'version','operation','parent_generation','target_pointer','timestamp','canonical','legacy',
    'before_sidecar_sha256','after_sidecar_sha256','projection_routes','changed_routes','retention','rows','uploaded','publication_ready'}


def parse(blob, expected_sha, review_sha):
    if not 0<len(blob)<=MAX_PACKED or plan.digest(blob)!=expected_sha:
        raise ValueError('archive does not match exact reviewed web batch digest')
    with ZipFile(BytesIO(blob)) as z:
        info=z.infolist()
        names={'review.json','retained.json','pointer.json','sidecar-before.gz','sidecar-after.gz'}
        names|={prefix+e+'.json' for e in plan.QUOTAS for prefix in ('canonical-','target-')}
        names|={i+'/'+f for i in IDS for f in ('document.html','document-manifest.json')}
        if (len(info)!=len(names) or {e.filename for e in info}!=names or any(e.file_size<1 for e in info)
                or sum(e.file_size for e in info)>MAX_EXPANDED):
            raise ValueError('invalid bounded web batch archive members')
        data={i.filename:z.read(i) for i in info}
    raw=data['review.json']
    if plan.digest(raw)!=review_sha or len(raw)>64*1024:
        raise ValueError('review digest mismatch')
    review=web._json(raw,'batch review')
    if (not isinstance(review,dict) or set(review)!=REVIEW_FIELDS or type(review['version']) is not int
            or review['version']!=1 or review['publication_ready'] is not False
            or review['uploaded'] is not False or review['changed_routes']!=10):
        raise ValueError('invalid ten-route review envelope')
    rows=review['rows']; pairs=[(r['old'],r['new']) for r in rows]
    if len(rows)!=10 or {r['id'] for r in rows}!=IDS:
        raise ValueError('invalid ten reviewed sample IDs')
    parents={e:data['canonical-'+e+'.json'] for e in plan.QUOTAS}
    targets=plan.canonical(parents,pairs,review['operation'])
    for e in parents:
        expected={'path':f'documents/web/{e}/index.json','parent_sha256':plan.digest(parents[e]),
                  'target_sha256':plan.digest(targets[e]),'changed_rows':plan.QUOTAS[e]}
        if targets[e]!=data['target-'+e+'.json'] or review['canonical'][e]!=expected:
            raise ValueError('invalid exact canonical successor identity')
    for r in rows:
        new=r['new']; name=r['id']; html=data[name+'/document.html']; manifest=data[name+'/document-manifest.json']
        expected={'version':1,'kind':'web-document-stream','source_extension':new['extension'],'source_sha256':new['source_sha256'],
                  'path':'document.html','bytes':new['bytes'],'sha256':new['sha256']}
        html.decode('utf-8',errors='strict')
        if (len(html)!=new['bytes'] or plan.digest(html)!=new['sha256']
                or plan.digest(manifest)!=r['new_manifest_sha256'] or not web._is_sha(r['old_manifest_sha256'])
                or not web._json_equal(web._json(manifest,'new manifest'),expected)):
            raise ValueError('invalid exact web bundle identity')
    retained={'version':1,'kind':'reader-web-retained-objects','resources':[
        {'bucket':web.BUCKET,'path':p} for p in sorted({row['manifest'] for pair in pairs for row in pair})]}
    pin=v3.encode(retained); h=plan.digest(pin)
    if (data['retained.json']!=pin or review['retention']!={'path':f'reader-index/retained-web-batch-{h}.json',
                 'sha256':h,'manifest_count':len(retained['resources'])}):
        raise ValueError('review omits independent old/new reachability')
    pointer=web._json(data['pointer.json'],'parent pointer')
    if (pointer['generation']!=review['parent_generation'] or
         any(plan.digest(data[f'sidecar-{x}.gz'])!=review[f'{x}_sidecar_sha256'] for x in ('before','after'))):
        raise ValueError('invalid reviewed sidecar or pointer parents')
    return review,data,parents,pairs


def authority(review,data,parents,*,checkpoints,pointers,legacy,hub,s3):
    def read_set():
        return {ext:checkpoints.read(web.BUCKET,item['path']) for ext,item in review['canonical'].items()}
    live=read_set()
    for ext,current in live.items():
        strong_etag(current.etag)
        if current.raw!=parents[ext] or hub.read_bytes(web.BUCKET,review['canonical'][ext]['path'])!=current.raw:
            raise ValueError('reviewed canonical index drift: '+ext)
    ptr,tag=pointers.read_for_update(web.BUCKET,v3.POINTER);strong_etag(tag)
    if ptr!=data['pointer.json'] or hub.read_bytes(web.BUCKET,v3.POINTER)!=ptr:
        raise ValueError('reviewed v3 pointer drift')
    cat=legacy.observe(CATALOG); side=legacy.observe(SIDECAR)
    for obj in (cat,side):strong_etag(obj.etag)
    if ((cat.sha256,cat.size)!=(review['legacy']['parent_sha256'],review['legacy']['parent_bytes'])
            or (side.sha256,side.size)!=(review['before_sidecar_sha256'],len(data['sidecar-before.gz']))
            or hub.read_bytes(web.BUCKET,SIDECAR)!=data['sidecar-before.gz']):
        raise ValueError('reviewed legacy roots drifted')
    for r in review['rows']:
        old=r['old']
        for path,h,w in ((old['object'],old['sha256'],old['bytes']),(old['manifest'],r['old_manifest_sha256'],None)):
            raw=s3.read_bytes(web.BUCKET,path)
            if plan.digest(raw)!=h or (w is not None and len(raw)!=w) or hub.read_bytes(web.BUCKET,path)!=raw:
                raise ValueError('historical web bytes no longer agree')
    for path,obj in ((CATALOG,cat),(SIDECAR,side)):
        h=s3._client('vomebook').head_object(Bucket='reader-assets-v2',Key=path)
        if (h.get('ETag'),h.get('ContentLength'))!=(obj.etag,obj.size):
            raise ValueError('mutable legacy roots moved during attestation')
    if read_set()!=live or pointers.read_for_update(web.BUCKET,v3.POINTER)!=(ptr,tag):
        raise ValueError('authority moved during attestation')
    return {'version':1,'catalog':asdict(cat),'sidecar':asdict(side),'index_etags':{e:s.etag for e,s in live.items()},
            'pointer_etag':tag,'pointer_sha256':plan.digest(ptr),'parent_generation':review['parent_generation'],
            'remote_writes':0,'publication_ready':False}
