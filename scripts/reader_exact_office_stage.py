"""Scoped staging and canonical transition for the reviewed Office report."""
import json
import re

from . import publish_reader_v3 as v3, reader_exact_office_plan as office
from . import reader_image_history_catalog as catalog
from . import reader_lifecycle
from .reader_image_history_store import S3ClaimStore
from .reader_web_exact_stage import immutable_creator

B = v3.ASSETS


def plan_legacy(parent_stream, target_stream, *, before, after, old, new, now, parent_sha):
    left, right = catalog._sidecar(before), catalog._sidecar(after)
    desired = {**left, 'f':{**left['f'], **{key:office.compact(row) for key,row in new.items()}}}
    if desired != right or any(left['f'].get(key) != office.compact(row) for key,row in old.items()):
        raise ValueError('legacy Office route difference is not exact')
    refs = lambda x:{(r['bucket'],r['path']) for r in reader_lifecycle.sidecar_references(x)}
    return catalog.plan_route_transition(parent_stream,target_stream,now=now,
        removed=frozenset((B,row['manifest']) for row in old.values()),
        verified_delta=(office.digest(v3.encode(left)),office.digest(v3.encode(right)),refs(left),refs(right)),
        after_sidecar=after,expected_parent_sha256=parent_sha)


def confirm(guard):
    if guard() is not True:
        raise RuntimeError('exact Office writer window not verified')


def stage(*, old, bundles, files, snapshot, packed, old_members, hub, s3, client, guard, observe_only):
    put = immutable_creator(client)
    pin = office.retention(old,files)
    pin_raw = v3.encode(pin)
    pin_path = 'reader-index/retained-office-white-district-'+office.digest(pin_raw)+'.json'

    def checked(path, raw=None, mime=None, identity=None):
        try:
            present = s3.read_bytes(B,path)
        except FileNotFoundError:
            if observe_only or raw is None:
                raise ValueError('Office stage missing reviewed bytes: '+path) from None
            confirm(guard)
            put(path,raw,mime)
            present = s3.read_bytes(B,path)
        if (raw is not None and raw != present) or (identity is not None and
                (len(present),office.digest(present)) != (identity['bytes'],identity['sha256'])):
            raise ValueError('Office stage differs from exact review: '+path)
        if hub.read_bytes(B,path)!=present:
            raise ValueError('Office S3 and resolver stage differ: '+path)

    for ext in sorted(office.PINS):
        for rel,identity in sorted(old_members[ext].items()):
            checked(office.root(ext,False)+'/'+rel,identity=identity)
    old_pin_raw = v3.encode({'version': 1, 'kind': 'reader-office-retained-bundles',
                              'resources': [{'bucket': B, 'path': path} for path in
                                            sorted({row['manifest'] for row in old.values()})]})
    old_pin_path = 'reader-index/retained-office-old-'+office.digest(old_pin_raw)+'.json'
    checked(old_pin_path,old_pin_raw,'application/json')
    # GC remains paused until both new bundles and independent retained roots
    # are observed. No canonical or pointer mutation is allowed before that.
    for ext in sorted(office.PINS):
        for rel,identity in sorted(files[ext].items()):
            raw=office.member(bundles[ext],rel)
            if (len(raw),office.digest(raw))!=(identity['bytes'],identity['sha256']):
                raise ValueError('Office local member changed before staging')
            mime='application/json' if rel.endswith('.json') else 'application/xhtml+xml' if rel.endswith('.xhtml') else 'text/css' if rel.endswith('.css') else 'application/gzip' if rel.endswith('.gz') else 'application/octet-stream'
            checked(office.root(ext,True)+'/'+rel,raw,mime)
    checked(pin_path,pin_raw,'application/json')
    path=snapshot['path']
    if (snapshot.get('bucket')!=B or not re.fullmatch(r'reader-index/generations/[0-9a-f]{32}/reader_assets\.json\.gz',path)
            or (snapshot['bytes'],snapshot['sha256'])!=(len(packed),office.digest(packed))):
        raise ValueError('Office legacy snapshot does not match sealed bytes')
    checked(path,packed,'application/gzip')
    confirm(guard)
    return {old_pin_path:old_pin_raw, pin_path:pin_raw}


def encode_index(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()


def canonical_once(checkpoints, client, *, before_sha, target, target_sha, review_sha, parent_etag, guard, observe):
    from .reader_web_exact_plan import _is_sha
    from .reader_page_cas import strong_etag
    if not all(_is_sha(x) for x in (before_sha,target_sha,review_sha)) or office.digest(encode_index(target))!=target_sha:
        raise ValueError('invalid canonical Office seal')
    strong_etag(parent_etag)
    operation=target.get('checkpoint_operation')
    if not isinstance(operation,str) or not re.fullmatch(r'[0-9a-f]{32}',operation):
        raise ValueError('invalid Office checkpoint operation')
    store=S3ClaimStore(client)
    claim='reader-index/gc/office-exact-index/v1/'+before_sha+'.json'
    permit=v3.encode(dict(kind='reader-office-index-permit',version=1,parent=before_sha,target=target_sha,
                          parent_etag=parent_etag,review=review_sha,operation=operation))
    confirm(guard)
    seen,_=store._get(claim,maximum=8192)
    if observe:
        if seen!=permit or checkpoints.read(B,office.INDEX).raw_sha256!=target_sha:
            raise ValueError('exact spent Office index target not observed; do not replay')
        return 'observed'
    if seen is not None:
        raise ValueError('Office canonical parent already spent; do not replay')
    parent=checkpoints.read(B,office.INDEX)
    if (parent.raw_sha256,parent.etag)!=(before_sha,parent_etag):
        raise ValueError('Office canonical parent changed before claim')
    # Recheck the structural replacement against exact GET bytes immediately
    # before spending the permit, in addition to matching the sealed digest.
    replacement={row['source_extension']:row for row in target['files']
                 if row.get('source_extension') in office.PINS and
                    row.get('source_sha256')==office.PINS[row['source_extension']][0]}
    exact,_,_=office.exact_rows(parent.raw,before_sha,replacement)
    exact['checkpoint_operation']=operation
    if encode_index(exact)!=encode_index(target):
        raise ValueError('Office candidate changes fields outside the four reviewed rows')
    confirm(guard)
    if not store._put(claim,permit,maximum=8192,IfNoneMatch='*'):
        raise ValueError('Office canonical permit lost; do not replay')
    confirm(guard)
    again=checkpoints.read(B,office.INDEX)
    if (again.raw_sha256,again.etag)!=(before_sha,parent_etag):
        raise ValueError('Office index changed after claiming; do not replay')
    return checkpoints.write_once(B,office.INDEX,target,parent_etag,operation_id=operation)
