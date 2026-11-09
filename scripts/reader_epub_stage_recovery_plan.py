"""Read-only exact recovery inventory for an interrupted EPUB stage.

This command never resumes writes. Missing protection/objects are identified
from the durable write-intent and bounded independent reads; a matching stored
object is not inferred from a path or a prior upload response. Safe resumption
still requires a separately persisted per-object intent and writer coordination.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re

import httpx
from huggingface_hub import HfApi,HfFileSystem

try:
    from . import reader_epub_plan as planning,reader_epub_protection as protection
    from . import reader_epub_stage_journal as journal,reader_epub_stage_readonly as readonly,search_snapshot
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import reader_epub_plan as planning
    import reader_epub_protection as protection
    import reader_epub_stage_journal as journal
    import reader_epub_stage_readonly as readonly
    import search_snapshot
    from reader_bucket_store import HubBucketStore


def inspect(store,operation,*,hub,api,fs,client,reader):
    current=journal.load(store,operation)
    if current is None or current.record['state']!='write-intent':
        raise ValueError('EPUB stage has no unresolved write intent')
    plan=current.intent['plan'];preview=current.intent['preview']
    if planning.inspect(hub=hub,api=api,fs=fs,client=client,shard=plan['shard'],operation=operation)!=plan:
        raise ValueError('EPUB source or Reader state changed before recovery inventory')
    actions=preview['actions'];protect_rows=[]
    for action in actions:
        expected=search_snapshot.canonical(protection.make_record(operation,plan['plan_sha256'],action['root'],current.intent['created_at']))
        try:raw=reader.protection(action['protection_path'])
        except FileNotFoundError:raw=None
        if raw is not None and raw!=expected:raise ValueError('EPUB protection conflicts with durable intent')
        protect_rows.append({'path':action['protection_path'],'state':'missing' if raw is None else 'verified'})
    files=[(action,item) for action in actions for item in action['files']]
    if len(files)>readonly.MAX_FILES or sum(item['bytes'] for _,item in files)>readonly.MAX_TOTAL:
        raise ValueError('EPUB recovery inventory exceeds bounds')
    def one(pair):
        action,item=pair;expected={'sha256':item['sha256'],'bytes':item['bytes']}
        try:actual=reader.object_identity(action['root'],item['path'],expected)
        except FileNotFoundError:actual=None
        if actual is not None and actual!=expected:raise ValueError('EPUB object conflicts with durable intent')
        return {'path':item['remote'],'sha256':item['sha256'],'bytes':item['bytes'],
                'state':'missing' if actual is None else 'verified'}
    with ThreadPoolExecutor(max_workers=readonly.WORKERS) as pool:
        object_rows=list(pool.map(one,files))
    if planning.inspect(hub=hub,api=api,fs=fs,client=client,shard=plan['shard'],operation=operation)!=plan:
        raise ValueError('EPUB source or Reader state changed during recovery inventory')
    active=journal.load(store,operation)
    if active is None or active.head_raw!=current.head_raw or active.etag!=current.etag:
        raise ValueError('EPUB staging journal changed during recovery inventory')
    result={'version':1,'kind':'reader-epub-stage-recovery-plan','operation':operation,
            'journal_head_sha256':hashlib.sha256(current.head_raw).hexdigest(),
            'journal_etag_sha256':hashlib.sha256(current.etag.encode()).hexdigest(),
            'intent_sha256':current.record['intent_sha256'],'plan_sha256':plan['plan_sha256'],
            'staging_sha256':preview['staging_sha256'],'protections':protect_rows,'objects':object_rows,
            'missing_protections':sum(row['state']=='missing' for row in protect_rows),
            'missing_objects':sum(row['state']=='missing' for row in object_rows),
            'remote_writes':0,'resume_authorized':False,'publication_ready':False,'external_writers_verified':False}
    result['recovery_plan_sha256']=hashlib.sha256(search_snapshot.canonical(result)).hexdigest()
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation',required=True);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    if not re.fullmatch(r'[0-9a-f]{32}',args.operation):raise ValueError('invalid EPUB stage operation')
    if args.output.exists() or args.output.is_symlink():raise ValueError('recovery inventory output must be new')
    token=os.environ.get('HF_TOKEN');hub=HubBucketStore()
    with httpx.Client(timeout=90,follow_redirects=True) as client:
        result=inspect(journal.S3Journal(),args.operation,hub=hub,api=HfApi(token=token),
                       fs=HfFileSystem(token=token),client=client,reader=readonly.HubEpubStageReader(hub))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf8') as out:json.dump(result,out,ensure_ascii=False,sort_keys=True,indent=2);out.write('\n')
    print(json.dumps({'kind':result['kind'],'operation':args.operation,
                      'missing_protections':result['missing_protections'],'missing_objects':result['missing_objects'],
                      'remote_writes':0,'resume_authorized':False},sort_keys=True),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
