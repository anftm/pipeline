"""Read-only exact v3 pointer CAS plan for one observed EPUB update.

The caller must coordinate all real writers separately. This module exposes no
production writer, CLI, cleanup, rollback or pointer switch. A strong ETag is
only a one-object precondition: it is not a global fence or ABA protection.
The returned plan preserves exact parent bytes for a future recovery protocol.
"""
from __future__ import annotations

import copy
import hashlib
import json

try:
    from . import reader_epub_v3_preview as preview, reader_page_cas as cas, search_snapshot
except ImportError:
    import reader_epub_v3_preview as preview
    import reader_page_cas as cas
    import search_snapshot

BUCKET=preview.backlog.READER_BUCKET
POINTER=preview.publication.POINTER
MAX_POINTER=1024*1024


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _parse_pointer(raw):
    if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_POINTER:
        raise ValueError('invalid bounded Reader pointer bytes')
    def object_pairs(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('duplicate Reader pointer field')
            value[key]=item
        return value
    try:value=json.loads(raw,object_pairs_hook=object_pairs)
    except (UnicodeDecodeError,json.JSONDecodeError) as error:
        raise ValueError('invalid Reader pointer JSON') from error
    if (not isinstance(value,dict) or type(value.get('version')) is not int or value['version']!=1
            or value.get('kind')!='reader-reading-pointer'):
        raise ValueError('invalid Reader pointer schema')
    generation=preview.publication.text.sha(value.get('generation'))
    catalog=preview.publication.index_resource(value.get('catalog'))
    if catalog['path']!=f'reader-index/v3/generations/{generation}/catalog.json':
        raise ValueError('Reader pointer generation and catalog differ')
    return value


def inspect(store,stage_store,shard_store,canonical_store,operation,*,pointer_store,checkpoints,
            protections,objects,revalidate,client):
    """Observe one pointer and rebuild the exact v3 candidate; never write."""
    old_raw,etag=pointer_store.read_for_update(BUCKET,POINTER)
    cas.strong_etag(etag);old_pointer=_parse_pointer(old_raw)
    if store.read_bytes(BUCKET,POINTER)!=old_raw:
        raise ValueError('Reader pointer differs between S3 and Hub')
    first,packed=preview.inspect(store,stage_store,shard_store,canonical_store,operation,checkpoints=checkpoints,
        protections=protections,objects=objects,revalidate=revalidate,client=client)
    if (not isinstance(first,dict) or first.get('version')!=1 or first.get('kind')!='reader-epub-v3-preview'
            or first.get('operation')!=operation or first.get('parent_pointer')!=old_pointer
            or first.get('remote_writes')!=0 or first.get('pointer_written') is not False
            or first.get('site_published') is not False or first.get('publication_ready') is not False
            or first.get('external_writers_verified') is not False or first.get('objects_deleted') is not False
            or first.get('preview_sha256')!=_digest(search_snapshot.canonical({k:v for k,v in first.items() if k!='preview_sha256'}))):
        raise ValueError('EPUB v3 preview does not bind the exact unpromoted pointer')
    catalog=first['candidate_catalog'];new_pointer=first['candidate_pointer']
    preview.publication.validate_catalog(catalog)
    if catalog.get('parent_generation')!=old_pointer['generation']:
        raise ValueError('EPUB v3 catalog parent differs')
    new_raw=preview.publication.encode(new_pointer)
    if _parse_pointer(new_raw)!=new_pointer or new_pointer['generation']!=catalog['generation']:
        raise ValueError('EPUB v3 candidate pointer differs')
    raw_catalog=preview.publication.encode(catalog)
    expected_catalog=preview.publication.metadata(BUCKET,
        f"reader-index/v3/generations/{catalog['generation']}/catalog.json",raw_catalog)
    if new_pointer.get('catalog')!=expected_catalog or new_pointer.get('resources')!=[expected_catalog]:
        raise ValueError('EPUB v3 pointer catalog identity differs')
    if not isinstance(packed,bytes) or not packed:
        raise ValueError('EPUB v3 base bytes missing')
    base_ref=catalog.get('base_sidecar')
    expected_base=preview.publication.metadata(BUCKET,
        f'reader-index/snapshots/{_digest(packed)}/reader_assets.json.gz',packed)
    if (base_ref!=expected_base or first.get('candidate_base_sha256')!=_digest(packed)
            or first.get('candidate_base_bytes')!=len(packed)):
        raise ValueError('EPUB v3 base bytes or reference differ')
    preview.publication.base_sidecar_ref(base_ref)
    base=preview._decode(packed)
    projection=preview.publication.project_sidecar(base,catalog)
    selected=first.get('selected_keys');after=first.get('candidate_routes');before=first.get('previous_routes')
    if (not isinstance(selected,list) or not selected or selected!=sorted(set(selected))
            or not isinstance(before,dict) or not isinstance(after,dict)
            or set(before)!=set(selected) or set(after)!=set(selected)
            or any(projection['f'].get(key)!=after[key] for key in selected)):
        raise ValueError('EPUB v3 route evidence differs')
    second,again=preview.inspect(store,stage_store,shard_store,canonical_store,operation,checkpoints=checkpoints,
        protections=protections,objects=objects,revalidate=revalidate,client=client)
    if second!=first or again!=packed:
        raise ValueError('EPUB v3 preview changed during pointer planning')
    latest_raw,latest_etag=pointer_store.read_for_update(BUCKET,POINTER)
    if (latest_raw!=old_raw or latest_etag!=etag or store.read_bytes(BUCKET,POINTER)!=old_raw):
        raise ValueError('Reader pointer exact bytes or validator changed during planning')
    result={'version':1,'kind':'reader-epub-v3-pointer-cas-plan','operation':operation,
            'preview_sha256':first['preview_sha256'],'stage_record_sha256':first['stage_record_sha256'],
            'shard_record_sha256':first['shard_record_sha256'],'canonical_record_sha256':first['canonical_record_sha256'],
            'search_generation':first['search_generation'],'parent_pointer':copy.deepcopy(old_pointer),
            'parent_pointer_raw_sha256':_digest(old_raw),'parent_pointer_etag_sha256':_digest(etag.encode()),
            'candidate_pointer':copy.deepcopy(new_pointer),'candidate_pointer_raw_sha256':_digest(new_raw),
            'candidate_catalog':copy.deepcopy(catalog),'candidate_base':copy.deepcopy(base_ref),
            'selected_keys':list(selected),'previous_routes':copy.deepcopy(before),'candidate_routes':copy.deepcopy(after),
            'consumers_before':copy.deepcopy(first['consumers_before']),
            'preserved_pdf_redirects':first['preserved_pdf_redirects'],
            'remote_writes':0,'pointer_written':False,'site_published':False,'publication_ready':False,
            'external_writers_verified':False,'rollback_authorized':False,'objects_deleted':False}
    result['plan_sha256']=_digest(search_snapshot.canonical(result))
    return result,old_raw,etag,new_raw,packed
