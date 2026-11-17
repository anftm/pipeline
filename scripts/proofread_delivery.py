"""Durable first-attempt proofreading submission; unknown effects never replay.

The request receipt is stored before any external effect. Each mutation records
an intent first and its bounded response after. Interrupted submissions remain
claimed for explicit investigation. Existing PR and branch safeguards still run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from . import submit_proofread as legacy, search_snapshot
from .reader_bucket_store import S3BucketStore
from .reader_page_cas import strong_etag

BUCKET='vomebook/bha-data'
ROOT='proofreading/delivery/'
MAX_BYTES=4*1024*1024
IDENTITY={'name':'vomebook','email':'vomebook@gmail.com'}


def digest(value):return hashlib.sha256(search_snapshot.canonical(value)).hexdigest()


class Store:
    def __init__(self):
        from botocore.config import Config
        self.backend=S3BucketStore()
        self.backend._config=self.backend._config.merge(Config(retries={'total_max_attempts':1}))
    def location(self,key):
        if not re.fullmatch(ROOT+r'[0-9a-f]{64}\.json',key):raise ValueError('invalid proofreading receipt key')
        namespace,bucket=self.backend._location(BUCKET)
        if (namespace,bucket)!=('vomebook','bha-data'):raise ValueError('unexpected receipt bucket')
        return self.backend._client(namespace),bucket
    def read(self,key):
        client,bucket=self.location(key)
        from botocore.exceptions import ClientError
        try:r=client.get_object(Bucket=bucket,Key=key)
        except ClientError as e:
            if e.response.get('Error',{}).get('Code') in {'NoSuchKey','404','NotFound'}:return None,None
            raise
        try:raw=r['Body'].read(MAX_BYTES+1)
        finally:r['Body'].close()
        if not 0<len(raw)<=MAX_BYTES:raise ValueError('receipt size invalid')
        return json.loads(raw),strong_etag(r.get('ETag'))
    def put(self,key,value,etag):
        client,bucket=self.location(key);raw=search_snapshot.canonical(value)
        if len(raw)>MAX_BYTES:raise ValueError('receipt too large')
        condition={'IfNoneMatch':'*'} if etag is None else {'IfMatch':strong_etag(etag)}
        client.put_object(Bucket=bucket,Key=key,Body=raw,ContentType='application/json',**condition)


class Delivery:
    def __init__(self,store,request,run):
        self.store=store
        self.request_hash=digest(request)
        request_id=request.get('request_id')
        if request_id is not None and (not isinstance(request_id,str) or not 1<=len(request_id)<=200):
            raise ValueError('invalid request identity')
        self.key=ROOT+digest({'request_id':request_id} if request_id else request)+'.json'
        self.before,self.etag=store.read(self.key)
        self.poisoned=False
        self.state={'version':1,'kind':'proofreading-delivery','request_sha256':self.request_hash,
                    'run':run,'state':'claimed','effects':[]}
    def save(self):
        # Never retry a failed conditional mutation, including lost responses.
        if getattr(self,'poisoned',False):raise RuntimeError('prior receipt outcome unknown')
        try:
            self.store.put(self.key,self.state,self.etag)
            actual,etag=self.store.read(self.key)
            if actual!=self.state:raise RuntimeError('receipt write outcome differs')
            self.etag=etag
        except Exception:
            self.poisoned=True
            raise
    def claim(self):
        if self.before is not None:
            if self.before.get('request_sha256')!=self.request_hash:raise ValueError('request ID reused for other content')
            if self.before.get('state')=='complete':return False
            raise RuntimeError('prior submission requires explicit recovery; effects will not be replayed')
        self.save();return True
    def call(self,send,token,method,path,payload=None):
        if method in {'GET','HEAD'}:return send(token,method,path,payload)
        if getattr(self,'poisoned',False):raise RuntimeError('prior receipt outcome unknown')
        if any(e.get('state')=='intent' for e in self.state['effects']):
            raise RuntimeError('unresolved effect blocks further mutations')
        if method not in {'POST','PUT','PATCH','DELETE'}:raise ValueError('unsupported mutation')
        # Preserve human review: live resumed delivery never auto-merges or
        # deletes branches. Review/revert workflows provide separate controls.
        if method=='DELETE' or (path.endswith('/merge') and not getattr(self,'review_merge',False)):
            raise ValueError('submission cannot merge or delete')
        if path.endswith('/merge') and (method!='PUT' or not isinstance(payload,dict)
                or not re.fullmatch(r'[0-9a-f]{40}',str(payload.get('sha','')))):
            raise ValueError('review merge requires exact head SHA')
        if not path.startswith('/repos/'):raise ValueError('unexpected mutation endpoint')
        if method=='PUT' and '/contents/' in path:
            payload={**payload,'author':IDENTITY,'committer':IDENTITY}
        effect={'method':method,'path':path,'payload_sha256':digest(payload),'state':'intent'}
        self.state['effects'].append(effect);self.save()
        status,data=send(token,method,path,payload)
        effect.update(state='responded',http_status=status,response_sha256=digest(data))
        self.save()
        return status,data


def submit(store,request,*,run,execute):
    delivery=Delivery(store,request,run)
    if not delivery.claim():return {'status':'already-complete','request_sha256':delivery.request_hash,'replayed':False}
    # No automatic low-risk merge: all submitted changes enter the normal
    # existing authorized review workflow, including metadata/config changes.
    execution=json.loads(json.dumps(request));execution['auto_merge']=False
    try:execute(delivery,execution)
    except Exception:
        # The prior effect intent is already durable. Do not obscure an unknown
        # write by attempting another state mutation in an exception handler.
        raise
    delivery.state['state']='complete';delivery.save()
    return {'status':'complete','request_sha256':delivery.request_hash,'effect_count':len(delivery.state['effects']),
            'receipt':delivery.key,'auto_merged':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--check',action='store_true',help='verify credential access and receipt reads only')
    args=parser.parse_args(argv)
    if os.environ.get('PROOFREAD_DELIVERY_ENABLED')!='true':raise RuntimeError('proofreading delivery not enabled')
    if (os.environ.get('GITHUB_REPOSITORY')!='anftm/pipeline' or os.environ.get('GITHUB_REF')!='refs/heads/main'
            or os.environ.get('GITHUB_RUN_ATTEMPT')!='1'):raise RuntimeError('first central main attempt required')
    if args.check:
        token=os.environ.get('GH_PAT','');tracker=os.environ.get('TRACKER_TOKEN','')
        if not token or not tracker:raise ValueError('submission credentials missing')
        for repository,credential in [('anftm/banned-historical-archives0',token),('anftm/pipeline',tracker)]:
            status,value=legacy.api_request(credential,'GET','/repos/'+repository)
            if status!=200 or value.get('full_name')!=repository:
                raise ValueError('expected repository access unavailable')
        Store().read(ROOT+'0'*64+'.json')
        result={'kind':'proofreading-delivery-check','credentials_readable':True,'receipt_store_readable':True,
                'remote_writes':0,'submission_created':False}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x') as f:json.dump(result,f,indent=2)
        print(json.dumps(result));return
    request=legacy.load_request()
    original=legacy.api_request
    original_crops=legacy.build_review_crops
    def execute(delivery,value):
        def crops(payload):
            if any(e.get('state')=='intent' for e in delivery.state['effects']):
                raise RuntimeError('unresolved effect blocks image upload')
            effect={'method':'HF-crop-commit','payload_sha256':digest(payload),'state':'intent'}
            delivery.state['effects'].append(effect);delivery.save()
            assets=original_crops(payload)
            effect.update(state='responded',assets=assets);delivery.save()
            return assets
        legacy.api_request=lambda *a,**k:delivery.call(original,*a,**k)
        legacy.build_review_crops=crops
        try:legacy.execute_request(value)
        finally:
            legacy.api_request=original
            legacy.build_review_crops=original_crops
    result=submit(Store(),request,run={'id':os.environ.get('GITHUB_RUN_ID'),'sha':os.environ.get('GITHUB_SHA')},execute=execute)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps(result))


if __name__=='__main__':main()
