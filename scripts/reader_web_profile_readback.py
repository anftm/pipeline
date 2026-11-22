"""Verify exact public replacements after the daily controller's profile batch."""
import argparse,json,os
from pathlib import Path
import httpx
from . import reader_gc_retirement as retirement
from .reader_format_daily import GitHub
from .reader_image_history_store import S3ClaimStore
from .reader_web_batch_cli import live
from .reader_web_profile_publish import authority_scope, ordinary_writer
from .publish_reader_v3 import ASSETS, CentralHubStore, current, encode


def record_hint(publication, rows, *, guard, hub, claims):
    hint = publication.get('retirement_hint')
    if hint is None:
        return None
    if not isinstance(hint, list) or len(hint) != 2:
        raise ValueError('invalid sealed web retirement hint')
    path, value = hint
    retirement.validate(path, value)
    old = {(r['old']['bucket'], r['old']['object']) for r in rows}
    new = {(r['new']['bucket'], r['new']['object']) for r in rows}
    removed = {(r['bucket'], r['path']) for r in value['removed']}
    if (value['reason'] != 'catalog-replacement' or not removed or not removed <= old - new
            or len(old) != len(rows) or any(bucket != ASSETS for bucket, _ in removed)):
        raise ValueError('web retirement hint differs from the sealed replacement rows')
    raw = retirement.encode(value)
    if guard() is not True:
        raise RuntimeError('web retirement writer fence lost')
    before, _ = claims._get(path)
    if before is None:
        if guard() is not True:
            raise RuntimeError('web retirement writer fence lost before create')
        claims._put(path, raw, IfNoneMatch='*')
    elif before != raw:
        raise ValueError('existing web retirement hint differs')
    if claims._get(path)[0] != raw or hub.read_bytes(ASSETS, path) != raw:
        raise ValueError('web retirement hint not visible with exact bytes')
    return path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('publication','consumers','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--resource-json')
    a=p.parse_args();result=json.loads(a.publication.read_bytes()); profile=result['profile']; n=result['changed_rows']
    if type(n) is not int or n!=profile['converted'] or n!=len(profile['rows']):
        raise ValueError('profile readback scope differs')
    with httpx.Client(follow_redirects=True,timeout=30) as http:
        out=live({'target_pointer':{'generation':result['generation']},'rows':profile['rows']},
                 json.loads(a.consumers.read_bytes()),result,http)
    if n and a.resource_json is not None:
        extension, _, parent_id = authority_scope(json.loads(a.resource_json))
        if extension != profile['extension']:
            raise ValueError('web retirement does not match dispatched extension')
        hub = CentralHubStore(); api = GitHub(os.environ['GH_TOKEN'])
        def guard():
            return ordinary_writer(hub, parent_id, api) and current(hub)[0]['generation'] == result['generation']
        out['retirement_hint'] = record_hint(result, profile['rows'], guard=guard, hub=hub, claims=S3ClaimStore())
    with a.output.open('xb') as f:f.write(encode(out))

if __name__=='__main__':main()
