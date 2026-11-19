"""Explicit Reader dispatch for bot-triggered, accepted Search publications."""
import json
import os
from pathlib import Path
import re
import sys
import time

from .reader_format_daily import DAILY, REPOSITORY, GitHub, read_report_archive

BOT = 'github-actions[bot]'
SYNC_PATH = '.github/workflows/search-daily-sync.yml'
ARTIFACT = 'search-reader-handoff'


def valid_run(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value):
        raise ValueError('Invalid source run ID')
    return int(value)


def source_identity(run, rid, sha):
    if (type(run.get('id')) is not int or run['id'] != rid
            or run.get('head_sha') != sha or run.get('head_branch') != 'main'
            or run.get('event') != 'workflow_dispatch' or run.get('path') != SYNC_PATH
            or type(run.get('run_attempt')) is not int or run['run_attempt'] != 1
            or run.get('actor', {}).get('login') != BOT or run.get('triggering_actor', {}).get('login') != BOT
            or any(run.get(k, {}).get('full_name') != REPOSITORY for k in ('repository', 'head_repository'))):
        raise ValueError('Source must be an exact first-attempt bot Search run on main')


def receipt(rid, sha, before, after, journal):
    gen = after.get('generation', '')
    transition = after.get('transition', '')
    if (not re.fullmatch('[0-9a-f]{64}', gen) or before.get('generation') == gen
            or not re.fullmatch('[0-9a-f]{32}', transition) or after.get('both_consumers_observed') is not True
            or journal.get('generation') != gen or journal.get('state') != 'complete'
            or journal.get('transition') != transition or journal.get('pointer_transition') != transition
            or not re.fullmatch('[0-9a-f]{64}', journal.get('head_sha256', ''))):
        raise ValueError('No exact fresh terminal Search publication to hand off')
    return {'run':rid,'attempt':1,'sha':sha,'previous':before['generation'],'generation':gen,
            'transition':transition,'head_sha256':journal['head_sha256']}


def send(api, rid, sha):
    api.check_main(sha)
    run=api.get_run(rid)
    source_identity(run,rid,sha)
    if run.get('status')!='in_progress':raise ValueError('Sender must be the running Search workflow')
    matches=api.runs(DAILY,created='>='+run['created_at'])
    if any(r.get('display_title')==f'reader-search-handoff/{rid}' for r in matches):
        raise ValueError('Handoff already observed; do not resend')
    api.dispatch(DAILY,{'search_source_run':str(rid)})


def observe(api,rid,sha,reader_id,*,clock=time.monotonic,sleep=time.sleep):
    deadline=clock()+300
    while True:
        run=api.get_run(rid);source_identity(run,rid,sha)
        if run.get('status')=='completed':break
        if run.get('status') not in {'in_progress','queued','pending'} or clock()>=deadline:
            raise ValueError('Exact source run is not terminal; no Reader writes')
        sleep(min(5,max(0,deadline-clock())))
    if run.get('conclusion')!='success':raise ValueError('Search source run did not succeed')
    source_identity(api.request('GET',f'/actions/runs/{rid}/attempts/1'),rid,sha)
    data=api.request('GET',f'/actions/runs/{rid}/attempts/1/jobs',params={'per_page':100});jobs=data.get('jobs')
    if not isinstance(jobs,list) or type(data.get('total_count')) is not int or data['total_count']!=len(jobs) or len(jobs)>100:
        raise ValueError('Incomplete source job inventory')
    matching=[j for j in jobs if j.get('name')=='sync']
    if (len(matching)!=1 or matching[0].get('conclusion')!='success' or matching[0].get('head_sha')!=sha
            or matching[0].get('run_id')!=rid or matching[0].get('run_attempt')!=1):
        raise ValueError('Exact full Search sync did not succeed')
    for step in ('Publish staged generation in this job','Verify fresh publication receipt and terminal journal',
                 'Preserve verified Search handoff receipt','Start Reader once for the bot Search handoff'):
        found=[s for s in matching[0].get('steps',[]) if s.get('name')==step]
        if len(found)!=1 or found[0].get('conclusion')!='success':raise ValueError('Search did not publish and verify a changed generation')
    raw,_,_=api.report_artifact(run,ARTIFACT,sha)
    reports=read_report_archive(raw,{'handoff.json'});r=reports['handoff.json']
    if (set(r)!={'run','attempt','sha','previous','generation','transition','head_sha256'} or type(r['run']) is not int or r['run']!=rid
            or type(r['attempt']) is not int or r['attempt']!=1 or r['sha']!=sha
            or not all(isinstance(r[k],str) and re.fullmatch('[0-9a-f]{64}',r[k]) for k in ('previous','generation','head_sha256'))
            or r['previous']==r['generation'] or not re.fullmatch('[0-9a-f]{32}',r['transition'])):
        raise ValueError('Unbound Search handoff receipt')
    ids=[x['id'] for x in api.runs(DAILY,created='>='+run['created_at']) if x.get('display_title')==f'reader-search-handoff/{rid}']
    if ids!=[reader_id]:raise ValueError('Reader handoff is not the unique run for this source')
    run=api.get_run(rid);source_identity(run,rid,sha)
    if run.get('status')!='completed' or run.get('conclusion')!='success':raise ValueError('Source run changed')
    api.check_main(sha)
    return r


def check_live(r, journal, publication):
    if (journal.get('state')!='complete' or journal.get('head_sha256')!=r['head_sha256']
            or any(journal.get(k)!=r['transition'] for k in ('transition','pointer_transition'))
            or publication.get('transition')!=r['transition']
            or publication.get('both_consumers_observed') is not True
            or publication.get('generation')!=r['generation'] or journal.get('generation')!=r['generation']):
        raise ValueError('Handoff is no longer the exact verified Search publication')


def check_backlog(handoff, summary):
    if handoff.get('generation') != summary.get('search_generation'):
        raise ValueError('Reader backlog is not pinned to the accepted Search handoff')


def main():
    mode=sys.argv[1:];env=os.environ
    if mode==['check-backlog']:
        p=Path(env['RUNNER_TEMP'])
        check_backlog(json.loads((p/'handoff.json').read_bytes()),
                      json.loads(Path('output/format-daily/summary.json').read_bytes()))
        return
    sha=env['GITHUB_SHA'];rid=valid_run(env['SOURCE_RUN'])
    if (env['GITHUB_REPOSITORY']!=REPOSITORY or env['GITHUB_REF']!='refs/heads/main' or env['GITHUB_RUN_ATTEMPT']!='1'
            or env['GITHUB_ACTOR']!=BOT or env['GITHUB_EVENT_NAME']!='workflow_dispatch'
            or env['GITHUB_WORKFLOW_SHA']!=sha):raise ValueError('Requires trusted bot workflow first attempt on main')
    workflow = '.github/workflows/' + DAILY if mode==['verify'] else SYNC_PATH
    if env.get('GITHUB_WORKFLOW_REF') != f'{REPOSITORY}/{workflow}@refs/heads/main':
        raise ValueError('Wrong trusted workflow for handoff mode')
    p=Path(env['RUNNER_TEMP']); path=p/'handoff.json'
    if mode==['prepare']:
        def read(name):return json.loads((p/name).read_bytes())
        v=receipt(rid,sha,read('search-published-receipt.json'),read('search-final-receipt.json'),read('search-final-journal.json'))
        with path.open('x') as f:json.dump(v,f)
    elif mode==['send']:
        send(GitHub(env['GH_TOKEN']),rid,sha)
    elif mode==['verify']:
        from . import search_daily, search_daily_journal, search_transition as tx
        v=observe(GitHub(env['GH_TOKEN']),rid,sha,valid_run(env['GITHUB_RUN_ID']))
        store=tx.CachedTransitionStore(tx.S3TransitionStore())
        j=search_daily_journal.inspect(store);check_live(v,j,search_daily.inspect_published())
        search_daily_journal.verify_unchanged(j,search_daily_journal.inspect(store))
        with path.open('x') as f:json.dump(v,f)
    else:raise ValueError('Unknown handoff mode')


if __name__=='__main__':main()
