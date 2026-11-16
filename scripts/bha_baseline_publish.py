"""Commit only a freshly verified BHA baseline pair with one exact main lease.

The caller authenticates publication provenance and revalidates the BHA handoff
before publication. Active Reader work blocks a main update. Ambiguous pushes
are read back once and never retried.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from . import bha_parsed_baseline as baseline, bha_build_plan as planning
from . import bha_reindex_journal as journal, check_bha_reindex_access as access
from . import reader_format_daily as daily, search_snapshot

FILES = ('state/bha-parsed-inputs.json', 'state/bha-parsed-revisions.json')
SHA = re.compile(r'[0-9a-f]{40}\Z')


def publish(repo, candidate, expected_sha, *, revalidate, check_window, remote_head,
            push, checkpoint, git=None):
    repo=Path(repo)
    if not SHA.fullmatch(expected_sha):raise ValueError('exact main SHA required')
    env={**os.environ,'GIT_AUTHOR_NAME':'vomebook','GIT_AUTHOR_EMAIL':'vomebook@gmail.com',
         'GIT_COMMITTER_NAME':'vomebook','GIT_COMMITTER_EMAIL':'vomebook@gmail.com'}
    def local_git(*args):
        return subprocess.check_output(['git',*args],cwd=repo,env=env,stderr=subprocess.PIPE,text=True).strip()
    git=git or local_git
    if git('rev-parse','HEAD')!=expected_sha or git('status','--porcelain'):
        raise ValueError('baseline publication requires a clean exact checkout')
    if (candidate.get('kind')!='bha-parsed-baseline-candidate' or candidate.get('baseline_published') is not False
            or candidate.get('remote_writes',0)!=0):
        raise ValueError('verified baseline candidate required')
    values=(candidate['inputs'],candidate['trees'])
    before_names=('before_inputs_sha256','before_trees_sha256')
    after_names=('inputs_sha256','trees_sha256')
    for path,before,after,value in zip(FILES,before_names,after_names,values):
        raw=(repo/path).read_bytes()
        if hashlib.sha256(search_snapshot.canonical(json.loads(raw))).hexdigest()!=candidate[before]:
            raise ValueError('baseline preimage changed')
        if hashlib.sha256(search_snapshot.canonical(value)).hexdigest()!=candidate[after]:
            raise ValueError('baseline candidate digest differs')
    archive=candidate.get('archive')
    if type(archive) is not int or not 0<=archive<32:
        raise ValueError('baseline archive invalid')
    before_inputs=json.loads((repo/FILES[0]).read_bytes())
    before_trees=json.loads((repo/FILES[1]).read_bytes())
    key=str(archive)
    if (set(candidate['inputs'])!=set(before_inputs) or set(candidate['trees'])!=set(before_trees)
            or set(candidate['inputs']['archives'])!=set(before_inputs['archives'])
            or any(candidate['inputs'][k]!=v for k,v in before_inputs.items() if k!='archives')
            or any(candidate['inputs']['archives'][k]!=v for k,v in before_inputs['archives'].items() if k!=key)
            or any(candidate['trees'][k]!=v for k,v in before_trees.items() if k!=key)):
        raise ValueError('baseline candidate changes another archive')
    if revalidate()!=candidate:raise ValueError('baseline acceptance changed')
    check_window()
    if remote_head()!=expected_sha:raise ValueError('main advanced before baseline commit')
    originals={path:(repo/path).read_bytes() for path in FILES}
    changed=False
    for path,value in zip(FILES,values):
        if json.loads(originals[path])!=value:changed=True
    if not changed:
        result={'status':'no-change','main':expected_sha,'push_attempts':0,'remote_writes':0}
        checkpoint(result);return result
    # Preserve local evidence if a commit step fails; never reset the checkout.
    try:
        for path,value in zip(FILES,values):
            (repo/path).write_bytes(search_snapshot.canonical(value)+b'\n')
        git('add','--',*FILES)
        staged=set(git('diff','--cached','--name-only').splitlines())
        if not staged or not staged<=set(FILES):raise ValueError('unexpected staged baseline changes')
        git('-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-m',
            'Record verified BHA parsed baseline for archive '+str(candidate['archive']))
    except Exception:
        # Keep any index/commit evidence rather than reset an unknown commit.
        raise
    commit=git('rev-parse','HEAD')
    if git('show','-s','--format=%an <%ae>|%cn <%ce>',commit) != 'vomebook <vomebook@gmail.com>|vomebook <vomebook@gmail.com>':
        raise ValueError('baseline commit identity differs')
    if git('rev-parse',commit+'^')!=expected_sha or not SHA.fullmatch(commit):
        raise ValueError('baseline commit parent differs')
    if not set(git('diff','--name-only',expected_sha,commit).splitlines())<=set(FILES):
        raise ValueError('baseline commit exceeds exact two-file scope')
    intent={'kind':'bha-baseline-publication','status':'push-intent','parent':expected_sha,
            'commit':commit,'archive':candidate['archive'],'operation':candidate['operation'],
            'push_attempts':1,'baseline_published':False}
    checkpoint(intent)
    if revalidate()!=candidate:raise ValueError('baseline acceptance changed before push')
    check_window()
    if remote_head()!=expected_sha:raise ValueError('main advanced before baseline push')
    error=None
    try:push(commit,expected_sha)
    except Exception as exc:error=type(exc).__name__
    try:observed=remote_head()
    except Exception:
        checkpoint({**intent,'status':'unknown','push_error':error})
        raise RuntimeError('baseline push outcome unknown; inspect exact commit before recovery') from None
    if observed!=commit:
        checkpoint({**intent,'status':'unknown','observed_main':observed,'push_error':error})
        raise RuntimeError('baseline push not confirmed; never retry automatically')
    result={**intent,'status':'published','observed_main':observed,'push_error':error,'baseline_published':True}
    checkpoint(result);return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication',type=Path,required=True)
    parser.add_argument('--publication-sha256',required=True)
    parser.add_argument('--expected-sha',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if (os.environ.get('GITHUB_REPOSITORY')!='anftm/pipeline'
            or os.environ.get('GITHUB_REF')!='refs/heads/main'
            or os.environ.get('GITHUB_RUN_ATTEMPT')!='1'):
        raise ValueError('baseline publication requires first central main attempt')
    if args.output.exists() or args.output.is_symlink():raise ValueError('fresh output required')
    # Evidence must live outside the checkout so Git cleanliness remains meaningful.
    repo=Path.cwd().resolve()
    if args.output.resolve().is_relative_to(repo):raise ValueError('output must be outside checkout')
    if args.publication.is_symlink() or args.publication.stat().st_size>4*1024*1024:
        raise ValueError('publication artifact invalid')
    raw=args.publication.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=args.publication_sha256:raise ValueError('publication checksum differs')
    proof=json.loads(raw)
    originals=[json.loads((repo/p).read_bytes()) for p in FILES]
    token=os.environ.get('GH_PAT','');bha_token=os.environ.get('BHA_REINDEX_TOKEN','')
    if not token or not bha_token:raise ValueError('existing publication and consumer credentials required')
    store=journal.S3Store();api=daily.GitHub(token)
    def revalidate():
        return baseline.prepare(proof,*originals,store,
            inspect_source=lambda:planning._snapshot(token,proof['archive']),
            observe_consumer=lambda:access.read_status(bha_token))
    def check_window():
        if api.active_children():raise ValueError('Reader operations active; preserve baseline candidate')
    def remote_head():return api.request('GET','/git/ref/heads/main')['object']['sha']
    def push(commit,parent):
        subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','credential.helper=',
                        'push','--force-with-lease=refs/heads/main:'+parent,
                        'https://github.com/anftm/pipeline.git',commit+':refs/heads/main'],
                       env=baseline.builder.git_environment(token),cwd=repo,check=True,capture_output=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def checkpoint(value):
        temporary=args.output.with_suffix('.pending')
        with temporary.open('wb') as stream:
            stream.write(search_snapshot.canonical(value));stream.flush();os.fsync(stream.fileno())
        temporary.replace(args.output)
    try:
        publish(repo,revalidate(),args.expected_sha,revalidate=revalidate,check_window=check_window,
                remote_head=remote_head,push=push,checkpoint=checkpoint)
    except Exception as error:
        prior=json.loads(args.output.read_bytes()) if args.output.exists() else {
            'kind':'bha-baseline-publication','status':'blocked','push_attempts':0,'baseline_published':False}
        checkpoint({**prior,'error_type':type(error).__name__,
                    'recovery':'inspect recorded commit and current main; do not replay automatically'})
        raise RuntimeError('baseline publication stopped; inspect preserved evidence') from None


if __name__=='__main__':main()
