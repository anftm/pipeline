"""Digest-addressed central preflight, apply and public acceptance for reviewed ten web routes."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode

import httpx
from . import publish_reader_v3 as v3, reader_web_batch_publish as publish, reader_web_batch_review as review
from .reader_image_history_publish_s3 import LegacyRoots
from .reader_image_history_store import S3ClaimStore
from .reader_page_cas import ConditionalPageStore
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore
from .reader_web_exact_plan import _is_sha

MAX_PUBLIC_WEB_BYTES = 8 * 1024 * 1024

PREFIX='reader-index/gc/web-exact-batch-inputs/v1/'
PROOFS='reader-index/gc/web-exact-batch-authority/v1/'


def read(store,path,maximum):
    raw,_=store._get(path,maximum=maximum)
    if raw is None:raise ValueError('missing exact web batch member')
    return raw


def options(raw,mode):
    d=json.loads(raw)
    fields={'bundle_sha256','review_sha256'} if mode in ('preflight','live') else {'bundle_sha256','review_sha256','authority_sha256','phase','external_writers_stopped'}
    if (not isinstance(d,dict) or d.keys()!=fields or any(not _is_sha(d[k]) for k in fields if k.endswith('_sha256'))
            or (mode=='apply' and d['external_writers_stopped'] is not True)):
        raise ValueError('invalid exact ten-web options')
    return d


def live(sealed, consumer_report, publication, client, *, sleep=time.sleep):
    generation=sealed['target_pointer']['generation']
    if (publication.get('generation')!=generation or consumer_report.get('generation')!=generation
            or consumer_report.get('complete') is not True or set(consumer_report.get('consumers',{}))!={'hf','pages'}
            or any(a.get('generation')!=generation or a.get('projection_verified') is not True
                   for a in consumer_report['consumers'].values())):
        raise ValueError('both full deployed web projections must be verified')
    def check(path,digest,size=None,*,original_object=False):
        # Retired Web Reader URLs may intentionally resolve to today's asset.
        # The exact old bytes must be checked at the immutable bucket key.
        url=('https://huggingface.co/buckets/vomebook/reader-assets-v2/resolve/'+path
             if original_object or path.endswith('/document-manifest.json')
             else 'https://voiceofml-search.hf.space/api/reader-bucket-resource?'+urlencode({'path':path}))
        for attempt in range(3):
            h=sha256();length=0
            try:
                with client.stream('GET',url,headers={'Cache-Control':'no-cache'}) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        length+=len(chunk)
                        if length>MAX_PUBLIC_WEB_BYTES:raise ValueError('public web asset exceeds reviewed cap')
                        h.update(chunk)
            except (httpx.HTTPStatusError,httpx.TransportError) as exc:
                if attempt==2 or (isinstance(exc,httpx.HTTPStatusError) and
                                  exc.response.status_code not in {408,429,502,503,504}):
                    raise
                sleep(2**attempt)
                continue
            if (size is not None and length!=size) or h.hexdigest()!=digest:
                raise ValueError('public web asset differs from sealed bytes')
            return
    for r in sealed['rows']:
        for kind in ('old','new'):
            row=r[kind]
            check(row['object'],row['sha256'],row['bytes'],original_object=kind=='old')
            check(row['manifest'],r[kind+'_manifest_sha256'])
    return {'generation':generation,'old_and_new_verified':len(sealed['rows']),'both_projections_verified':True,'deleted_objects':0}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['preflight','apply','live'])
    p.add_argument('--resource-json',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--consumers',type=Path);p.add_argument('--publication',type=Path);p.add_argument('--managed',action='store_true');a=p.parse_args()
    if (os.environ.get('GITHUB_REPOSITORY')!='anftm/pipeline' or os.environ.get('GITHUB_EVENT_NAME')!='workflow_dispatch'
            or os.environ.get('GITHUB_REF')!='refs/heads/main' or os.environ.get('GITHUB_RUN_ATTEMPT')!='1'):
        raise RuntimeError('first exact central main dispatch required')
    d=options(a.resource_json,a.mode)
    cps=ConditionalCheckpointStore();s3=cps.store;client=s3._client('vomebook');store=S3ClaimStore(client)
    if a.managed:
        from . import reader_web_managed_review as managed, reader_web_managed_pages
    schema=managed if a.managed else review
    blob=read(store,PREFIX+d['bundle_sha256']+'.zip',schema.MAX_PACKED)
    if a.mode=='apply':
        raw=read(store,PROOFS+d['authority_sha256']+'.json',32*1024)
        if sha256(raw).hexdigest()!=d['authority_sha256']:raise ValueError('web authority digest mismatch')
        result=publish.run(blob,d['bundle_sha256'],d['review_sha256'],json.loads(raw),phase=d['phase'],
                           external_writers_stopped=d['external_writers_stopped'],managed=a.managed)
    else:
        sealed,data,parents,pairs=schema.parse(blob,d['bundle_sha256'],d['review_sha256'])
        if a.mode=='live':
            with httpx.Client(follow_redirects=True,timeout=30) as http:
                result=live(sealed,json.loads(a.consumers.read_bytes()),json.loads(a.publication.read_bytes()),http)
        else:
            hub=v3.CentralHubStore()
            result=review.authority(sealed,data,parents,checkpoints=cps,pointers=ConditionalPageStore(hub,s3=s3),
                   legacy=LegacyRoots(client),hub=hub,s3=s3)
            if a.managed:
                from . import reader_web_batch_plan as plan
                from .reader_image_history_publish_s3 import CATALOG
                if hub.digest(v3.ASSETS,CATALOG)!=result['catalog']['sha256']:
                    raise ValueError('legacy catalog S3/resolver mismatch')
                p,t,packed=plan.project(hub,data['sidecar-before.gz'],pairs)
                if p!=sealed['parent_pointer'] or t!=sealed['target_pointer'] or packed!=data['sidecar-after.gz']:
                    raise ValueError('managed route projection differs from review')
                result['pages']=reader_web_managed_pages.current(p)
            result.update(bundle_sha256=d['bundle_sha256'],review_sha256=d['review_sha256'])
    with a.output.open('xb') as f:f.write(v3.encode(result))

if __name__=='__main__':main()
