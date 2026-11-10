"""Plan one observed EPUB update as a new v3 pinned base, without writes.

It binds completed stage/shard/canonical journals, current Search/Reader
consumers and every protected bundle. Only selected EPUB compact routes may
change; existing v3 overrides, suppressions, PDF redirects, and all unrelated
routes remain identical. This is not pointer CAS or publication authorization.
"""
from __future__ import annotations

import copy
import gzip
import hashlib

try:
    from . import reader_epub_canonical_transaction as canonical_tx,reader_epub_canonical_delivery as delivery
    from . import reader_epub_index_transaction as shard_tx,reader_epub_stage_journal as staging
    from . import reader_format_backlog as backlog,reader_page_redirects as redirects,reader_route_control as control
    from . import publish_reader_v3 as publication,search_snapshot
except ImportError:
    import reader_epub_canonical_transaction as canonical_tx
    import reader_epub_canonical_delivery as delivery
    import reader_epub_index_transaction as shard_tx
    import reader_epub_stage_journal as staging
    import reader_format_backlog as backlog
    import reader_page_redirects as redirects
    import reader_route_control as control
    import publish_reader_v3 as publication
    import search_snapshot


MAX_BASE=50*1024*1024


def _decode(raw):
    import io,json
    if not isinstance(raw,bytes) or not 0<len(raw)<=8*1024*1024:
        raise ValueError('Reader v3 preview base size invalid')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:data=stream.read(MAX_BASE+1)
    if len(data)>MAX_BASE:raise ValueError('Reader v3 preview base expands beyond limit')
    value=json.loads(data)
    if not isinstance(value,dict) or value.get('v')!=1 or not isinstance(value.get('f'),dict):
        raise ValueError('Reader v3 preview base is invalid')
    return value


def inspect(store,stage_store,shard_store,canonical_store,operation,*,checkpoints,protections,objects,revalidate,client):
    current=canonical_tx.load(canonical_store,operation)
    if current is None or current['record']['state']!='observed':raise ValueError('EPUB canonical index not durably observed')
    intent=current['intent'];staged=staging.load(stage_store,operation);shard=shard_tx.load(shard_store,operation)
    canonical_tx._bind(staged,shard,intent);plan=staged.intent['plan'];stage_preview=staged.intent['preview']
    before=publication.current(store);pointer,catalog=before
    if not pointer or pointer['generation']!=plan['reader_generation'] or not catalog.get('base_sidecar'):
        raise ValueError('Reader pointer differs from EPUB plan')
    base=publication.pinned_base(store,catalog,{})
    mapping=redirects.validated(catalog)
    if redirects.rewrite(base,mapping)[0]!=base:
        raise ValueError('pinned Reader base does not preserve current PDF redirects')
    suppressed=set(control.validated_sources(catalog));selected=intent['selected_keys']
    if any(k in suppressed or k in catalog['files'] for k in selected):
        raise ValueError('selected EPUB has a suppression or v3 override requiring separate resolution')
    old_projection=publication.project_sidecar(base,catalog)
    canonical=checkpoints.read(backlog.READER_BUCKET,canonical_tx.CANONICAL)
    part=checkpoints.read(backlog.READER_BUCKET,intent['shard_path'])
    if (canonical.value!=intent['candidate'] or canonical.raw_sha256!=current['record']['observed_raw_sha256']
            or canonical.etag is None
            or hashlib.sha256(canonical.etag.encode()).hexdigest()!=current['record']['observed_etag_sha256']):
        raise ValueError('EPUB canonical index or validator changed after observation')
    delivery._shard(part,intent,shard)
    if not callable(getattr(revalidate,'after_canonical',None)):
        raise ValueError('fresh canonical source and consumer revalidation required')
    revalidate.after_canonical(plan,stage_preview,canonical.value,part.value)
    delivery._verify_stage(staged,protections=protections,objects=objects)
    receipts={}
    for surface in ('pages','hf'):
        value=publication.observe_consumer(store,surface,client)
        if value.get('generation')!=pointer['generation'] or value.get('projection_verified') is not True:
            raise ValueError('Reader consumer changed before EPUB v3 preview')
        receipts[surface]={'generation':value['generation'],'projection_verified':True}
    candidates={row['key']:row for row in canonical.value['files']};sources={row['key']:row for row in plan['sources']}
    actions={row['key']:row for row in stage_preview['actions']};old_rows=intent['previous_rows']
    revised=copy.deepcopy(base);before_routes={};after_routes={}
    for key in selected:
        source=sources[key];action=actions[key];entry=candidates[key];old=old_rows[key]
        old_route=old_projection['f'].get(key)
        if old is None:
            if source['previous_manifest'] is not None or old_route is not None:
                raise ValueError('new EPUB route appeared before v3 preview')
        elif (source['previous_manifest']!=old.get('manifest') or not isinstance(old_route,dict)
              or old_route.get('s')!=2 or old_route.get('m')!='e' or old_route.get('p')!=old.get('manifest')
              or old_route.get('b')!=backlog.READER_BUCKET
              or not backlog._matches_route(old,old_route)):
            raise ValueError('old EPUB route differs from canonical parent')
        manifest=action['root']+'/chapter-manifest.json'
        if (entry.get('manifest')!=manifest or entry.get('source_sha256')!=action['source_sha256']
                or entry.get('source_revision')!=source['source_revision'] or entry.get('bucket')!=backlog.READER_BUCKET):
            raise ValueError('EPUB canonical route differs from protected staged content')
        route={'s':2,'m':'e','p':manifest,'b':backlog.READER_BUCKET,'c':manifest,'cb':backlog.READER_BUCKET}
        before_routes[key]=copy.deepcopy(old_route);after_routes[key]=route;revised['f'][key]=route
    # Reuse the currently pinned base rather than reconstructing all formats.
    # This keeps the existing exact redirect projection and every old URL.
    packed=gzip.compress(publication.encode(revised),mtime=0)
    sha=hashlib.sha256(packed).hexdigest()
    ref=publication.metadata(backlog.READER_BUCKET,f'reader-index/snapshots/{sha}/reader_assets.json.gz',packed)
    publication.base_sidecar_ref(ref)
    candidate={'version':3,'kind':'reader-reading-catalog','files':copy.deepcopy(catalog['files']),
               **({'suppressed_sources':copy.deepcopy(catalog['suppressed_sources'])} if 'suppressed_sources' in catalog else {}),
               **({'derived_pdf_redirects':copy.deepcopy(catalog['derived_pdf_redirects'])} if 'derived_pdf_redirects' in catalog else {}),
               'base_sidecar':ref,'parent_generation':pointer['generation']}
    redirects.preserve(catalog,candidate)
    candidate['generation']=publication.text.digest(candidate);publication.validate_catalog(candidate)
    projected=publication.project_sidecar(revised,candidate)
    old_files=old_projection['f'];new_files=projected['f']
    changed={key for key in set(old_files)|set(new_files) if old_files.get(key)!=new_files.get(key)}
    if changed!=set(selected) or any(new_files.get(k)!=after_routes[k] for k in selected):
        raise ValueError('EPUB v3 candidate changes unselected routes')
    catalog_raw=publication.encode(candidate)
    catalog_ref=publication.metadata(backlog.READER_BUCKET,f"reader-index/v3/generations/{candidate['generation']}/catalog.json",catalog_raw)
    new_pointer={'version':1,'kind':'reader-reading-pointer','generation':candidate['generation'],
                 'catalog':catalog_ref,'resources':[catalog_ref]}
    revalidate.after_canonical(plan,stage_preview,canonical.value,part.value)
    delivery._verify_stage(staged,protections=protections,objects=objects)
    for surface in ('pages','hf'):
        value=publication.observe_consumer(store,surface,client)
        if value.get('generation')!=pointer['generation'] or value.get('projection_verified') is not True:
            raise ValueError('Reader consumer changed during EPUB v3 preview')
    if (publication.current(store)!=before or checkpoints.read(backlog.READER_BUCKET,canonical_tx.CANONICAL)!=canonical
            or checkpoints.read(backlog.READER_BUCKET,intent['shard_path'])!=part):
        raise ValueError('Reader or EPUB index changed during v3 preview')
    active=canonical_tx.load(canonical_store,operation);again_stage=staging.load(stage_store,operation);again_shard=shard_tx.load(shard_store,operation)
    if (active is None or active['head_raw']!=current['head_raw'] or active['etag']!=current['etag']):
        raise ValueError('EPUB canonical journal changed during v3 preview')
    canonical_tx._bind(again_stage,again_shard,intent)
    result={'version':1,'kind':'reader-epub-v3-preview','operation':operation,
            'canonical_record_sha256':current['head']['record'],'stage_record_sha256':staged.head['record'],
            'shard_record_sha256':shard['head']['record'],'search_generation':plan['search_generation'],
            'parent_pointer':pointer,'candidate_pointer':new_pointer,'candidate_catalog':candidate,
            'candidate_base_sha256':sha,'candidate_base_bytes':len(packed),
            'selected_keys':selected,'previous_routes':before_routes,'candidate_routes':after_routes,
            'consumers_before':receipts,'preserved_pdf_redirects':len(redirects.validated(catalog)),
            'remote_writes':0,'pointer_written':False,'site_published':False,
            'publication_ready':False,'external_writers_verified':False,'objects_deleted':False}
    result['preview_sha256']=hashlib.sha256(search_snapshot.canonical(result)).hexdigest()
    return result,packed
