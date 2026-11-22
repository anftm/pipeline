"""Digest-bound, single-index release input for managed web reconversion.

No hard-coded books or sample IDs. This proves shape and exact routes, not
editorial acceptance; the immutable review must be approved independently.
"""
from dataclasses import asdict
from io import BytesIO
from stat import S_IFMT, S_IFREG
from zipfile import ZipFile
import re
from . import publish_reader_v3 as v3, reader_web_batch_plan as plan, reader_web_exact_plan as web
from .reader_image_history_publish_s3 import CATALOG, SIDECAR
from .reader_stream_checkpoint_cas import strong_etag

MAX_PACKED=16*1024**2
MAX_EXPANDED=40*1024**2
MAX_REVIEW=2*1024**2
MAX_DOC=4*1024**2
MAX_CONTENT=12*1024**2
FIXED={'review.json','retention.json','pointer.json','sidecar-before.gz','sidecar-after.gz','canonical-before.json','canonical-after.json'}
FIELDS={'kind','publication_ready','remote_writes','operation','selected','parent_pointer','target_pointer','timestamp','canonical',
        'unchanged_indexes','sidecar','legacy','pages_parent','rows','producer_bytes','retention'}


def parse(blob,bundle_sha,review_sha):
    if not 0<len(blob)<=MAX_PACKED or plan.digest(blob)!=bundle_sha:
        raise ValueError('managed web bundle digest or size differs')
    with ZipFile(BytesIO(blob)) as z:
        info=z.infolist(); seen={x.filename:x for x in info}
        if (len(info)!=len(seen) or not 9<=len(info)<=7+2*plan.MAX_MANAGED_ROWS or not FIXED<=seen.keys()
                or sum(x.file_size for x in info)>MAX_EXPANDED or any(x.file_size<1 or x.is_dir() or S_IFMT(x.external_attr>>16) not in (0,S_IFREG) for x in info)):
            raise ValueError('managed web ZIP shape, file type or expansion limit')
        def read(path,limit):
            item=seen[path]
            if item.file_size>limit:raise ValueError('managed web member exceeds limit')
            with z.open(item) as f:raw=f.read(limit+1)
            if len(raw)!=item.file_size:raise ValueError('managed web member length differs')
            return raw
        raw=read('review.json',MAX_REVIEW)
        if plan.digest(raw)!=review_sha:raise ValueError('managed review differs from approved digest')
        r=web._json(raw,'managed web review')
        if (not isinstance(r,dict) or r.keys()!=FIELDS or r['kind']!='reader-web-managed-v1'
                or r['publication_ready'] is not False or type(r['remote_writes']) is not int or r['remote_writes']!=0):
            raise ValueError('invalid managed web review envelope')
        n=r['selected'];rows=r['rows']
        if type(n) is not int or not 1<=n<=plan.MAX_MANAGED_ROWS or not isinstance(rows,list) or len(rows)!=n:
            raise ValueError('managed web row count outside bounds')
        ids=set()
        for row in rows:
            if (not isinstance(row,dict) or row.keys()!={'id','old','new','old_manifest_sha256','new_manifest_sha256'}
                    or not isinstance(row['id'],str) or re.fullmatch('[0-9]{4}',row['id']) is None or row['id'] in ids
                    or not all(web._is_sha(row[k]) for k in ('old_manifest_sha256','new_manifest_sha256'))):
                raise ValueError('invalid managed web review row')
            ids.add(row['id'])
        if seen.keys()!=FIXED|{i+'/'+f for i in ids for f in ('document.html','document-manifest.json')}:
            raise ValueError('managed ZIP members differ from reviewed rows')
        limits={'retention.json':MAX_REVIEW,'pointer.json':8192,'sidecar-before.gz':8*1024**2,'sidecar-after.gz':8*1024**2,
                'canonical-before.json':web.MAX_CANONICAL,'canonical-after.json':web.MAX_CANONICAL}
        data={name:read(name,cap) for name,cap in limits.items()}
        pairs=[(row['old'],row['new']) for row in rows];ext=rows[0]['old'].get('extension')
        parents={ext:data['canonical-before.json']}; targets=plan.canonical(parents,pairs,r['operation'],managed=True)
        path=f'documents/web/{ext}/index.json'
        expected={'path':path,'before_sha256':plan.digest(parents[ext]),'after_sha256':plan.digest(targets[ext])}
        if r['canonical']!=expected or targets[ext]!=data['canonical-after.json']:
            raise ValueError('managed canonical successor differs')
        total=0
        for row in rows:
            new=row['new']; i=row['id'];a=i+'/document.html'; b=i+'/document-manifest.json'
            html=read(a,MAX_DOC);manifest=read(b,web.MAX_MANIFEST);html.decode('utf-8',errors='strict');total+=len(html)
            if (total>MAX_CONTENT or len(html)!=new['bytes'] or plan.digest(html)!=new['sha256']
                    or plan.digest(manifest)!=row['new_manifest_sha256'] or not web._json_equal(web._json(manifest,'web manifest'),
                        {'kind':'web-document-stream','version':1,'path':'document.html','source_extension':ext,
                         'source_sha256':new['source_sha256'],'bytes':new['bytes'],'sha256':new['sha256']})):
                raise ValueError('invalid reviewed HTML or manifest bytes')
            data[a]=html;data[b]=manifest
    refs={a['manifest'] for pair in pairs for a in pair}
    pin=v3.encode({'version':1,'kind':'reader-web-retained-objects','resources':[{'bucket':web.BUCKET,'path':p} for p in sorted(refs)]});h=plan.digest(pin)
    if (len(refs)!=2*n or pin!=data['retention.json'] or r['retention']!={'path':f'reader-index/retained-web-managed-{h}.json','sha256':h,'manifest_count':2*n}):
        raise ValueError('managed old/new independent retention root differs')
    ptr=web._json(data['pointer.json'],'managed pointer')
    if (ptr!=r['parent_pointer'] or total!=r['producer_bytes'] or
        any(plan.digest(data[f'sidecar-{tag}.gz'])!=r['sidecar'][tag+'_sha256'] for tag in ('before','after'))):
        raise ValueError('managed root/byte attestation differs')
    controls=r['unchanged_indexes']
    if not isinstance(controls,dict) or set(controls)- (web.EXTENSIONS-{ext}) or not all(web._is_sha(h) for h in controls.values()):
        raise ValueError('invalid unrelated index pins')
    r.update(parent_generation=ptr['generation'],before_sidecar_sha256=r['sidecar']['before_sha256'],after_sidecar_sha256=r['sidecar']['after_sha256'])
    r['canonical']={ext:{'path':path,'parent_sha256':expected['before_sha256'],'target_sha256':expected['after_sha256'],'changed_rows':n}}
    data['retained.json']=pin;data[f'target-{ext}.json']=targets[ext]
    return r,data,parents,pairs
