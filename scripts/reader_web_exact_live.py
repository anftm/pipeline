"""Read public live MHT route evidence after the exact dual-consumer receipt."""
import argparse
import json
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from .reader_web_exact_plan import MAX_HTML

READER_ID = '28f50a563fl74'


def verify(publication, consumer_report, client):
    generation = publication['generation']
    if (consumer_report.get('generation') != generation or consumer_report.get('complete') is not True
            or set(consumer_report.get('consumers',{})) != {'hf','pages'}
            or any(x.get('generation') != generation or x.get('projection_verified') is not True
                   for x in consumer_report['consumers'].values())):
        raise ValueError('exact HF and Pages consumer projections are required')
    url = 'https://voiceofml-search.hf.space/api/reader-resolve?' + urlencode({'id':READER_ID})
    response=client.get(url,headers={'Cache-Control':'no-cache'}); response.raise_for_status()
    data=response.json()
    def object_url(path):
        return 'https://voiceofml-search.hf.space/api/reader-bucket-resource?'+urlencode({'path':path})
    target=publication['staging']['new_html']
    route = urlsplit(data.get('url') or '')
    if ((route.scheme, route.netloc, route.path, parse_qs(route.query), route.fragment) !=
            ('https','voiceofml-search.hf.space','/api/reader-bucket-resource',{'path':[target]},'')
            or data.get('extension')!='html' or not data.get('download')):
        raise ValueError('public MHT resolver does not serve exact route and original download')
    def observe(path,digest):
        h=sha256(); size=0
        with client.stream('GET',object_url(path),headers={'Cache-Control':'no-cache'}) as res:
            res.raise_for_status()
            for chunk in res.iter_bytes():
                size+=len(chunk)
                if size>MAX_HTML: raise ValueError('public MHT exceeds reviewed limit')
                h.update(chunk)
        if h.hexdigest()!=digest:
            raise ValueError('public MHT SHA differs from review')
    observe(target,publication['new_sha256'])
    observe(publication['old_object'],publication['old_sha256'])
    return {'generation':generation,'reader_id':READER_ID,'old_live':True,'new_live':True,
            'both_projections_verified':True,'objects_deleted':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('publication','consumers','output'):p.add_argument('--'+key,type=Path,required=True)
    args=p.parse_args()
    with httpx.Client(timeout=30,follow_redirects=True) as client:
        result=verify(json.loads(args.publication.read_bytes()),json.loads(args.consumers.read_bytes()),client)
    with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,sort_keys=True)

if __name__=='__main__':main()
