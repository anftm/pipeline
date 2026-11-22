"""Verify a hosted web profile run has a result for every planned source."""
import argparse,json
from collections import Counter
from pathlib import Path
from .reader_web_managed_convert import digest,encode
from .reader_web_profile import WEB_PROFILE


def summarize(plan,artifacts):
    p=json.loads((plan/'report.json').read_bytes());matrix=json.loads((plan/'matrix.json').read_bytes())['include']
    if p['profile']!=WEB_PROFILE or p['remote_writes']!=0:
        raise ValueError('web profile plan differs')
    status=Counter();reasons=Counter(); omissions=Counter(); formats={};seen=set()
    for item in matrix:
        shard=item['shard'];req=(plan/(shard+'.json')).read_bytes();requests=json.loads(req)['items']; ledger=json.loads((artifacts/f'managed-web-profile-{shard}'/'report.json').read_bytes())
        if (ledger['request_sha256']!=digest(req) or ledger['profile']!=WEB_PROFILE or ledger['remote_writes']!=0
                or ledger['publication_ready'] is not False or len(ledger['items'])!=len(requests)):
            raise ValueError('incomplete or foreign web profile shard '+shard)
        for before,row in zip(requests,ledger['items']):
            key=(before['repo'],before['path'])
            if key in seen or any(row.get(k)!=v for k,v in before.items()) or row['status'] not in {'converted','hold'}:
                raise ValueError('unexpected managed conversion result')
            seen.add(key);status[row['status']]+=1
            formats.setdefault(row['extension'],Counter())[row['status']]+=1
            if row['status']=='hold':reasons[row['reason']]+=1
            missing=row.get('omissions',{}).get('missing_images',0)
            if type(missing) is not int or missing<0 or (missing and row['status']!='converted'):
                raise ValueError('invalid missing-image omission count')
            if missing:
                omissions['documents_with_missing_images']+=1
                omissions['missing_images']+=missing
    if len(seen)!=p['counts']['queued']:
        raise ValueError('managed web profile rollout is missing results')
    return {'kind':'reader-web-managed-profile-summary','profile':WEB_PROFILE,'reader_generation':p['reader_generation'],
            'search_generation':p['search_generation'],'planned':p['counts'],'results':dict(status),'by_format':{k:dict(v) for k,v in formats.items()},
            'hold_reasons':dict(reasons),'omissions':dict(omissions),'dual_reader_verified':False,'publication_ready':False,'remote_writes':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--artifacts',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);a=parser.parse_args()
    with a.output.open('xb') as o:o.write(encode(summarize(a.plan,a.artifacts)))

if __name__=='__main__':main()
