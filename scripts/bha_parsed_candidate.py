"""Build one exact reviewed BHA parsed candidate; never publish remote branches."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import bha_build_plan as planning, build_fork_parsed as builder, search_snapshot


def reviewed(path, sha):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('reviewed plan must be a bounded regular file')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('reviewed build plan bytes differ')
    value = json.loads(raw)
    body = {k:v for k,v in value.items() if k != 'plan_sha256'}
    if (value.get('kind') != 'bha-parsed-build-readonly-plan' or value.get('remote_writes') != 0
            or value.get('publication_ready') is not False
            or hashlib.sha256(search_snapshot.canonical(body)).hexdigest() != value.get('plan_sha256')):
        raise ValueError('invalid reviewed build plan')
    return value


def generate(plan, *, inspect_live, prepare_helper, build_candidate, bundle, workspace, emit):
    """Recheck exact identities on both sides of generation, retaining candidate evidence."""
    if inspect_live() != plan:
        raise ValueError('build plan changed before generation')
    if plan['action'] == 'no-input-change':
        result = {'kind':'bha-parsed-candidate-result','status':'no-change','remote_writes':0,
                  'plan_sha256':plan['plan_sha256'],'build_requested':False}
        emit(result)
        return result
    if plan['action'] != 'build-candidate':
        raise ValueError('reviewed plan requires separate drift or upstream inspection')
    snapshot = plan['snapshot']
    emit({'kind':'bha-parsed-candidate-result','status':'building','plan_sha256':plan['plan_sha256'],
          'remote_writes':0,'build_requested':True})
    helper = prepare_helper(workspace, snapshot['helper_revision'])
    candidate = build_candidate(helper, workspace, plan['archive'], snapshot['mirror_branches'])
    if (candidate['revisions'] != snapshot['mirror_branches']
            or candidate['helper_revision'] != snapshot['helper_revision']
            or candidate['before_tree'] != snapshot['parsed_tree'] or candidate['remote_writes'] != 0):
        raise ValueError('generated candidate source identities differ')
    if inspect_live() != plan:
        raise ValueError('build plan changed during generation; do not publish candidate')
    artifact = bundle(workspace, candidate) if candidate['changed'] else None
    result = {'kind':'bha-parsed-candidate-result','status':'candidate-ready' if candidate['changed'] else 'no-change',
              'remote_writes':0,'build_requested':True,'plan_sha256':plan['plan_sha256'],
              'plan':plan,'candidate':candidate,'bundle':artifact,'publication_authorized':False}
    emit(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--plan-sha256',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--inputs',type=Path,default=Path('state/bha-parsed-inputs.json'))
    parser.add_argument('--trees',type=Path,default=Path('state/bha-parsed-revisions.json'))
    args=parser.parse_args(argv)
    inputs={p.resolve() for p in (args.plan,args.inputs,args.trees)}
    if (args.output.exists() or args.output.is_symlink() or args.bundle.exists() or args.bundle.is_symlink()
            or args.output.resolve() in inputs or args.bundle.resolve() in inputs
            or args.output.resolve()==args.bundle.resolve()):
        raise ValueError('candidate outputs must be new paths outside inputs')
    plan=reviewed(args.plan,args.plan_sha256)
    token=os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN','')
    if not token:raise ValueError('existing GitHub credential required')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def emit(value):
        temporary=args.output.with_suffix('.pending')
        temporary.write_bytes(search_snapshot.canonical(value));temporary.replace(args.output)
    def inspect_live():return planning.inspect(token,plan['archive'],input_path=args.inputs,tree_path=args.trees)
    def bundle(workspace,candidate):
        args.bundle.parent.mkdir(parents=True,exist_ok=True)
        repo=workspace/f'{builder.REPOSITORY_PREFIX}{candidate["archive"]}'/builder.PARSED_BRANCH
        # Candidate HEAD is the only advertised ref, with no remote authentication.
        builder.run(['git','bundle','create',str(args.bundle.resolve()),'HEAD'],cwd=repo,env=builder.clean_environment())
        builder.run(['git','bundle','verify',str(args.bundle.resolve())],cwd=repo,env=builder.clean_environment())
        digest=hashlib.sha256()
        with args.bundle.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        return {'filename':args.bundle.name,'sha256':digest.hexdigest(),'bytes':args.bundle.stat().st_size}
    try:
        with tempfile.TemporaryDirectory(prefix='bha-parsed-candidate-') as directory:
            return generate(plan,inspect_live=inspect_live,
                prepare_helper=lambda root,sha:builder.prepare_helper(root,builder.git_environment(token),sha),
                build_candidate=lambda helper,root,archive,revisions:builder.build_candidate(token,helper,root,archive,revisions),
                bundle=bundle,workspace=Path(directory),emit=emit)
    except Exception as error:
        emit({'kind':'bha-parsed-candidate-result','status':'failed','error_type':type(error).__name__,
              'plan_sha256':plan['plan_sha256'],'remote_writes':0,'publication_authorized':False})
        raise


if __name__ == '__main__':
    main()
