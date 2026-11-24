"""Plan an exact legacy Web pin release without mutating roots or deleting assets."""
import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import reader_gc_retirement as retirement

ROOT = re.compile(r'reader-index/retained-web-(?:batch-|managed-|html-|htm-|mht-|mhtml-)[a-f0-9]{64}\.json')
WEB = re.compile(r'documents/web/(?:html|htm|mht|mhtml)/[a-f0-9]{64}/document\.html')
ROLLBACK_AGE = timedelta(hours=24)


def rollback_age(verification, generation, count, now):
    """Policy clock from independently reviewed dual-site replacement evidence.

    This is only Web rollback eligibility. It must not predate the separate
    collector clock, which starts when the production graph is unreferenced.
    """
    if (not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{64}', generation)
            or type(count) is not int or not 1 <= count <= 200
            or not isinstance(verification, dict) or verification.get('generation') != generation
            or verification.get('both_projections_verified') is not True
            or type(verification.get('old_and_new_verified')) is not int
            or verification['old_and_new_verified'] != count):
        raise ValueError('exact Web dual-site replacement proof required')
    stamp = verification.get('verified_at')
    if not isinstance(stamp, str) or not isinstance(now, datetime):
        raise ValueError('invalid Web replacement verification time')
    checked = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if checked.utcoffset() is None or now.utcoffset() is None or checked > now:
        raise ValueError('invalid Web replacement verification time')
    eligible = checked + ROLLBACK_AGE
    return {'eligible_after': eligible.isoformat(), 'rollback_age_satisfied': now >= eligible,
            'gc_observation_started': False}


def historical_sidecar_preview(historical, accepted_successor, rows):
    """Preview a narrow Web rewrite while preserving every other route.

    Caller reads both sidecars at their immutable verified identities and binds
    rows to a separately accepted same-original publication. Returned routes
    are not a replacement catalog, a rollback alias, or a release grant.
    """
    if not 1 <= len(rows) <= 200 or any(v.get('v') != 1 or not isinstance(v.get('f'), dict)
                                        for v in (historical, accepted_successor)):
        raise ValueError('invalid exact Web history preview input')
    intended = {}
    for row in rows:
        key = row['key']
        if not isinstance(key, str) or key.count('\0') != 1 or key in intended:
            raise ValueError('ambiguous historical Web source')
        old, new = row['old'], row['new']
        a, b = old['object'], new['object']
        if (old['bucket'] != retirement.ASSETS or new['bucket'] != retirement.ASSETS
                or not WEB.fullmatch(a) or not WEB.fullmatch(b) or a == b
                or a.split('/')[2] != b.split('/')[2]):
            raise ValueError('invalid historical Web bundle substitution')
        intended[key] = (a, b)
    selected = {a: key for key, (a, _) in intended.items()}
    successor_paths = {entry.get('p') for entry in accepted_successor['f'].values()
                       if isinstance(entry, dict) and entry.get('b') == retirement.ASSETS}
    if len(selected) != len(intended) or selected.keys() & successor_paths:
        raise ValueError('old Web bundles overlap or are still present in the accepted successor')
    changed = {}
    for key, entry in historical['f'].items():
        if not isinstance(entry, dict) or entry.get('b') != retirement.ASSETS or entry.get('p') not in selected:
            continue
        if selected[entry['p']] != key:
            raise ValueError('old Web bundle is also referenced by another route')
        target = accepted_successor['f'].get(key, {})
        if entry.get('m') != 'h' or {**entry, 'p': intended[key][1]} != target:
            raise ValueError('historical Web substitution changes source or route metadata')
        changed[key] = target
    return {**historical, 'f': {**historical['f'], **changed}}, len(changed)


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


def accepted_publication_clock(publication_raw, consumer_raw, readback, now):
    """Validate an artifact-bound clock before using a Web rollback preview."""
    from .reader_web_profile_readback import retention_evidence

    proof = readback.get('web_rollback_verification')
    if not isinstance(proof, dict) or not isinstance(proof.get('verified_at'), str):
        raise ValueError('missing exact Web verification clock')
    verified_at = datetime.fromisoformat(proof['verified_at'])
    if proof != retention_evidence(readback, publication_raw=publication_raw,
                                   consumer_raw=consumer_raw, now=verified_at):
        raise ValueError('Web verification clock differs from accepted artifacts')
    published = json.loads(publication_raw)
    return rollback_age(proof, published['generation'], published['changed_rows'], now)


def json_read(path):
    return json.loads(path.read_bytes())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('rows','observations','output'):
        p.add_argument('--'+name, type=Path, required=True)
    for name in ('publication','consumers','readback'):
        p.add_argument('--'+name, type=Path)
    args=p.parse_args(); obs=json_read(args.observations)
    artifacts = (args.publication, args.consumers, args.readback)
    if any(artifacts) and not all(artifacts):
        p.error('--publication, --consumers and --readback must be supplied together')
    roots={key:Path(path).read_bytes() for key,path in obs['roots'].items()}
    hints={key:json_read(Path(path)) for key,path in obs['hints'].items()}
    rows=json_read(args.rows)['rows']
    result=plan(rows,roots,hints)
    if all(artifacts):
        from datetime import timezone
        published = args.publication.read_bytes()
        if json.loads(published)['profile']['rows'] != rows:
            raise ValueError('Web retirement rows differ from the accepted publication')
        result['web_rollback_clock'] = accepted_publication_clock(
            published,args.consumers.read_bytes(),json_read(args.readback),datetime.now(timezone.utc))
    with args.output.open('xb') as f:f.write(retirement.encode(result))


if __name__ == '__main__':main()
