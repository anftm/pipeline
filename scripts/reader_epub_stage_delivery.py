"""Journal-bound EPUB staging with independently rechecked recovery.

This module has no production workflow or CLI. The caller wires actual readers,
S3 protections and objects only after coordinating known writers. A persisted
claim protects a single protocol attempt, not a global lock, external writer
fence, or ABA. Recovery performs no object/protection writes and never treats
missing objects as success.
"""
from __future__ import annotations

from pathlib import Path

try:
    from . import reader_epub_protected_stage as staging, reader_epub_protection as protection
    from . import reader_epub_shadow as shadow, reader_epub_stage_journal as journal, search_snapshot
except ImportError:
    import reader_epub_protected_stage as staging
    import reader_epub_protection as protection
    import reader_epub_shadow as shadow
    import reader_epub_stage_journal as journal
    import search_snapshot


def prepare(store, plan, report, directory: Path, *, created_at, revalidate):
    if not callable(revalidate):
        raise ValueError('fresh EPUB plan revalidation is required')
    preview=shadow.staging_plan(plan,report,directory)
    revalidate(plan,preview)
    current=journal.begin(store,plan,preview,created_at)
    revalidate(plan,preview)
    return current


def apply_once(store, current, report, directory: Path, *, protections, objects, revalidate):
    if current.record['state']!='prepared':
        raise ValueError('EPUB stage is not prepared; reconcile instead')
    plan=current.intent['plan'];preview=shadow.staging_plan(plan,report,directory)
    if preview!=current.intent['preview']:
        raise ValueError('local EPUB staging data differs from durable intent')
    revalidate(plan,preview)
    claimed=journal.claim(store,current)
    active=journal.load(store,plan['operation'])
    if active is None or active.head_raw!=claimed.head_raw or active.etag!=claimed.etag:
        raise ValueError('EPUB staging journal changed after write claim')
    # If interrupted after this claim, only the read-only recovery function may
    # advance; no second writer can claim this operation through the protocol.
    result=staging.stage(plan,report,directory,created_at=current.intent['created_at'],
                         protections=protections,objects=objects,revalidate=revalidate)
    active=journal.load(store,plan['operation'])
    if active is None or active.head_raw!=claimed.head_raw or active.etag!=claimed.etag:
        raise ValueError('EPUB staging journal changed before receipt')
    return journal.record_staged(store,claimed,result)


def recover(store, operation, *, protections, objects, revalidate):
    """Read exact roots and all objects after an interrupted stage; no writes to them."""
    current=journal.load(store,operation)
    if current is None or current.record['state']!='write-intent':
        raise ValueError('EPUB stage has no unresolved write intent')
    plan=current.intent['plan'];preview=current.intent['preview']
    revalidate(plan,preview)
    for action in preview['actions']:
        expected=search_snapshot.canonical(protection.make_record(
            operation,plan['plan_sha256'],action['root'],current.intent['created_at']))
        if protections.inspect(action['protection_path'])!=expected:
            raise ValueError('EPUB protection is missing or changed; recovery remains unresolved')
    for action in preview['actions']:
        for item in action['files']:
            if objects.inspect(action['root'],item['path'])!={'sha256':item['sha256'],'bytes':item['bytes']}:
                raise ValueError('EPUB object is missing or changed; recovery remains unresolved')
    revalidate(plan,preview)
    active=journal.load(store,operation)
    if active is None or active.head_raw!=current.head_raw or active.etag!=current.etag:
        raise ValueError('EPUB staging journal changed during recovery')
    # Recheck every object after source/consumer verification, then record a
    # durable observation. This advances only the journal, never a Reader root.
    outcomes=[]
    for action in preview['actions']:
        expected=search_snapshot.canonical(protection.make_record(
            operation,plan['plan_sha256'],action['root'],current.intent['created_at']))
        if protections.inspect(action['protection_path'])!=expected:
            raise ValueError('EPUB protection changed during recovery')
        outcomes.append({'kind':'protection','path':action['protection_path'],'status':'already-present'})
    for action in preview['actions']:
        for item in action['files']:
            if objects.inspect(action['root'],item['path'])!={'sha256':item['sha256'],'bytes':item['bytes']}:
                raise ValueError('EPUB object changed during recovery')
            outcomes.append({'kind':'object','path':item['remote'],'status':'already-present'})
    receipt={'version':1,'kind':'reader-epub-protected-staging','operation':operation,
             'plan_sha256':plan['plan_sha256'],'staging_sha256':preview['staging_sha256'],
             'protected_sources':len(preview['actions']),
             'verified_objects':sum(len(action['files']) for action in preview['actions']),
             'outcomes':outcomes,'index_written':False,'pointer_written':False,
             'publication_ready':False,'external_writers_verified':False}
    return journal.record_staged(store,current,receipt)
