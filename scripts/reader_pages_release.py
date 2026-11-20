"""Request one Pages deployment for an exact Reader/Search pair, without writing Git.

The claim is conditional and durable even if GitHub never returns a response.
Its key deliberately excludes the code SHA: a new code release cannot reopen
an ambiguous request for the same data pair. Unknown outcomes are read only.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

import httpx
import requests

try:
    from . import publish_reader_v3 as reader, search_daily, search_daily_journal
    from . import search_default_receipts as defaults, search_transition as search
    from .reader_bucket_store import S3BucketStore
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    import publish_reader_v3 as reader
    import search_daily
    import search_daily_journal
    import search_default_receipts as defaults
    import search_transition as search
    from reader_bucket_store import S3BucketStore
    from shared import READER_ASSETS_BUCKET

REPO = 'vomebook/search'
API = 'https://api.github.com/repos/' + REPO
LIVE = 'https://vomebook.github.io/search'
STATIC = '.github/workflows/static.yml'
RECEIPT = LIVE + '/data/pages_release_receipt.json'
PREFIX = 'reader-index/gc/pages-reader-releases/'


def hex_id(value, length):
    if not isinstance(value, str) or not re.fullmatch(f'[0-9a-f]{{{length}}}', value) or not int(value, 16):
        raise ValueError('invalid exact release identity')
    return value


def get_json(session, path, *, params=None):
    with session.get(API + path, params=params, timeout=20, allow_redirects=False, stream=True) as r:
        if r.status_code != 200:
            raise RuntimeError(f'Pages metadata HTTP {r.status_code}')
        raw = bytearray()
        for piece in r.iter_content(65536):
            raw.extend(piece)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('oversized Pages metadata')
        return json.loads(raw), bool(r.links.get('next'))


def list_runs(session, *, created=None):
    query = {'per_page': 100, 'branch': 'main', 'event': 'workflow_dispatch'}
    if created:
        query['created'] = '>=' + created
    d, more = get_json(session, '/actions/workflows/static.yml/runs', params=query)
    rows = d['workflow_runs']
    if more or not isinstance(rows, list) or d['total_count'] != len(rows):
        raise ValueError('incomplete Pages run inventory')
    return rows


def matching_run(rows, claim):
    title = 'reader-refresh/' + claim['operation']
    hits = [r for r in rows if r.get('display_title') == title]
    if len(hits) > 1:
        raise ValueError('duplicate Pages runs for the Reader request')
    if not hits:
        return None
    [r] = hits
    if (type(r.get('id')) is not int or r['id'] < 1 or type(r.get('run_attempt')) is not int or r['run_attempt'] != 1
            or r.get('event') != 'workflow_dispatch' or r.get('head_branch') != 'main'
            or r.get('head_sha') != claim['workflow_sha'] or r.get('path', '').split('@')[0] != STATIC
            or r['created_at'] < claim['created']):
        raise ValueError('Pages run does not match the Reader request')
    return r


def validate_live(value, search_generation):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or type(value.get('run_attempt')) is not int or value['run_attempt'] < 1
            or type(value.get('run_id')) is not int or value['run_id'] < 1
            or value.get('search_generation') != search_generation):
        raise ValueError('Pages live code receipt is missing or not on the exact Search generation')
    for k,n in (('code_sha',40),('workflow_sha',40),('reader_generation',64),('reader_catalog_sha256',64)):
        hex_id(value.get(k),n)
    return value


def claim_key(generation, search_generation):
    return PREFIX + hex_id(generation,64) + '/' + hex_id(search_generation,64) + '.json'


def put_claim_once(client, bucket, path, value):
    raw=reader.encode(value)
    # boto client is configured with only one total attempt for this mutation.
    client.put_object(Bucket=bucket, Key=path, Body=raw, ContentType='application/json', IfNoneMatch='*')
    response=client.get_object(Bucket=bucket, Key=path)
    with response['Body'] as stream:
        if stream.read(32769) != raw:
            raise RuntimeError('Reader Pages claim readback differs')


def release(*, generation, session, write_store, read_target, read_live, verify, workflow_sha, pipeline_run,
            clock=time.monotonic, sleep=time.sleep, wait=960, now=None, observe_only=False):
    generation, workflow_sha=hex_id(generation,64),hex_id(workflow_sha,40)
    first=read_target()
    if first['reader_generation']!=generation:
        raise ValueError('Expected Reader generation is not current')
    current=validate_live(read_live(),first['search_generation'])

    def accepted(value):
        verify(generation)
        if read_target()!=first or validate_live(read_live(),first['search_generation'])!=value:
            raise ValueError('Pages evidence or Reader/Search pin changed during acceptance')

    def loaded(value):
        return (value.get('reader_generation'),value.get('reader_catalog_sha256')) == (generation,first['reader_catalog_sha256'])
    if loaded(current):
        accepted(current)
        return {'kind':'reader-pages-release','status':'already-deployed','pages_run':current['run_id'],
                'code_sha':current['code_sha']}
    path=claim_key(generation,first['search_generation'])
    prior=write_store.read(path)
    if prior is None:
        if observe_only:
            raise ValueError('no Reader Pages claim to observe')
        now=now or datetime.now(timezone.utc)
        created=now.strftime('%Y-%m-%dT%H:%M:%SZ')
        request={'reader_code_sha':current['code_sha'],'reader_workflow_sha':workflow_sha,'reader_generation':generation,
                 'reader_catalog_sha256':first['reader_catalog_sha256'],'search_generation':first['search_generation']}
        operation=hashlib.sha256(reader.encode({**request,'created':created,'pipeline_run':pipeline_run})).hexdigest()[:32]
        claim={'version':1,'operation':operation,'created':created,'workflow_sha':workflow_sha,'pipeline_run':pipeline_run,
               'target':first,'request':{**request,'reader_refresh_id':operation}}
        if read_target()!=first or read_live()!=current:
            raise ValueError('Pages evidence or Reader/Search pin moved before dispatch')
        write_store.create(path,claim)
        with session.post(API+'/actions/workflows/static.yml/dispatches', json={'ref':'main','inputs':claim['request']},
                          timeout=25, allow_redirects=False) as result:
            if result.status_code not in (200,204):
                raise RuntimeError('Pages dispatch outcome not accepted; reconcile recorded intent without resending')
    else:
        claim=prior
        if claim.get('version')!=1 or claim.get('target')!=first:
            raise ValueError('recorded Reader Pages claim does not bind these exact pins')
        hex_id(claim.get('operation'),32);hex_id(claim.get('workflow_sha'),40)
        request=claim['request']
        if (set(request)!={'reader_refresh_id','reader_code_sha','reader_workflow_sha','reader_generation',
                          'reader_catalog_sha256','search_generation'}
                or request['reader_refresh_id']!=claim['operation'] or request['reader_workflow_sha']!=claim['workflow_sha']
                or request['reader_generation']!=generation or request['reader_catalog_sha256']!=first['reader_catalog_sha256']
                or request['search_generation']!=first['search_generation']):
            raise ValueError('invalid recorded Reader Pages claim')
        hex_id(request['reader_code_sha'],40)
    deadline=clock()+wait
    while True:
        r=matching_run(list_runs(session,created=claim['created']),claim)
        if r and r.get('status')=='completed':
            if r.get('conclusion')!='success':
                raise RuntimeError('claimed Pages run failed; inspect before any recovery')
            if read_target()!=first:
                raise ValueError('Reader or Search pin changed since dispatch')
            actual=validate_live(read_live(),first['search_generation'])
            if (actual['run_id']!=r['id'] or actual['run_attempt']!=1 or actual.get('reader_refresh_id')!=claim['operation']
                    or actual['code_sha']!=claim['request']['reader_code_sha'] or actual['workflow_sha']!=claim['workflow_sha']
                    or not loaded(actual)):
                raise ValueError('Pages public receipt does not match the claimed request')
            accepted(actual)
            return {'kind':'reader-pages-release','status':'deployed','operation':claim['operation'],'pages_run':r['id'],
                    'code_sha':actual['code_sha']}
        if clock()>=deadline:
            raise TimeoutError('Reader Pages outcome unresolved; inspect the existing claim without redispatch')
        sleep(min(12,max(0,deadline-clock())))


class Claims:
    def __init__(self):
        import boto3
        from botocore.config import Config
        self.reader=S3BucketStore()
        namespace,self.bucket=self.reader._location(READER_ASSETS_BUCKET)
        self.writer=boto3.client('s3',endpoint_url=f'https://s3.hf.co/{namespace}',
            aws_access_key_id=os.environ['HF_S3_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['HF_S3_SECRET_ACCESS_KEY'],
            config=Config(region_name='us-east-1',s3={'addressing_style':'path'},connect_timeout=10,read_timeout=20,
                          retries={'total_max_attempts':1}))

    def read(self,path):
        try: raw=self.reader.read_bytes(READER_ASSETS_BUCKET,path)
        except FileNotFoundError:return None
        if not 0<len(raw)<=32768:raise ValueError('invalid Reader Pages claim size')
        return json.loads(raw)

    def create(self,path,value):
        put_claim_once(self.writer,self.bucket,path,value)


def session_for_pages():
    session=requests.Session()
    session.headers.update({'Authorization':'Bearer '+os.environ['PAGES_TOKEN'],
                            'Accept':'application/vnd.github+json','Cache-Control':'no-cache',
                            'User-Agent':'reader-pages-release'})
    return session


def pages_preflight(session, generation):
    ref,_=get_json(session,'/git/ref/heads/main')
    if ref.get('ref')!='refs/heads/main' or ref['object'].get('type')!='commit':
        raise ValueError('Pages default branch is not a usable workflow ref')
    sha=hex_id(ref['object']['sha'],40)
    flow,_=get_json(session,'/actions/workflows/static.yml')
    if (flow.get('state'),flow.get('path'))!=('active',STATIC):
        raise ValueError('Pages publisher workflow is not active on its known path')
    blob,_=get_json(session,'/contents/'+STATIC,params={'ref':sha})
    import yaml
    definition=yaml.load(base64.b64decode(blob['content']),Loader=yaml.BaseLoader)
    inputs=definition.get('on',{}).get('workflow_dispatch',{}).get('inputs',{})
    for k in ('reader_refresh_id','reader_code_sha','reader_workflow_sha','reader_generation',
              'reader_catalog_sha256','search_generation'):
        if inputs.get(k,{}).get('type')!='string':
            raise ValueError('Pages main does not support the pinned Reader contract')
    validate_live(json.loads(search_daily.bounded_get(RECEIPT)),generation)
    defaults.observe_pages(generation,session=session)
    return sha


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--preflight',action='store_true')
    args=parser.parse_args()
    if (os.environ.get('GITHUB_REPOSITORY')!='anftm/pipeline' or os.environ.get('GITHUB_REF')!='refs/heads/main'
            or os.environ.get('GITHUB_WORKFLOW_REF')!='anftm/pipeline/.github/workflows/reader-v3-publish.yml@refs/heads/main'
            or os.environ.get('GITHUB_RUN_ATTEMPT')!='1'):
        raise ValueError('requires exact central first-attempt workflow')
    if args.preflight:
        if args.publication or args.output:
            parser.error('--preflight is a separate read-only check')
        with session_for_pages() as session:
            search_live=search_daily.inspect_published()
            observed=search_daily_journal.inspect(search.S3TransitionStore())
            if search_live['generation']!=observed['generation']:
                raise ValueError('Search live consumers differ from the settled journal')
            print('Verified Pages workflow contract',pages_preflight(session,observed['generation']))
        return
    if not args.publication or not args.output or args.output.exists():
        parser.error('requires publication receipt and new output')
    result=json.loads(args.publication.read_bytes())
    if result.get('applied') is not True:raise ValueError('Reader publication did not apply')
    generation=hex_id(result.get('generation'),64)
    store=reader.CentralHubStore();reader.assert_writer(store)
    journal=search.S3TransitionStore()
    p=os.environ['GITHUB_RUN_ID']; assert p.isascii() and p.isdigit()
    with session_for_pages() as session,httpx.Client(timeout=40,follow_redirects=True) as client:
        initial=search_daily_journal.inspect(journal)
        def live():
            return json.loads(search_daily.bounded_get(RECEIPT))
        def target():
            state=search.load(journal)
            if (state is None or state.record['pending'] is not None
                    or hashlib.sha256(state.head_raw).hexdigest()!=initial['head_sha256']
                    or hashlib.sha256(journal.read_pointer()).hexdigest()!=initial['pointer_sha256']):
                raise ValueError('Search journal or pointer changed during Reader Pages publication')
            pointer,catalog=reader.current(store)
            if not pointer or not catalog.get('base_sidecar'):
                raise ValueError('current Reader catalog does not have a pinned base')
            defaults.observe_pages(initial['generation'],session=session)
            return {'reader_generation':pointer['generation'],'reader_catalog_sha256':pointer['catalog']['sha256'],
                    'search_generation':initial['generation'],'search_head':initial['head_sha256']}
        def verify(g):
            d=reader.verify_generation(store,client,g,timeout_seconds=120)
            if not d.get('complete'):raise ValueError('both Reader projections were not independently verified')
            if search_daily.inspect_published()['generation']!=initial['generation']:
                raise ValueError('live Search consumers do not match the pinned generation')
            return d
        sha=pages_preflight(session,initial['generation'])
        proof=release(generation=generation,session=session,write_store=Claims(),read_target=target,read_live=live,
                      verify=verify,workflow_sha=sha,pipeline_run=p)
    with args.output.open('x') as f:json.dump(proof,f,sort_keys=True)
    print('Verified Pages Reader publication',proof['status'],proof['pages_run'],flush=True)


if __name__=='__main__':
    main()
