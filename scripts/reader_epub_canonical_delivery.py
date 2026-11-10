"""Apply one journaled EPUB canonical CAS, with exact read-only recovery.

No CLI or workflow. Requires caller-supplied independent live revalidation and
coordination of actual writers. A per-object ETag/claim does not fence other
writers, prevent ABA or coordinate sidecar/Reader publication.
"""
from __future__ import annotations

import hashlib
import json

try:
    from . import reader_epub_canonical_transaction as tx, reader_epub_canonical_preview as preview
    from . import reader_epub_index_transaction as shard_tx, reader_epub_stage_journal as staging
    from . import reader_epub_protection as protection, search_snapshot
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    import reader_epub_canonical_transaction as tx
    import reader_epub_canonical_preview as preview
    import reader_epub_index_transaction as shard_tx
    import reader_epub_stage_journal as staging
    import reader_epub_protection as protection
    import search_snapshot
    from shared import READER_ASSETS_BUCKET


def _actual(stage_store,shard_store,operation,intent):
    staged=staging.load(stage_store,operation);shard=shard_tx.load(shard_store,operation)
    tx._bind(staged,shard,intent)
    return staged,shard


def _parent(snapshot,intent):
    if (snapshot.value!=intent['parent_canonical'] or snapshot.raw_sha256!=intent['parent_raw_sha256']
            or snapshot.etag is None
            or hashlib.sha256(snapshot.etag.encode()).hexdigest()!=intent['parent_etag_sha256']):
        raise ValueError('EPUB canonical exact parent or validator changed')


def _shard(snapshot,intent,shard_current):
    if (snapshot.value!=shard_current['intent']['candidate']
            or snapshot.raw_sha256!=shard_current['record']['observed_raw_sha256'] or snapshot.etag is None
            or hashlib.sha256(snapshot.etag.encode()).hexdigest()!=shard_current['record']['observed_etag_sha256']):
        raise ValueError('EPUB observed shard bytes or validator changed')


def _verify_stage(staged,*,protections,objects):
    plan=staged.intent['plan'];staging_preview=staged.intent['preview']
    for action in staging_preview['actions']:
        expected=search_snapshot.canonical(protection.make_record(plan['operation'],plan['plan_sha256'],action['root'],staged.intent['created_at']))
        if protections.inspect(action['protection_path'])!=expected:
            raise ValueError('EPUB staging protection changed before canonical acceptance')
        for item in action['files']:
            if objects.inspect(action['root'],item['path'])!={'sha256':item['sha256'],'bytes':item['bytes']}:
                raise ValueError('EPUB staged object changed before canonical acceptance')


def prepare(store,stage_store,shard_store,operation,*,checkpoints,protections,objects,revalidate):
    staged=staging.load(stage_store,operation);shard=shard_tx.load(shard_store,operation)
    if staged is None or shard is None or shard['record']['state']!='observed':
        raise ValueError('EPUB staged source and observed shard are required')
    canonical=checkpoints.read(READER_ASSETS_BUCKET,tx.CANONICAL)
    shard_snapshot=checkpoints.read(READER_ASSETS_BUCKET,shard['intent']['shard_path'])
    if canonical.value is None:raise ValueError('EPUB canonical parent is absent')
    _shard(shard_snapshot,{'shard_path':shard['intent']['shard_path']},shard)
    artifact=preview.inspect(shard_store,stage_store,operation,canonical.value,shard_snapshot.value,
        protections=protections,objects=objects,revalidate=revalidate)
    if (checkpoints.read(READER_ASSETS_BUCKET,tx.CANONICAL)!=canonical
            or checkpoints.read(READER_ASSETS_BUCKET,shard['intent']['shard_path'])!=shard_snapshot):
        raise ValueError('EPUB canonical or shard changed during intent preparation')
    return tx.begin(store,staged,shard,artifact,canonical)


def apply_once(store,stage_store,shard_store,current,*,checkpoints,protections,objects,revalidate):
    if current['record']['state']!='prepared':raise ValueError('EPUB canonical is not prepared; reconcile instead')
    if not callable(getattr(revalidate,'after_canonical',None)):
        raise ValueError('EPUB canonical post-write revalidation is required before write')
    tx._active(store,current);intent=current['intent'];operation=current['record']['operation']
    staged,shard=_actual(stage_store,shard_store,operation,intent)
    canonical=checkpoints.read(READER_ASSETS_BUCKET,tx.CANONICAL);_parent(canonical,intent)
    part=checkpoints.read(READER_ASSETS_BUCKET,intent['shard_path']);_shard(part,intent,shard)
    artifact=preview.inspect(shard_store,stage_store,operation,canonical.value,part.value,
        protections=protections,objects=objects,revalidate=revalidate)
    if artifact['candidate']!=intent['candidate'] or artifact['candidate_sha256']!=intent['candidate_sha256']:
        raise ValueError('EPUB canonical candidate changed before write')
    if (checkpoints.read(READER_ASSETS_BUCKET,tx.CANONICAL)!=canonical
            or checkpoints.read(READER_ASSETS_BUCKET,intent['shard_path'])!=part):
        raise ValueError('EPUB indexes changed during canonical verification')
    claimed=tx.claim(store,current);tx._active(store,claimed);_actual(stage_store,shard_store,operation,intent)
    if (checkpoints.read(READER_ASSETS_BUCKET,tx.CANONICAL)!=canonical
            or checkpoints.read(READER_ASSETS_BUCKET,intent['shard_path'])!=part):
        raise ValueError('EPUB indexes changed after canonical write claim')
    checkpoints.write_once(READER_ASSETS_BUCKET,tx.CANONICAL,intent['candidate'],canonical.etag,operation_id=operation)
    observed=checkpoints.read(READER_ASSETS_BUCKET,tx.CANONICAL)
    revalidate.after_canonical(staged.intent['plan'],staged.intent['preview'],intent['candidate'],part.value)
    _verify_stage(staged,protections=protections,objects=objects)
    if (checkpoints.read(READER_ASSETS_BUCKET,tx.CANONICAL)!=observed
            or checkpoints.read(READER_ASSETS_BUCKET,intent['shard_path'])!=part):
        raise ValueError('EPUB indexes changed during canonical post-write observation')
    _actual(stage_store,shard_store,operation,intent)
    return tx.record_observed(store,claimed,observed)


def reconcile(store,stage_store,shard_store,operation,*,checkpoints,protections,objects,revalidate):
    current=tx.load(store,operation)
    if current is None or current['record']['state']!='write-intent':
        raise ValueError('EPUB canonical has no unresolved write intent')
    intent=current['intent'];staged,shard=_actual(stage_store,shard_store,operation,intent)
    observed=checkpoints.read(READER_ASSETS_BUCKET,tx.CANONICAL)
    expected_raw=hashlib.sha256((json.dumps(intent['candidate'],ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()).hexdigest()
    if observed.value!=intent['candidate'] or observed.raw_sha256!=expected_raw:
        raise ValueError('EPUB canonical does not contain the exact candidate; recovery remains unresolved')
    part=checkpoints.read(READER_ASSETS_BUCKET,intent['shard_path']);_shard(part,intent,shard)
    if not callable(getattr(revalidate,'after_canonical',None)):
        raise ValueError('EPUB canonical post-write revalidation is required')
    revalidate.after_canonical(staged.intent['plan'],staged.intent['preview'],intent['candidate'],part.value)
    _verify_stage(staged,protections=protections,objects=objects)
    revalidate.after_canonical(staged.intent['plan'],staged.intent['preview'],intent['candidate'],part.value)
    if (checkpoints.read(READER_ASSETS_BUCKET,tx.CANONICAL)!=observed
            or checkpoints.read(READER_ASSETS_BUCKET,intent['shard_path'])!=part):
        raise ValueError('EPUB indexes changed during canonical recovery')
    _actual(stage_store,shard_store,operation,intent)
    return tx.record_observed(store,current,observed)
