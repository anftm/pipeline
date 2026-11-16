"""Publish a reviewed parsed candidate bundle once; never rebuild or retry a push."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import bha_build_plan as planning, build_fork_parsed as builder, search_snapshot

MAX_RESULT = 4 * 1024 * 1024
MAX_BUNDLE = 8 * 1024 * 1024 * 1024


def read_candidate(path, digest, bundle):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RESULT:
        raise ValueError('candidate receipt is missing or oversized')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('reviewed candidate receipt hash differs')
    value = json.loads(raw)
    if (value.get('kind') != 'bha-parsed-candidate-result' or value.get('status') != 'candidate-ready'
            or value.get('remote_writes') != 0 or value.get('publication_authorized') is not False):
        raise ValueError('candidate is not a reviewed changed build')
    plan = value['plan']
    body = {k:v for k,v in plan.items() if k != 'plan_sha256'}
    if (plan.get('kind') != 'bha-parsed-build-readonly-plan' or plan.get('action') != 'build-candidate'
            or hashlib.sha256(search_snapshot.canonical(body)).hexdigest() != plan.get('plan_sha256')
            or value.get('plan_sha256') != plan['plan_sha256']):
        raise ValueError('candidate plan identity differs')
    candidate = value['candidate']; snapshot = plan['snapshot']
    if (candidate.get('archive') != plan['archive'] or candidate.get('changed') is not True
            or candidate.get('revisions') != snapshot['mirror_branches']
            or candidate.get('helper_revision') != snapshot['helper_revision']
            or candidate.get('before_tree') != snapshot['parsed_tree']):
        raise ValueError('candidate does not bind reviewed inputs')
    artifact = value['bundle']
    if (bundle.is_symlink() or not bundle.is_file() or not 0 < bundle.stat().st_size <= MAX_BUNDLE
            or bundle.stat().st_size != artifact['bytes'] or artifact['filename'] != bundle.name):
        raise ValueError('candidate bundle size or name differs')
    hashed = hashlib.sha256()
    with bundle.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024 * 1024), b''): hashed.update(chunk)
    if hashed.hexdigest() != artifact['sha256']:
        raise ValueError('candidate bundle checksum differs')
    return value


def publish_reviewed(value, *, inspect_live, restore, publish, workspace, checkpoint):
    plan, candidate = value['plan'], value['candidate']
    if inspect_live() != plan:
        raise ValueError('candidate source/repository identity changed before restore')
    restore(workspace, candidate)
    if inspect_live() != plan:
        raise ValueError('candidate source/repository identity changed before publication')
    def before_push(result):
        checkpoint(result)
        if result['status'] == 'push-intent' and inspect_live() != plan:
            raise ValueError('candidate identity changed during intent checkpoint')
    changed = publish(workspace, candidate, before_push)
    if changed is not True:
        raise ValueError('changed candidate unexpectedly reported no publication')
    # This is publication evidence only; the caller must separately deliver and
    # verify the index before updating any consumer baseline.
    result = {'kind':'bha-parsed-publication-result','status':'published','archive':plan['archive'],
              'commit':candidate['commit'],'tree':candidate['tree'],'plan_sha256':plan['plan_sha256'],
              'built_inputs':plan['current_inputs'],'consumer_verified':False,'baseline_updated':False}
    checkpoint(result)
    return result


def restore_bundle(root, candidate, bundle, token, *, source_url=None):
    """Restore only the reviewed objects into a fresh repository, without checkout."""
    repo=root/f'{builder.REPOSITORY_PREFIX}{candidate["archive"]}'/builder.PARSED_BRANCH
    repo.mkdir(parents=True)
    env=builder.clean_environment()
    builder.run(['git','init',str(repo)],env=env)
    url=source_url or f'https://github.com/{builder.MIRROR_OWNER}/{builder.REPOSITORY_PREFIX}{candidate["archive"]}.git'
    builder.run(['git','-c','core.hooksPath=/dev/null','fetch','--depth','1','--no-tags',url,
                 candidate['revisions'][builder.PARSED_BRANCH]],cwd=repo,env=builder.git_environment(token))
    builder.run(['git','bundle','verify',str(bundle.resolve())],cwd=repo,env=env)
    builder.run(['git','-c','core.hooksPath=/dev/null','fetch','--no-tags',str(bundle.resolve()),'HEAD'],cwd=repo,env=env)
    if builder.run_output(['git','rev-parse','FETCH_HEAD'],cwd=repo,env=env)!=candidate['commit']:
        raise ValueError('bundle HEAD differs from reviewed commit')
    (repo.parent/'push-home').mkdir()
    builder.sanitize_repository(repo)
    return repo


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--candidate-sha256',required=True)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--inputs',type=Path,default=Path('state/bha-parsed-inputs.json'))
    parser.add_argument('--trees',type=Path,default=Path('state/bha-parsed-revisions.json'))
    args=parser.parse_args(argv)
    if (args.output.exists() or args.output.is_symlink() or args.output.resolve() in
            {p.resolve() for p in (args.candidate,args.bundle,args.inputs,args.trees)}):
        raise ValueError('publication output must be new and separate from inputs')
    if os.environ.get('GITHUB_RUN_ATTEMPT','1') != '1':
        raise ValueError('publication cannot be rerun; inspect the previous exact commit')
    value=read_candidate(args.candidate,args.candidate_sha256,args.bundle)
    token=os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN','')
    if not token:raise ValueError('existing GitHub credential required')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def checkpoint(result):
        temporary=args.output.with_suffix('.pending')
        with temporary.open('wb') as stream:
            stream.write(search_snapshot.canonical(result));stream.flush();os.fsync(stream.fileno())
        temporary.replace(args.output)
    def inspect_live():
        return planning.inspect(token,value['plan']['archive'],input_path=args.inputs,tree_path=args.trees)
    def restore(root,candidate):
        return restore_bundle(root,candidate,args.bundle,token)
    with tempfile.TemporaryDirectory(prefix='bha-parsed-publish-') as directory:
        publish_reviewed(value,inspect_live=inspect_live,restore=restore,
            publish=lambda root,candidate,record:builder.publish_candidate(token,root,candidate,checkpoint=record),
            workspace=Path(directory),checkpoint=checkpoint)


if __name__ == '__main__':
    main()
