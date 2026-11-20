"""Read-only source revision check for bounded event-driven search updates."""
import json
import os
from . import fetch_and_parse as fetch, search_daily as daily


def inspect(*, observe=daily.inspect_published, revision=fetch.get_repo_sha, token=None):
    before=observe()
    revisions=before.get('source_revisions')
    if not fetch.accepts_baseline(revisions, fetch.REPOS):
        raise ValueError('published source revision set is incomplete')
    token=os.environ.get('HF_TOKEN','') if token is None else token
    heads={repo:revision(repo,token) for repo in fetch.REPOS}
    if not fetch.accepts_baseline(heads, fetch.REPOS):
        raise ValueError('source head identity unavailable')
    after=observe()
    if before!=after:raise ValueError('published generation changed during source check')
    changed=sorted(repo for repo in fetch.REPOS if heads[repo]!=revisions.get(repo))
    return {'kind':'search-source-watch','generation':before['generation'],
            'changed':changed,'update_needed':bool(changed),'remote_writes':0}


def main():
    result=inspect();print(json.dumps(result,sort_keys=True))
    path=os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path,'a',encoding='utf8') as f:f.write('update_needed='+str(result['update_needed']).lower()+'\n')
if __name__=='__main__':main()
