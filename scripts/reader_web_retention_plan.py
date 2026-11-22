"""Plan an exact legacy Web pin release without mutating roots or deleting assets."""
import argparse
import hashlib
import json
import re
from pathlib import Path

from . import reader_gc_retirement as retirement

ROOT = re.compile(r'reader-index/retained-web-(?:batch-|managed-|html-|htm-|mht-|mhtml-)[a-f0-9]{64}\.json')
WEB = re.compile(r'documents/web/(?:html|htm|mht|mhtml)/[a-f0-9]{64}/document\.html')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def plan(rows, roots, hints):
    """Subtract only exact old manifests; require live hint proofs and all old pins."""
    if not 1 <= len(rows) <= 200:
        raise ValueError('invalid exact web retirement cohort size')
    old, new = set(), set()
    for row in rows:
        for field, dest in (('old', old), ('new', new)):
            item = row[field]
            path = item['object']
            if (item['bucket'] != retirement.ASSETS or not WEB.fullmatch(path)
                    or item['manifest'] != path.removesuffix('.html') + '-manifest.json'):
                raise ValueError('unexpected web bundle member')
            dest.add(path)
    if len(old) != len(rows) or old & new:
        raise ValueError('ambiguous web bundle ownership')
    if any(row['old']['source_sha256'] != row['new']['source_sha256'] for row in rows):
        raise ValueError('retirement requires same original bytes')
    hinted = set()
    for path, value in hints.items():
        retirement.validate(path, value)
        if value['reason'] != 'catalog-replacement':
            raise ValueError('retirement hint is not a catalog replacement')
        for item in value['removed']:
            member = item['path']
            if item['bucket'] != retirement.ASSETS or member not in old or member in hinted:
                raise ValueError('retirement hint outside exact old web cohort')
            hinted.add(member)
    if hinted != old:
        raise ValueError('missing independently observed old web retirement hints')
    remove = {p.removesuffix('.html') + '-manifest.json' for p in old}
    seen = set(); transitions = []
    for path, raw in sorted(roots.items()):
        value = json.loads(raw)
        if (not ROOT.fullmatch(path) or value.keys() != {'version','kind','resources'}
                or value['version'] != 1 or value['kind'] != 'reader-web-retained-objects'
                or not isinstance(value['resources'], list) or not value['resources']):
            raise ValueError('unreviewed web retention root')
        changed, keep = [], []
        for ref in value['resources']:
            if ref.keys() != {'bucket','path'} or ref['bucket'] != retirement.ASSETS:
                raise ValueError('unreviewed resource in web retention root')
            if ref['path'] in remove:
                if ref['path'] in seen:
                    raise ValueError('duplicate old web reference')
                seen.add(ref['path']); changed.append(ref['path'])
            else:
                keep.append(ref)
        if changed:
            successor = {**value,'resources':keep}
            target = json.dumps(successor,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
            transitions.append({'root':path,'before_sha256':digest(raw),'after_sha256':digest(target),
                                'retain':keep,'remove':sorted(changed)})
    if seen != remove:
        raise ValueError('explicit roots do not account for every old web manifest')
    return {'version':1,'kind':'reader-web-retention-plan','release_authorized':False,'objects_deleted':0,
            'retiring': sorted(old | remove),'roots':transitions}


def json_read(path):
    return json.loads(path.read_bytes())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('rows','observations','output'):
        p.add_argument('--'+name, type=Path, required=True)
    args=p.parse_args(); obs=json_read(args.observations)
    roots={key:Path(path).read_bytes() for key,path in obs['roots'].items()}
    hints={key:json_read(Path(path)) for key,path in obs['hints'].items()}
    result=plan(json_read(args.rows)['rows'],roots,hints)
    with args.output.open('xb') as f:f.write(retirement.encode(result))


if __name__ == '__main__':main()
