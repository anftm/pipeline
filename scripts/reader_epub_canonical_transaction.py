"""Persistent single-CAS intent for an observed EPUB canonical-index merge.

Not a publisher or CLI. It changes no Reader pointer or site and does not
fence other writers or ABA. A claimed unknown CAS may only be reconciled by
reading exact candidate bytes; it is never retried by this protocol.
"""
from __future__ import annotations

import hashlib
import json
import re

try:
    from . import reader_epub_index_transaction as shard_tx, reader_epub_stage_journal as staging
    from . import reader_epub_canonical_preview as preview, reader_stream_checkpoint_cas as cas, search_snapshot
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    import reader_epub_index_transaction as shard_tx
    import reader_epub_stage_journal as staging
    import reader_epub_canonical_preview as preview
    import reader_stream_checkpoint_cas as cas
    import search_snapshot
    from shared import READER_ASSETS_BUCKET


ROOT='reader-index/epub-canonical-transactions/'
CANONICAL='chapters/ebook/epub/index.json'
MAX=32*1024*1024


def digest(value):return hashlib.sha256(search_snapshot.canonical(value)).hexdigest()


def key(operation,kind,value=None):
    staging._operation(operation)
    if kind=='head':return f'{ROOT}{operation}/head.json'
    if kind not in {'intents','records'}:raise ValueError('invalid EPUB canonical transaction key')
    return f'{ROOT}{operation}/{kind}/{staging._sha(value)}.json'


def _decode(raw):return staging._canonical(raw,MAX)


def _intent(value):
    fields={'version','kind','operation','stage_record_sha256','stage_intent_sha256','shard_record_sha256',
            'shard_intent_sha256','shard','shard_path','shard_candidate_sha256','parent_canonical_sha256',
            'parent_raw_sha256','parent_etag_sha256','parent_canonical','candidate_sha256','candidate',
            'selected_keys','previous_rows'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value.get('version')) is not int or value['version']!=1
            or value['kind']!='reader-epub-canonical-intent' or type(value.get('shard')) is not int
            or not 0<=value['shard']<8 or value['shard_path']!=f"chapters/ebook/epub/index-{value['shard']:02d}.json"):
        raise ValueError('invalid EPUB canonical intent')
    staging._operation(value['operation'])
    for name in ('stage_record_sha256','stage_intent_sha256','shard_record_sha256','shard_intent_sha256',
                 'shard_candidate_sha256','parent_canonical_sha256','parent_raw_sha256','parent_etag_sha256','candidate_sha256'):
        staging._sha(value[name])
    parent=value['parent_canonical'];candidate=value['candidate'];keys=value['selected_keys'];prior=value['previous_rows']
    if (not isinstance(parent,dict) or digest(parent)!=value['parent_canonical_sha256']
            or not isinstance(candidate,dict) or digest(candidate)!=value['candidate_sha256']
            or candidate.get('checkpoint_operation')!=value['operation']
            or not isinstance(keys,list) or not keys or keys!=sorted(keys) or len(keys)!=len(set(keys))
            or not isinstance(prior,dict) or set(prior)!=set(keys)):
        raise ValueError('EPUB canonical transaction identity differs')
    for index in (parent,candidate):
        cas._index((json.dumps(index,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8'))
    before={row['key']:row for row in parent['files']};after={row['key']:row for row in candidate['files']}
    if ({k for k in set(before)|set(after) if before.get(k)!=after.get(k)}!=set(keys)
            or any(prior[k]!=before.get(k) for k in keys)
            or candidate.get('failures',[])!=parent.get('failures',[])):
        raise ValueError('EPUB canonical candidate changes unrelated rows')
    return value


def _active(store,current):
    active=load(store,current['record']['operation'])
    if active is None or active['head_raw']!=current['head_raw'] or active['etag']!=current['etag']:
        raise ValueError('EPUB canonical journal changed; inspect before continuing')
    return active


def _bind(stage_current,shard_current,intent):
    if (stage_current is None or stage_current.record['state']!='staged'
            or stage_current.head['record']!=intent['stage_record_sha256']
            or stage_current.record['intent_sha256']!=intent['stage_intent_sha256']
            or shard_current is None or shard_current['record']['state']!='observed'
            or shard_current['head']['record']!=intent['shard_record_sha256']
            or shard_current['record']['intent_sha256']!=intent['shard_intent_sha256']
            or shard_current['intent']['candidate_sha256']!=intent['shard_candidate_sha256']
            or shard_current['intent']['shard_path']!=intent['shard_path']):
        raise ValueError('EPUB staged or observed shard journal changed')
    selected={row['key'] for row in stage_current.intent['plan']['sources']}
    if selected!=set(intent['selected_keys']):raise ValueError('EPUB canonical selected sources differ')
    shard_rows={row['key']:row for row in shard_current['intent']['candidate']['files']}
    candidate_rows={row['key']:row for row in intent['candidate']['files']}
    if any(candidate_rows.get(k)!=shard_rows.get(k) for k in selected):
        raise ValueError('EPUB canonical candidate differs from observed shard')


def load(store,operation):
    raw,etag=store.read_head(key(operation,'head'))
    if raw is None:
        if etag is not None:raise ValueError('missing EPUB canonical head has an ETag')
        return None
    staging.strong_etag(etag);head=shard_tx._head(_decode(raw))
    if head['operation']!=operation:raise ValueError('EPUB canonical head operation differs')
    def named(kind,sha):
        raw=store.read_immutable(key(operation,kind,sha))
        if raw is None or hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('EPUB canonical journal object missing or changed')
        return _decode(raw)
    record=shard_tx._record(named('records',head['record']))
    if record['operation']!=operation or record['sequence']!=head['sequence']:raise ValueError('EPUB canonical journal record differs')
    intent=_intent(named('intents',record['intent_sha256']))
    if intent['operation']!=operation:raise ValueError('EPUB canonical journal intent differs')
    prior=record
    while prior['prior'] is not None:
        previous=shard_tx._record(named('records',prior['prior']));shard_tx._advance(previous,prior,prior['prior']);prior=previous
    if prior['sequence']!=0:raise ValueError('EPUB canonical history is incomplete')
    if record['state']=='observed':
        raw_candidate=(json.dumps(intent['candidate'],ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8')
        if record['observed_raw_sha256']!=hashlib.sha256(raw_candidate).hexdigest():
            raise ValueError('EPUB canonical observation differs from candidate')
    return {'head':head,'head_raw':raw,'etag':etag,'record':record,'intent':intent}


def _commit(store,operation,record,current):
    shard_tx._record(record)
    if current is not None:shard_tx._advance(current['record'],record,current['head']['record'])
    raw=search_snapshot.canonical(record);sha=hashlib.sha256(raw).hexdigest()
    shard_tx._create(store,key(operation,'records',sha),raw)
    head={'version':1,'kind':'reader-epub-index-head','operation':operation,'sequence':record['sequence'],'record':sha}
    head_raw=search_snapshot.canonical(head)
    try:store.write_head(key(operation,'head'),head_raw,current['etag'] if current else None)
    except Exception:
        after=load(store,operation)
        if after is not None and after['head_raw']==head_raw:return after
        raise
    after=load(store,operation)
    if after is None or after['head_raw']!=head_raw:raise RuntimeError('EPUB canonical head readback differs')
    return after


def begin(store,stage_current,shard_current,artifact,canonical_snapshot):
    if not isinstance(canonical_snapshot,cas.Snapshot) or canonical_snapshot.value is None:
        raise ValueError('exact EPUB canonical snapshot required')
    staging.strong_etag(canonical_snapshot.etag);staging._sha(canonical_snapshot.raw_sha256)
    operation=stage_current.record['operation'];plan=stage_current.intent['plan']
    if load(store,operation) is not None:raise ValueError('EPUB canonical transaction already exists')
    if (not isinstance(artifact,dict) or artifact.get('kind')!='reader-epub-canonical-preview'
            or artifact.get('operation')!=operation or artifact.get('stage_record_sha256')!=stage_current.head['record']
            or artifact.get('index_record_sha256')!=shard_current['head']['record']
            or artifact.get('parent_canonical_sha256')!=digest(canonical_snapshot.value)
            or artifact.get('remote_writes')!=0 or artifact.get('index_written') is not False
            or artifact.get('sidecar_written') is not False or artifact.get('pointer_written') is not False
            or artifact.get('publication_ready') is not False or artifact.get('external_writers_verified') is not False
            or artifact.get('preview_sha256')!=digest({k:v for k,v in artifact.items() if k!='preview_sha256'})):
        raise ValueError('EPUB canonical preview differs from staged source')
    intent={'version':1,'kind':'reader-epub-canonical-intent','operation':operation,
            'stage_record_sha256':stage_current.head['record'],'stage_intent_sha256':stage_current.record['intent_sha256'],
            'shard_record_sha256':shard_current['head']['record'],'shard_intent_sha256':shard_current['record']['intent_sha256'],
            'shard':plan['shard'],'shard_path':shard_current['intent']['shard_path'],
            'shard_candidate_sha256':shard_current['intent']['candidate_sha256'],
            'parent_canonical_sha256':artifact['parent_canonical_sha256'],'parent_raw_sha256':canonical_snapshot.raw_sha256,
            'parent_etag_sha256':hashlib.sha256(canonical_snapshot.etag.encode()).hexdigest(),
            'parent_canonical':canonical_snapshot.value,'candidate_sha256':artifact['candidate_sha256'],
            'candidate':artifact['candidate'],'selected_keys':artifact['selected_keys'],'previous_rows':artifact['previous_rows']}
    _intent(intent);_bind(stage_current,shard_current,intent)
    raw=search_snapshot.canonical(intent);sha=hashlib.sha256(raw).hexdigest();shard_tx._create(store,key(operation,'intents',sha),raw)
    record={'version':1,'kind':'reader-epub-index-record','operation':operation,'sequence':0,'state':'prepared',
            'prior':None,'intent_sha256':sha,'claim_id':None,'observed_raw_sha256':None,'observed_etag_sha256':None}
    return _commit(store,operation,record,None)


def claim(store,current):
    if current['record']['state']!='prepared':raise ValueError('EPUB canonical write already claimed; reconcile instead')
    _active(store,current)
    import uuid
    value={**current['record'],'sequence':1,'state':'write-intent','prior':current['head']['record'],'claim_id':uuid.uuid4().hex}
    return _commit(store,value['operation'],value,current)


def record_observed(store,current,snapshot):
    if current['record']['state']!='write-intent' or not isinstance(snapshot,cas.Snapshot) or snapshot.value is None:
        raise ValueError('EPUB canonical observation is not awaiting an exact snapshot')
    staging.strong_etag(snapshot.etag);candidate=current['intent']['candidate']
    raw=(json.dumps(candidate,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf8')
    if snapshot.value!=candidate or snapshot.raw_sha256!=hashlib.sha256(raw).hexdigest():
        raise ValueError('EPUB canonical snapshot differs from intended bytes')
    _active(store,current)
    value={**current['record'],'sequence':2,'state':'observed','prior':current['head']['record'],
           'observed_raw_sha256':snapshot.raw_sha256,'observed_etag_sha256':hashlib.sha256(snapshot.etag.encode()).hexdigest()}
    return _commit(store,value['operation'],value,current)


class S3Journal(shard_tx.S3Journal):
    def _check(self,path,head):
        pattern=(r'reader-index/epub-canonical-transactions/[0-9a-f]{32}/head\.json' if head else
                 r'reader-index/epub-canonical-transactions/[0-9a-f]{32}/(?:intents|records)/[0-9a-f]{64}\.json')
        if not isinstance(path,str) or not re.fullmatch(pattern,path):raise ValueError('EPUB canonical journal path outside allowlist')
        ns,bucket=self.store._location(READER_ASSETS_BUCKET)
        if (ns,bucket)!=('vomebook','reader-assets-v2'):raise ValueError('unexpected EPUB canonical journal bucket')
        return self.store._client(ns),bucket
    def _read(self,path,head):
        client,bucket=self._check(path,head)
        try:response=client.get_object(Bucket=bucket,Key=path)
        except Exception as error:
            from botocore.exceptions import ClientError
            if isinstance(error,ClientError) and error.response.get('Error',{}).get('Code') in {'404','NoSuchKey','NotFound'}:
                return None,None
            raise
        body,length=response.get('Body'),response.get('ContentLength');limit=staging.MAX_RECORD if head or '/records/' in path else MAX
        if body is None:raise ValueError('EPUB canonical journal object lacks body')
        try:
            if type(length) is not int or not 0<length<=limit:raise ValueError('EPUB canonical journal size invalid')
            raw=body.read(length+1)
        finally:body.close()
        if not isinstance(raw,bytes) or len(raw)!=length:raise ValueError('EPUB canonical journal object truncated')
        return raw,response.get('ETag')
    def create_immutable(self,path,raw):
        client,bucket=self._check(path,False)
        if not isinstance(raw,bytes) or hashlib.sha256(raw).hexdigest()!=path.rsplit('/',1)[1][:-5]:
            raise ValueError('EPUB canonical journal object is not content addressed')
        value=_decode(raw);_intent(value) if '/intents/' in path else shard_tx._record(value)
        client.put_object(Bucket=bucket,Key=path,Body=raw,ContentType='application/json',IfNoneMatch='*')
