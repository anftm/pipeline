"""Read-only effective Pages full-code proof before any managed web write."""
import json
import os
import re
from urllib.parse import urlencode
from uuid import uuid4
import httpx

SITE='https://vomebook.github.io/search/'
API='https://api.github.com/repos/vomebook/search'
RUN=re.compile(r'https://github\.com/vomebook/search/actions/runs/([1-9][0-9]*)(?:/attempts/([1-9][0-9]*))?(?:/job/([1-9][0-9]*))?(?:\?attempt=([1-9][0-9]*))?')
PREPARE={'Run production Pages source gates','Check content-addressed PDF opening','Require exact Reader matrix and current source before deployment','Verify tested artifact fingerprint','Upload artifact','Verify uploaded candidate fingerprint'}
DEPLOY={'Wait for the uploaded Pages artifact','Check final promotion','Deploy to GitHub Pages','Read back the public Pages release'}
REFRESH={'Prove active code and Search for Reader refresh','Verify tested artifact fingerprint','Upload artifact','Verify uploaded candidate fingerprint'}


def get(client,url):
    with client.stream('GET',url,headers={'Cache-Control':'no-cache'},follow_redirects=False) as r:
        r.raise_for_status();raw=bytearray()
        for b in r.iter_bytes():
            if len(raw)+len(b)>2*1024**2:raise ValueError('Pages evidence exceeds size cap')
            raw.extend(b)
        return json.loads(raw),'rel="next"' in r.headers.get('Link','')


def verify(api,public,pointer,expected=None):
    def good(v):return v.get('status')=='completed' and v.get('conclusion')=='success'
    def require(ok,msg):
        if not ok:raise ValueError(msg)
    receipt=public('data/pages_release_receipt.json'); code=receipt['code_sha']; search=receipt['search_generation']
    require(isinstance(code,str) and re.fullmatch('[0-9a-f]{40}',code) and receipt['workflow_sha']==code
            and api('/git/ref/heads/main')[0]['object']['sha']==code and (receipt['reader_generation'],receipt['reader_catalog_sha256'])==
            (pointer['generation'],pointer['catalog']['sha256']),'Pages active source or exact Reader parent differs')
    live=None
    for page in range(1,4):
        deployments,next_page=api('/deployments',{'environment':'github-pages','per_page':30,'page':page})
        for d in deployments:
            require(type(d.get('id')) is int and d['environment']=='github-pages','invalid Pages deployment identity')
            statuses,more=api(f'/deployments/{d["id"]}/statuses',{'per_page':30})
            require(bool(statuses),'Pages deployment has no terminal status')
            s=statuses[0]
            if s['state'] in {'failure','error'} and not more and not any(x['state']=='success' for x in statuses):continue
            require(s['state']=='success' and s['environment']=='github-pages','Pages effective deployment uncertain')
            live=(d,s);break
        if live or not next_page:break
    require(live is not None,'cannot prove the effective Pages deployment')
    d,s=live
    links=[m.groups() for n in ('log_url','target_url') if (m:=RUN.fullmatch(s.get(n) or ''))]
    rid,attempt=receipt['run_id'],receipt['run_attempt']
    attempts={int(n) for _,a,_,b in links for n in (a,b) if n}; jobs={int(j) for _,_,j,_ in links if j}
    require(d['sha']==code and {int(r) for r,_,_,_ in links}=={rid} and len(jobs)<=1 and attempts in (set(),{attempt}),
            'effective Pages run/attempt association differs')
    if jobs:
        jid=next(iter(jobs)); status=api(f'/actions/jobs/{jid}')[0]
        require((status.get('id'),status.get('run_id'),status.get('run_attempt'))==(jid,rid,attempt),'deploy job association differs')
    elif not attempts:
        status=api(f'/actions/runs/{rid}')[0];require(attempt==status['run_attempt']==1,'plain Pages run URL does not prove attempt')
    def prove(runid,runattempt,prepare):
        run=api(f'/actions/runs/{runid}/attempts/{runattempt}')[0]; page,more=api(f'/actions/runs/{runid}/attempts/{runattempt}/jobs',{'per_page':50})
        jobs={j['name']:j for j in page['jobs']}
        require(good(run) and run['head_sha']==code and run['id']==runid and run['run_attempt']==runattempt
               and run['head_branch']=='main' and run['event'] in {'push','workflow_dispatch'}
               and run['path'].split('@')[0]=='.github/workflows/static.yml' and
               not more and page['total_count']==len(page['jobs'])==2 and set(jobs)=={'prepare','deploy'},'exact Pages run or job inventory differs')
        for name,want in (('prepare',prepare),('deploy',DEPLOY)):
            job=jobs[name]; names=[x['name'] for x in job['steps'] if good(x)]
            require(good(job) and job['run_id']==runid and job['run_attempt']==runattempt and job['head_sha']==code
                    and all(names.count(k)==1 for k in want),'required Pages source/promotion step did not pass')
        return run,jobs['deploy']
    refresh=receipt.get('reader_refresh_id'); active,deploy=prove(rid,attempt,REFRESH if refresh else PREPARE)
    require(not jobs or jobs=={deploy['id']},'effective deployment job mismatch')
    full=(receipt['code_proof_run_id'],receipt['code_proof_run_attempt']);require(full[1]==1,'Pages first-attempt full code proof required')
    if refresh:
        require(active['display_title']=='reader-refresh/'+refresh and attempt==1,'Pages refresh identity mismatch')
        run,_=prove(*full,PREPARE); require(not run['display_title'].startswith('reader-refresh/'),'Pages full proof is a refresh')
    else:require(full==(rid,attempt) and not active['display_title'].startswith('reader-refresh/'),'Pages full proof is not the effective full build')
    manifest=public('data/generation-manifest.json');require(manifest['version']==1 and manifest['kind']=='search-snapshot-local' and manifest['generation']==search
            and public('data/pages_release_receipt.json')==receipt,'Pages receipt or Search data changed')
    result={'code_sha':code,'search_generation':search,'reader_generation':pointer['generation'],'catalog_sha256':pointer['catalog']['sha256'],
            'release_run':rid,'release_attempt':attempt,'full_run':full[0],'full_attempt':full[1],'deployment':d['id'],'deploy_job':deploy['id']}
    require(expected is None or result==expected,'effective Pages proof changed from review')
    return result


def current(pointer,*,expected=None):
    token=os.environ.get('READER_WEB_PAGES_PROOF_TOKEN','')
    if not token:raise ValueError('dedicated Pages proof credential is required')
    with httpx.Client(timeout=18) as http,httpx.Client(timeout=18,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'managed-web-pages-proof'}) as gh:
        def api(path,params=None):return get(gh,API+path+('?' + urlencode(params) if params else ''))
        def pub(path):return get(http,SITE+path+'?reader_check='+uuid4().hex)[0]
        return verify(api,pub,pointer,expected)
