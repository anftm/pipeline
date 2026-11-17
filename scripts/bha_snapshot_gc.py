"""Bounded BHA snapshot GC: retain current/recent/control pins; mark then sweep.

Only one generation may be marked or swept per run. Incomplete inventory,
active index jobs, changed control roots or object identities stop collection.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time

from . import bha_reindex_journal as journal, check_bha_reindex_access as access
from . import search_snapshot
from .reader_bucket_store import S3BucketStore

BUCKET='vomebook/bha-data'
GEN=re.compile(r'bha-([0-9]{10})-[0-9a-f]{8}\Z')
ROOT='gc/snapshots/'
RETENTION=30*86400
MARK_DELAY=86400
MAX_OBJECTS=1000
MAX_BYTES=40*1024**3


def sha(raw):return hashlib.sha256(raw).hexdigest()


def references(value):
    found=set()
    if isinstance(value,dict):
        for v in value.values():found.update(references(v))
    elif isinstance(value,list):
        for v in value:found.update(references(v))
    elif isinstance(value,str):
        if GEN.fullmatch(value):found.add(value)
        found.update(re.findall(r'generations/(bha-[0-9]{10}-[0-9a-f]{8})(?:/|\Z)',value))
    return found


class Store:
    def __init__(self):
        from botocore.config import Config
        self.backend=S3BucketStore()
        self.backend._config=self.backend._config.merge(Config(retries={'total_max_attempts':1}))
        namespace,bucket=self.backend._location(BUCKET)
        if (namespace,bucket)!=('vomebook','bha-data'):raise ValueError('unexpected GC bucket')
        self.client=self.backend._client(namespace);self.bucket=bucket
    def inventory(self):
        rows={}
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket):
            for r in page.get('Contents',[]):
                key=r['Key']
                if key in rows or len(rows)>=100000:raise ValueError('invalid or oversized snapshot inventory')
                rows[key]={'bytes':r['Size'],'etag':r['ETag'],'modified':int(r['LastModified'].timestamp())}
        return rows
    def read(self,key):
        r=self.client.get_object(Bucket=self.bucket,Key=key)
        try:raw=r['Body'].read(4*1024*1024+1)
        finally:r['Body'].close()
        if len(raw)>4*1024*1024:raise ValueError('GC control root too large')
        return raw
    def create(self,key,value):
        if not key.startswith(ROOT):raise ValueError('GC receipt key invalid')
        raw=search_snapshot.canonical(value)
        self.client.put_object(Bucket=self.bucket,Key=key,Body=raw,ContentType='application/json',IfNoneMatch='*')
        if self.read(key)!=raw:raise ValueError('GC receipt readback differs')
    def delete(self,key,etag):
        if not re.fullmatch(r'generations/bha-[0-9]{10}-[0-9a-f]{8}/.+',key):raise ValueError('delete outside snapshot generation')
        self.client.delete_object(Bucket=self.bucket,Key=key,IfMatch=etag)


def observe(store,token,*,status=access.read_status,now=None):
    now=int(time.time()) if now is None else now
    before=status(token)
    if (before.get('index_status')!='ready' or before.get('switch_pending') is not False
            or before.get('job') is not None and before['job'].get('state') not in {'succeeded','failed'}):
        raise ValueError('index operation active or unknown')
    inventory=store.inventory();controls={};pins=set();groups={};marks={}
    for key,row in sorted(inventory.items()):
        if (type(row.get('bytes')) is not int or row['bytes']<0 or type(row.get('modified')) is not int
                or not isinstance(row.get('etag'),str) or not row['etag'].startswith('"')):
            raise ValueError('object identity incomplete')
        if key.startswith('generations/'):
            parts=key.split('/')
            if len(parts)<3 or not GEN.fullmatch(parts[1]) or any(p in {'','.','..'} for p in parts):
                raise ValueError('unknown snapshot path')
            groups.setdefault(parts[1],{})[key]=row
        elif key.startswith(ROOT):
            if key.endswith('/mark.json'):marks[key]=json.loads(store.read(key))
        else:
            if not key.endswith('.json'):raise ValueError('unknown non-snapshot root')
            raw=store.read(key);value=json.loads(raw);controls[key]=sha(raw);pins.update(references(value))
            if key==journal.HEAD:
                rec_key=journal._record_key(value['operation'],value['record'])
                rec=json.loads(store.read(rec_key))
                if rec.get('state') not in {'verified','abandoned'}:raise ValueError('BHA index journal not terminal')
    current=json.loads(store.read('current.json'))
    if current.get('schema')!=3 or not GEN.fullmatch(str(current.get('generation'))):raise ValueError('unknown snapshot manifest')
    gen=current['generation']
    if (gen not in groups or current.get('index_metadata',{}).get('archive_revisions')!=before.get('archive_revisions')
            or current.get('index_metadata',{}).get('build_fingerprint')!=before.get('build_fingerprint')):
        raise ValueError('snapshot and serving identities differ')
    for name in ['docs','facets']:
        item=current.get('sidecars',{}).get(name,{})
        path=f'generations/{gen}/'+str(item.get('filename'))
        if path not in inventory or inventory[path]['bytes']!=item.get('bytes'):raise ValueError('current sidecar missing or wrong size')
    if not any('/elasticsearch/' in k for k in groups[gen]):raise ValueError('current Elasticsearch snapshot missing')
    protected={gen}|pins
    ordered=sorted(groups,key=lambda x:(int(GEN.fullmatch(x).group(1)),x),reverse=True)
    # At least current plus newest other snapshot remain available for rollback.
    others=[g for g in ordered if g!=gen]
    if others:protected.add(others[0])
    for g,objects in groups.items():
        if now-max([int(GEN.fullmatch(g).group(1)),*[v['modified'] for v in objects.values()]])<=RETENTION:protected.add(g)
    if status(token)!=before:raise ValueError('serving state changed during GC observation')
    again=store.inventory()
    if again!=inventory:raise ValueError('bucket inventory changed during observation')
    return {'current':gen,'controls':controls,'protected':sorted(protected),'groups':groups,'marks':marks,
            'status':before,'observed_at':now}


def run(store,token,*,apply=False,status=access.read_status,now=None):
    observed=observe(store,token,status=status,now=now);now=observed['observed_at']
    result={'kind':'bha-snapshot-gc','current':observed['current'],'protected':observed['protected'],
            'remote_writes':0,'deleted_objects':0,'status':'no-candidates'}
    for g in sorted(set(observed['groups'])-set(observed['protected'])):
        objects=observed['groups'][g]
        if len(objects)>MAX_OBJECTS or sum(r['bytes'] for r in objects.values())>MAX_BYTES:
            return {**result,'status':'over-budget','candidate':g}
        mark_key=ROOT+g+'/mark.json';mark=observed['marks'].get(mark_key)
        if mark is None:
            result.update(status='mark-candidate',candidate=g,objects=len(objects))
            if apply:
                store.create(mark_key,{'kind':'bha-snapshot-gc-mark','generation':g,'marked_at':now,'objects':objects})
                result.update(status='marked',remote_writes=1)
            return result
        if (mark.get('kind')!='bha-snapshot-gc-mark' or mark.get('generation')!=g
                or type(mark.get('marked_at')) is not int or mark['objects']!=objects):
            raise ValueError('GC mark differs from exact generation')
        if now-mark['marked_at']<=MARK_DELAY:return {**result,'status':'mark-waiting','candidate':g}
        intent_key=ROOT+g+'/delete-intent.json'
        if intent_key in store.inventory():raise ValueError('prior deletion intent requires explicit recovery')
        if not apply:return {**result,'status':'sweep-candidate','candidate':g,'objects':len(objects)}
        fresh=observe(store,token,status=status,now=now)
        if fresh['controls']!=observed['controls'] or g in fresh['protected'] or fresh['groups'][g]!=objects:
            raise ValueError('GC state changed before deletion')
        store.create(intent_key,{'kind':'bha-snapshot-gc-intent','generation':g,'objects':objects,'controls':observed['controls'],'created_at':now})
        for key,row in sorted(objects.items()):
            if status(token)!=observed['status']:raise ValueError('serving state changed before object deletion')
            for path,digest in observed['controls'].items():
                if sha(store.read(path))!=digest:raise ValueError('control root changed before object deletion')
            current_inventory=store.inventory()
            control_paths={p for p in current_inventory if not p.startswith(('generations/',ROOT))}
            if control_paths!=set(observed['controls']):raise ValueError('new control root appeared')
            if current_inventory.get(key)!=row:raise ValueError('candidate object changed')
            store.delete(key,row['etag'])
        remaining=store.inventory()
        if any(k in remaining for k in objects):raise ValueError('deleted snapshot still present')
        for retained in observed['protected']:
            for key,row in observed['groups'].get(retained,{}).items():
                if remaining.get(key)!=row:raise ValueError('retained snapshot changed')
        if status(token)!=observed['status']:raise ValueError('serving changed at deletion acceptance')
        for path,digest in observed['controls'].items():
            if sha(store.read(path))!=digest:raise ValueError('control changed at deletion acceptance')
        store.create(ROOT+g+'/complete.json',{'kind':'bha-snapshot-gc-complete','generation':g,'deleted_objects':len(objects),'finished_at':now})
        return {**result,'status':'deleted','candidate':g,'remote_writes':len(objects)+2,'deleted_objects':len(objects)}
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');p.add_argument('--output',type=Path,required=True);a=p.parse_args(argv)
    if a.apply and (os.environ.get('GITHUB_REPOSITORY')!='anftm/pipeline' or os.environ.get('GITHUB_RUN_ATTEMPT')!='1'):raise ValueError('first central workflow attempt required')
    result=run(Store(),os.environ.get('BHA_REINDEX_TOKEN',''),apply=a.apply)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps(result))

if __name__=='__main__':main()
