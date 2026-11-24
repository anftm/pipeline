"""Verify exact public replacements after the daily controller's profile batch."""
import argparse,json,os
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import httpx
from . import reader_gc_retirement as retirement
from .reader_format_daily import GitHub
from .reader_image_history_store import S3ClaimStore
from .reader_web_batch_cli import PUBLIC_WEB_READERS, live
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


def retention_evidence(result, *, publication_raw, consumer_raw, now=None):
    """Anchor the Web-only clock to completed object and dual-consumer readback."""
    published = json.loads(publication_raw)
    consumers = json.loads(consumer_raw)
    n = published['changed_rows']
    if type(n) is int and n == 0:
        return None
    if (type(n) is not int or not 1 <= n <= 200 or n != len(published['profile']['rows'])
            or n != published['profile']['converted'] or type(result.get('old_and_new_verified')) is not int
            or result['old_and_new_verified'] != n
            or result.get('both_projections_verified') is not True
            or result.get('generation') != published['generation']
            or consumers.get('generation') != published['generation'] or consumers.get('complete') is not True
            or set(consumers.get('consumers', {})) != {'hf', 'pages'}):
        raise ValueError('Web retention evidence differs from the completed publication')
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError('Web retention clock requires timezone')
    for surface, receipt in consumers['consumers'].items():
        if (receipt.get('surface') != surface or receipt.get('projection_verified') is not True
                or receipt.get('generation') != published['generation']):
            raise ValueError('Web retention evidence requires both verified projections')
        checked = datetime.fromisoformat(receipt['checked_at'].replace('Z', '+00:00'))
        if checked.utcoffset() is None or checked > now:
            raise ValueError('invalid Web consumer verification time')
    return {'generation': result['generation'], 'old_and_new_verified': n,
            'both_projections_verified': True, 'verified_at': now.astimezone(timezone.utc).isoformat(),
            'publication_sha256': sha256(publication_raw).hexdigest(),
            'consumers_sha256': sha256(consumer_raw).hexdigest()}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('publication','consumers','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--resource-json')
    a=p.parse_args(); publication_raw=a.publication.read_bytes(); consumer_raw=a.consumers.read_bytes()
    result=json.loads(publication_raw); profile=result['profile']; n=result['changed_rows']
    if type(n) is not int or n!=profile['converted'] or n!=len(profile['rows']):
        raise ValueError('profile readback scope differs')
    with httpx.Client(follow_redirects=True,timeout=30) as http:
        out=live({'target_pointer':{'generation':result['generation']},'rows':profile['rows']},
                 json.loads(consumer_raw),result,http,workers=PUBLIC_WEB_READERS)
    if n:
        out['web_rollback_verification']=retention_evidence(out, publication_raw=publication_raw, consumer_raw=consumer_raw)
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
