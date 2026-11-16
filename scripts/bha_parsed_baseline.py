"""Prepare local baseline changes only after exact parsed publication and index acceptance.

The caller must authenticate publication-artifact provenance. This module checks
its source identities and BHA's audited verified journal, then returns updated
baseline documents. It never commits or pushes a baseline itself.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from . import bha_reindex_journal as journal, build_fork_parsed as builder
from . import bha_build_plan as planning, check_bha_reindex_access as access, search_snapshot

SHA = re.compile(r'[0-9a-f]{40}\Z')


def prepare(publication, inputs, trees, store, *, inspect_source, observe_consumer):
    if (not isinstance(publication,dict) or publication.get('kind')!='bha-parsed-publication-result'
            or publication.get('status')!='published' or publication.get('baseline_updated') is not False
            or publication.get('consumer_verified') is not False):
        raise ValueError('exact completed parsed publication required')
    archive=publication.get('archive')
    if type(archive) is not int or not 0<=archive<32:
        raise ValueError('invalid published archive')
    key=str(archive)
    if any(not SHA.fullmatch(str(publication.get(k))) for k in ('commit','tree','previous_tree')):
        raise ValueError('invalid parsed publication identities')
    if (inputs.get('version')!=1 or set(inputs.get('archives',{}))!={str(i) for i in range(32)}
            or set(trees)!={str(i) for i in range(32)}):
        raise ValueError('complete baseline documents required')
    if (inputs['archives'][key]!=publication.get('previous_inputs')
            or trees[key]!=publication['previous_tree']):
        raise ValueError('baseline changed since reviewed build')
    snapshot=publication['source_snapshot']
    expected=copy.deepcopy(snapshot)
    expected['mirror_branches']['parsed']=publication['commit']
    expected['parsed_tree']=publication['tree']
    if inspect_source()!=expected:
        raise ValueError('published source changed before baseline handoff')
    built=builder.source_snapshot(snapshot['mirror_branches'],snapshot['helper_revision'])
    if built!=publication.get('built_inputs'):
        raise ValueError('built input identities differ')
    current=journal.load(store)
    if current is None:
        raise ValueError('verified BHA index operation required')
    journal.audit(store,current)
    record=current.record
    if (record['state']!='verified' or record['archive_commits'][key]!=publication['commit']
            or record['archive_revisions'][key]!=publication['tree']
            or record['result'].get('snapshot_sidecars_verified') is not True
            or record['result'].get('consumer_revisions_verified') is not True):
        raise ValueError('parsed candidate has not completed exact index/snapshot acceptance')
    observed=observe_consumer()
    if (observed.get('index_status')!='ready' or observed.get('switch_pending') is not False
            or observed.get('archive_revisions')!=record['archive_revisions']
            or observed.get('job',{}).get('job_id')!=record['job_id']
            or observed.get('job',{}).get('operation_id')!=record['operation']
            or observed.get('job',{}).get('state')!='succeeded'):
        raise ValueError('current BHA consumer differs from verified operation')
    after=journal.load(store)
    if (after is None or after.head_raw!=current.head_raw or after.etag!=current.etag
            or inspect_source()!=expected):
        raise ValueError('publication or acceptance changed during baseline handoff')
    updated_inputs=copy.deepcopy(inputs);updated_trees=dict(trees)
    updated_inputs['archives'][key]=built;updated_trees[key]=publication['tree']
    return {'kind':'bha-parsed-baseline-candidate','archive':archive,'inputs':updated_inputs,
            'trees':updated_trees,'operation':record['operation'],'job_id':record['job_id'],
            'published_commit':publication['commit'],'published_tree':publication['tree'],
            'snapshot_generation':record['result']['snapshot_generation'],
            'before_inputs_sha256':hashlib.sha256(search_snapshot.canonical(inputs)).hexdigest(),
            'before_trees_sha256':hashlib.sha256(search_snapshot.canonical(trees)).hexdigest(),
            'inputs_sha256':hashlib.sha256(search_snapshot.canonical(updated_inputs)).hexdigest(),
            'trees_sha256':hashlib.sha256(search_snapshot.canonical(updated_trees)).hexdigest(),
            'remote_writes':0,'baseline_published':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication',type=Path,required=True)
    parser.add_argument('--publication-sha256',required=True)
    parser.add_argument('--inputs',type=Path,default=Path('state/bha-parsed-inputs.json'))
    parser.add_argument('--trees',type=Path,default=Path('state/bha-parsed-revisions.json'))
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    for path in (args.publication,args.inputs,args.trees):
        if path.is_symlink() or not path.is_file() or path.stat().st_size>4*1024*1024:
            raise ValueError('baseline input missing or oversized')
    if args.output_dir.exists() or args.output_dir.is_symlink():
        raise ValueError('baseline output directory must be new')
    raw=args.publication.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=args.publication_sha256:
        raise ValueError('reviewed publication bytes differ')
    publication=json.loads(raw);inputs=json.loads(args.inputs.read_bytes());trees=json.loads(args.trees.read_bytes())
    github_token=os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN','')
    bha_token=os.environ.get('BHA_REINDEX_TOKEN','')
    if not github_token or not bha_token:raise ValueError('existing source and consumer credentials required')
    result=prepare(publication,inputs,trees,journal.S3Store(),
        inspect_source=lambda:planning._snapshot(github_token,publication['archive']),
        observe_consumer=lambda:access.read_status(bha_token))
    args.output_dir.parent.mkdir(parents=True,exist_ok=True)
    # Stage the pair with its receipt together; no partially written final output.
    with tempfile.TemporaryDirectory(prefix='bha-baseline-',dir=args.output_dir.parent) as directory:
        staging=Path(directory)/'result';staging.mkdir()
        for name,value in [('bha-parsed-inputs.json',result['inputs']),('bha-parsed-revisions.json',result['trees'])]:
            (staging/name).write_bytes(search_snapshot.canonical(value))
        (staging/'receipt.json').write_bytes(search_snapshot.canonical({k:v for k,v in result.items() if k not in {'inputs','trees'}}))
        staging.rename(args.output_dir)
    print('exact verified baseline candidate prepared; no remote writes')


if __name__ == '__main__':
    main()
