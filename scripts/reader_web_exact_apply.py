"""Apply one reviewed historical web replacement in the central writer window.

This is not a dispatch command. The caller supplies a private bundle reviewed
against the exact deployment parents; no credentials belong in that bundle.
Resume is an explicit phase, never an automatic rerun of a mutating command.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryFile

from . import publish_reader_v3 as v3
from . import reader_image_history_publish as legacy
from . import reader_image_history_v3 as promotion
from . import reader_web_exact_legacy as web
from . import reader_web_exact_plan as preflight
from . import reader_web_exact_stage as assets
from . import reader_web_index_guard as index_guard
from .reader_gc_fence import GitHubProducerFence
from .reader_image_history_fence import WORKFLOW, _active_owner, _owner
from .reader_image_history_parent import CatalogParent
from .reader_image_history_publish_journal import LegacyJournal
from .reader_image_history_publish_s3 import LegacyRoots
from .reader_page_cas import ConditionalPageStore
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore, merge_observed

B = preflight.BUCKET


def digest(raw):
    return sha256(raw).hexdigest()


def read_json_file(root, name, size):
    return preflight._json(preflight._read(root / name, size, name), name)


def verify_options(root, review_sha256):
    raw = preflight._read(root / 'review.json', 32 * 1024, 'web review')
    if digest(raw) != review_sha256:
        raise ValueError('web review differs from independently supplied SHA')
    review = preflight._json(raw, 'web review')
    required = {'version', 'operation', 'index_sha256', 'target_index_sha256', 'old_manifest_sha256',
                'old_row_sha256', 'new_row_sha256', 'new_manifest_sha256', 'packed_before_sha256',
                'packed_after_sha256', 'legacy_before', 'legacy_after_sha256', 'legacy_after_bytes',
                'timestamp', 'reader_pointer_sha256', 'sidecar_etag_sha256', 'target_pointer'}
    if review.keys() != required or type(review['version']) is not int or review['version'] != 1:
        raise ValueError('invalid exact web apply review fields')
    for field in required:
        if field.endswith('_sha256') and not preflight._is_sha(review[field]):
            raise ValueError('invalid SHA field: ' + field)
    return review


def guard_window(primary, external_writers_stopped):
    # An explicit declaration is separate from GitHub. Default to blocked;
    # neither ETags nor the observed Actions inventory proves external isolation.
    if external_writers_stopped is not True:
        raise RuntimeError('independent Reader writers and GC must be stopped throughout this operation')
    v3.assert_writer(primary)
    owner = _owner()
    fence = GitHubProducerFence()
    _active_owner(fence, *owner)
    fence.check(allow_current_workflow=WORKFLOW, all_workflows=True, web_retirement_receipts=True)
    _active_owner(fence, *owner)
    if _owner() != owner:
        raise RuntimeError('central web publisher owner changed')
    return True


class PointerStore(ConditionalPageStore):
    """Recheck window before the actual pointer CAS, not just at launch."""
    def __init__(self, primary, s3, guard, parent_raw):
        super().__init__(primary, s3=s3)
        self.guard = guard
        self.parent_raw = parent_raw

    def put_bytes(self, bucket, path, raw):
        # Ordinary v3 unification writes only these content-addressed records
        # before the pointer. Never use the unconditional Hub transport here.
        if (bucket != B or not re.fullmatch(r'reader-index/(?:snapshots/[0-9a-f]{64}/reader_assets\.json\.gz|'
                r'v3/(?:generations/[0-9a-f]{64}/catalog\.json|promotions/[0-9a-f]{64}\.json))',path)):
            raise ValueError('exact web v3 writer cannot write an unrelated object')
        if self.guard() is not True:
            raise RuntimeError('web pointer guard missing before immutable v3 staging')
        return self.immutable_put(bucket,path,raw)

    def put_if_match(self, bucket, path, raw, etag):
        if (bucket, path) != (B, v3.POINTER):
            raise ValueError('web pointer adapter cannot change another root')
        if self.guard() is not True:
            raise RuntimeError('web pointer guard missing before CAS')
        before=self.primary.read_bytes(bucket,path)
        s3_before,fresh=self.read_for_update(bucket,path)
        if fresh!=etag or s3_before!=self.parent_raw or before!=self.parent_raw:
            raise ValueError('same-response pointer parents differ just before CAS')
        return super().put_if_match(bucket, path, raw, etag)


def _check_pointer_phase(primary, s3, expected_live):
    raw, tag = ConditionalPageStore(primary,s3=s3).read_for_update(B,v3.POINTER)
    if raw!=expected_live or primary.read_bytes(B,v3.POINTER)!=expected_live:
        raise ValueError('same-response S3 and Hub pointers must match the reviewed phase')
    return tag


def run(root, review_sha256, *, phase, external_writers_stopped):
    if phase not in {'apply', 'resume-index', 'resume-legacy', 'observe-v3'}:
        raise ValueError('explicit web write/recovery phase required')
    review = verify_options(root, review_sha256)
    local = preflight.plan(canonical=root/'canonical.json', canonical_sha256=review['index_sha256'],
                           old_row=root/'old.json', candidate_row=root/'new.json', manifest=root/'document-manifest.json',
                           html=root/'document.html', original=root/'original')
    for file, key, maximum in [('old.json','old_row_sha256',64*1024), ('new.json','new_row_sha256',64*1024),
                              ('pointer.json','reader_pointer_sha256',8192), ('sidecar-etag','sidecar_etag_sha256',4096),
                              ('sidecar-before.gz','packed_before_sha256',50*1024*1024)]:
        if digest(preflight._read(root/file, maximum, file)) != review[key]:
            raise ValueError('sealed bundle input differs: '+file)
    if local['candidate']['manifest_sha256'] != review['new_manifest_sha256']:
        raise ValueError('reviewed candidate manifest differs')
    old = read_json_file(root,'old.json',64*1024)
    new = read_json_file(root,'new.json',64*1024)
    if (old['key'] != 'VoiceOfML/MLMRL-Hub\0' +
            '000733/1870520043_8337_《资本论》为什么遭那么多人反感_ - 快资讯.mht'
            or new['sha256'] != 'c069e5671c3949701d842fc8ebcba7e22085eb9ce90b9cacf9e9428fcda866c0'
            or old['sha256'] != 'c77dd3e2fe5f2c84995052454e2baa235ac992ab3e66f1d142a56c26b1e24af5'):
        raise ValueError('this one-shot publisher only admits the reviewed MHT successor')
    parent_value = read_json_file(root,'canonical.json',preflight.MAX_CANONICAL)
    pointer_raw = preflight._read(root/'pointer.json',8192,'pointer')
    before = preflight._read(root/'sidecar-before.gz',50*1024*1024,'sidecar')
    after = preflight._read(root/'sidecar-after.gz',50*1024*1024,'sidecar successor')
    if digest(after) != review['packed_after_sha256']:
        raise ValueError('sidecar successor digest differs')
    web.verified_delta(before,after,old,new)
    index_path = local['canonical_index']
    entry = {**new,'source_revision':old['source_revision']}
    if 'checkpointed_at' in old:
        entry['checkpointed_at'] = old['checkpointed_at']
    target = merge_observed(parent_value,parent_value,[entry],[],kind=preflight.KIND,
                            operation_id=review['operation'],preserve_canonical_rows=True)
    target_raw = index_guard._encode(target)
    if digest(target_raw) != review['target_index_sha256']:
        raise ValueError('sealed target index digest differs')

    primary = v3.CentralHubStore()
    window = lambda: guard_window(primary, external_writers_stopped)
    window()
    checkpoints = ConditionalCheckpointStore()
    s3 = checkpoints.store
    client = s3._client('vomebook')
    transport = LegacyRoots(client)
    journal = LegacyJournal(client)
    parent = legacy.Version(**review['legacy_before'])
    archive = CatalogParent(client,digest=parent.sha256,size=parent.size,etag=parent.etag)
    sidecar_observed = transport.observe(legacy.SIDECAR)
    if phase in {'apply','resume-index'} and sidecar_observed.sha256 != review['packed_before_sha256']:
        raise ValueError('legacy sidecar parent changed')
    # The legacy protocol's journal binds original sidecar ETag for recovery.
    # Recover it from the independently retained, same-response review.
    sidecar_etag = preflight._read(root/'sidecar-etag',4096,'parent sidecar ETag').decode('ascii').strip()
    from .reader_page_cas import strong_etag
    strong_etag(sidecar_etag)
    if phase in {'apply','resume-index'} and sidecar_observed.etag != sidecar_etag:
        raise ValueError('same-response reviewed sidecar ETag changed')

    pointer = json.loads(pointer_raw)
    expected_live = v3.encode(review['target_pointer']) if phase=='observe-v3' else pointer_raw
    def check_pointer():
        return _check_pointer_phase(primary,s3,expected_live)
    check_pointer()
    if phase!='observe-v3':
        if web.project_delta(primary,before,after,old,new)!=pointer:
            raise ValueError('v3 projection parent differs')
        class TargetView:
            def read_bytes(self,bucket,path):
                return after if (bucket,path)==(B,legacy.SIDECAR) else primary.read_bytes(bucket,path)
        expected = v3.unify(TargetView(),apply=False,expected_pointer=pointer,expected_canonical=digest(after))
        if expected.get('pointer')!=review['target_pointer']:
            raise ValueError('sealed v3 successor differs before staging')
    html = preflight._read(root/'document.html',preflight.MAX_HTML,'candidate HTML')
    manifest = preflight._read(root/'document-manifest.json',preflight.MAX_MANIFEST,'candidate manifest')
    pins = {'review':review_sha256,'pointer':review['reader_pointer_sha256'],
            'legacy_catalog':parent.sha256,'sidecar':review['packed_before_sha256']}

    with TemporaryFile() as parent_file:
        if phase=='apply':
            if transport.observe(legacy.CATALOG) != type(sidecar_observed)(parent.sha256,parent.size,parent.etag):
                raise ValueError('same-response legacy catalog parent changed')
            if checkpoints.read(B,index_path).raw_sha256 != review['index_sha256']:
                raise ValueError('canonical MHT parent changed')
            archive._download(legacy.CATALOG,parent_file,condition=parent.etag)
        else:
            archive.load(parent_file)
        with TemporaryFile() as next_catalog:
            summary = web.plan(parent_file,next_catalog,before_packed=before,after_packed=after,old=old,candidate=new,
                               now=review['timestamp'],parent_sha=parent.sha256)
            if (summary['target_sha256'],summary['target_bytes']) != (review['legacy_after_sha256'],review['legacy_after_bytes']):
                raise ValueError('legacy successor differs from sealed review')
        parent_file.seek(0)
        if phase=='apply':
            archive.stage(parent_file,guard=window)
        parent_file.seek(0)

        def no_staging_write(_path,_raw,_mime):
            raise ValueError('recovery requires the exact existing immutable staging objects')

        stage = assets.stage(old=old,candidate=new,html=html,manifest=manifest,
                             old_manifest_sha=review['old_manifest_sha256'],new_manifest_sha=review['new_manifest_sha256'],
                             snapshot=summary['snapshot'],snapshot_bytes=after,s3_read=s3.read_bytes,
                             resolver_read=primary.read_bytes,
                             put_once=assets.immutable_creator(client) if phase=='apply' else no_staging_write,
                             guard=lambda _:window())

        def validate_assets():
            assets.assert_old(primary.read_bytes,old,manifest_sha=review['old_manifest_sha256'])
            pin_path,pin_raw=assets.retention(old)
            new_pin_path,new_pin_raw=assets.new_retention(new)
            if (primary.read_bytes(B,pin_path)!=pin_raw or s3.read_bytes(B,pin_path)!=pin_raw
                    or primary.read_bytes(B,new_pin_path)!=new_pin_raw or s3.read_bytes(B,new_pin_path)!=new_pin_raw
                    or primary.read_bytes(B,new['object'])!=html
                    or primary.read_bytes(B,new['manifest'])!=manifest or primary.read_bytes(B,summary['snapshot']['path'])!=after):
                raise ValueError('exact staging evidence changed')

        def index_window(_phase):
            window();validate_assets()
            if phase in {'apply','resume-index'} and primary.read_bytes(B,legacy.SIDECAR)!=before:
                raise ValueError('sidecar parent changed during canonical transition')
            check_pointer()
            return True

        written=index_guard.apply(checkpoints,index_guard.S3Journal(client),path=index_path,
                                  parent_sha256=review['index_sha256'],target_raw=target_raw,prerequisites=pins,
                                  guard=index_window,resume=phase!='apply')

        def prior_stages():
            window();validate_assets()
            if checkpoints.read(B,index_path).raw!=target_raw or primary.read_bytes(B,index_path)!=target_raw:
                raise ValueError('canonical target missing before legacy/v3 transition')
            return True

        if phase=='observe-v3':
            prior_stages()
            prior=legacy.Version(digest(before),len(before),sidecar_etag)
            cat_target=(review['legacy_after_sha256'],review['legacy_after_bytes'])
            side_target=(digest(after),len(after))
            seal=legacy.transition_seal('reader-web-legacy-publish-v1',parent,prior,cat_target,side_target,pointer_raw)
            record=journal.read(parent.sha256)
            observed_catalog=transport.observe(legacy.CATALOG)
            if (record != legacy.Record(seal,'legacy-observed',observed_catalog.etag,sidecar_observed.etag)
                    or (observed_catalog.sha256,observed_catalog.size)!=cat_target
                    or (sidecar_observed.sha256,sidecar_observed.size)!=side_target
                    or primary.digest(B,legacy.CATALOG)!=cat_target[0]
                    or primary.read_bytes(B,legacy.SIDECAR)!=after):
                raise ValueError('v3 observation requires the exact legacy-observed successor')
            moved={'seal':seal,'catalog':observed_catalog,'sidecar':sidecar_observed}
        else:
            moved=web.publish(parent_file,before_packed=before,after_packed=after,old=old,candidate=new,now=review['timestamp'],
                              parent=parent,sidecar_etag=sidecar_etag,expected_catalog_sha256=review['legacy_after_sha256'],
                              expected_catalog_bytes=review['legacy_after_bytes'],expected_pointer_raw=pointer_raw,
                              journal=journal,observe=transport.observe,reader_catalog_digest=lambda:primary.digest(B,legacy.CATALOG),
                              cas=transport.put_once,store=primary,guard=lambda _:prior_stages(),resume=phase=='resume-legacy')
    def promote_guard():
        prior_stages()
        # Streaming S3 digest and independent resolver once at the transition
        # are part of moved; fresh validator checks keep both legacy roots pinned.
        for path,expected in ((legacy.CATALOG,moved['catalog']),(legacy.SIDECAR,moved['sidecar'])):
            actual=client.head_object(Bucket='reader-assets-v2',Key=path)
            if actual.get('ETag')!=expected.etag or actual.get('ContentLength')!=expected.size:
                raise ValueError('legacy root changed after its sealed readback')
        return True
    result=promotion.promote_route(store=PointerStore(primary,s3,promote_guard,pointer_raw),client=client,parent_raw=pointer_raw,
                  target_pointer=review['target_pointer'],canonical_sha256=review['packed_after_sha256'],
                  legacy_seal=moved['seal'],guard=promote_guard,observe_only=phase=='observe-v3',
                  permit_kind='reader-web-v3-permit')
    return {**result,'applied':True,'canonical':written,'staging':stage,
            'old_object':old['object'],'old_sha256':old['sha256'],'new_sha256':new['sha256'],
            'objects_deleted':False,'deletion_authorized':False,'pages_pending':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--review-sha256',required=True)
    parser.add_argument('--phase',choices=('apply','resume-index','resume-legacy','observe-v3'),required=True)
    parser.add_argument('--external-writers-stopped',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=run(args.bundle,args.review_sha256,phase=args.phase,external_writers_stopped=args.external_writers_stopped)
    with args.output.open('xb') as f:
        f.write(v3.encode(report))

if __name__=='__main__':main()
