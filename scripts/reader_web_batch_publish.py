"""Publish a separately attested exact ten-web batch in the central writer window."""
from tempfile import TemporaryFile
from . import publish_reader_v3 as v3, reader_web_batch_indexes as indexes, reader_web_batch_plan as plan
from . import reader_image_history_publish as legacy, reader_image_history_v3 as promote
from . import reader_web_batch_review as review_module, reader_web_index_guard as index
from .reader_image_history_parent import CatalogParent
from .reader_image_history_publish_journal import LegacyJournal
from .reader_image_history_publish_s3 import LegacyRoots
from .reader_image_history_store import S3ClaimStore
from .reader_page_cas import ConditionalPageStore
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore
from .reader_web_exact_apply import PointerStore, _check_pointer_phase, guard_window
from .reader_web_exact_stage import immutable_creator

B=v3.ASSETS
SEAL='reader-web-legacy-publish-v1'


def validate_legacy_plan(actual, reviewed):
    # Round-trip the Python retirement tuple through the same schema as the
    # reviewed JSON. Comparing native container types rejects valid inputs.
    if v3.encode(actual)!=v3.encode(reviewed):
        raise ValueError('sealed streamed catalog plan differs')


def run(blob, bundle_sha, review_sha, proof, *, phase, external_writers_stopped, managed=False,
        controller_guard=None, immutable_payload_guard=None):
    if phase not in {'apply','resume-index','resume-legacy','observe-v3'}:
        raise ValueError('explicit ten-web phase required')
    if managed:
        from . import reader_web_managed_review, reader_web_managed_pages
        review,data,parents,pairs=reader_web_managed_review.parse(blob,bundle_sha,review_sha)
    else:
        review,data,parents,pairs=review_module.parse(blob,bundle_sha,review_sha)
    if (proof.get('version')!=1 or proof.get('remote_writes')!=0
            or proof.get('parent_generation')!=review['parent_generation']
            or proof.get('pointer_sha256')!=plan.digest(data['pointer.json'])
            or proof.get('review_sha256')!=review_sha or proof.get('bundle_sha256')!=bundle_sha):
        raise ValueError('authority receipt does not bind this exact batch')
    if controller_guard is not None and (not managed or phase!='apply' or external_writers_stopped is not False):
        raise ValueError('controller admission is restricted to fresh managed apply')
    if immutable_payload_guard is not None and controller_guard is None:
        raise ValueError('immutable payload guard requires managed controller admission')
    hub=v3.CentralHubStore()
    hold_proof=None
    if managed:
        from . import reader_web_hold_admission as admission
        if admission.DIGEST_FIELD in review:
            if phase!='apply' or not isinstance(proof.get('hold_admission'),dict):
                raise ValueError('managed hold replacement requires its exact hosted proof and fresh manual apply')
            from . import reader_web_hold_live as hold_live
            hold_proof=proof['hold_admission']
            hold_search=hub.read_bytes(hold_live.backlog.SEARCH_BUCKET,hold_live.backlog.CURRENT)
    def window():
        ok=controller_guard() if controller_guard is not None else guard_window(hub,external_writers_stopped)
        if ok is not True:raise RuntimeError('exact web publishing guard is absent')
        if managed:
            if hold_proof is not None:
                # These pins are independent of the v3 parent becoming the target.
                if hub.read_bytes(hold_live.backlog.SEARCH_BUCKET,hold_live.backlog.CURRENT)!=hold_search:
                    raise RuntimeError('hold admission Search changed during publication')
            for ext,expect in review['unchanged_indexes'].items():
                if plan.digest(hub.read_bytes(B,f'documents/web/{ext}/index.json'))!=expect:
                    raise ValueError('unrelated web index moved during managed batch')
        return True
    window()
    if managed and phase!='observe-v3':
        pages=reader_web_managed_pages.current(review['parent_pointer'],expected=proof['pages'])
        if hold_proof is not None:
            hold_live.prove(review,data,parents,hub,pages,prior=hold_proof)
    cps=ConditionalCheckpointStore();s3=cps.store;client=s3._client('vomebook')
    transport=LegacyRoots(client);journal=LegacyJournal(client);claims=S3ClaimStore(client)
    pointers=ConditionalPageStore(hub,s3=s3)
    cat_parent=legacy.Version(**proof['catalog']);side_parent=legacy.Version(**proof['sidecar'])
    before=data['sidecar-before.gz'];after=data['sidecar-after.gz'];ptr=data['pointer.json'];successor=review['target_pointer']
    meta=review['legacy']; sx=v3.encode(successor)
    cat_target=(meta['target_sha256'],meta['target_bytes']);side_target=(plan.digest(after),len(after))
    if ((cat_parent.sha256,cat_parent.size)!=(meta['parent_sha256'],meta['parent_bytes'])
        or (side_parent.sha256,side_parent.size)!=(plan.digest(before),len(before))):
        raise ValueError('legacy parent attestation differs from the sealed batch')
    archive=CatalogParent(client,digest=cat_parent.sha256,size=cat_parent.size,etag=cat_parent.etag)
    expected_live=sx if phase=='observe-v3' else ptr
    _check_pointer_phase(hub,s3,expected_live)
    if phase!='observe-v3':
        prior,want,packed=plan.project(hub,before,pairs)
        if prior['generation']!=review['parent_generation'] or want!=successor or packed!=after:
            raise ValueError('the exact published batch projection differs')
    seal=legacy.transition_seal(SEAL,cat_parent,side_parent,cat_target,side_target,ptr)
    def protect_input(path,raw=None,*,digest=None,length=None,mime='application/json',guard=window):
        try:found=s3.read_bytes(B,path)
        except FileNotFoundError:
            if phase!='apply' or raw is None:raise
            if guard() is not True:raise RuntimeError('immutable Web staging guard is absent')
            immutable_creator(client)(path,raw,mime);found=s3.read_bytes(B,path)
        if ((raw is not None and found!=raw) or (digest is not None and plan.digest(found)!=digest)
            or (length is not None and len(found)!=length) or hub.read_bytes(B,path)!=found):
            raise ValueError('exact batch asset protection bytes differ: '+path)
    pin=review['retention']['path']; targets={e:data[f'target-{e}.json'] for e in parents}
    prereq={'review':review_sha,'sidecar':side_parent.sha256,'legacy_catalog':cat_parent.sha256,'pointer':plan.digest(ptr),'retention':review['retention']['sha256']}
    # Canonical permits continue to bind the entire previous review schema;
    # manual readers can still identify a spent automatic index permit.
    if phase in {'apply','resume-index'}:
        if journal.read(cat_parent.sha256) is not None:
            raise RuntimeError('legacy parent already spent; reconcile before changing indexes')
        permit=promote.PREFIX+plan.digest(ptr)+'.json'
        if claims._get(permit,maximum=promote.LIMIT)[0] is not None:
            raise RuntimeError('v3 parent already spent; reconcile before changing indexes')
        if (pointers.read_for_update(B,legacy.SIDECAR)!=(before,side_parent.etag)
                or hub.read_bytes(B,legacy.SIDECAR)!=before):
            raise ValueError('legacy sidecar parent no longer agrees')
        cat=transport.observe(legacy.CATALOG)
        if (cat.sha256,cat.size,cat.etag)!=(cat_parent.sha256,cat_parent.size,cat_parent.etag):
            raise ValueError('live legacy catalog parent no longer agrees')
        for e,obj in review['canonical'].items():
            s=cps.read(B,obj['path']); allowed=(parents[e],targets[e]) if phase=='resume-index' else (parents[e],)
            if s.raw not in allowed or s.raw!=hub.read_bytes(B,obj['path']):
                raise ValueError('canonical parent/view mismatch before staging: '+e)
            if (s.raw==parents[e] and s.etag!=proof['index_etags'][e]):
                raise ValueError('canonical parent ETag mismatch: '+e)
            if phase=='apply' and index.S3Journal(client).read(obj['parent_sha256'],guard=lambda _:window()) is not None:
                raise ValueError('exact canonical parent permit is already spent: '+e)
    with TemporaryFile() as catalog_parent:
        if phase=='apply':archive._download(legacy.CATALOG,catalog_parent,condition=cat_parent.etag)
        else:archive.load(catalog_parent)
        def make_plan(out):
            catalog_parent.seek(0)
            return plan.plan_legacy(catalog_parent,out,before=before,after=after,pairs=pairs,
                                    timestamp=review['timestamp'],parent_sha=cat_parent.sha256)
        with TemporaryFile() as out:
            validate_legacy_plan(make_plan(out),meta)
        # Managed review rejects declared sizes above the public cap. Verify
        # actual old bytes as well before the first managed immutable write.
        if phase=='apply' and not managed:archive.stage(catalog_parent,guard=window)
        for row in review['rows']:
            old=row['old'];protect_input(old['object'],digest=old['sha256'],length=old['bytes'])
            protect_input(old['manifest'],digest=row['old_manifest_sha256'])
        if phase=='apply' and managed:archive.stage(catalog_parent,guard=window)
        # Only this loop creates content-addressed, unpublished payloads.
        # The first missing payload triggers a full scan; leave staging with
        # a fresh boundary check even when retention/snapshot already exist.
        payload_guard = window if immutable_payload_guard is None else immutable_payload_guard
        for row in review['rows']:
            new=row['new']; ident=row['id']
            protect_input(new['object'],data[ident+'/document.html'],mime='text/html; charset=utf-8',guard=payload_guard)
            protect_input(new['manifest'],data[ident+'/document-manifest.json'],guard=payload_guard)
        if immutable_payload_guard is not None:
            window()
        protect_input(pin,data['retained.json'])
        snapshot=meta['snapshot']['path'];protect_input(snapshot,after,mime='application/gzip')
        def protected():
            if window() is not True:return False
            _check_pointer_phase(hub,s3,expected_live)
            for path,raw in ((pin,data['retained.json']),(snapshot,after)):
                if s3.read_bytes(B,path)!=raw or hub.read_bytes(B,path)!=raw:
                    raise ValueError('exact batch reachability or snapshot drifted')
            return True
        index_journal=index.S3Journal(client)
        def all_targets():
            for ext,item in review['canonical'].items():
                if cps.read(B,item['path']).raw!=targets[ext] or hub.read_bytes(B,item['path'])!=targets[ext]:
                    raise RuntimeError('exact canonical batch target missing: '+ext)
                expected=index._permit(item['path'],item['parent_sha256'],item['target_sha256'],prereq)
                if index_journal.read(item['parent_sha256'],guard=lambda _:True)!=expected:
                    raise RuntimeError('exact canonical batch permit missing: '+ext)
        if phase in {'apply','resume-index'}:
            if managed:
                reader_web_managed_pages.current(review['parent_pointer'],expected=proof['pages'])
                if hold_proof is not None:
                    hold_live.pins(hub,review['parent_pointer'],hold_search,hold_proof['space_commit'])
            def before_indexes():
                if protected() is not True:return False
                head=client.head_object(Bucket='reader-assets-v2',Key=legacy.CATALOG)
                return ((head.get('ETag'),head.get('ContentLength'))==(cat_parent.etag,cat_parent.size)
                        and pointers.read_for_update(B,legacy.SIDECAR)==(before,side_parent.etag)
                        and hub.read_bytes(B,legacy.SIDECAR)==before)
            outcomes=indexes.canonical_set(checkpoints=cps,journal=index_journal,hub=hub,parents=parents,pairs=pairs,
                       operation=review['operation'],etags=proof['index_etags'],prerequisites=prereq,guard=before_indexes,recover=phase!='apply',managed=managed)
        else:outcomes={e:'observed' for e in targets}
        def legacy_guard(_):
            if protected() is not True:return False
            all_targets();return True
        if phase=='observe-v3':
            if legacy_guard('observe') is not True:raise RuntimeError('cannot verify exact writer window')
            c=transport.observe(legacy.CATALOG);s=transport.observe(legacy.SIDECAR)
            if ((c.sha256,c.size)!=cat_target or (s.sha256,s.size)!=side_target or
                journal.read(cat_parent.sha256)!=legacy.Record(seal,'legacy-observed',c.etag,s.etag)
                or hub.digest(B,legacy.CATALOG)!=cat_target[0] or hub.read_bytes(B,legacy.SIDECAR)!=after):
                raise ValueError('exact legacy batch successor not observed')
            moved={'seal':seal,'catalog':c,'sidecar':s}
        else:
            moved=legacy.publish_route_transition(parent=cat_parent,before_sidecar=before,sidecar_etag=side_parent.etag,
                after_sidecar=after,key=frozenset(o['key'] for o,_ in pairs),expected_catalog_sha256=cat_target[0],
                expected_catalog_bytes=cat_target[1],expected_pointer_raw=ptr,journal=journal,observe=transport.observe,
                reader_catalog_digest=lambda:hub.digest(B,legacy.CATALOG),cas=transport.put_once,store=hub,
                guard=legacy_guard,resume=phase=='resume-legacy',seal_kind=SEAL,plan_catalog=make_plan)
    def promotion_guard():
        if window() is not True:return False
        current=hub.read_bytes(B,v3.POINTER)
        if current not in (ptr,sx):raise RuntimeError('Reader pointer is neither sealed state')
        _check_pointer_phase(hub,s3,current);all_targets()
        if any(s3.read_bytes(B,path)!=raw or hub.read_bytes(B,path)!=raw for path,raw in ((pin,data['retained.json']),(snapshot,after))):
            raise RuntimeError('batch protection drifted')
        for path,key in ((legacy.CATALOG,'catalog'),(legacy.SIDECAR,'sidecar')):
            o=moved[key];h=client.head_object(Bucket='reader-assets-v2',Key=path)
            if (h.get('ContentLength'),h.get('ETag'))!=(o.size,o.etag):raise RuntimeError('legacy successor ETag drifted')
        return True
    result=promote.promote_route(store=PointerStore(hub,s3,promotion_guard,ptr),client=client,
             parent_raw=ptr,target_pointer=successor,canonical_sha256=plan.digest(after),legacy_seal=moved['seal'],
             guard=promotion_guard,observe_only=phase=='observe-v3',permit_kind='reader-web-v3-permit')
    result={**result,'applied':True,'canonical':outcomes,'changed_rows':len(pairs),'retention_root':pin,
            'retirement_hint':meta['retirement_hint'],'objects_deleted':False,'pages_pending':True,'deleted_objects':0}
    if hold_proof is not None:
        result['pages_pins']={k:proof['pages'][k] for k in ('code_sha','search_generation')}
        result['hold_admission']=hold_proof
        result['failure_notices_retained']=True
    return result
