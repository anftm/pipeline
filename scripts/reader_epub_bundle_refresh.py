"""Refresh one retained EPUB bundle against current, explicitly selected sources.

Only source/consumer/index reads and local output writes. The prior bundle is
never modified; no native book, bucket object, index, or pointer is created.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

import httpx
from huggingface_hub import HfApi,HfFileSystem

try:
    from . import reader_epub_plan as planning,reader_epub_revalidate as live,reader_epub_shadow as shadow
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import reader_epub_plan as planning
    import reader_epub_revalidate as live
    import reader_epub_shadow as shadow
    from reader_bucket_store import HubBucketStore


def _json(path):
    if path.is_symlink() or not path.is_file() or not 0<path.stat().st_size<=8*1024*1024:
        raise ValueError('EPUB retained metadata is missing or oversized')
    data=json.loads(path.read_bytes())
    if not isinstance(data,dict):raise ValueError('EPUB retained metadata must be an object')
    return data


def refresh(retained,output,*,hub,api,fs,client):
    if output.exists() or output.is_symlink():raise ValueError('EPUB refresh output must be new')
    if retained.is_symlink() or not retained.is_dir():raise ValueError('EPUB retained bundle must be an ordinary directory')
    old_plan=_json(retained/'plan.json');old_report=_json(retained/'report.json');old_preview=_json(retained/'staging-preview.json')
    shadow._verify_plan(old_plan)
    old_intent={'version':1,'kind':'reader-epub-staging-intent','operation':old_plan['operation'],
                'created_at':'2000-01-01T00:00:00Z','plan':old_plan,'preview':old_preview}
    try:
        from . import reader_epub_stage_journal as journal,search_snapshot
    except ImportError:
        import reader_epub_stage_journal as journal
        import search_snapshot
    journal._intent(search_snapshot.canonical(old_intent))
    if len(old_plan['sources'])!=1:raise ValueError('refresh accepts exactly one retained source')
    plan=planning.inspect_explicit(hub=hub,api=api,fs=fs,client=client,shard=old_plan['shard'],
                                   operation=old_plan['operation'],source_keys=(old_plan['sources'][0]['key'],))
    if plan['sources']!=old_plan['sources']:
        raise ValueError('retained EPUB source or prior route no longer matches fresh selection')
    def check(value):return live.revalidate_staging_plan(value,hub=hub,api=api,fs=fs,client=client)
    report={**old_report,'plan_sha256':plan['plan_sha256'],
            'search_generation':plan['search_generation'],'reader_generation':plan['reader_generation']}
    preview=shadow.bound_staging_plan(plan,report,retained,validate_source=check)
    if preview['actions']!=old_preview['actions']:
        raise ValueError('retained EPUB objects or protection identity changed before refresh')
    output.mkdir(parents=True,mode=0o700)
    for name,value in (('plan.json',plan),('report.json',report),('staging-preview.json',preview)):
        with (output/name).open('x',encoding='utf8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True,indent=2);stream.write('\n')
    source_bundle=retained/'0'/'bundle';destination=output/'0'/'bundle'
    shutil.copytree(source_bundle,destination,symlinks=False)
    # Rehash the copied files and repeat the live plan after all copies.
    if shadow.bound_staging_plan(plan,report,output,validate_source=check)!=preview:
        raise ValueError('refreshed EPUB copy differs from exact bundle')
    return {'kind':'reader-epub-retained-bundle-refresh','plan_sha256':plan['plan_sha256'],
            'staging_sha256':preview['staging_sha256'],'source_count':1,
            'objects':len(preview['actions'][0]['files']),
            'chapters':report['sources'][0]['chapter_count'],'remote_writes':0,
            'index_written':False,'publication_ready':False,'external_writers_verified':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retained',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv);token=os.environ.get('HF_TOKEN')
    with httpx.Client(timeout=90,follow_redirects=True) as client:
        result=refresh(args.retained,args.output,hub=HubBucketStore(),api=HfApi(token=token),
                       fs=HfFileSystem(token=token),client=client)
    print(json.dumps(result,sort_keys=True),flush=True);return 0


if __name__=='__main__':raise SystemExit(main())
