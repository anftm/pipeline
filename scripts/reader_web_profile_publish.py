"""One bounded web profile batch in the central, serialized publisher.

Select and build from live, pinned backlog under the same locks as legacy and
v3 promotion. Failed transformations keep their published output. The normal
format controller dispatches this operation and validates its publication.
"""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile, ZIP_DEFLATED

import httpx
from . import reader_format_backlog as backlog, reader_format_daily as daily
from . import publish_reader_v3 as v3, reader_web_index_guard as index
from . import reader_web_managed_convert as convert, reader_web_managed_pages as pages
from . import reader_web_managed_prepare as prepare, reader_web_managed_review as schema
from . import reader_web_profile_replay as replay, reader_web_profile_candidates as candidates
from . import reader_web_batch_review as batch_review, reader_web_batch_publish as publish
from .reader_image_history_fence import _owner, _active_owner
from .reader_image_history_publish_s3 import LegacyRoots, SIDECAR
from .reader_image_history_store import S3ClaimStore
from .reader_gc_fence import GitHubProducerFence
from .reader_gc_maintenance import DURABLE_JOURNAL
from .reader_page_cas import ConditionalPageStore
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore
from .reader_web_profile import WEB_PROFILE

B=v3.ASSETS
MAX_ROWS=10


def authority_scope(resource):
    if (not isinstance(resource,dict) or set(resource)!={'extension','search_generation','parent_run_id'}
            or not isinstance(resource['extension'],str) or resource['extension'] not in convert.EXTENSIONS
            or not isinstance(resource['search_generation'],str) or not daily.GENERATION.fullmatch(resource['search_generation'])
            or type(resource['parent_run_id']) is not int or resource['parent_run_id']<=0):
        raise ValueError('invalid automatic web profile operation')
    return resource['extension'],resource['search_generation'],resource['parent_run_id']


def ordinary_writer(hub, parent_id, api, *, observed=None):
    """Narrow admission within existing daily-controller and publisher locks.

    This is not the out-of-band fence used by manual exact operations. Require
    the current opted-in repository's common maintenance and writer boundary;
    additionally stop on any active, non-own Reader/Search workflow.
    """
    owner=_owner() if observed is None else observed
    if (_owner()!=owner or owner[1]!='1' or os.getenv('READER_WEB_PROFILE_REBUILD_ENABLED')!='true'
            or os.getenv('READER_WEB_PROFILE_PAGES_SCOPE_REVIEWED')!='true'
            or os.getenv('READER_PROFILE_PAUSED')=='true'):
        raise RuntimeError('automatic web profile writer is not active')
    v3.assert_writer(hub); fence=GitHubProducerFence()
    _active_owner(fence,*owner)
    parent=api.get_run(parent_id)
    daily.validate_daily_run(parent,owner[2])
    current=api.get_run(int(owner[0]))
    if (parent['status']!='in_progress' or parent['id']!=parent_id or str(parent_id)==owner[0]
            or current.get('display_title')!='reader-auto/'+os.getenv('WEB_PROFILE_AUTOMATION_ID','')
            or current.get('actor',{}).get('login')!='github-actions[bot]'):
        raise RuntimeError('exact in-progress controller required')
    state=json.loads(hub.read_bytes(B,DURABLE_JOURNAL))
    if (state.get('kind')!='reader-maintenance-journal' or state.get('version')!=1 or state.get('restored') is not True
            or state.get('callback_recovery_required') is True or
            (state.get('callback_mutation_started') and state.get('callback_committed') is not True)):
        raise RuntimeError('ordinary Reader maintenance state is not cleared')
    if any(str(row['id'])!=owner[0] for row in api.active_children(parent_id)):
        raise RuntimeError('a competing Reader/Search job is still active')
    api.check_main(owner[2])
    return True


def candidate(item,old,*,reviewed=None):
    # Recapture same-directory resources from the exact published revision;
    # no arbitrary external fetch. The old source digest remains the identity.
    req=candidates.source_identity(item,old)
    convert.validate_request({'version':1,'max_rows':1,'max_source_bytes':convert.MAX_SOURCE_BYTES,
                              'max_output_bytes':convert.MAX_OUTPUT_BYTES,'items':[req]})
    if reviewed is None:
        url=f'https://{convert.HF}/datasets/{item["repo"]}/resolve/{item["revision"]}/{quote(item["path"])}'
        dl=convert.Downloads(convert.MAX_SOURCE_BYTES,convert.anonymous_fetch)
        raw=dl.get(url,convert.MAX_ITEM_SOURCE)
        if sha256(raw).hexdigest()!=old['source_sha256']:
            raise RuntimeError('profile source bytes differ')
        omissions={}; data=convert.convert(raw,item['extension'],url,dl,schema.MAX_DOC,omissions=omissions)
    else:
        data,omissions=reviewed
    h=sha256(data).hexdigest(); bundle=sha256(f"{old['source_sha256']}\0{h}".encode()).hexdigest()
    root=f"documents/web/{item['extension']}/{bundle}"
    new={k:v for k,v in old.items() if k not in {'checkpointed_at','processing_roots'}}
    new.update(profile=WEB_PROFILE,object=root+'/document.html',manifest=root+'/document-manifest.json',bytes=len(data),sha256=h)
    if h==old['sha256'] and len(data)==old['bytes']:
        new.update(object=old['object'],manifest=old['manifest'])
    manifest=(json.dumps({'kind':'web-document-stream','version':1,'source_extension':item['extension'],
              'source_sha256':new['source_sha256'],'path':'document.html','bytes':len(data),'sha256':h},
              ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')
    return new,data,manifest,omissions


def checkpoint_non_routing(cps,hub,path,old,index_updates,failures,guard):
    if not index_updates and not failures:return
    start=cps.read(B,path); _,rows=index._index(start.raw)
    if hub.read_bytes(B,path)!=start.raw:
        raise ValueError('profile-only checkpoint not visible')
    for k in index_updates.keys()|failures.keys():
        if rows[k]!=old[k]:raise RuntimeError('profile hold or no-op row changed')
    notices={row['key']:row for row in start.value['failures']};notices.update(failures)
    for k in index_updates:notices.pop(k,None)
    op=os.environ['WEB_PROFILE_AUTOMATION_ID']
    payload={**start.value,'checkpoint_operation':op,
             'files':[row|{'profile':WEB_PROFILE} if row['key'] in index_updates else row for row in start.value['files']],
             'failures':[notices[k] for k in sorted(notices)]}
    store=cps.store; journal=index.S3Journal(store._client('vomebook')); digest=sha256(index._encode(payload)).hexdigest()
    def check(_):
        if guard() is not True:raise RuntimeError('profile metadata gate lost')
        return True
    permit=index._permit(path,start.raw_sha256,digest,{'profile':sha256(WEB_PROFILE.encode()).hexdigest()})
    if (journal.read(start.raw_sha256,guard=check) is not None or cps.read(B,path)!=start or
            hub.read_bytes(B,path)!=start.raw or journal.claim(start.raw_sha256,permit,guard=check) is not True):
        raise RuntimeError('profile metadata checkpoint is spent or changed; observe only')
    if not check('checkpoint') or cps.read(B,path)!=start:
        raise RuntimeError('profile-only checkpoint parent moved')
    cps.write_once(B,path,payload,start.etag,operation_id=op)
    if sha256(hub.read_bytes(B,path)).hexdigest()!=digest:
        raise RuntimeError('profile-only checkpoint not observed; no retry')


def execute(resource,output):
    ext,search_generation,parent_id=authority_scope(resource)
    hub=v3.CentralHubStore(); api=daily.GitHub(os.environ['GH_TOKEN']); owner=_owner()
    guard=lambda:ordinary_writer(hub,parent_id,api,observed=owner)
    search_raw=hub.read_bytes(backlog.SEARCH_BUCKET,backlog.CURRENT)
    def validate_search():
        if hub.read_bytes(backlog.SEARCH_BUCKET,backlog.CURRENT)!=search_raw:
            raise RuntimeError('automatic web profile Search pointer changed')
        return guard()
    validate_search()
    root=v3.current(hub)[0]
    # This proof is used before the first data mutation and sealed to the
    # ordinary projection promotion. Tokens stay inside the existing writer.
    render_parent=pages.current(root)
    if render_parent['search_generation']!=search_generation:
        raise RuntimeError('legacy web profile operation Search parent differs')
    cps=ConditionalCheckpointStore(); store=cps.store; client=store._client('vomebook'); legacy=LegacyRoots(client)
    pointer_raw=hub.read_bytes(B,v3.POINTER); side_before=hub.read_bytes(B,SIDECAR)
    path=f'documents/web/{ext}/index.json'; observed=cps.read(B,path);_,old=index._index(observed.raw)
    details={}
    with httpx.Client(timeout=90,follow_redirects=True) as http:
        summary,_=backlog.inspect(hub=hub,api=backlog.HfApi(token=hub.token(B)),fs=backlog.HfFileSystem(token=hub.token(B)),
            client=http,threshold=daily.THRESHOLD,scope='all',details_out=details,include_web_profile_upgrades=True,
            include_profile_upgrades=os.getenv('READER_CHM_PROFILE_REBUILD_ENABLED')=='true')
    selected=daily.validate_summary(summary)[ext]
    if (summary['search_generation'],summary['reader_generation'])!=(search_generation,root['generation']) or daily.blockers(selected):
        raise RuntimeError('fresh web profile backlog became blocked')
    pin=os.getenv('READER_WEB_PROFILE_CANDIDATE_INDEX','')
    reviewed={}
    if pin:
        claims=S3ClaimStore(client)
        def cached_get(path, ceiling):
            return claims._get(path,maximum=ceiling)[0]
        reviewed=candidates.load(cached_get,pin)
    pairs=[]; objects={}; same={}; holds={}; notes={}; total=0
    reused=set()
    def hold(key, item, before, reason):
        holds[key]={'key':key,'repo':before['repo'],'path':before['path'],'extension':ext,'source_revision':item['revision'],
            'profile':WEB_PROFILE,'source_sha256':before['source_sha256'],'error':reason,'profile_upgrade_hold':True}
    for key,row in sorted(details['rows'].items()):
        item=details['records'][key]
        if item['extension']!=ext or row['state']!='profile_outdated' or row['recorded_failure'] or row.get('pending_shard_checkpoint'):
            continue
        if len(pairs)+len(same)+len(holds)>=MAX_ROWS:break
        before=old[key]
        if any(before[k]!=row['previous_'+k] for k in ('source_sha256','object','manifest','sha256','bytes')):
            raise RuntimeError('old route changed during profile scan')
        prepared=candidates.reuse(cached_get,reviewed,candidates.source_identity(item,before)) if reviewed else None
        try:
            new,data,manifest,omissions=candidate(item,before,reviewed=prepared)
        except convert.Hold as error:
            if str(error) in {'download_failed','download_timeout','download_destination','download_response'}:
                raise
            hold(key,item,before,str(error))
            continue
        notes[key]=omissions
        if new['object']==before['object']:
            if prepared is not None:reused.add(key)
            same[key]=new;continue
        if total+len(data)>schema.MAX_CONTENT:
            break
        if prepared is not None:reused.add(key)
        total+=len(data);i=f'{len(pairs):04d}'
        pairs.append({'id':i,'new':new,'expected_old_object':before['object']})
        objects[i+'/document.html']=data;objects[i+'/document-manifest.json']=manifest
    if not (pairs or same or holds):raise RuntimeError('no exact profile work remains')
    replay_report=None
    if pairs:
        documents={row['new']['sha256']:objects[row['id']+'/document.html'] for row in pairs}
        replay_report=replay.verify(documents,render_parent['code_sha'])
        (output/'web-profile-replay.json').write_bytes(v3.encode(replay_report))
        rejected=replay.rejected(replay_report,documents)
        for row in pairs:
            if row['new']['sha256'] in rejected:
                key=row['new']['key']
                hold(key,details['records'][key],old[key],'reader_replay_mismatch')
                for member in ('document.html','document-manifest.json'):
                    del objects[row['id']+'/'+member]
        pairs=[row for row in pairs if row['new']['sha256'] not in rejected]
    validate_search()
    if cps.read(B,path)!=observed or hub.read_bytes(B,v3.POINTER)!=pointer_raw or hub.read_bytes(B,SIDECAR)!=side_before:
        raise RuntimeError('web batch parents moved before preparation')
    ret={'applied':True,'generation':root['generation'],'changed_rows':0,'pages_pending':False,'objects_deleted':False}
    rows=[]
    if pairs:
        buf=BytesIO(); request={'version':1,'rows':pairs,'expected_generation':root['generation'],'timestamp':datetime.now(timezone.utc).isoformat()}
        with ZipFile(buf,'w',ZIP_DEFLATED) as z:
            z.writestr('approval.json',v3.encode(request))
            for name,data in sorted(objects.items()):z.writestr(name,data)
        raw=buf.getvalue(); blob,receipt=prepare.prepare(raw,sha256(raw).hexdigest(),hub,legacy,client)
        report,files,parents,_=schema.parse(blob,receipt['bundle_sha256'],receipt['review_sha256'])
        if parents[ext]!=observed.raw or files['pointer.json']!=pointer_raw or files['sidecar-before.gz']!=side_before:
            raise RuntimeError('sealed profile review differs from producer parents')
        proof=batch_review.authority(report,files,parents,checkpoints=cps,pointers=ConditionalPageStore(hub,s3=store),legacy=legacy,hub=hub,s3=store)
        publish_pages=pages.current(root,expected=render_parent)
        with httpx.Client(timeout=20, follow_redirects=True, headers={'Cache-Control':'no-cache'}) as http:
            if replay.space_revision(http)!=replay_report['space']['code']:
                raise RuntimeError('Space changed after Reader replay')
        proof.update(bundle_sha256=receipt['bundle_sha256'],review_sha256=receipt['review_sha256'],pages=publish_pages)
        # Persist exact preparation locally before the one-shot indexes. Failed
        # jobs upload these artifacts; they do not authorize automatic replay.
        (output/'web-profile-review.zip').write_bytes(blob)
        (output/'web-profile-authority.json').write_bytes(v3.encode(proof))
        (output/'web-profile-review.json').write_bytes(v3.encode({'target':report['target_pointer'],'rows':report['rows']}))
        ret=publish.run(blob,receipt['bundle_sha256'],receipt['review_sha256'],proof,phase='apply',
                        external_writers_stopped=False,managed=True,controller_guard=validate_search)
        rows=report['rows']
    if same or holds:
        # Record non-routing outcomes only against the precise canonical
        # successor of this run (or the original index if no route moved).
        expected_raw=files[f'target-{ext}.json'] if pairs else observed.raw
        if cps.read(B,path).raw!=expected_raw or hub.read_bytes(B,path)!=expected_raw:
            raise RuntimeError('web routing or hold checkpoint drifted')
        checkpoint_non_routing(cps,hub,path,old,same,holds,validate_search)
    ret.update(profile={'version':WEB_PROFILE,'extension':ext,'converted':len(pairs),'identical':len(same),
                        'held':len(holds),'holds':holds,'omissions':notes,'rows':rows,'reused':len(reused)})
    return ret


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--resource-json',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.parent.mkdir(parents=True,exist_ok=True); result=execute(json.loads(a.resource_json),a.output.parent)
    with a.output.open('xb') as f:f.write(v3.encode(result))

if __name__=='__main__':main()
