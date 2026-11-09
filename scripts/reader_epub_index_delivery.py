"""Write one journaled EPUB shard index with exact-parent CAS and recovery.

No CLI or workflow is attached. The caller must separately coordinate known
writers and supply real independent source/consumer/object revalidation; this
is not an external writer fence or ABA protection. This step never mutates the
canonical index, sidecar, Reader pointer or historic objects.
"""
from __future__ import annotations

import hashlib
import json

try:
    from . import reader_epub_index_preview as preview,reader_epub_index_transaction as tx
    from . import reader_epub_stage_journal as staging,reader_stream_checkpoint_cas as cas
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    import reader_epub_index_preview as preview
    import reader_epub_index_transaction as tx
    import reader_epub_stage_journal as staging
    import reader_stream_checkpoint_cas as cas
    from shared import READER_ASSETS_BUCKET


CANONICAL='chapters/ebook/epub/index.json'


def _active(store,current):
    active=tx.load(store,current['record']['operation'])
    if active is None or active['head_raw']!=current['head_raw'] or active['etag']!=current['etag']:
        raise ValueError('EPUB index journal changed; inspect before continuing')
    return active


def _stage(stage_store,current):
    operation=current['record']['operation'];stage=staging.load(stage_store,operation)
    if (stage is None or stage.record['state']!='staged' or stage.head['record']!=current['intent']['stage_record_sha256']
            or stage.record['intent_sha256']!=current['intent']['stage_intent_sha256']):
        raise ValueError('EPUB staged transaction changed or is incomplete')
    return stage


def _parents(checkpoints,plan):
    canonical=checkpoints.read(READER_ASSETS_BUCKET,CANONICAL)
    shard_path=f"chapters/ebook/epub/index-{plan['shard']:02d}.json"
    shard=checkpoints.read(READER_ASSETS_BUCKET,shard_path)
    if canonical.value is None or canonical.etag is None:
        raise ValueError('EPUB canonical index is missing or unversioned')
    cas.strong_etag(canonical.etag)
    if tx.digest(canonical.value)!=plan['canonical_index_sha256']:
        raise ValueError('EPUB canonical index differs from the staged plan')
    value={} if shard.value is None else shard.value
    if tx.digest(value)!=plan['shard_index_sha256'] or (shard.value is None)!=(shard.etag is None):
        raise ValueError('EPUB shard index differs from the staged plan')
    if shard.etag is not None:cas.strong_etag(shard.etag)
    return canonical,shard_path,shard


def prepare(index_store,stage_store,operation,*,checkpoints,protections,objects,revalidate,read_bytes,checkpointed_at):
    stage=staging.load(stage_store,operation)
    if stage is None or stage.record['state']!='staged':raise ValueError('EPUB source bundle is not staged')
    plan=stage.intent['plan'];canonical,shard_path,shard=_parents(checkpoints,plan)
    artifact=preview.inspect(stage_store,operation,canonical.value,{} if shard.value is None else shard.value,
        protections=protections,objects=objects,revalidate=revalidate,read_bytes=read_bytes,checkpointed_at=checkpointed_at)
    if checkpoints.read(READER_ASSETS_BUCKET,CANONICAL)!=canonical or checkpoints.read(READER_ASSETS_BUCKET,shard_path)!=shard:
        raise ValueError('EPUB index parent changed during transaction preparation')
    return tx.begin(index_store,stage,artifact,canonical.value,{} if shard.value is None else shard.value,
                    shard.etag,shard.raw_sha256)


def _expected_raw(candidate):
    return hashlib.sha256((json.dumps(candidate,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8')).hexdigest()


def apply_once(index_store,stage_store,current,*,checkpoints,protections,objects,revalidate,read_bytes):
    if current['record']['state']!='prepared':raise ValueError('EPUB index is not prepared; reconcile instead')
    active=_active(index_store,current);stage=_stage(stage_store,active);plan=stage.intent['plan']
    canonical,path,shard=_parents(checkpoints,plan)
    if (hashlib.sha256((shard.etag or 'absent').encode()).hexdigest()!=active['intent']['parent_etag_sha256']
            or (shard.raw_sha256 or hashlib.sha256(b'absent').hexdigest())!=active['intent']['parent_raw_sha256']):
        raise ValueError('EPUB shard validator or exact bytes changed before index write')
    new=active['intent']['candidate'];selected=set(s['key'] for s in plan['sources'])
    times={row.get('checkpointed_at') for row in new['files'] if row['key'] in selected}
    if len(times)!=1:raise ValueError('EPUB selected checkpoint time differs')
    checked=preview.inspect(stage_store,plan['operation'],canonical.value,{} if shard.value is None else shard.value,
        protections=protections,objects=objects,revalidate=revalidate,read_bytes=read_bytes,checkpointed_at=times.pop())
    if checked['candidate']!=new or checked['candidate_sha256']!=active['intent']['candidate_sha256']:
        raise ValueError('EPUB candidate changed before index write')
    if checkpoints.read(READER_ASSETS_BUCKET,CANONICAL)!=canonical or checkpoints.read(READER_ASSETS_BUCKET,path)!=shard:
        raise ValueError('EPUB index parent changed during verification')
    claimed=tx.claim(index_store,current)
    _active(index_store,claimed)
    # Check the other mutable root and the exact shard validator again after the
    # durable claim; neither read is an external writer fence.
    if checkpoints.read(READER_ASSETS_BUCKET,CANONICAL)!=canonical or checkpoints.read(READER_ASSETS_BUCKET,path)!=shard:
        raise ValueError('EPUB index parent changed after claim; inspect before recovery')
    _active(index_store,claimed)
    checkpoints.write_once(READER_ASSETS_BUCKET,path,new,shard.etag,operation_id=plan['operation'])
    observed=checkpoints.read(READER_ASSETS_BUCKET,path)
    if observed.raw_sha256!=_expected_raw(new):raise ValueError('EPUB index readback differs')
    if checkpoints.read(READER_ASSETS_BUCKET,CANONICAL)!=canonical:
        raise ValueError('EPUB canonical index changed after shard write')
    _stage(stage_store,claimed)
    # The pre-write verifier expects the parent shard; after our successful
    # CAS that condition is intentionally false. Post-write revalidation must
    # instead inspect the candidate and pinned consumers without replaying.
    revalidate.after_write(plan,stage.intent['preview'],new)
    if (checkpoints.read(READER_ASSETS_BUCKET,CANONICAL)!=canonical
            or checkpoints.read(READER_ASSETS_BUCKET,path)!=observed):
        raise ValueError('EPUB index changed during post-write observation')
    _stage(stage_store,claimed)
    return tx.record_observed(index_store,claimed,snapshot=observed)


def reconcile(index_store,stage_store,operation,*,checkpoints,protections,objects,revalidate):
    """Read exact intended shard and protections after unknown outcome; never write the shard."""
    current=tx.load(index_store,operation)
    if current is None or current['record']['state']!='write-intent':
        raise ValueError('EPUB index has no unresolved write intent')
    stage=_stage(stage_store,current);plan=stage.intent['plan'];intent=current['intent']
    canonical=checkpoints.read(READER_ASSETS_BUCKET,CANONICAL)
    if canonical.value is None or tx.digest(canonical.value)!=intent['parent_canonical_sha256']:
        raise ValueError('EPUB canonical index changed during recovery')
    observed=checkpoints.read(READER_ASSETS_BUCKET,intent['shard_path'])
    if observed.value!=intent['candidate'] or observed.raw_sha256!=_expected_raw(intent['candidate']):
        raise ValueError('EPUB shard does not contain the exact intended candidate; recovery remains unresolved')
    revalidate.after_write(plan,stage.intent['preview'],intent['candidate'])
    try:
        from . import reader_epub_protection as protection,search_snapshot
    except ImportError:
        import reader_epub_protection as protection
        import search_snapshot
    for action in stage.intent['preview']['actions']:
        expected=search_snapshot.canonical(protection.make_record(operation,plan['plan_sha256'],action['root'],stage.intent['created_at']))
        if protections.inspect(action['protection_path'])!=expected:
            raise ValueError('EPUB protection changed during index recovery')
        for item in action['files']:
            if objects.inspect(action['root'],item['path'])!={'sha256':item['sha256'],'bytes':item['bytes']}:
                raise ValueError('EPUB object changed during index recovery')
    revalidate.after_write(plan,stage.intent['preview'],intent['candidate'])
    if (checkpoints.read(READER_ASSETS_BUCKET,CANONICAL)!=canonical
            or checkpoints.read(READER_ASSETS_BUCKET,intent['shard_path'])!=observed):
        raise ValueError('EPUB index changed during recovery')
    _stage(stage_store,current)
    return tx.record_observed(index_store,current,snapshot=observed)
