"""Digest-bound single Web hold operation under the existing daily owner."""
from hashlib import sha256
import json
import os
from pathlib import Path

from . import publish_reader_v3 as v3
from . import reader_format_daily as daily
from . import reader_web_exact_plan as web
from . import reader_web_batch_cli as batch
from . import reader_web_batch_publish as publisher
from . import reader_web_hold_admission as admission
from . import reader_web_managed_review as managed
from . import reader_web_profile_publish as profile
from .reader_image_history_store import S3ClaimStore

FIELD='hold_approval_sha256'


def automation_id(parent, approval):
    return sha256(f'web-hold:{parent}:{approval}'.encode('ascii')).hexdigest()[:32]


def validate_parent(run, head, approval):
    if not web._is_sha(approval):raise ValueError('invalid hold authority digest')
    title='reader-web-hold/'+approval
    if run.get('display_title')!=title or run.get('name') not in {title,daily.DAILY_NAME} or run.get('event')!='workflow_dispatch':
        raise RuntimeError('exact hold controller dispatch required')
    daily.validate_daily_run(run|{'display_title':'Reader format backlog / workflow_dispatch','name':daily.DAILY_NAME},head)


def parse_resource(value):
    if (not isinstance(value,dict) or set(value)!={FIELD,'extension','search_generation','parent_run_id'}
            or value['extension']!='html' or not web._is_sha(value[FIELD])):
        raise ValueError('invalid exact hold publication resource')
    profile.authority_scope({k:v for k,v in value.items() if k!=FIELD})
    return value


def resource_from_env():
    return parse_resource(web._json(os.environ['RESOURCE_JSON'].encode('utf-8'),'hold owner resource'))


def load_authority(approval, store):
    raw=batch.read(store,batch.PROOFS+approval+'.json',32*1024)
    if sha256(raw).hexdigest()!=approval:raise ValueError('hold authority digest differs')
    proof=web._json(raw,'exact hold authority')
    if (not isinstance(proof,dict) or not isinstance(proof.get('hold_admission'),dict)
            or not all(web._is_sha(proof.get(k)) for k in ('bundle_sha256','review_sha256'))):
        raise ValueError('hold authority has no exact hosted hold proof')
    return proof


def existing_apply_inputs(resource, store):
    r=parse_resource(resource); d=load_authority(r[FIELD],store)
    if d['hold_admission'].get('search_generation')!=r['search_generation']:
        raise ValueError('hold authority Search pin differs from owner')
    return d


def owner(hub, resource):
    r=parse_resource(resource); api=daily.GitHub(os.environ['GH_TOKEN']); observed=profile._owner()
    def check():
        return profile.ordinary_writer(hub,r['parent_run_id'],api,observed=observed,hold_approval=r[FIELD])
    check(); return check


def apply(resource):
    hub=v3.CentralHubStore(); guard=owner(hub,resource)
    store=S3ClaimStore(); proof=existing_apply_inputs(resource,store)
    blob=batch.read(store,batch.PREFIX+proof['bundle_sha256']+'.zip',managed.MAX_PACKED)
    review,_,_,_=managed.parse(blob,proof['bundle_sha256'],proof['review_sha256'])
    if review.get(admission.DIGEST_FIELD)!=proof['hold_admission'].get(admission.DIGEST_FIELD):
        raise ValueError('hold bundle and hosted authority differ')
    return publisher.run(blob,proof['bundle_sha256'],proof['review_sha256'],proof,phase='apply',managed=True,
                         external_writers_stopped=False,controller_guard=guard)|{FIELD:resource[FIELD]}


def controller(approval, journal, result, *, api, head, parent_id, store):
    run=api.get_run(parent_id); validate_parent(run,head,approval)
    if run.get('id')!=parent_id or run['status']!='in_progress':raise RuntimeError('hold controller no longer active')
    proof=load_authority(approval,store)
    search=proof['hold_admission'].get('search_generation')
    r=parse_resource({FIELD:approval,'parent_run_id':parent_id,'search_generation':search,'extension':'html'})
    ctx=daily.Controller(api=api,refresh=None,verify=None,journal=journal,expected_sha=head,parent_run_id=parent_id)
    ctx.result={'kind':'reader-web-hold-controller','expected_sha':head,'parent_run_id':parent_id,FIELD:approval,
                'operations':[],'operation_ids':[],'status':'running'}
    daily.save(journal,ctx.result,exclusive=True)
    opid=automation_id(parent_id,approval)
    ctx.dispatch(daily.PUBLISH,{'operation':'upgrade-web-profile','apply':'true','automation_id':opid,
                                'resource_json':json.dumps(r,separators=(',',':'))},'html','web-hold')
    op=ctx.result['operations'][0]
    child=api.get_run(op['run_id']); daily.validate_run(child,op,head)
    first=api.request('GET',f"/actions/runs/{op['run_id']}/attempts/1"); daily.validate_run(first,op,head)
    if any(r.get('id')!=op['run_id'] or r.get('conclusion')!='success' or r.get('status')!='completed' for r in (child,first)):
        raise RuntimeError('exact hold run unsuccessful')
    raw,artifact,digest=api.report_artifact(child,daily.PUBLICATION_ARTIFACT,head)
    required={'publication.json','format-automation-verification.json','web-batch-object-readback.json','web-hold-cleanup.json'}
    files=daily.read_report_archive(raw,required,optional_root='v3/')
    pub,verify,back,clean=(files[n] for n in ('publication.json','format-automation-verification.json','web-batch-object-readback.json','web-hold-cleanup.json'))
    hold=proof['hold_admission']; n=hold['selected']; generation=pub['generation']; daily.validate_verification(verify,generation)
    if (pub.get(FIELD)!=approval or pub.get('hold_admission')!=hold or pub.get('changed_rows')!=n or pub.get('deleted_objects')!=0
            or back.get('generation')!=generation or back.get('old_and_new_verified')!=n or back.get('both_projections_verified') is not True
            or back.get('deleted_objects')!=0 or clean.get('generation')!=generation or clean.get('removed_count')!=n
            or clean.get(admission.DIGEST_FIELD)!=hold[admission.DIGEST_FIELD]):
        raise ValueError('exact hold publication, readback or cleanup receipt differs')
    r=api.get_run(op['run_id']); daily.validate_run(r,op,head)
    if r.get('id')!=op['run_id'] or r.get('status')!='completed' or r.get('conclusion')!='success':
        raise RuntimeError('exact hold child changed during acceptance')
    ctx.result.update(status='complete',generation=generation,verified=n,cleared=n,artifact_id=artifact['id'],artifact_digest=digest)
    ctx.checkpoint(); daily.save(result,ctx.result,exclusive=True)


def main():
    run_id=os.environ['GITHUB_RUN_ID']; head=os.environ['GITHUB_SHA']; approval=os.environ['HOLD_APPROVAL']
    if (not run_id.isascii() or not run_id.isdecimal() or os.environ.get('GITHUB_RUN_ATTEMPT')!='1'
            or daily.local_head()!=head or not web._is_sha(approval)):
        raise RuntimeError('exact first-attempt Pipeline main hold controller required')
    api=daily.GitHub(os.environ['GH_TOKEN'],local_head=daily.local_head); api.check_main(head)
    root=Path('output/format-daily');root.mkdir(parents=True,exist_ok=True)
    controller(approval,root/'operations.jsonl',root/'result.json',api=api,head=head,parent_id=int(run_id),store=S3ClaimStore())

if __name__=='__main__': main()
