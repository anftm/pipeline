"""Read-only exact canonical EPUB index merge for one observed shard operation.

Both journals, the published parent, selected shard bytes and every staged
object are rechecked. Other canonical rows are retained byte-for-byte as JSON
values. This module writes no index, sidecar, pointer, or remote journal.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

try:
    from . import reader_epub_index_transaction as index_tx, reader_epub_stage_journal as stage_tx
    from . import reader_epub_protection as protection
    from . import reader_stream_index as streams, search_snapshot
except ImportError:
    import reader_epub_index_transaction as index_tx
    import reader_epub_stage_journal as stage_tx
    import reader_epub_protection as protection
    import reader_stream_index as streams
    import search_snapshot


KIND='ebook-chapter-stream-index'


def _rows(index,label):
    if (not isinstance(index,dict) or index.get('version')!=1 or index.get('kind')!=KIND
            or not isinstance(index.get('files'),list) or not isinstance(index.get('failures',[]),list)):
        raise ValueError('invalid '+label+' EPUB index')
    streams.validate_index(index,label)
    for field in ('files','failures'):
        keys=[row['key'] for row in index.get(field,[])]
        if len(keys)!=len(set(keys)):raise ValueError('duplicate '+label+' EPUB source key')
    return {row['key']:row for row in index['files']}


def _newer(new, old):
    value=new.get('checkpointed_at')
    if not isinstance(value,str) or not value.endswith('Z'):
        raise ValueError('candidate EPUB checkpoint time is not exact UTC')
    try:
        current=datetime.fromisoformat(value)
        before=datetime.fromisoformat(old['checkpointed_at'].replace('Z','+00:00')) if old.get('checkpointed_at') else None
    except (ValueError,TypeError) as error:
        raise ValueError('EPUB checkpoint time is invalid') from error
    if current.tzinfo is None or current.utcoffset().total_seconds()!=0:
        raise ValueError('candidate EPUB checkpoint time is not UTC')
    if before is not None and (before.tzinfo is None or current<=before):
        raise ValueError('candidate EPUB checkpoint is not newer than its parent')


def inspect(index_store,stage_store,operation,canonical,shard,*,protections,objects,revalidate):
    index_current=index_tx.load(index_store,operation)
    staged=stage_tx.load(stage_store,operation)
    if index_current is None or index_current['record']['state']!='observed':
        raise ValueError('EPUB shard index is not durably observed')
    if staged is None or staged.record['state']!='staged':
        raise ValueError('EPUB object stage is not complete')
    intent=index_current['intent'];plan=staged.intent['plan'];staging=staged.intent['preview']
    if (staged.head['record']!=intent['stage_record_sha256']
            or staged.record['intent_sha256']!=intent['stage_intent_sha256']
            or operation!=plan['operation']):
        raise ValueError('EPUB staged parent does not match index intent')
    old=_rows(canonical,'canonical');current=_rows(shard,'shard')
    if index_tx.digest(canonical)!=intent['parent_canonical_sha256']:
        raise ValueError('EPUB canonical parent changed')
    if shard!=intent['candidate'] or index_tx.digest(shard)!=intent['candidate_sha256']:
        raise ValueError('EPUB shard differs from its exact observed candidate')
    if not callable(revalidate) or not callable(getattr(revalidate,'after_write',None)):
        raise ValueError('fresh independent EPUB source and consumer checks are required')
    revalidate.after_write(plan,staging,shard)
    if index_tx.load(index_store,operation)['head_raw']!=index_current['head_raw']:
        raise ValueError('EPUB index journal changed during inspection')
    selected={row['key'] for row in plan['sources']}
    if len(selected)!=len(plan['sources']):raise ValueError('duplicate selected EPUB source')
    parent_rows=_rows(intent['parent_shard'],'previous shard') if intent['parent_shard'] else {}
    changed={k for k in set(parent_rows)|set(current) if parent_rows.get(k)!=current.get(k)}
    if changed!=selected or shard.get('failures',[])!=intent['parent_shard'].get('failures',[]):
        raise ValueError('EPUB observed shard changes unrelated data')
    if any(row['key'] in selected for row in canonical.get('failures',[])):
        raise ValueError('EPUB canonical parent has selected source failures')
    new_rows=[];prior={}
    for source,action in zip(plan['sources'],staging['actions']):
        key=source['key'];old_row=old.get(key);candidate=current.get(key)
        if not isinstance(candidate,dict):raise ValueError('EPUB selected candidate row is missing')
        if (old_row is None)!=(source['previous_manifest'] is None):
            raise ValueError('EPUB canonical source presence differs')
        if old_row is not None:
            if (old_row.get('manifest')!=source['previous_manifest']
                    or old_row.get('source_revision')!=source['previous_revision']
                    or old_row.get('source_sha256')!=source['previous_source_sha256']):
                raise ValueError('EPUB canonical source differs from approved parent')
            _newer(candidate,old_row)
        if (not candidate or candidate.get('manifest')!=action['root']+'/chapter-manifest.json'
                or candidate.get('source_sha256')!=action['source_sha256']
                or candidate.get('source_revision')!=source['source_revision']
                or candidate.get('processing_roots')!=[action['protection_path']]):
            raise ValueError('EPUB shard candidate does not match staged content')
        expected=search_snapshot.canonical(protection.make_record(
            operation,plan['plan_sha256'],action['root'],staged.intent['created_at']))
        if protections.inspect(action['protection_path'])!=expected:
            raise ValueError('EPUB staged protection differs')
        for item in action['files']:
            if objects.inspect(action['root'],item['path'])!={'sha256':item['sha256'],'bytes':item['bytes']}:
                raise ValueError('EPUB staged object differs')
        prior[key]=old_row;new_rows.append(candidate)
    # The operation's target rows are the only rows admitted into the
    # canonical merge. Legacy full-index shard copies are not bulk merged.
    candidate=streams.merge_indexes(canonical,new_rows,[],KIND)
    candidate['checkpoint_operation']=operation
    merged=_rows(candidate,'candidate canonical')
    if {key for key in set(old)|set(merged) if old.get(key)!=merged.get(key)}!=selected:
        raise ValueError('EPUB canonical preview changes unselected sources')
    if candidate.get('failures',[])!=canonical.get('failures',[]):
        raise ValueError('EPUB canonical preview changes failure records')
    revalidate.after_write(plan,staging,shard)
    again=index_tx.load(index_store,operation);stage_again=stage_tx.load(stage_store,operation)
    if (again is None or again['head_raw']!=index_current['head_raw'] or again['etag']!=index_current['etag']
            or stage_again is None or stage_again.head_raw!=staged.head_raw or stage_again.etag!=staged.etag):
        raise ValueError('EPUB journal changed during canonical preview')
    result={'version':1,'kind':'reader-epub-canonical-preview','operation':operation,
            'index_record_sha256':index_current['head']['record'],'stage_record_sha256':staged.head['record'],
            'parent_canonical_sha256':intent['parent_canonical_sha256'],
            'observed_shard_sha256':intent['candidate_sha256'],'candidate_sha256':index_tx.digest(candidate),
            'selected_keys':sorted(selected),'previous_rows':prior,'candidate':candidate,
            'remote_writes':0,'index_written':False,'sidecar_written':False,'pointer_written':False,
            'publication_ready':False,'external_writers_verified':False}
    result['preview_sha256']=hashlib.sha256(search_snapshot.canonical(result)).hexdigest()
    return result
