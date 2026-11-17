"""Publish reviewed corrections with a durable state head, never Pipeline commits.

Existing open upstream PRs remain claimed. Interrupted effects require explicit
reconciliation and are never replayed. Historical branch cleanup is deferred.
"""
import argparse
import copy
import json
import os
from pathlib import Path

from . import publish_proofread_upstream as legacy, proofread_delivery as delivery
from . import search_snapshot

KEY='proofreading/upstream/state.json'


class Store(delivery.Store):
    def location(self,key):
        if key!=KEY:raise ValueError('invalid upstream state key')
        namespace,bucket=self.backend._location(delivery.BUCKET)
        if (namespace,bucket)!=('vomebook','bha-data'):raise ValueError('unexpected publisher bucket')
        return self.backend._client(namespace),bucket


def run(store,bootstrap,*,run_id,execute,apply=False):
    before,etag=store.read(KEY)
    if before is not None:
        if before.get('kind')!='proofreading-upstream-delivery' or before.get('state')!='complete':
            raise RuntimeError('prior upstream publication requires recovery; no replay')
        state=copy.deepcopy(before['publisher_state'])
    else:state=copy.deepcopy(bootstrap)
    if state.get('version')!=1 or not isinstance(state.get('claimed'),dict):raise ValueError('invalid publisher baseline')
    if not apply:
        return {'status':'inspected','remote_writes':0,'claims':len(state['claimed']),
                'baseline':len(state.get('baseline',[])),'published':len(state.get('published',[]))}
    # Reuse mutation-intent recorder with this narrowly scoped publisher key.
    record=object.__new__(delivery.Delivery);record.store=store;record.key=KEY;record.etag=etag
    record.state={'version':1,'kind':'proofreading-upstream-delivery','state':'claimed','run':run_id,
                  'publisher_state':state,'effects':[]};record.save()
    def checkpoint(value):
        record.state['publisher_state']=copy.deepcopy(value);record.save()
    execute(record,state,checkpoint)
    record.state['state']='complete';record.save()
    return {'status':'complete','effect_count':len(record.state['effects']),
            'claims':len(record.state['publisher_state']['claimed']),'state_saved':True}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');p.add_argument('--output',type=Path,required=True);a=p.parse_args(argv)
    if a.apply and (os.environ.get('PROOFREAD_UPSTREAM_ENABLED')!='true'
            or os.environ.get('GITHUB_REPOSITORY')!='anftm/pipeline'
            or os.environ.get('GITHUB_REF')!='refs/heads/main' or os.environ.get('GITHUB_RUN_ATTEMPT')!='1'):
        raise ValueError('enabled first central main attempt required')
    bootstrap=legacy.load_state()
    if bootstrap is None:raise ValueError('existing baseline required; no implicit bootstrap')
    original_api,original_load,original_save=legacy.api_request,legacy.load_state,legacy.save_state
    original_delete,original_cleanup=legacy.delete_branch,legacy.cleanup_legacy_claim
    def execute(record,state,checkpoint):
        legacy.api_request=lambda *args,**kwargs:record.call(original_api,*args,**kwargs)
        legacy.load_state=lambda:copy.deepcopy(state)
        legacy.save_state=checkpoint
        legacy.delete_branch=lambda *_args,**_kwargs:False
        def reject_cleanup(*_args,**_kwargs):raise ValueError('legacy claim requires explicit review')
        legacy.cleanup_legacy_claim=reject_cleanup
        try:legacy.main()
        finally:
            legacy.api_request=original_api;legacy.load_state=original_load;legacy.save_state=original_save
            legacy.delete_branch=original_delete;legacy.cleanup_legacy_claim=original_cleanup
    store=Store()
    result=run(store,bootstrap,run_id=os.environ.get('GITHUB_RUN_ID'),execute=execute,apply=a.apply)
    if not a.apply:
        value,_etag=store.read(KEY)
        state=value['publisher_state'] if value else bootstrap
        pulls=legacy.current_pulls(os.environ.get('GH_PAT',''))
        known=set(state.get('baseline',[])) | set(state.get('claimed',{}))
        candidates=[p for p in pulls if legacy.source_key(p['repo'],p['number']) not in known]
        result['pending_sources']=[legacy.source_key(p['repo'],p['number']) for p in candidates]
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps(result))

if __name__=='__main__':main()
