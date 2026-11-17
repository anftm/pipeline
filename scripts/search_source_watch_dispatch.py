"""Dispatch one source-driven sync only when no existing sync is active."""
import os
from .reader_format_daily import GitHub


def dispatch(api, sha):
    api.check_main(sha)
    # Active syncs coalesce; incomplete inventory remains a hard failure.
    for status in ('queued','pending','in_progress','waiting','requested'):
        if api.runs('search-daily-sync.yml',status=status):
            return {'status':'coalesced','writes':0}
    api.check_main(sha)
    api.dispatch('search-daily-sync.yml',{'force_sync':'false','known_writers_coordinated':'true','target':'all'})
    return {'status':'dispatched','writes':1}


def main():
    token=os.environ.get('GH_TOKEN','');sha=os.environ.get('GITHUB_SHA','')
    if not token or os.environ.get('GITHUB_RUN_ATTEMPT')!='1':raise ValueError('first authorized attempt required')
    import json
    print(json.dumps(dispatch(GitHub(token),sha),sort_keys=True))
if __name__=='__main__':main()
