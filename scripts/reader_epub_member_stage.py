"""Durable single-attempt claims for individual protected EPUB stage members.

This module has no CLI or workflow. Its S3 adapter is restricted to journal
keys. A claim prevents protocol replay of one member, not delayed requests,
external writers, ABA or cross-object races. A missing claimed member remains
unresolved; only an exact readback can complete it. No index or pointer writes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import uuid
from pathlib import Path

try:
    from . import reader_epub_stage_journal as stage, reader_epub_protection as protection
    from . import reader_epub_immutable as immutable, reader_epub_shadow as shadow, search_snapshot
except ImportError:
    import reader_epub_stage_journal as stage
    import reader_epub_protection as protection
    import reader_epub_immutable as immutable
    import reader_epub_shadow as shadow
    import search_snapshot


ROOT='reader-index/epub-stage-members/'
MAX=16*1024
MAX_MEMBER_INTENT=32*1024


def _hash(raw):return hashlib.sha256(raw).hexdigest()


def key(operation, member_sha256, kind, digest=None):
    stage._operation(operation);stage._sha(member_sha256)
    prefix=f'{ROOT}{operation}/{member_sha256}/'
    if kind=='head':return prefix+'head.json'
    if kind not in {'intents','records'}:raise ValueError('invalid EPUB member journal key')
    return prefix+kind+'/'+stage._sha(digest)+'.json'


def _member(current, path):
    if current is None or current.record.get('state')!='write-intent':
        raise ValueError('EPUB stage must have a durable write intent')
    plan=current.intent['plan'];preview=current.intent['preview'];matches=[]
    for number,action in enumerate(preview['actions']):
        if path==action['protection_path']:
            raw=search_snapshot.canonical(protection.make_record(plan['operation'],plan['plan_sha256'],action['root'],current.intent['created_at']))
            matches.append({'kind':'protection','path':path,'root':action['root'],'relative':None,
                            'sha256':_hash(raw),'bytes':len(raw),'source_number':number})
        for item in action['files']:
            if path==item['remote']:
                matches.append({'kind':'object','path':path,'root':action['root'],'relative':item['path'],
                                'sha256':item['sha256'],'bytes':item['bytes'],'source_number':number})
    if len(matches)!=1:raise ValueError('EPUB member is not unique in the durable staging intent')
    member=matches[0]
    # Prefix hashing permits a fresh process to address an existing claim
    # without trusting content supplied by a caller. The immutable intent binds
    # the full expected identity; a missing claimed object remains pending.
    return member


def _intent(value):
    fields={'version','kind','operation','stage_record_sha256','stage_intent_sha256','member_sha256','member'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value.get('version')) is not int
            or value['version']!=1 or value['kind']!='reader-epub-member-intent'):
        raise ValueError('invalid EPUB member intent')
    stage._operation(value['operation'])
    for name in ('stage_record_sha256','stage_intent_sha256','member_sha256'):stage._sha(value[name])
    member=value['member']
    if (not isinstance(member,dict) or set(member)!={'kind','path','root','relative','sha256','bytes','source_number'}
            or member['kind'] not in {'protection','object'} or not isinstance(member['path'],str)
            or type(member['source_number']) is not int or not 0<=member['source_number']<=2):
        raise ValueError('invalid EPUB member identity')
    stage._sha(member['sha256'])
    if type(member['bytes']) is not int or not 0<member['bytes']<=immutable.MAX_OBJECT_BYTES:
        raise ValueError('invalid EPUB member length')
    immutable._root(member['root'])
    if member['kind']=='protection':
        if (member['relative'] is not None or member['bytes']>protection.MAX_RECORD
                or not protection.PROTECTION.fullmatch(member['path'])):
            raise ValueError('invalid EPUB protection identity')
    elif immutable._key(member['root'],member['relative'])!=member['path']:
        raise ValueError('invalid EPUB object identity')
    if _hash(search_snapshot.canonical(member))!=value['member_sha256']:
        raise ValueError('EPUB member digest differs')
    return value


def _head(value):
    if (not isinstance(value,dict) or set(value)!={'version','kind','operation','member_sha256','sequence','record'}
            or type(value.get('version')) is not int or value['version']!=1 or value['kind']!='reader-epub-member-head'
            or type(value.get('sequence')) is not int or value['sequence'] not in (0,1,2)):
        raise ValueError('invalid EPUB member journal head')
    stage._operation(value['operation']);stage._sha(value['member_sha256']);stage._sha(value['record'])
    return value


def _record(value):
    fields={'version','kind','operation','member_sha256','sequence','state','prior','intent_sha256','claim_id','observed_sha256','observed_bytes'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value.get('version')) is not int
            or value['version']!=1 or value['kind']!='reader-epub-member-record'
            or type(value.get('sequence')) is not int or value['sequence'] not in (0,1,2)
            or value.get('state')!={0:'prepared',1:'claimed',2:'observed'}[value['sequence']]):
        raise ValueError('invalid EPUB member journal record')
    stage._operation(value['operation']);stage._sha(value['member_sha256']);stage._sha(value['intent_sha256'])
    if value['sequence']==0:
        if any(value[field] is not None for field in ('prior','claim_id','observed_sha256','observed_bytes')):
            raise ValueError('prepared EPUB member has successor data')
    else:
        stage._sha(value['prior']);stage._operation(value['claim_id'])
        if value['sequence']==1 and (value['observed_sha256'] is not None or value['observed_bytes'] is not None):
            raise ValueError('claimed EPUB member already has an observation')
        if value['sequence']==2:
            stage._sha(value['observed_sha256'])
            if type(value['observed_bytes']) is not int or value['observed_bytes']<=0:
                raise ValueError('invalid EPUB member observed length')
    return value


def _transition(old,new,prior):
    if (new['operation']!=old['operation'] or new['member_sha256']!=old['member_sha256']
            or new['intent_sha256']!=old['intent_sha256'] or new['sequence']!=old['sequence']+1
            or new['prior']!=prior or (old['sequence']==1 and new['claim_id']!=old['claim_id'])):
        raise ValueError('EPUB member journal history differs')


@dataclass(frozen=True)
class Current:
    head:dict
    head_raw:bytes
    etag:str
    record:dict
    intent:dict


def _named(store, operation, member_sha, kind, digest):
    raw=store.read_immutable(key(operation,member_sha,kind,digest))
    if raw is None or _hash(raw)!=digest:raise ValueError('EPUB member journal object missing or changed')
    return stage._canonical(raw,MAX_MEMBER_INTENT if kind=='intents' else MAX)


def load(store,operation,member_sha):
    raw,etag=store.read_head(key(operation,member_sha,'head'))
    if raw is None:
        if etag is not None:raise ValueError('missing EPUB member head has an ETag')
        return None
    stage.strong_etag(etag);head=_head(stage._canonical(raw,MAX))
    if head['operation']!=operation or head['member_sha256']!=member_sha:raise ValueError('EPUB member head identity differs')
    record=_record(_named(store,operation,member_sha,'records',head['record']))
    if record['operation']!=operation or record['member_sha256']!=member_sha or record['sequence']!=head['sequence']:
        raise ValueError('EPUB member head record differs')
    intent=_intent(_named(store,operation,member_sha,'intents',record['intent_sha256']))
    if intent['operation']!=operation or intent['member_sha256']!=member_sha:raise ValueError('EPUB member intent differs')
    prior=record
    while prior['prior'] is not None:
        previous=_record(_named(store,operation,member_sha,'records',prior['prior']))
        _transition(previous,prior,prior['prior']);prior=previous
    if prior['sequence']!=0:raise ValueError('EPUB member history is incomplete')
    if record['state']=='observed' and (record['observed_sha256']!=intent['member']['sha256']
                                      or record['observed_bytes']!=intent['member']['bytes']):
        raise ValueError('EPUB member observation differs from intent')
    return Current(head,raw,etag,record,intent)


def _commit(store,operation,member_sha,record,current):
    _record(record)
    if current is not None:_transition(current.record,record,current.head['record'])
    raw=search_snapshot.canonical(record);digest=_hash(raw)
    stage._create(store,key(operation,member_sha,'records',digest),raw)
    head={'version':1,'kind':'reader-epub-member-head','operation':operation,'member_sha256':member_sha,
          'sequence':record['sequence'],'record':digest}
    head_raw=search_snapshot.canonical(head)
    try:store.write_head(key(operation,member_sha,'head'),head_raw,current.etag if current else None)
    except Exception:
        after=load(store,operation,member_sha)
        if after is not None and after.head_raw==head_raw:return after
        raise
    after=load(store,operation,member_sha)
    if after is None or after.head_raw!=head_raw:raise RuntimeError('EPUB member head readback differs')
    return after


def _active_stage(store,current):
    observed=stage.load(store,current.record['operation'])
    if observed is None or observed.head_raw!=current.head_raw or observed.etag!=current.etag:
        raise ValueError('EPUB staging journal changed during member operation')
    return observed


def _bind(stage_current,member_current):
    intent=member_current.intent
    if (intent['operation']!=stage_current.record['operation']
            or intent['stage_record_sha256']!=stage_current.head['record']
            or intent['stage_intent_sha256']!=stage_current.record['intent_sha256']
            or intent['member']!=_member(stage_current,intent['member']['path'])):
        raise ValueError('EPUB member journal does not bind current stage')


def prepare(store, stage_store, stage_current, path):
    _active_stage(stage_store,stage_current);member=_member(stage_current,path)
    member_sha=_hash(search_snapshot.canonical(member));op=stage_current.record['operation']
    if load(store,op,member_sha) is not None:raise ValueError('EPUB member already has a journal; inspect it')
    intent={'version':1,'kind':'reader-epub-member-intent','operation':op,
            'stage_record_sha256':stage_current.head['record'],'stage_intent_sha256':stage_current.record['intent_sha256'],
            'member_sha256':member_sha,'member':member}
    _intent(intent);raw=search_snapshot.canonical(intent);digest=_hash(raw)
    stage._create(store,key(op,member_sha,'intents',digest),raw)
    record={'version':1,'kind':'reader-epub-member-record','operation':op,'member_sha256':member_sha,
            'sequence':0,'state':'prepared','prior':None,'intent_sha256':digest,'claim_id':None,
            'observed_sha256':None,'observed_bytes':None}
    _active_stage(stage_store,stage_current)
    return _commit(store,op,member_sha,record,None)


def _active(store,current):
    observed=load(store,current.record['operation'],current.record['member_sha256'])
    if observed is None or observed.head_raw!=current.head_raw or observed.etag!=current.etag:
        raise ValueError('EPUB member journal changed; inspect before continuing')
    return observed


def _verify_protections(stage_current,protections):
    plan=stage_current.intent['plan']
    for action in stage_current.intent['preview']['actions']:
        expected=search_snapshot.canonical(protection.make_record(plan['operation'],plan['plan_sha256'],action['root'],stage_current.intent['created_at']))
        if protections.inspect(action['protection_path'])!=expected:
            raise ValueError('EPUB protection is absent or differs; no object write permitted')


def _observe(member,*,protections,objects):
    if member['kind']=='protection':
        raw=protections.inspect(member['path'])
        return None if raw is None else {'sha256':_hash(raw),'bytes':len(raw)}
    return objects.inspect(member['root'],member['relative'])


def _record_observation(store, current, observed):
    member=current.intent['member'];expected={'sha256':member['sha256'],'bytes':member['bytes']}
    if observed!=expected:raise ValueError('EPUB member is missing or differs; keep claim unresolved')
    _active(store,current)
    value={**current.record,'sequence':2,'state':'observed','prior':current.head['record'],
           'observed_sha256':observed['sha256'],'observed_bytes':observed['bytes']}
    return _commit(store,value['operation'],value['member_sha256'],value,current)


def apply_once(store,stage_store,stage_current,current,*,directory:Path,protections,objects,revalidate):
    if current.record['state']!='prepared':raise ValueError('EPUB member is already claimed; reconcile instead')
    _active_stage(stage_store,stage_current);_active(store,current);_bind(stage_current,current)
    plan=stage_current.intent['plan'];preview=stage_current.intent['preview'];member=current.intent['member']
    revalidate(plan,preview)
    if member['kind']=='object':_verify_protections(stage_current,protections)
    if member['kind']=='object':
        local=directory/str(member['source_number'])/'bundle'/member['relative']
        expected={'sha256':member['sha256'],'bytes':member['bytes']}
        if immutable.local_identity(local)!=expected:raise ValueError('EPUB local member differs from durable intent')
    observed=_observe(member,protections=protections,objects=objects)
    expected={'sha256':member['sha256'],'bytes':member['bytes']}
    if observed is not None and observed!=expected:raise ValueError('existing EPUB member conflicts with durable intent')
    _active_stage(stage_store,stage_current);_active(store,current)
    claim={**current.record,'sequence':1,'state':'claimed','prior':current.head['record'],'claim_id':uuid.uuid4().hex}
    claimed=_commit(store,current.record['operation'],current.record['member_sha256'],claim,current)
    _active_stage(stage_store,stage_current);_active(store,claimed)
    if member['kind']=='object':_verify_protections(stage_current,protections)
    if observed is None:
        if member['kind']=='protection':
            raw=search_snapshot.canonical(protection.make_record(plan['operation'],plan['plan_sha256'],member['root'],stage_current.intent['created_at']))
            protection.validate(member['path'],raw)
            protections.create_once(member['path'],raw)
        else:
            objects.create_once(member['root'],member['relative'],local,expected)
    # Any exception leaves a durable single claim; another invocation may only
    # reconcile via exact reads, never resend the mutation.
    observed=_observe(member,protections=protections,objects=objects)
    _active_stage(stage_store,stage_current);_active(store,claimed)
    if member['kind']=='object':_verify_protections(stage_current,protections)
    return _record_observation(store,claimed,observed)


def reconcile(store,stage_store,stage_current,member_sha,*,protections,objects,revalidate):
    _active_stage(stage_store,stage_current)
    current=load(store,stage_current.record['operation'],member_sha)
    if current is None or current.record['state']!='claimed':raise ValueError('EPUB member has no unresolved claim')
    _bind(stage_current,current);plan=stage_current.intent['plan'];preview=stage_current.intent['preview']
    revalidate(plan,preview);member=current.intent['member']
    if member['kind']=='object':_verify_protections(stage_current,protections)
    observed=_observe(member,protections=protections,objects=objects)
    revalidate(plan,preview);_active_stage(stage_store,stage_current);_active(store,current)
    if member['kind']=='object':_verify_protections(stage_current,protections)
    return _record_observation(store,current,observed)


def finalize(store,stage_store,stage_current,*,protections,objects,revalidate):
    """Observe every claimed member before advancing only the stage journal."""
    _active_stage(stage_store,stage_current)
    plan=stage_current.intent['plan'];preview=stage_current.intent['preview']
    revalidate(plan,preview);_verify_protections(stage_current,protections)
    outcomes=[];members=[]
    for action in preview['actions']:
        members.append(_member(stage_current,action['protection_path']))
    for action in preview['actions']:
        members.extend(_member(stage_current,item['remote']) for item in action['files'])
    if len(members)>36_003:raise ValueError('EPUB member staging exceeds bounds')
    for member in members:
        member_sha=_hash(search_snapshot.canonical(member));current=load(store,plan['operation'],member_sha)
        if current is None or current.record['state']!='observed':
            raise ValueError('EPUB member is not fully observed')
        _bind(stage_current,current)
        if _observe(member,protections=protections,objects=objects)!={'sha256':member['sha256'],'bytes':member['bytes']}:
            raise ValueError('EPUB member changed before staging acceptance')
        outcomes.append({'kind':member['kind'],'path':member['path'],'status':'already-present'})
    revalidate(plan,preview);_active_stage(stage_store,stage_current);_verify_protections(stage_current,protections)
    for member in members:
        current=load(store,plan['operation'],_hash(search_snapshot.canonical(member)))
        if current is None or current.record['state']!='observed':raise ValueError('EPUB member journal changed before staging acceptance')
        _bind(stage_current,current)
        if _observe(member,protections=protections,objects=objects)!={'sha256':member['sha256'],'bytes':member['bytes']}:
            raise ValueError('EPUB member changed during staging acceptance')
    _active_stage(stage_store,stage_current)
    receipt={'version':1,'kind':'reader-epub-protected-staging','operation':plan['operation'],
             'plan_sha256':plan['plan_sha256'],'staging_sha256':preview['staging_sha256'],
             'protected_sources':len(preview['actions']),
             'verified_objects':sum(len(action['files']) for action in preview['actions']),
             'outcomes':outcomes,'index_written':False,'pointer_written':False,
             'publication_ready':False,'external_writers_verified':False}
    return stage.record_staged(stage_store,stage_current,receipt)


class S3Journal(stage.S3Journal):
    def _check(self,path,head):
        pattern=(r'reader-index/epub-stage-members/[0-9a-f]{32}/[0-9a-f]{64}/head\.json' if head else
                 r'reader-index/epub-stage-members/[0-9a-f]{32}/[0-9a-f]{64}/(?:intents|records)/[0-9a-f]{64}\.json')
        if not isinstance(path,str) or not re.fullmatch(pattern,path):raise ValueError('EPUB member journal path outside allowlist')
        ns,bucket=self.store._location(stage.READER_ASSETS_BUCKET)
        if (ns,bucket)!=('vomebook','reader-assets-v2'):raise ValueError('unexpected EPUB member journal bucket')
        return self.store._client(ns),bucket
    def _read(self,path,head):
        # The parent adapter supplies bounded reads and only calls this
        # subclass' path allowlist.
        return super()._read(path,head)
    def create_immutable(self,path,raw):
        client,bucket=self._check(path,False)
        if not isinstance(raw,bytes) or _hash(raw)!=path.rsplit('/',1)[1][:-5]:
            raise ValueError('EPUB member journal object is not content addressed')
        value=stage._canonical(raw,MAX_MEMBER_INTENT if '/intents/' in path else MAX)
        _intent(value) if '/intents/' in path else _record(value)
        client.put_object(Bucket=bucket,Key=path,Body=raw,ContentType='application/json',IfNoneMatch='*')
    def write_head(self,path,raw,etag):
        client,bucket=self._check(path,True);_head(stage._canonical(raw,MAX))
        condition={'IfNoneMatch':'*'} if etag is None else {'IfMatch':stage.strong_etag(etag)}
        client.put_object(Bucket=bucket,Key=path,Body=raw,ContentType='application/json',**condition)
