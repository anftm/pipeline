"""Central exact Office report transaction, with spent-permit observation phases.

Called with reviewed private files and an independent exact SHA-256 only
from the verified central writer window.
"""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryFile

from . import publish_reader_v3 as v3
from . import reader_exact_office_plan as plan, reader_exact_office_stage as stage
from . import reader_image_history_publish as legacy, reader_image_history_v3 as promote
from .reader_image_history_parent import CatalogParent
from .reader_image_history_publish_journal import LegacyJournal
from .reader_image_history_publish_s3 import LegacyRoots
from .reader_page_cas import ConditionalPageStore
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore
from .reader_web_exact_apply import PointerStore, _check_pointer_phase, guard_window

B=v3.ASSETS
SEAL='reader-office-legacy-publish-v1'


def _sealed(root, path, digest):
    raw=plan.member(root,path)
    if sha256(raw).hexdigest()!=digest:
        raise ValueError('review input hash mismatch: '+path)
    return raw


def run(root: Path, expected_sha: str, *, phase, external_writers_stopped):
    if phase not in {'apply','resume-index','resume-legacy','observe-v3'}:
        raise ValueError('explicit exact Office phase required')
    review=json.loads(_sealed(root,'review.json',expected_sha))
    raw=_sealed(root,'index.json',review['index_sha256'])
    before=_sealed(root,'sidecar-before.gz',review['before_sidecar_sha256'])
    after=_sealed(root,'sidecar-after.gz',review['after_sidecar_sha256'])
    pointer_raw=_sealed(root,'pointer.json',review['pointer_sha256'])
    pointer=json.loads(pointer_raw)
    bundles={ext:root/ext for ext in plan.PINS}
    replacements={ext:dict(root=plan.root(ext,True),manifest=plan.root(ext,True)+'/chapter-manifest.json',
                 search_index=plan.root(ext,True)+'/epub-search-index.json.gz') for ext in plan.PINS}
    target,old,new=plan.exact_rows(raw,review['index_sha256'],replacements)
    target['checkpoint_operation']=review['operation']
    if plan.digest(stage.encode_index(target))!=review['target_index_sha256']:
        raise ValueError('sealed Office index successor differs')
    hub=v3.CentralHubStore()
    window=lambda:guard_window(hub,external_writers_stopped)
    stage.confirm(window)
    cps=ConditionalCheckpointStore();s3=cps.store;client=s3._client('vomebook')
    controls=ConditionalPageStore(hub,s3=s3)
    transport=LegacyRoots(client);journal=LegacyJournal(client)
    cat_parent=legacy.Version(**review['catalog'])
    archive=CatalogParent(client,digest=cat_parent.sha256,size=cat_parent.size,etag=cat_parent.etag)
    successor=review['target_pointer']
    expected_live=v3.encode(successor) if phase=='observe-v3' else pointer_raw
    _check_pointer_phase(hub,s3,expected_live)
    if phase!='observe-v3':
        projected,generated=plan.project(hub,before,old,new)
        if projected!=pointer or plan.routes.decode_sidecar(generated)!=plan.routes.decode_sidecar(after):
            raise ValueError('Office proposed projection differs from the reviewed four routes')
        class TargetView:
            def read_bytes(self,bucket,path):
                return after if (bucket,path)==(B,legacy.SIDECAR) else hub.read_bytes(bucket,path)
        expected=v3.unify(TargetView(),apply=False,expected_pointer=pointer,expected_canonical=plan.digest(after))
        if expected.get('pointer')!=successor:
            raise ValueError('Office reviewed Reader successor differs')
    files={}
    for ext,bundle in bundles.items():
        prior=hub.read_bytes(B,plan.root(ext,False)+'/chapter-manifest.json')
        if plan.digest(prior)!=review['old_members'][ext]['chapter-manifest.json']['sha256']:
            raise ValueError('Office source manifest differs from review')
        _,files[ext]=plan.validate_bundle(ext,bundle,json.loads(prior))
    side_parent=legacy.Version(plan.digest(before),len(before),review['sidecar_etag'])
    def check_side_parent():
        if controls.read_for_update(B,legacy.SIDECAR)!=(before,side_parent.etag) or hub.read_bytes(B,legacy.SIDECAR)!=before:
            raise ValueError('Office authored sidecar parent no longer agrees')
    if phase in {'apply','resume-index'}:check_side_parent()
    with TemporaryFile() as parent_stream:
        if phase=='apply':
            parent=cps.read(B,plan.INDEX)
            if (parent.raw,parent.etag)!=(raw,review['index_etag']) or hub.read_bytes(B,plan.INDEX)!=raw:
                raise ValueError('Office current canonical chapter index differs')
            archive._download(legacy.CATALOG,parent_stream,condition=cat_parent.etag)
        else:
            archive.load(parent_stream)
        def make_plan(out):
            parent_stream.seek(0)
            return stage.plan_legacy(parent_stream,out,before=before,after=after,old=old,new=new,
                                     now=review['timestamp'],parent_sha=cat_parent.sha256)
        with TemporaryFile() as scratch: summary=make_plan(scratch)
        cat_target=(summary['target_sha256'],summary['target_bytes'])
        if cat_target!=(review['legacy_successor_sha256'],review['legacy_successor_bytes']):
            raise ValueError('Office proposed legacy successor differs')
        if phase=='apply':archive.stage(parent_stream,guard=window)
        pins=stage.stage(old=old,bundles=bundles,files=files,snapshot=summary['snapshot'],packed=after,
                         old_members=review['old_members'],hub=hub,s3=s3,client=client,guard=window,
                         observe_only=phase!='apply')
        def protected():
            stage.confirm(window)
            _check_pointer_phase(hub,s3,expected_live)
            for path,record in pins.items():
                if s3.read_bytes(B,path)!=record or hub.read_bytes(B,path)!=record:
                    raise ValueError('Office independent retention proof disappeared')
            return True
        def before_index():
            protected()
            if phase in {'apply','resume-index'}:check_side_parent()
            return True
        outcome=stage.canonical_once(cps,client,before_sha=review['index_sha256'],target=target,
                    target_sha=review['target_index_sha256'],review_sha=expected_sha,parent_etag=review['index_etag'],
                    guard=before_index,observe=phase!='apply')
        def after_index():
            protected()
            wanted=stage.encode_index(target)
            if cps.read(B,plan.INDEX).raw!=wanted or hub.read_bytes(B,plan.INDEX)!=wanted:
                raise ValueError('Office exact canonical successor is not visible to both readers')
            return True
        if phase=='observe-v3':
            after_index()
            cat=transport.observe(legacy.CATALOG);side=transport.observe(legacy.SIDECAR)
            wanted=(plan.digest(after),len(after))
            seal=legacy.transition_seal(SEAL,cat_parent,side_parent,cat_target,wanted,pointer_raw)
            if ((cat.sha256,cat.size)!=cat_target or (side.sha256,side.size)!=wanted
                    or journal.read(cat_parent.sha256)!=legacy.Record(seal,'legacy-observed',cat.etag,side.etag)
                    or hub.read_bytes(B,legacy.SIDECAR)!=after or hub.digest(B,legacy.CATALOG)!=cat_target[0]):
                raise ValueError('Office exact legacy successor not observed')
            moved=dict(seal=seal,catalog=cat,sidecar=side)
        else:
            moved=legacy.publish_route_transition(parent=cat_parent,before_sidecar=before,sidecar_etag=side_parent.etag,
                  after_sidecar=after,key=frozenset(new),expected_catalog_sha256=cat_target[0],
                  expected_catalog_bytes=cat_target[1],expected_pointer_raw=pointer_raw,journal=journal,
                  observe=transport.observe,reader_catalog_digest=lambda:hub.digest(B,legacy.CATALOG),
                  cas=transport.put_once,store=hub,guard=lambda _:after_index(),resume=phase=='resume-legacy',
                  seal_kind=SEAL,plan_catalog=make_plan)
    def check_promotion():
        # The shared promotion helper checks the successor after PUT. At that
        # point do not require the parent still to be live, but accept no third state.
        stage.confirm(window)
        current=hub.read_bytes(B,v3.POINTER)
        if current not in (pointer_raw,v3.encode(successor)):
            raise ValueError('Office v3 pointer diverged from its sealed states')
        _check_pointer_phase(hub,s3,current)
        for path,record in pins.items():
            if s3.read_bytes(B,path)!=record or hub.read_bytes(B,path)!=record:
                raise ValueError('Office retention drift before promotion')
        if cps.read(B,plan.INDEX).raw_sha256!=review['target_index_sha256']:
            raise ValueError('Office canonical index changed before promotion')
        for path,record in ((legacy.CATALOG,moved['catalog']),(legacy.SIDECAR,moved['sidecar'])):
            observed=client.head_object(Bucket='reader-assets-v2',Key=path)
            if (observed['ContentLength'],observed['ETag'])!=(record.size,record.etag):
                raise ValueError('Office legacy root changed before v3 promotion')
        return True
    answer=promote.promote_route(store=PointerStore(hub,s3,check_promotion,pointer_raw),client=client,
        parent_raw=pointer_raw,target_pointer=successor,canonical_sha256=plan.digest(after),legacy_seal=moved['seal'],
        guard=check_promotion,observe_only=phase=='observe-v3',permit_kind='reader-office-v3-permit')
    return {**answer,'applied':True,'canonical':outcome,'changed_rows':4,'objects_deleted':False,
            'retention_roots':sorted(pins),'pages_pending':True}
