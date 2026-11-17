"""Durable authorized review/revert commands; unknown effects require recovery."""
import argparse
import json
import os
from pathlib import Path
from . import proofread_delivery as delivery, submit_proofread as submit
from . import review_proofread as review, revert_proofread as revert


def execute(record, mode, action):
    module=review if mode=='review' else revert
    original_submit=submit.api_request
    original_module=module.api_request
    original_delete=review.delete_pull_branch
    record.review_merge=mode=='review'
    submit.api_request=lambda *a,**k:record.call(original_submit,*a,**k)
    module.api_request=lambda *a,**k:record.call(original_module,*a,**k)
    # Branch cleanup needs a separate dependency review; approvals only merge
    # the exact head validated by the existing authorized review implementation.
    review.delete_pull_branch=lambda *_a,**_k:False
    try:action()
    finally:
        submit.api_request=original_submit
        module.api_request=original_module
        review.delete_pull_branch=original_delete
        record.review_merge=False


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['review','revert']);p.add_argument('--output',type=Path,required=True);a=p.parse_args(argv)
    if (os.environ.get('PROOFREAD_DELIVERY_ENABLED')!='true' or os.environ.get('GITHUB_REPOSITORY')!='anftm/pipeline'
            or os.environ.get('GITHUB_REF')!='refs/heads/main' or os.environ.get('GITHUB_RUN_ATTEMPT')!='1'):
        raise ValueError('enabled first central main attempt required')
    event=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    # GitHub comment identity deduplicates retries across runs, while manual
    # revert commands retain their explicit run identity and confirmation.
    comment=event.get('comment',{}).get('id')
    request={'request_id':f'{a.mode}:{comment or os.environ["GITHUB_RUN_ID"]}',
             'mode':a.mode,'event':event,'correction_id':os.environ.get('PROOFREAD_CORRECTION_ID',''),
             'confirm':os.environ.get('PROOFREAD_CONFIRM','')}
    action=review.main if a.mode=='review' else revert.main
    result=delivery.submit(delivery.Store(),request,run={'id':os.environ['GITHUB_RUN_ID'],'sha':os.environ['GITHUB_SHA']},
                           execute=lambda record,_value:execute(record,a.mode,action))
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps(result))

if __name__=='__main__':main()
