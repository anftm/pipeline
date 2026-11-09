"""Build disjoint bounded EPUB batches and retain verified chapter bundles.

This is local conversion, not publication. It captures the live source/Reader
snapshot before work, independently repeats it after, stores individual failure
receipts, and never writes a Reader bucket, index, site, or journal. Source EPUB
bytes are local temporary inputs; export packages contain converted files only.
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
    from . import reader_epub_plan as planning, reader_epub_shadow as shadow, search_snapshot
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import reader_epub_plan as planning
    import reader_epub_shadow as shadow
    import search_snapshot
    from reader_bucket_store import HubBucketStore


def _write(path, value):
    with path.open('x', encoding='utf8') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')


def _sha(value):
    return hashlib.sha256(search_snapshot.canonical(value)).hexdigest()


def run(*, hub, api, fs, client, shard, output, start=0, max_batches=12, token=None, fetch=None, parse=None):
    if output.exists() or output.is_symlink():
        raise ValueError('EPUB batch output must be a new directory')
    plans = planning.inspect_batches(hub=hub, api=api, fs=fs, client=client, shard=shard,
                                     start=start, max_batches=max_batches)
    if not plans:
        raise ValueError('EPUB batch contains no exact plans')
    selections = [(p.get('selection_offset', 0), p['operation']) for p in plans]
    output.mkdir(parents=True, mode=0o700)
    _write(output/'plans.json', {'version':1, 'kind':'reader-epub-local-batch-plans',
                                'plans':plans, 'remote_writes':0, 'publication_ready':False})
    rows = []
    for plan in plans:
        offset = plan.get('selection_offset', 0)
        # Give every source an independent, journal-compatible exact plan.
        # A damaged book cannot prevent later members of the same range.
        for number, source in enumerate(plan['sources']):
            source_offset = offset + number
            member_operation = hashlib.sha256((plan['operation'] + ':' + str(source_offset)).encode('ascii')).hexdigest()[:32]
            body = {**plan, 'operation':member_operation, 'sources':[source], 'max_attempts':1}
            body.pop('plan_sha256')
            if source_offset:
                body['selection_offset'] = source_offset
            else:
                body.pop('selection_offset', None)
            body['plan_sha256'] = _sha(body)
            directory = output/f'batch-{source_offset:06d}'
            _write(output/f'batch-{source_offset:06d}-plan.json', body)
            try:
                kwargs = {'token':token}
                if fetch is not None: kwargs['fetch'] = fetch
                if parse is not None: kwargs['parse'] = parse
                report = shadow.build(body, directory, **kwargs)
                preview = shadow.staging_plan(body, report, directory)
                _write(directory/'report.json', report)
                _write(directory/'staging-preview.json', preview)
                rows.append({'offset':source_offset, 'operation':body['operation'], 'plan_sha256':body['plan_sha256'],
                             'state':'converted-local', 'source_count':1,
                             'chapter_count':report['sources'][0]['chapter_count'],
                             'object_count':len(report['sources'][0]['files']),
                             'bundle_bytes':sum(item['bytes'] for item in report['sources'][0]['files']),
                             'staging_sha256':preview['staging_sha256'], 'error_type':None})
            except Exception as error:
                # Preserve other books and their exact plans; never replace a
                # failure with an inferred success or publish partial outputs.
                rows.append({'offset':source_offset, 'operation':body['operation'], 'plan_sha256':body['plan_sha256'],
                             'state':'failed-local', 'source_count':1,
                             'chapter_count':0, 'object_count':0, 'bundle_bytes':0,
                             'staging_sha256':None, 'error_type':type(error).__name__})
                _write(output/f'batch-{source_offset:06d}-failure.json', rows[-1])
    revalidation_error = None
    try:
        later = planning.inspect_batches(hub=hub, api=api, fs=fs, client=client, shard=shard,
                                          selections=selections)
        stable = later == plans
    except Exception as error:
        stable = False
        revalidation_error = type(error).__name__
    result = {'version':1, 'kind':'reader-epub-local-batch-results', 'shard':shard,
              'search_generation':plans[0]['search_generation'], 'reader_generation':plans[0]['reader_generation'],
              'plans_sha256':_sha(plans), 'snapshot_revalidated':stable,
              'revalidation_error_type':revalidation_error, 'batches':rows,
              'selected_sources':sum(row['source_count'] for row in rows),
              'converted_sources':sum(row['source_count'] for row in rows if row['state']=='converted-local'),
              'failed_sources':sum(row['source_count'] for row in rows if row['state']=='failed-local'),
              'chapters':sum(row['chapter_count'] for row in rows),
              'objects':sum(row['object_count'] for row in rows),
              'bundle_bytes':sum(row['bundle_bytes'] for row in rows),
              'remote_writes':0, 'index_written':False, 'publication_ready':False, 'external_writers_verified':False}
    result['results_sha256'] = _sha(result)
    _write(output/'results.json', result)
    if not stable:
        raise ValueError('EPUB source, Reader or index changed during local batch conversion')
    return result


def recheck(output, *, hub, api, fs, client, destination):
    """Independently recheck existing conversions after a read-only timeout.

    The original report is immutable; a new receipt records exactly which
    plans were re-read and never implies that an object was staged remotely.
    """
    if destination.exists() or destination.is_symlink():
        raise ValueError('EPUB recheck receipt must be a new file')
    plans_value = json.loads((output/'plans.json').read_bytes())
    result = json.loads((output/'results.json').read_bytes())
    plans = plans_value.get('plans')
    if (plans_value.get('kind')!='reader-epub-local-batch-plans' or plans_value.get('remote_writes')!=0
            or not isinstance(plans,list) or not plans or result.get('kind')!='reader-epub-local-batch-results'
            or result.get('remote_writes')!=0 or result.get('publication_ready') is not False
            or result.get('results_sha256')!=_sha({k:v for k,v in result.items() if k!='results_sha256'})
            or result.get('plans_sha256')!=_sha(plans)):
        raise ValueError('EPUB batch inputs or results changed before recheck')
    selections=[(p.get('selection_offset',0),p['operation']) for p in plans]
    later=planning.inspect_batches(hub=hub,api=api,fs=fs,client=client,shard=result['shard'],selections=selections)
    if later!=plans:
        raise ValueError('EPUB batch source, Reader or index changed during recheck')
    value={'version':1,'kind':'reader-epub-local-batch-recheck','results_sha256':result['results_sha256'],
           'plans_sha256':result['plans_sha256'],'shard':result['shard'],'snapshot_revalidated':True,
           'remote_writes':0,'index_written':False,'publication_ready':False,'external_writers_verified':False}
    value['recheck_sha256']=_sha(value)
    destination.parent.mkdir(parents=True,exist_ok=True)
    _write(destination,value)
    return value


def export_converted(output, destination, *, recheck_receipt=None):
    """Copy only verified conversions; reject missing, extra or altered files.

    This intentionally does not copy the source EPUB. The export is a local
    artifact, not an authorization or evidence that a bucket write happened.
    """
    if destination.exists() or destination.is_symlink():
        raise ValueError('EPUB bundle export destination must be new')
    result = json.loads((output/'results.json').read_bytes())
    if (result.get('kind')!='reader-epub-local-batch-results'
            or result.get('remote_writes')!=0 or result.get('publication_ready') is not False
            or result.get('results_sha256')!=_sha({k:v for k,v in result.items() if k!='results_sha256'})):
        raise ValueError('EPUB batch result is incomplete or changed')
    receipt = None
    if result.get('snapshot_revalidated') is not True:
        if recheck_receipt is None:
            raise ValueError('EPUB batch requires a fresh independent recheck')
        receipt = json.loads(recheck_receipt.read_bytes())
        if (receipt.get('kind')!='reader-epub-local-batch-recheck' or receipt.get('snapshot_revalidated') is not True
                or receipt.get('results_sha256')!=result['results_sha256'] or receipt.get('plans_sha256')!=result['plans_sha256']
                or receipt.get('shard')!=result['shard'] or receipt.get('remote_writes')!=0
                or receipt.get('publication_ready') is not False
                or receipt.get('recheck_sha256')!=_sha({k:v for k,v in receipt.items() if k!='recheck_sha256'})):
            raise ValueError('EPUB batch recheck does not match exact results')
    package = destination/'epub-converted'
    package.mkdir(parents=True, mode=0o700)
    exported = []
    for row in result['batches']:
        if row['state']!='converted-local':
            continue
        offset = row['offset']; batch = output/f'batch-{offset:06d}'
        plan = json.loads((output/f'batch-{offset:06d}-plan.json').read_bytes())
        report = json.loads((batch/'report.json').read_bytes())
        preview = json.loads((batch/'staging-preview.json').read_bytes())
        if (plan['plan_sha256']!=row['plan_sha256'] or preview['staging_sha256']!=row['staging_sha256']
                or shadow.staging_plan(plan, report, batch)!=preview):
            raise ValueError('EPUB local bundle changed before export')
        target = package/f'batch-{offset:06d}'
        target.mkdir()
        _write(target/'plan.json',plan);_write(target/'report.json',report);_write(target/'staging-preview.json',preview)
        for number in range(len(plan['sources'])):
            shutil.copytree(batch/str(number)/'bundle', target/str(number)/'bundle', symlinks=False)
        exported.append(offset)
    _write(package/'results.json',result)
    if receipt is not None:_write(package/'recheck.json',receipt)
    return {'kind':'reader-epub-local-batch-export', 'exported_batches':len(exported),
            'exported_sources':result['converted_sources'], 'objects':result['objects'],
            'bundle_bytes':result['bundle_bytes'], 'remote_writes':0, 'publication_ready':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard',required=True,type=int);parser.add_argument('--start',type=int,default=0)
    parser.add_argument('--max-batches',type=int,default=12);parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--export',required=True,type=Path)
    args=parser.parse_args(argv);token=os.environ.get('HF_TOKEN')
    with httpx.Client(timeout=90,follow_redirects=True) as client:
        result=run(hub=HubBucketStore(),api=HfApi(token=token),fs=HfFileSystem(token=token),client=client,
                   shard=args.shard,output=args.output,start=args.start,max_batches=args.max_batches,token=token)
    exported=export_converted(args.output,args.export)
    print(json.dumps({**exported, 'failed_sources':result['failed_sources'], 'chapters':result['chapters'],
                      'snapshot_revalidated':result['snapshot_revalidated']},sort_keys=True),flush=True)
    return 1 if result['failed_sources'] else 0


if __name__=='__main__':raise SystemExit(main())
