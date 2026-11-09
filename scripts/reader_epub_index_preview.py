"""Preview one exact staged EPUB batch without writing Reader indexes.

The preview requires a completed staging journal, rechecks each protected
object through independently supplied readers, preserves unrelated canonical
and shard rows, and asserts the selected old routes remain the sole changes.
It does not publish the resulting index, release protections or update sites.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

try:
    from . import reader_epub_protection as protection, reader_epub_stage_journal as journal
    from . import reader_stream_checkpoint_cas as cas, reader_stream_index as streams, search_snapshot
    from . import reader_assets
except ImportError:
    import reader_epub_protection as protection
    import reader_epub_stage_journal as journal
    import reader_stream_checkpoint_cas as cas
    import reader_stream_index as streams
    import search_snapshot
    import reader_assets


KIND='ebook-chapter-stream-index'


def _digest(value):return hashlib.sha256(search_snapshot.canonical(value)).hexdigest()


def _index(value, label):
    if not isinstance(value,dict) or value.get('version')!=1 or value.get('kind')!=KIND:
        raise ValueError('invalid '+label+' EPUB index')
    streams.validate_index(value,label)
    for field in ('files','failures'):
        keys=[row['key'] for row in value.get(field,[])]
        if len(keys)!=len(set(keys)):raise ValueError('duplicate '+label+' EPUB index key')
    return value


def _rows(value):return {row['key']:row for row in value.get('files',[])}


def _strictly_newer(value, older):
    if not isinstance(value,str) or not value.endswith('Z'):
        raise ValueError('EPUB preview requires an exact UTC checkpoint time')
    try:
        timestamp=datetime.fromisoformat(value)
        previous=datetime.fromisoformat(older.replace('Z','+00:00')) if older else None
    except (ValueError,TypeError) as error:
        raise ValueError('EPUB checkpoint timestamp is malformed') from error
    if timestamp.tzinfo is None or timestamp.utcoffset()!=timezone.utc.utcoffset(timestamp):
        raise ValueError('EPUB checkpoint timestamp is not UTC')
    if previous is not None and (previous.tzinfo is None or timestamp<=previous):
        raise ValueError('EPUB checkpoint must be newer than its exact parent')
    return value


def inspect(store, operation, canonical, shard, *, protections, objects, revalidate, read_bytes, checkpointed_at):
    current=journal.load(store,operation)
    if current is None or current.record['state']!='staged':
        raise ValueError('EPUB staging journal is not fully staged')
    plan=current.intent['plan'];preview=current.intent['preview']
    if not callable(revalidate) or not callable(read_bytes):
        raise ValueError('independent EPUB read-only revalidation and bounded reads are required')
    _index(canonical,'canonical')
    if shard != {}:
        _index(shard,'shard')
    if _digest(canonical)!=plan['canonical_index_sha256'] or _digest(shard)!=plan['shard_index_sha256']:
        raise ValueError('EPUB index parent differs from exact plan')
    if shard:
        for field in ('files','failures'):
            for row in shard.get(field,[]):
                if streams.shard_for(row['key'],8)!=plan['shard']:
                    raise ValueError('EPUB shard contains a foreign source key')
    revalidate(plan,preview)
    canonical_rows,shard_rows=_rows(canonical),_rows(shard)
    selected={source['key'] for source in plan['sources']}
    if len(selected)!=len(plan['sources']):raise ValueError('duplicate selected EPUB source')
    for field,index in (('canonical',canonical),('shard',shard)):
        if any(row['key'] in selected for row in index.get('failures',[])):
            raise ValueError('selected EPUB has unresolved '+field+' failure')
    new=[]
    for source,action in zip(plan['sources'],preview['actions']):
        key=source['key'];old=canonical_rows.get(key);shard_old=shard_rows.get(key)
        if (old is None)!=(source['previous_manifest'] is None):
            raise ValueError('EPUB canonical route no longer matches plan')
        if old is not None and (old.get('manifest')!=source['previous_manifest']
                or old.get('source_revision')!=source['previous_revision']
                or old.get('source_sha256')!=source['previous_source_sha256']):
            raise ValueError('EPUB canonical source identity changed')
        if shard_old is not None and shard_old!=old:
            raise ValueError('EPUB shard and canonical parent differ for selected source')
        _strictly_newer(checkpointed_at,old.get('checkpointed_at') if old else None)
        expected_protection=search_snapshot.canonical(protection.make_record(
            operation,plan['plan_sha256'],action['root'],current.intent['created_at']))
        if protections.inspect(action['protection_path'])!=expected_protection:
            raise ValueError('EPUB staging protection is missing or altered')
        file_paths={entry['path'] for entry in action['files']}
        if not {'chapter-manifest.json','epub-search-index.json.gz'} <= file_paths:
            raise ValueError('EPUB staged chapter manifest or search index missing')
        for item in action['files']:
            if objects.inspect(action['root'],item['path'])!={'sha256':item['sha256'],'bytes':item['bytes']}:
                raise ValueError('EPUB staged object is missing or altered')
        member=next(item for item in action['files'] if item['path']=='chapter-manifest.json')
        raw=read_bytes(action['root'],member['path'],member['bytes'])
        if (not isinstance(raw,bytes) or len(raw)!=member['bytes']
                or hashlib.sha256(raw).hexdigest()!=member['sha256']):
            raise ValueError('EPUB staged manifest bytes differ')
        import json
        manifest=reader_assets.validate_chapter_manifest(json.loads(raw))
        if not isinstance(manifest.get('search_index'),dict):
            raise ValueError('EPUB staged manifest lacks a verified search index')
        new.append({'key':key,'repo':source['repo'],'path':source['path'],'source_extension':'epub',
                    'extension':'epub','source_revision':source['source_revision'],
                    'source_sha256':action['source_sha256'],'mode':'chapter-stream',
                    'bucket':reader_assets.READER_ASSETS_BUCKET,'profile':plan['profile'],
                    'root':action['root'],'manifest':action['root']+'/chapter-manifest.json',
                    'search_index':action['root']+'/epub-search-index.json.gz',
                    'chapter_count':len(manifest['chapters']), 'checkpointed_at':checkpointed_at,
                    'processing_roots':[action['protection_path']]})
    parent=shard if shard else {'version':1,'kind':KIND,'files':[],'failures':[]}
    # Do not use checkpoint timestamps to resolve a race. The exact source
    # and index parents are already bound; the future writer must CAS them.
    candidate=cas.merge_observed(parent,parent,new,[],kind=KIND,operation_id=operation,
                                 preserve_canonical_rows=True)
    before=_rows(parent);after=_rows(candidate)
    if {k for k in set(before)|set(after) if before.get(k)!=after.get(k)}!=selected:
        raise ValueError('EPUB preview changes rows outside the exact selected set')
    if candidate.get('failures',[])!=parent.get('failures',[]):
        raise ValueError('EPUB preview changes failure checkpoints')
    revalidate(plan,preview)
    active=journal.load(store,operation)
    if active is None or active.head_raw!=current.head_raw or active.etag!=current.etag:
        raise ValueError('EPUB staged journal changed during index preview')
    return {'version':1,'kind':'reader-epub-index-preview','operation':operation,
            'plan_sha256':plan['plan_sha256'],'staging_sha256':preview['staging_sha256'],
            'canonical_index_sha256':plan['canonical_index_sha256'],'shard_index_sha256':plan['shard_index_sha256'],
            'candidate_sha256':_digest(candidate),'candidate':candidate,
            'changed_keys':sorted(selected),'previous_manifests':{s['key']:s['previous_manifest'] for s in plan['sources']},
            'remote_writes':0,'index_written':False,'pointer_written':False,
            'publication_ready':False,'external_writers_verified':False}
