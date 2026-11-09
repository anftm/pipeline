"""Convert a deterministic window of unmapped JPG or MP3 sources locally.

The window is a snapshot, not an ingestion cursor or publication. Every source
is pinned to a verified LFS/Git identity and bounded to 16 MiB. Converted
files and exact source plans are retained without touching production buckets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

import httpx
from huggingface_hub import HfApi, HfFileSystem

try:
    from . import reader_threshold_autobuild as conversion, search_snapshot
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import reader_threshold_autobuild as conversion
    import search_snapshot
    from reader_bucket_store import HubBucketStore


def _sha(value):return hashlib.sha256(search_snapshot.canonical(value)).hexdigest()


def _write(path, value):
    with path.open('x',encoding='utf8') as stream:
        json.dump(value,stream,ensure_ascii=False,sort_keys=True,indent=2);stream.write('\n')


def run(extension, *, hub, api, fs, client, start, window, output, token=None, fetch=None, convert=None):
    if output.exists() or output.is_symlink():raise ValueError('unmapped conversion output already exists')
    if extension=='mp3' and not shutil.which('ffprobe'):
        raise ValueError('MP3 conversion requires ffprobe')
    plan=conversion.inspect_unmapped(extension,hub=hub,api=api,fs=fs,client=client,
                                     start=start,window=window,limit=100)
    if not plan.get('sources') or len(plan['sources'])>100:raise ValueError('unmapped conversion plan out of bounds')
    output.mkdir(parents=True,mode=0o700);_write(output/'plan.json',plan)
    rows=[]
    for number, source in enumerate(plan['sources']):
        subplan={**plan,'sources':[source]};folder=output/f'item-{number:03d}'
        try:
            kwargs={'token':token}
            if fetch is not None:kwargs['fetch']=fetch
            if convert is not None:kwargs['convert']=convert
            report=conversion.build_unmapped(subplan,folder,**kwargs)
            _write(folder/'report.json',report)
            rows.append({'index':number,'key':source['key'],'state':'converted-local',
                         'output':report['sources'][0]['output'],'error_type':None})
        except Exception as error:
            rows.append({'index':number,'key':source['key'],'state':'failed-local',
                         'output':None,'error_type':type(error).__name__})
    error_type=None
    try:
        later=conversion.inspect_unmapped(extension,hub=hub,api=api,fs=fs,client=client,
                                           start=start,window=window,limit=100)
        verified=later==plan
    except Exception as error:
        verified=False;error_type=type(error).__name__
    result={'version':1,'kind':'reader-unmapped-local-batch-results','extension':extension,
            'search_generation':plan['search_generation'],'reader_generation':plan['reader_generation'],
            'selection_window':plan['selection_window'],'plan_sha256':_sha(plan),'snapshot_revalidated':verified,
            'revalidation_error_type':error_type,'selected_sources':len(rows),
            'converted_sources':sum(row['state']=='converted-local' for row in rows),
            'failed_sources':sum(row['state']=='failed-local' for row in rows),'rows':rows,
            'remote_writes':0,'index_written':False,'publication_ready':False,'external_writers_verified':False}
    result['results_sha256']=_sha(result);_write(output/'results.json',result)
    if not verified:raise ValueError('unmapped source, Reader or index changed during conversion')
    return result


def export(output, destination):
    if destination.exists() or destination.is_symlink():raise ValueError('unmapped conversion export already exists')
    plan=json.loads((output/'plan.json').read_bytes());result=json.loads((output/'results.json').read_bytes())
    if (result.get('kind')!='reader-unmapped-local-batch-results' or result.get('snapshot_revalidated') is not True
            or result.get('remote_writes')!=0 or result.get('publication_ready') is not False
            or result.get('plan_sha256')!=_sha(plan)
            or result.get('results_sha256')!=_sha({k:v for k,v in result.items() if k!='results_sha256'})):
        raise ValueError('unmapped conversion result is incomplete or altered')
    if len(result.get('rows',[]))!=len(plan['sources']):raise ValueError('unmapped result coverage differs')
    target=destination/'reader-converted';target.mkdir(parents=True,mode=0o700)
    _write(target/'plan.json',plan);_write(target/'results.json',result)
    for number,(source,row) in enumerate(zip(plan['sources'],result['rows'])):
        if row.get('index')!=number or row.get('key')!=source['key']:raise ValueError('unmapped conversion source differs')
        if row['state']=='failed-local':continue
        if row['state']!='converted-local':raise ValueError('unknown unmapped conversion status')
        folder=output/f'item-{number:03d}';subplan={**plan,'sources':[source]}
        report=json.loads((folder/'report.json').read_bytes())
        name='document.webp' if plan['extension']=='jpg' else 'audio.mp3'
        native=folder/'0'/('source.'+plan['extension']);converted=folder/'0'/name
        digest=conversion._source_digest(native,source);identity=conversion._converted_file(converted)
        if (report.get('plan_sha256')!=_sha(subplan) or report.get('remote_writes')!=0
                or report.get('publication_ready') is not False or len(report.get('sources',[]))!=1
                or report['sources'][0].get('key')!=source['key']
                or report['sources'][0].get('source_sha256')!=digest
                or report['sources'][0].get('output')!=identity or row['output']!=identity):
            raise ValueError('unmapped local conversion changed before export')
        item=target/f'item-{number:03d}';item.mkdir();_write(item/'report.json',report)
        shutil.copyfile(converted,item/name)
    return {'kind':'reader-unmapped-local-batch-export','extension':result['extension'],
            'converted_sources':result['converted_sources'],'failed_sources':result['failed_sources'],
            'remote_writes':0,'publication_ready':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extension',choices=('jpg','mp3'),required=True)
    parser.add_argument('--start',type=int,default=0);parser.add_argument('--window',type=int,default=100)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--export',type=Path,required=True)
    args=parser.parse_args(argv);token=os.environ.get('HF_TOKEN')
    with httpx.Client(timeout=90,follow_redirects=True) as client:
        result=run(args.extension,hub=HubBucketStore(),api=HfApi(token=token),fs=HfFileSystem(token=token),
                   client=client,start=args.start,window=args.window,output=args.output,token=token)
    printed=export(args.output,args.export);print(json.dumps(printed,sort_keys=True),flush=True)
    return 1 if result['failed_sources'] else 0


if __name__=='__main__':raise SystemExit(main())
