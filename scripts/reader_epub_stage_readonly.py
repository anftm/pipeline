"""Independently inspect one journaled EPUB stage; never mutate remote state.

A successful report requires the immutable intent/head, both Reader consumers,
current source content identities, protected roots, and all object hashes.
This does not authorize index publication or prove external-writer isolation.
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
from huggingface_hub import HfApi, HfFileSystem

try:
    from . import reader_epub_immutable as immutable, reader_epub_protection as protection
    from . import reader_epub_stage_journal as journal, reader_format_backlog as backlog, search_snapshot
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import reader_epub_immutable as immutable
    import reader_epub_protection as protection
    import reader_epub_stage_journal as journal
    import reader_format_backlog as backlog
    import search_snapshot
    from reader_bucket_store import HubBucketStore


MAX_FILES=36_000
MAX_TOTAL=3*512*1024*1024
WORKERS=4


def _hash_hub(hub,root,relative,expected):
    immutable._root(root);path=immutable._key(root,relative);immutable._expected(expected)
    from huggingface_hub.utils import get_session,hf_raise_for_status
    from urllib.parse import quote
    token=hub.token(backlog.READER_BUCKET)
    def read():
        hub.pace_resolver()
        url=f"https://huggingface.co/buckets/{backlog.READER_BUCKET}/resolve/{quote(path,safe='/')}"
        with get_session().stream('GET',url,headers={'Authorization':'Bearer '+token} if token else {},
                                  follow_redirects=True,timeout=120) as response:
            if response.status_code==404:raise FileNotFoundError(path)
            hf_raise_for_status(response)
            digest,size=hashlib.sha256(),0
            for chunk in response.iter_bytes():
                size+=len(chunk)
                if size>expected['bytes']:raise ValueError('EPUB staged object exceeds expected size')
                digest.update(chunk)
        if size!=expected['bytes'] or digest.hexdigest()!=expected['sha256']:
            raise ValueError('EPUB staged object differs from exact manifest')
        return {'sha256':digest.hexdigest(),'bytes':size}
    return backlog.retry_read(read)


class HubEpubStageReader:
    def __init__(self,hub):self.hub=hub
    def protection(self,path):
        if not isinstance(path,str) or not protection.PROTECTION.fullmatch(path):
            raise ValueError('EPUB protection key is outside the allowed namespace')
        raw=self.hub.read_bytes(backlog.READER_BUCKET,path)
        protection.validate(path,raw)
        return raw
    def object_identity(self,root,relative,expected):return _hash_hub(self.hub,root,relative,expected)


def inspect(store,operation,*,hub,api,fs,client,reader):
    current=journal.load(store,operation)
    if current is None or current.record['state'] not in {'prepared','write-intent','staged'}:
        raise ValueError('EPUB stage operation is missing or not inspectable')
    plan=current.intent['plan'];preview=current.intent['preview']
    raw_pointer,manifest_raw,corpus,revisions,_=backlog.capture_search(hub)
    pointer=backlog.search_publication_pointer.decode_pointer(raw_pointer)
    if (pointer['desired']!=plan['search_generation'] or
            hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest()!=plan['source_revisions_sha256']):
        raise ValueError('published search sources changed since EPUB staging')
    records=backlog.source_records(corpus,revisions)
    try:
        from . import reader_epub_plan as planning
    except ImportError:
        import reader_epub_plan as planning
    if plan.get('selection_mode')=='explicit-source-keys-v1':
        inspect=planning.inspect_explicit;selection={'source_keys':tuple(row['key'] for row in plan['sources'])}
    else:
        inspect=planning.inspect_one if plan['max_attempts']==1 else planning.inspect
        selection={'offset':plan['selection_offset']} if 'selection_offset' in plan else {}
    proof=inspect(hub=hub,api=api,fs=fs,client=client,shard=plan['shard'],operation=operation,**selection)
    if proof!=plan:
        raise ValueError('fresh EPUB backlog proof differs from durable plan')
    reader_pointer,catalog=backlog.publication.current(hub)
    if reader_pointer is None or reader_pointer['generation']!=plan['reader_generation'] or not catalog.get('base_sidecar'):
        raise ValueError('Reader generation changed since EPUB staging')
    routes=backlog.publication.project_sidecar(backlog.publication.pinned_base(hub,catalog,{}),catalog)['f']
    canon_path='chapters/ebook/epub/index.json';shard_path=f"chapters/ebook/epub/index-{plan['shard']:02d}.json"
    canonical=backlog.matrix.read_json(fs,backlog.READER_BUCKET,canon_path)
    shard=backlog.matrix.read_json(fs,backlog.READER_BUCKET,shard_path)
    if (hashlib.sha256(search_snapshot.canonical(canonical)).hexdigest()!=plan['canonical_index_sha256'] or
            hashlib.sha256(search_snapshot.canonical(shard)).hexdigest()!=plan['shard_index_sha256']):
        raise ValueError('EPUB parent indexes changed since staging')
    if (not isinstance(canonical,dict) or canonical.get('version')!=1
            or canonical.get('kind')!='ebook-chapter-stream-index'
            or not isinstance(canonical.get('files'),list)):
        raise ValueError('EPUB canonical source index is invalid')
    entries={x['key']:x for x in canonical.get('files',[])}
    if len(entries)!=len(canonical.get('files',[])):
        raise ValueError('EPUB canonical index contains duplicates')
    actions={row['key']:row for row in preview['actions']}
    needed=set()
    for source in plan['sources']:
        key=source['key'];record=records.get(key);previous=entries.get(key);action=actions.get(key)
        if not record or record['revision']!=source['source_revision'] or not action:
            raise ValueError('EPUB source no longer matches published search corpus')
        if source['reason']=='content_changed':
            if (not previous or previous.get('source_revision')!=source['previous_revision']
                    or previous.get('source_sha256')!=source['previous_source_sha256']
                    or previous.get('manifest')!=source['previous_manifest']
                    or not backlog._matches_route(previous,routes.get(key))):
                raise ValueError('EPUB old route or source identity changed')
            if source['proof']=='exact-revision-git-blobs':
                needed.add((source['repo'],source['previous_revision'],source['path']))
        elif source['reason']=='not_mapped':
            if previous is not None or key in routes:
                raise ValueError('new EPUB already has a canonical route')
        else:raise ValueError('invalid EPUB staged source reason')
        needed.add((source['repo'],source['source_revision'],source['path']))
    values=backlog._metadata(api,needed)
    for source in plan['sources']:
        key=source['key'];item=values.get((source['repo'],source['source_revision'],source['path']))
        if item is None:raise ValueError('EPUB source metadata is unavailable')
        lfs=backlog._lfs_identity(item);blob=backlog._blob_identity(item)
        if source['reason']=='content_changed' and source['proof']=='current-lfs-sha256':
            if lfs!=source['current_source_sha256'] or lfs!=actions[key]['source_sha256']:
                raise ValueError('EPUB current LFS identity changed')
        elif source['reason']=='content_changed' and source['proof']=='exact-revision-git-blobs':
            old=values.get((source['repo'],source['previous_revision'],source['path']))
            if (lfs is not None or backlog._lfs_identity(old) is not None
                    or blob!=source['current_git_blob'] or backlog._blob_identity(old)!=source['previous_git_blob']):
                raise ValueError('EPUB Git blob identity changed')
        elif source['reason']=='not_mapped':
            if (source.get('proof')!='unmapped-lfs-sha256' or lfs is None
                    or lfs!=source.get('current_source_sha256') or lfs!=actions[key]['source_sha256']):
                raise ValueError('new EPUB lacks independently verified LFS identity')
        else:raise ValueError('invalid EPUB source proof')
    for repo,revision in sorted(revisions.items()):
        info=backlog.retry_read(lambda:api.repo_info(repo,repo_type='dataset',timeout=30))
        if getattr(info,'sha',None)!=revision:raise ValueError('dataset head changed during EPUB stage inspection')
    receipts={}
    for surface in ('pages','hf'):
        observed=backlog.publication.observe_consumer(hub,surface,client)
        if observed.get('generation')!=plan['reader_generation'] or observed.get('projection_verified') is not True:
            raise ValueError('Reader consumer changed during EPUB stage inspection')
        receipts[surface]={'generation':observed['generation'],'projection_verified':True}
    files=[(action,item) for action in preview['actions'] for item in action['files']]
    if len(files)>MAX_FILES or sum(item['bytes'] for _,item in files)>MAX_TOTAL:
        raise ValueError('EPUB staged member inventory exceeds inspection limit')
    protections=[]
    for action in preview['actions']:
        expected=search_snapshot.canonical(protection.make_record(operation,plan['plan_sha256'],action['root'],current.intent['created_at']))
        try:actual=reader.protection(action['protection_path'])
        except FileNotFoundError:actual=None
        protections.append(actual==expected)
    def read_one(pair):
        action,item=pair;expected={'sha256':item['sha256'],'bytes':item['bytes']}
        try:result=reader.object_identity(action['root'],item['path'],expected)
        except FileNotFoundError:return False
        if result!=expected:raise ValueError('EPUB staged object identity differs')
        return True
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        objects=list(pool.map(read_one,files))
    if current.record['state']=='staged' and (not all(protections) or not all(objects)):
        raise ValueError('staged EPUB journal references missing protection or objects')
    for surface in ('pages','hf'):
        observed=backlog.publication.observe_consumer(hub,surface,client)
        if observed.get('generation')!=plan['reader_generation'] or observed.get('projection_verified') is not True:
            raise ValueError('Reader consumer changed during EPUB stage acceptance')
    if (hub.read_bytes(backlog.SEARCH_BUCKET,backlog.CURRENT)!=raw_pointer
            or hub.read_bytes(backlog.SEARCH_BUCKET,f"generations/{pointer['desired']}/manifest.json")!=manifest_raw
            or backlog.publication.current(hub)[0]!=reader_pointer
            or backlog.matrix.read_json(fs,backlog.READER_BUCKET,canon_path)!=canonical
            or backlog.matrix.read_json(fs,backlog.READER_BUCKET,shard_path)!=shard):
        raise ValueError('Reader/search roots changed during EPUB stage inspection')
    after=journal.load(store,operation)
    if after is None or after.head_raw!=current.head_raw or after.etag!=current.etag:
        raise ValueError('EPUB staging journal changed during inspection')
    return {'version':1,'kind':'reader-epub-stage-readonly-observation','operation':operation,
            'state':current.record['state'],'plan_sha256':plan['plan_sha256'],
            'search_generation':plan['search_generation'],'reader_generation':plan['reader_generation'],
            'consumers':receipts,'expected_protections':len(protections),'present_protections':sum(protections),
            'expected_objects':len(objects),'present_objects':sum(objects),'missing_objects':len(objects)-sum(objects),
            'candidate_complete':current.record['state']=='staged' and all(protections) and all(objects),
            'remote_writes':0,'index_written':False,'publication_ready':False,'external_writers_verified':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    if not re.fullmatch(r'[0-9a-f]{32}',args.operation):raise ValueError('invalid EPUB stage operation')
    if args.output.exists() or args.output.is_symlink():raise ValueError('output must be a new path')
    token=os.environ.get('HF_TOKEN');hub=HubBucketStore();store=journal.S3Journal()
    with httpx.Client(timeout=90,follow_redirects=True) as client:
        result=inspect(store,args.operation,hub=hub,api=HfApi(token=token),fs=HfFileSystem(token=token),
                       client=client,reader=HubEpubStageReader(hub))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf8') as stream:
        json.dump(result,stream,ensure_ascii=False,sort_keys=True,indent=2);stream.write('\n')
    print(json.dumps(result,ensure_ascii=False,sort_keys=True),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
