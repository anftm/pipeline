"""Seal historical Web hints in the Reader publisher, without releasing pins."""
import argparse
import json
import os
from pathlib import Path
import re

import httpx

from . import publish_reader_v3 as v3, reader_gc_retirement as retirement
from .reader_format_daily import GitHub
from .reader_gc_fence import GitHubProducerFence
from .reader_gc_maintenance import DURABLE_JOURNAL
from .reader_image_history_fence import _active_owner, _owner
from .reader_image_history_store import S3ClaimStore
from .reader_web_batch_cli import live
from .reader_web_profile_readback import record_hint


BUCKET = v3.ASSETS


def parse(payload):
    if (not isinstance(payload,dict) or set(payload)!= {'generation','rows','hints'}
            or not isinstance(payload['generation'],str) or not re.fullmatch(r'[a-f0-9]{64}',payload['generation'])
            or not isinstance(payload['rows'],list) or not 1 <= len(payload['rows']) <= 12
            or not isinstance(payload['hints'],list) or not 1 <= len(payload['hints']) <= len(payload['rows'])):
        raise ValueError('invalid exact Web backfill scope')
    old, new, seen = set(), set(), set()
    keys = set()
    for row in payload['rows']:
        if (not isinstance(row,dict) or set(row)!={'key','old','new','old_manifest_sha256','new_manifest_sha256'}
                or not isinstance(row['key'],str) or row['key'].count('\0')!=1 or row['key'] in keys):
            raise ValueError('invalid exact Web source key')
        keys.add(row['key'])
        for side,dest in (('old',old),('new',new)):
            v=row[side]
            if (set(v)!= {'bucket','object','manifest','sha256','bytes'} or v['bucket']!=BUCKET
                    or not re.fullmatch(r'documents/web/(?:html|htm|mht|mhtml)/[a-f0-9]{64}/document\.html',v['object'])
                    or v['manifest']!=v['object'].removesuffix('.html')+'-manifest.json'
                    or not re.fullmatch(r'[a-f0-9]{64}',v['sha256']) or not re.fullmatch(r'[a-f0-9]{64}',row[side+'_manifest_sha256'])
                    or type(v['bytes']) is not int or not 0<v['bytes']<=4*1024**2):
                raise ValueError('invalid exact old/new Web bundle')
            dest.add(v['object'])
    if len(old)!=len(payload['rows']) or old & new:
        raise ValueError('old/new Web routes overlap or are ambiguous')
    for path,value in payload['hints']:
        retirement.validate(path,value)
        removed={r['path'] for r in value['removed']}
        if (value['reason']!='catalog-replacement' or any(r['bucket']!=BUCKET for r in value['removed'])
                or not removed or not removed <= old or removed & seen):
            raise ValueError('retirement hint exceeds the exact old Web cohort')
        seen.update(removed)
    if seen!=old:
        raise ValueError('missing sealed retirement hint')
    return payload


def public_guard(store, generation, owner, api):
    if _owner()!=owner or owner[1]!='1':
        raise RuntimeError('exact first attempt is required')
    v3.assert_writer(store)
    _active_owner(GitHubProducerFence(),*owner)
    api.check_main(owner[2])
    state=json.loads(store.read_bytes(BUCKET,DURABLE_JOURNAL))
    if (state.get('kind')!='reader-maintenance-journal' or state.get('version')!=1 or state.get('restored') is not True
            or state.get('callback_recovery_required') is True
            or (state.get('callback_mutation_started') and state.get('callback_committed') is not True)):
        raise RuntimeError('Reader maintenance is not clear')
    if any(str(r['id'])!=owner[0] for r in api.active_children()):
        raise RuntimeError('competing Reader or Search run')
    if v3.current(store)[0]['generation']!=generation:
        raise RuntimeError('Reader generation changed')
    return True


def run(data, *, guard, hub, http, claims=None):
    data=parse(data)
    generation=data['generation']; rows=data['rows']
    if guard() is not True:
        raise RuntimeError('missing exact Web backfill guard')
    accepted=v3.verify_generation(hub,http,generation)
    pointer,catalog=v3.current(hub)
    if not pointer or pointer['generation']!=generation or not catalog.get('base_sidecar'):
        raise RuntimeError('exact Web backfill requires a pinned current projection')
    routes=v3.project_sidecar(v3.pinned_base(hub,catalog,{}),catalog)['f']
    current_paths={(row.get('b'),row.get('p')) for row in routes.values() if isinstance(row,dict)}
    for row in rows:
        target=routes.get(row['key'],{})
        if ((target.get('b'),target.get('p'),target.get('m'))!=(BUCKET,row['new']['object'],'h')
                or (BUCKET,row['old']['object']) in current_paths):
            raise ValueError('sealed Web replacement differs from the current projection')
    # Transport failures are bounded and retried only before any write.
    for attempt in range(3):
        try:
            result=live({'target_pointer':{'generation':generation},'rows':rows},accepted,{'generation':generation},http)
            break
        except httpx.TransportError:
            if attempt==2 or guard() is not True:
                raise
    if guard() is not True:
        raise RuntimeError('Web backfill guard drifted after verification')
    paths=[]
    if claims is not None:
        for hint in data['hints']:
            matching={item['path'] for item in hint[1]['removed']}
            scope=[row for row in rows if row['old']['object'] in matching]
            paths.append(record_hint({'retirement_hint':hint},scope,guard=guard,hub=hub,claims=claims))
    return {**result,'hints_written_or_verified':paths,'pin_release_authorized':False,'deleted_objects':0}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--resource-json',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--apply',action='store_true');a=p.parse_args()
    data=parse(json.loads(a.resource_json));store=v3.CentralHubStore();api=GitHub(os.environ['GH_TOKEN']);owner=_owner()
    def guard():return public_guard(store,data['generation'],owner,api)
    with httpx.Client(follow_redirects=True,timeout=30) as client:
        result=run(data,guard=guard,hub=store,http=client,claims=S3ClaimStore() if a.apply else None)
    with a.output.open('xb') as f:f.write(v3.encode(result))


if __name__=='__main__': main()
