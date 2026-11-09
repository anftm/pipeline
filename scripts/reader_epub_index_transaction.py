"""Durable intent for exactly one staged EPUB shard-index CAS.

The caller must independently revalidate source/consumers and every staged
object. This journal cannot publish a canonical index or Reader site. Its CAS
is one-object only; cooperating writers and external credentials are not fenced.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

try:
    from . import reader_epub_stage_journal as stage_journal, reader_stream_checkpoint_cas as cas, search_snapshot
except ImportError:
    import reader_epub_stage_journal as stage_journal
    import reader_stream_checkpoint_cas as cas
    import search_snapshot


ROOT='reader-index/epub-index-transactions/'
HEX32=re.compile(r'[0-9a-f]{32}\Z')
HEX64=re.compile(r'[0-9a-f]{64}\Z')
MAX=8*1024*1024


def digest(value):return hashlib.sha256(search_snapshot.canonical(value)).hexdigest()


def _id(value):
    if not isinstance(value,str) or not HEX32.fullmatch(value):raise ValueError('invalid EPUB index operation')
    return value


def _sha(value):
    if not isinstance(value,str) or not HEX64.fullmatch(value):raise ValueError('invalid EPUB index digest')
    return value


def key(operation,kind,value=None):
    _id(operation)
    if kind=='head':return f'{ROOT}{operation}/head.json'
    if kind not in {'intents','records'}:raise ValueError('invalid EPUB index journal key kind')
    return f'{ROOT}{operation}/{kind}/{_sha(value)}.json'


def _decode(raw):
    if not isinstance(raw,bytes) or not 0<len(raw)<=MAX:raise ValueError('invalid EPUB index journal bytes')
    try:value=json.loads(raw)
    except (UnicodeDecodeError,json.JSONDecodeError) as error:raise ValueError('invalid EPUB index journal JSON') from error
    if not isinstance(value,dict) or search_snapshot.canonical(value)!=raw:
        raise ValueError('EPUB index journal JSON must be canonical')
    return value


def _intent(raw):
    value=_decode(raw)
    keys={'version','kind','operation','stage_record_sha256','stage_intent_sha256','shard','shard_path',
          'parent_canonical_sha256','parent_shard_sha256','parent_etag_sha256','parent_raw_sha256',
          'parent_shard','candidate_sha256','candidate'}
    if (set(value)!=keys or type(value['version']) is not int or value['version']!=1
            or value['kind']!='reader-epub-index-intent' or type(value['shard']) is not int
            or not 0<=value['shard']<8 or value['shard_path']!=f"chapters/ebook/epub/index-{value['shard']:02d}.json"):
        raise ValueError('invalid EPUB index intent')
    _id(value['operation'])
    for name in ('stage_record_sha256','stage_intent_sha256','parent_canonical_sha256','parent_shard_sha256',
                 'parent_etag_sha256','parent_raw_sha256','candidate_sha256'):_sha(value[name])
    parent=value['parent_shard']
    candidate=value['candidate']
    if (not isinstance(parent,dict) or digest(parent)!=value['parent_shard_sha256']):
        raise ValueError('EPUB index parent identity differs')
    if parent:
        cas._index((json.dumps(parent,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8'))
    if (not isinstance(candidate,dict) or candidate.get('checkpoint_operation')!=value['operation']
            or digest(candidate)!=value['candidate_sha256']):
        raise ValueError('EPUB index candidate identity differs')
    cas._index((json.dumps(candidate,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8'))
    return value


def _record(value):
    fields={'version','kind','operation','sequence','state','prior','intent_sha256','claim_id','observed_raw_sha256','observed_etag_sha256'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['version']) is not int or value['version']!=1
            or value['kind']!='reader-epub-index-record' or type(value['sequence']) is not int
            or value['sequence'] not in (0,1,2)):
        raise ValueError('invalid EPUB index record')
    _id(value['operation']);_sha(value['intent_sha256'])
    if value['state']!={0:'prepared',1:'write-intent',2:'observed'}[value['sequence']]:
        raise ValueError('EPUB index state sequence differs')
    if value['sequence']==0:
        if (value['prior'] is not None or value['claim_id'] is not None or value['observed_raw_sha256'] is not None
                or value['observed_etag_sha256'] is not None):
            raise ValueError('prepared index record has successor data')
    else:
        _sha(value['prior']);_id(value['claim_id'])
        if value['sequence']==1 and (value['observed_raw_sha256'] is not None or value['observed_etag_sha256'] is not None):
            raise ValueError('EPUB index observation precedes the write')
        if value['sequence']==2:
            _sha(value['observed_raw_sha256']);_sha(value['observed_etag_sha256'])
    return value


def _head(value):
    if (not isinstance(value,dict) or set(value)!={'version','kind','operation','sequence','record'}
            or type(value['version']) is not int or value['version']!=1 or value['kind']!='reader-epub-index-head'
            or type(value['sequence']) is not int or value['sequence'] not in (0,1,2)):
        raise ValueError('invalid EPUB index head')
    _id(value['operation']);_sha(value['record']);return value


def _advance(before,after,previous):
    if (after['operation']!=before['operation'] or after['intent_sha256']!=before['intent_sha256']
            or after['sequence']!=before['sequence']+1 or after['prior']!=previous
            or before['sequence']==1 and after['claim_id']!=before['claim_id']):
        raise ValueError('EPUB index journal history differs')


def _named(store,op,kind,sha):
    raw=store.read_immutable(key(op,kind,sha))
    if raw is None or hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('EPUB index journal object missing or changed')
    return raw


def load(store,operation):
    path=key(operation,'head');raw,etag=store.read_head(path)
    if raw is None:
        if etag is not None:raise ValueError('missing EPUB index head has a validator')
        return None
    stage_journal.strong_etag(etag);head=_head(_decode(raw))
    if head['operation']!=operation:raise ValueError('EPUB index head operation differs')
    record=_record(_decode(_named(store,operation,'records',head['record'])))
    if record['operation']!=operation or record['sequence']!=head['sequence']:raise ValueError('EPUB index head record differs')
    intent=_intent(_named(store,operation,'intents',record['intent_sha256']))
    if intent['operation']!=operation:raise ValueError('EPUB index intent operation differs')
    prior=record
    while prior['prior'] is not None:
        previous=_record(_decode(_named(store,operation,'records',prior['prior'])))
        _advance(previous,prior,prior['prior']);prior=previous
    if prior['sequence']!=0:raise ValueError('EPUB index journal history is incomplete')
    if record['state']=='observed':
        raw_candidate=(json.dumps(intent['candidate'],ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8')
        if record['observed_raw_sha256']!=hashlib.sha256(raw_candidate).hexdigest():
            raise ValueError('EPUB index observation does not bind candidate bytes')
    return {'head':head,'head_raw':raw,'etag':etag,'record':record,'intent':intent}


def _create(store,path,raw):
    current=store.read_immutable(path)
    if current is None:
        try:store.create_immutable(path,raw)
        except Exception:
            if store.read_immutable(path)!=raw:raise
            return
        current=store.read_immutable(path)
    if current!=raw:raise ValueError('immutable EPUB index journal conflict')


def _commit(store,operation,record,previous):
    _record(record)
    if previous is not None:_advance(previous['record'],record,previous['head']['record'])
    raw=search_snapshot.canonical(record);sha=hashlib.sha256(raw).hexdigest()
    _create(store,key(operation,'records',sha),raw)
    head={'version':1,'kind':'reader-epub-index-head','operation':operation,'sequence':record['sequence'],'record':sha}
    head_raw=search_snapshot.canonical(head)
    try:store.write_head(key(operation,'head'),head_raw,previous['etag'] if previous else None)
    except Exception:
        after=load(store,operation)
        if after is not None and after['head_raw']==head_raw:return after
        raise
    after=load(store,operation)
    if after is None or after['head_raw']!=head_raw:raise RuntimeError('EPUB index head readback differs')
    return after


def begin(store,stage_current,preview,canonical,shard,shard_etag,shard_raw_sha256):
    if stage_current is None or stage_current.record['state']!='staged':raise ValueError('EPUB objects are not staged')
    if shard_etag is not None:
        stage_journal.strong_etag(shard_etag)
    operation=stage_current.record['operation'];plan=stage_current.intent['plan']
    if load(store,operation) is not None:raise ValueError('EPUB index transaction already exists')
    if (not isinstance(preview,dict) or preview.get('kind')!='reader-epub-index-preview'
            or preview.get('operation')!=operation or preview.get('plan_sha256')!=plan['plan_sha256']
            or preview.get('staging_sha256')!=stage_current.intent['preview']['staging_sha256']
            or preview.get('canonical_index_sha256')!=plan['canonical_index_sha256']
            or preview.get('shard_index_sha256')!=plan['shard_index_sha256']
            or preview.get('remote_writes')!=0 or preview.get('index_written') is not False
            or preview.get('pointer_written') is not False or preview.get('publication_ready') is not False
            or preview.get('external_writers_verified') is not False
            or preview.get('changed_keys')!=sorted(row['key'] for row in plan['sources'])
            or preview.get('previous_manifests')!={row['key']:row['previous_manifest'] for row in plan['sources']}):
        raise ValueError('EPUB index preview does not bind the staged operation')
    if digest(canonical)!=plan['canonical_index_sha256'] or digest(shard)!=plan['shard_index_sha256']:
        raise ValueError('EPUB index parent differs from the staged plan')
    if (shard_etag is None) != (shard == {}):
        raise ValueError('EPUB shard presence and validator are inconsistent')
    if shard_etag is None:
        if shard_raw_sha256 is not None:raise ValueError('absent EPUB shard cannot have a raw digest')
        parent_raw_digest=hashlib.sha256(b'absent').hexdigest()
    else:
        parent_raw_digest=_sha(shard_raw_sha256)
    candidate=preview.get('candidate')
    if not isinstance(candidate,dict) or not isinstance(candidate.get('files'),list):
        raise ValueError('EPUB index preview candidate is invalid')
    indexed={row['key']:row for row in candidate['files'] if isinstance(row,dict) and isinstance(row.get('key'),str)}
    if len(indexed)!=len(candidate['files']):raise ValueError('EPUB index preview has duplicate or invalid sources')
    old_rows={row['key']:row for row in shard.get('files',[]) if isinstance(row,dict) and isinstance(row.get('key'),str)}
    if len(old_rows)!=len(shard.get('files',[])):
        raise ValueError('EPUB shard parent has duplicate or invalid sources')
    selected={row['key'] for row in plan['sources']}
    changed={key for key in set(old_rows)|set(indexed) if old_rows.get(key)!=indexed.get(key)}
    if changed!=selected or candidate.get('failures',[])!=shard.get('failures',[]):
        raise ValueError('EPUB index candidate changes unrelated rows or failures')
    if any(row['key'] in selected for row in shard.get('failures',[])):
        raise ValueError('EPUB selected sources have unresolved failures')
    actions={row['key']:row for row in stage_current.intent['preview']['actions']}
    for source in plan['sources']:
        action=actions.get(source['key']);row=indexed.get(source['key'])
        if (not action or not row or row.get('source_revision')!=source['source_revision']
                or row.get('source_sha256')!=action['source_sha256']
                or row.get('manifest')!=action['root']+'/chapter-manifest.json'
                or row.get('processing_roots')!=[action['protection_path']]):
            raise ValueError('EPUB index candidate does not name its staged objects')
    intent={'version':1,'kind':'reader-epub-index-intent','operation':operation,
            'stage_record_sha256':stage_current.head['record'],'stage_intent_sha256':stage_current.record['intent_sha256'],
            'shard':plan['shard'],'shard_path':f"chapters/ebook/epub/index-{plan['shard']:02d}.json",
            'parent_canonical_sha256':plan['canonical_index_sha256'],'parent_shard_sha256':plan['shard_index_sha256'],
            'parent_etag_sha256':hashlib.sha256((shard_etag or 'absent').encode()).hexdigest(),
            'parent_raw_sha256':parent_raw_digest, 'parent_shard':shard, 'candidate_sha256':preview.get('candidate_sha256'),'candidate':preview.get('candidate')}
    raw=search_snapshot.canonical(intent);_intent(raw);sha=hashlib.sha256(raw).hexdigest()
    _create(store,key(operation,'intents',sha),raw)
    record={'version':1,'kind':'reader-epub-index-record','operation':operation,'sequence':0,'state':'prepared',
            'prior':None,'intent_sha256':sha,'claim_id':None,'observed_raw_sha256':None,'observed_etag_sha256':None}
    return _commit(store,operation,record,None)


def claim(store,current):
    if current['record']['state']!='prepared':raise ValueError('EPUB index write already claimed; reconcile instead')
    active=load(store,current['record']['operation'])
    if active is None or active['head_raw']!=current['head_raw'] or active['etag']!=current['etag']:
        raise ValueError('EPUB index head changed before claim')
    value={**current['record'],'sequence':1,'state':'write-intent','prior':current['head']['record'],'claim_id':uuid.uuid4().hex}
    return _commit(store,value['operation'],value,current)


def record_observed(store,current,*,snapshot):
    """Record only an actual read of the exact canonical serialized shard."""
    if current['record']['state']!='write-intent':raise ValueError('EPUB index is not awaiting observation')
    if not isinstance(snapshot,cas.Snapshot) or snapshot.value is None:
        raise ValueError('EPUB index observation is not an exact stored snapshot')
    stage_journal.strong_etag(snapshot.etag)
    candidate=current['intent']['candidate']
    expected=(json.dumps(candidate,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8')
    if (snapshot.raw_sha256!=hashlib.sha256(expected).hexdigest() or snapshot.value!=candidate
            or digest(snapshot.value)!=current['intent']['candidate_sha256']):
        raise ValueError('EPUB index snapshot does not match intended bytes')
    active=load(store,current['record']['operation'])
    if active is None or active['head_raw']!=current['head_raw'] or active['etag']!=current['etag']:
        raise ValueError('EPUB index head changed before observation')
    value={**current['record'],'sequence':2,'state':'observed','prior':current['head']['record'],
           'observed_raw_sha256':snapshot.raw_sha256,
           'observed_etag_sha256':hashlib.sha256(snapshot.etag.encode('utf8')).hexdigest()}
    return _commit(store,value['operation'],value,current)


class S3Journal(stage_journal.S3Journal):
    def _check(self,path,head):
        pattern=(r'reader-index/epub-index-transactions/[0-9a-f]{32}/head\.json' if head else
                 r'reader-index/epub-index-transactions/[0-9a-f]{32}/(?:intents|records)/[0-9a-f]{64}\.json')
        if not isinstance(path,str) or not re.fullmatch(pattern,path):raise ValueError('EPUB index journal path outside allowlist')
        ns,bucket=self.store._location(stage_journal.READER_ASSETS_BUCKET)
        if (ns,bucket)!=('vomebook','reader-assets-v2'):raise ValueError('unexpected EPUB index journal bucket')
        return self.store._client(ns),bucket
    def _read(self,path,head):
        client,bucket=self._check(path,head)
        try:response=client.get_object(Bucket=bucket,Key=path)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error,ClientError) and error.response.get('Error',{}).get('Code') in {'404','NoSuchKey','NotFound'}:
                return None,None
            raise
        body,length=response.get('Body'),response.get('ContentLength')
        if body is None:raise ValueError('EPUB index journal object lacks body')
        limit=stage_journal.MAX_RECORD if head or '/records/' in path else MAX
        try:
            if type(length) is not int or not 0<length<=limit:raise ValueError('EPUB index journal size invalid')
            raw=body.read(length+1)
        finally:body.close()
        if not isinstance(raw,bytes) or len(raw)!=length:raise ValueError('EPUB index journal object truncated')
        return raw,response.get('ETag')
    def create_immutable(self,path,raw):
        client,bucket=self._check(path,False)
        if not isinstance(raw,bytes) or hashlib.sha256(raw).hexdigest()!=path.rsplit('/',1)[1][:-5]:
            raise ValueError('EPUB index journal object is not content addressed')
        _decode(raw)
        if '/intents/' in path:_intent(raw)
        else:_record(_decode(raw))
        client.put_object(Bucket=bucket,Key=path,Body=raw,ContentType='application/json',IfNoneMatch='*')
    def write_head(self,path,raw,etag):
        client,bucket=self._check(path,True);_head(_decode(raw))
        condition={'IfNoneMatch':'*'} if etag is None else {'IfMatch':stage_journal.strong_etag(etag)}
        client.put_object(Bucket=bucket,Key=path,Body=raw,ContentType='application/json',**condition)
