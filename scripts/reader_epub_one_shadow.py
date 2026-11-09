"""Build and bind one exact changed EPUB for an independently reviewed stage.

This read-only shadow downloads one immutable source, verifies its actual
content identity, produces bounded chapter files, and repeats source/consumer
inspection. It does not upload Reader objects or mutate any index or journal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets

import httpx
from huggingface_hub import HfApi,HfFileSystem

try:
    from . import reader_epub_plan as planning,reader_epub_shadow as shadow,search_snapshot
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import reader_epub_plan as planning
    import reader_epub_shadow as shadow
    import search_snapshot
    from reader_bucket_store import HubBucketStore


def build(*,hub,api,fs,client,shard,operation,output:Path,token=None):
    if output.exists() or output.is_symlink():raise ValueError('EPUB one-source output already exists')
    plan=planning.inspect_one(hub=hub,api=api,fs=fs,client=client,shard=shard,operation=operation)
    if len(plan['sources'])!=1 or plan['max_attempts']!=1:
        raise ValueError('EPUB plan must select exactly one source')
    report=shadow.build(plan,output,token=token)
    staging=shadow.staging_plan(plan,report,output)
    if planning.inspect_one(hub=hub,api=api,fs=fs,client=client,shard=shard,operation=operation)!=plan:
        raise ValueError('EPUB source or Reader state changed during one-source build')
    bundle_bytes=sum(row['bytes'] for action in staging['actions'] for row in action['files'])
    summary={'version':1,'kind':'reader-epub-one-shadow','operation':operation,'shard':shard,
             'plan_sha256':plan['plan_sha256'],'staging_sha256':staging['staging_sha256'],
             'search_generation':plan['search_generation'],'reader_generation':plan['reader_generation'],
             'source_count':1,'chapter_count':report['sources'][0]['chapter_count'],
             'object_count':sum(len(action['files']) for action in staging['actions']),
             'bundle_bytes':bundle_bytes,'remote_writes':0,'index_written':False,
             'publication_ready':False,'external_writers_verified':False}
    summary['summary_sha256']=hashlib.sha256(search_snapshot.canonical(summary)).hexdigest()
    for name,value in (('plan.json',plan),('report.json',report),('staging-preview.json',staging),('summary.json',summary)):
        with (output/name).open('x',encoding='utf8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True,indent=2);stream.write('\n')
    return summary


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard',required=True,type=int);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    if not 0<=args.shard<8:raise ValueError('EPUB shard must be between 0 and 7')
    token=os.environ.get('HF_TOKEN');operation=secrets.token_hex(16)
    # Print the operation before any local work. It is not an authorization or
    # a remote intent; later production staging must create its own journal.
    print(json.dumps({'kind':'reader-epub-one-shadow-start','operation':operation,'shard':args.shard}),flush=True)
    with httpx.Client(timeout=90,follow_redirects=True) as client:
        result=build(hub=HubBucketStore(),api=HfApi(token=token),fs=HfFileSystem(token=token),
                     client=client,shard=args.shard,operation=operation,output=args.output,token=token)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
