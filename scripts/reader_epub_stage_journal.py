"""Per-operation immutable EPUB staging intent and conditional journal head.

No workflow or CLI enables this journal. It records a plan and exact staging
preview before any production staging; a claim is single-use and uncertain
writes are read back, never replayed. Head CAS is only per object: it does not
fence independent producers, provide global isolation, or prevent ABA.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import uuid

try:
    from . import reader_epub_shadow as shadow, reader_epub_immutable as immutable, reader_epub_protection as protection
    from . import search_snapshot
    from .reader_bucket_store import S3BucketStore
    from .reader_page_cas import strong_etag
    from .shared import READER_ASSETS_BUCKET
except ImportError:
    import reader_epub_shadow as shadow
    import reader_epub_immutable as immutable
    import reader_epub_protection as protection
    import search_snapshot
    from reader_bucket_store import S3BucketStore
    from reader_page_cas import strong_etag
    from shared import READER_ASSETS_BUCKET


ROOT = 'reader-index/epub-staging/'
MAX_INTENT = 4 * 1024 * 1024
MAX_RECORD = 16 * 1024
HEX32 = re.compile(r'[0-9a-f]{32}\Z')
HEX64 = re.compile(r'[0-9a-f]{64}\Z')


def _canonical(raw, limit):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= limit:
        raise ValueError('invalid or oversized EPUB staging journal object')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid EPUB staging journal JSON') from error
    if not isinstance(value, dict) or search_snapshot.canonical(value) != raw:
        raise ValueError('EPUB staging journal must be canonical JSON')
    return value


def _sha(value):
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise ValueError('invalid EPUB staging digest')
    return value


def _operation(value):
    if not isinstance(value, str) or not HEX32.fullmatch(value):
        raise ValueError('invalid EPUB staging operation ID')
    return value


def _head(value):
    if (not isinstance(value, dict) or set(value) != {'version','kind','operation','sequence','record'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'reader-epub-staging-head' or type(value['sequence']) is not int
            or not 0 <= value['sequence'] <= 2):
        raise ValueError('invalid EPUB staging head')
    _operation(value['operation']);_sha(value['record'])
    return value


def _record(value):
    keys={'version','kind','operation','sequence','state','prior','intent_sha256','claim_id','receipt_sha256'}
    if (not isinstance(value, dict) or set(value) != keys or type(value['version']) is not int
            or value['version'] != 1 or value['kind'] != 'reader-epub-staging-record'
            or type(value['sequence']) is not int or value['sequence'] not in (0,1,2)):
        raise ValueError('invalid EPUB staging record')
    _operation(value['operation']);_sha(value['intent_sha256'])
    expected={0:'prepared',1:'write-intent',2:'staged'}[value['sequence']]
    if value['state'] != expected:
        raise ValueError('EPUB staging state and sequence differ')
    if value['sequence']==0:
        if value['prior'] is not None or value['claim_id'] is not None or value['receipt_sha256'] is not None:
            raise ValueError('EPUB prepared record has unexpected successor data')
    else:
        _sha(value['prior']);_operation(value['claim_id'])
        if value['sequence']==1 and value['receipt_sha256'] is not None:
            raise ValueError('EPUB receipt precedes staged state')
        if value['sequence']==2:_sha(value['receipt_sha256'])
    return value


def _transition(previous, current, prior_digest):
    if (current['sequence'] != previous['sequence']+1 or current['prior'] != prior_digest
            or current['operation'] != previous['operation'] or current['intent_sha256'] != previous['intent_sha256']):
        raise ValueError('EPUB staging predecessor or immutable identity changed')
    if previous['sequence']==1 and current['claim_id']!=previous['claim_id']:
        raise ValueError('EPUB staging claim identity changed')


def _intent(raw):
    value = _canonical(raw, MAX_INTENT)
    if (set(value) != {'version','kind','operation','created_at','plan','preview'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'reader-epub-staging-intent'):
        raise ValueError('invalid EPUB staging intent')
    op = _operation(value['operation'])
    plan = shadow._verify_plan(value['plan'])
    preview = value['preview']
    if (plan['operation'] != op or not isinstance(preview, dict)
            or preview.get('version') != 1 or preview.get('kind') != 'reader-epub-immutable-staging-preview'
            or preview.get('operation') != op or preview.get('plan_sha256') != plan['plan_sha256']
            or preview.get('search_generation') != plan['search_generation']
            or preview.get('reader_generation') != plan['reader_generation']
            or preview.get('remote_writes') != 0 or preview.get('index_written') is not False
            or preview.get('publication_ready') is not False or preview.get('external_writers_verified') is not False
            or not isinstance(preview.get('actions'), list) or len(preview['actions']) != len(plan['sources'])):
        raise ValueError('EPUB staging preview differs from plan')
    if hashlib.sha256(search_snapshot.canonical({k:v for k,v in preview.items() if k!='staging_sha256'})).hexdigest() != preview.get('staging_sha256'):
        raise ValueError('EPUB staging preview digest differs')
    paths=set()
    for number, (source,action) in enumerate(zip(plan['sources'],preview['actions'])):
        if (not isinstance(action,dict) or set(action)!={'key','source_revision','source_sha256','previous_manifest','root','protection_path','files'}
                or action.get('key') != source['key']
                or action.get('source_revision') != source['source_revision']
                or action.get('previous_manifest') != source['previous_manifest']
                or not isinstance(action.get('source_sha256'),str) or not HEX64.fullmatch(action['source_sha256'])
                or not isinstance(action.get('files'),list) or not action['files']):
            raise ValueError('EPUB staging action differs from source intent')
        root=immutable._root(action.get('root'))
        if root.split('/')[3] != action['source_sha256']:
            raise ValueError('EPUB staging root differs from source digest')
        if source['reason']=='content_changed' and source['proof']=='current-lfs-sha256' and action['source_sha256']!=source['current_source_sha256']:
            raise ValueError('EPUB staged source digest differs from LFS proof')
        path=f'reader-index/processing/epub-exact-{op}-{number:02d}.json'
        if action.get('protection_path') != path:
            raise ValueError('EPUB staging protection path differs')
        protection.make_record(op,plan['plan_sha256'],root,value['created_at'])
        for item in action['files']:
            if not isinstance(item,dict) or set(item)!={'path','bytes','sha256','remote'}:
                raise ValueError('invalid EPUB staging member')
            immutable._expected({'sha256':item['sha256'],'bytes':item['bytes']})
            remote=immutable._key(root,item['path'])
            if item['remote']!=remote or remote in paths:
                raise ValueError('duplicate or foreign EPUB staging member')
            paths.add(remote)
    return value


def _receipt(raw, record, intent):
    value=_canonical(raw,MAX_INTENT)
    keys={'version','kind','operation','plan_sha256','staging_sha256','protected_sources','verified_objects',
          'outcomes','index_written','pointer_written','publication_ready','external_writers_verified'}
    if (set(value)!=keys or type(value['version']) is not int or value['version']!=1
            or value['kind']!='reader-epub-protected-staging' or value['operation']!=record['operation']
            or value['plan_sha256']!=intent['plan']['plan_sha256']
            or value['staging_sha256']!=intent['preview']['staging_sha256']
            or value['index_written'] is not False or value['pointer_written'] is not False
            or value['publication_ready'] is not False or value['external_writers_verified'] is not False):
        raise ValueError('EPUB staging receipt identity differs')
    actions=intent['preview']['actions']; objects={x['remote'] for a in actions for x in a['files']}
    protections={a['protection_path'] for a in actions}
    if (type(value['protected_sources']) is not int or value['protected_sources']!=len(protections)
            or type(value['verified_objects']) is not int or value['verified_objects']!=len(objects)
            or not isinstance(value['outcomes'],list) or len(value['outcomes'])!=len(objects)+len(protections)):
        raise ValueError('EPUB staging receipt coverage differs')
    seen=set()
    for row in value['outcomes']:
        if (not isinstance(row,dict) or set(row)!={'kind','path','status'}
                or row['kind'] not in {'protection','object'} or row['status'] not in {'written','already-present','written-response-unknown'}
                or row['path'] not in (protections if row['kind']=='protection' else objects)
                or (row['kind'],row['path']) in seen):
            raise ValueError('invalid EPUB staging receipt member')
        seen.add((row['kind'],row['path']))
    return value


def key(operation, kind, digest=None):
    _operation(operation)
    if kind=='head':return f'{ROOT}{operation}/head.json'
    if kind not in {'intents','records','receipts'}:raise ValueError('invalid EPUB journal key kind')
    _sha(digest)
    return f'{ROOT}{operation}/{kind}/{digest}.json'


@dataclass(frozen=True)
class Current:
    head: dict
    head_raw: bytes
    etag: str
    record: dict
    record_raw: bytes
    intent: dict


def _read_named(store, operation, kind, digest, limit):
    raw=store.read_immutable(key(operation,kind,digest))
    if raw is None or hashlib.sha256(raw).hexdigest()!=digest:
        raise ValueError('EPUB staging journal immutable object missing or changed')
    _canonical(raw,limit)
    return raw


def load(store, operation):
    _operation(operation)
    raw,etag=store.read_head(key(operation,'head'))
    if raw is None:
        if etag is not None:raise ValueError('missing EPUB head has an ETag')
        return None
    strong_etag(etag);head=_head(_canonical(raw,MAX_RECORD))
    if head['operation']!=operation:raise ValueError('EPUB head operation differs')
    record_raw=_read_named(store,operation,'records',head['record'],MAX_RECORD)
    record=_record(_canonical(record_raw,MAX_RECORD))
    if record['operation']!=operation or record['sequence']!=head['sequence']:
        raise ValueError('EPUB head record differs')
    intent=_intent(_read_named(store,operation,'intents',record['intent_sha256'],MAX_INTENT))
    if intent['operation']!=operation:raise ValueError('EPUB intent operation differs')
    if record['receipt_sha256'] is not None:
        _receipt(_read_named(store,operation,'receipts',record['receipt_sha256'],MAX_INTENT),record,intent)
    prior=record
    while prior['prior'] is not None:
        raw_prior=_read_named(store,operation,'records',prior['prior'],MAX_RECORD)
        previous=_record(_canonical(raw_prior,MAX_RECORD))
        _transition(previous,prior,prior['prior']);prior=previous
    if prior['sequence']!=0:raise ValueError('EPUB journal history is incomplete')
    return Current(head,raw,etag,record,record_raw,intent)


def _create(store, path, raw):
    old=store.read_immutable(path)
    if old is None:
        try:store.create_immutable(path,raw)
        except Exception:
            # The conditional create may have succeeded. One exact read is
            # enough to reconcile; never retry the mutation.
            if store.read_immutable(path)!=raw:raise
            return
        old=store.read_immutable(path)
    if old!=raw:raise ValueError('immutable EPUB journal object conflicts')


def _commit(store,operation,record,current):
    _record(record)
    if current is not None:_transition(current.record,record,current.head['record'])
    raw=search_snapshot.canonical(record);digest=hashlib.sha256(raw).hexdigest()
    _create(store,key(operation,'records',digest),raw)
    head={'version':1,'kind':'reader-epub-staging-head','operation':operation,
          'sequence':record['sequence'],'record':digest}
    head_raw=search_snapshot.canonical(head)
    try:store.write_head(key(operation,'head'),head_raw,current.etag if current else None)
    except Exception:
        after=load(store,operation)
        if after is not None and after.head_raw==head_raw:return after
        raise
    after=load(store,operation)
    if after is None or after.head_raw!=head_raw:raise RuntimeError('EPUB staging head readback differs')
    return after


def begin(store,plan,preview,created_at):
    op=_operation(plan.get('operation'))
    if load(store,op) is not None:raise ValueError('EPUB staging operation already exists')
    raw=search_snapshot.canonical({'version':1,'kind':'reader-epub-staging-intent','operation':op,
                                   'created_at':created_at,'plan':plan,'preview':preview})
    _intent(raw);digest=hashlib.sha256(raw).hexdigest()
    _create(store,key(op,'intents',digest),raw)
    record={'version':1,'kind':'reader-epub-staging-record','operation':op,'sequence':0,'state':'prepared',
            'prior':None,'intent_sha256':digest,'claim_id':None,'receipt_sha256':None}
    return _commit(store,op,record,None)


def claim(store,current):
    if current.record['state']!='prepared':raise ValueError('EPUB stage write is already claimed; reconcile instead')
    active=load(store,current.record['operation'])
    if active is None or active.head_raw!=current.head_raw or active.etag!=current.etag:
        raise ValueError('EPUB journal head changed before claim')
    value={**current.record,'sequence':1,'state':'write-intent','prior':current.head['record'],
           'claim_id':uuid.uuid4().hex}
    return _commit(store,current.record['operation'],value,current)


def record_staged(store,current,receipt):
    """Record caller-observed exact staged receipt; this function performs no content write."""
    if current.record['state']!='write-intent':raise ValueError('EPUB stage is not awaiting observation')
    active=load(store,current.record['operation'])
    if active is None or active.head_raw!=current.head_raw or active.etag!=current.etag:
        raise ValueError('EPUB journal changed before staged observation')
    raw=search_snapshot.canonical(receipt);_receipt(raw,current.record,current.intent)
    digest=hashlib.sha256(raw).hexdigest();_create(store,key(current.record['operation'],'receipts',digest),raw)
    value={**current.record,'sequence':2,'state':'staged','prior':current.head['record'],'receipt_sha256':digest}
    return _commit(store,current.record['operation'],value,current)


class S3Journal:
    def __init__(self,store=None):
        if store is None:
            from botocore.config import Config
            store=S3BucketStore()
            if store._namespace!='vomebook':raise ValueError('EPUB journal requires vomebook namespace')
            store._config=store._config.merge(Config(retries={'total_max_attempts':1},
                request_checksum_calculation='when_required',response_checksum_validation='when_required'))
        self.store=store
    def _check(self,path,head):
        pattern=(r'reader-index/epub-staging/[0-9a-f]{32}/head\.json' if head else
                 r'reader-index/epub-staging/[0-9a-f]{32}/(?:intents|records|receipts)/[0-9a-f]{64}\.json')
        if not isinstance(path,str) or not re.fullmatch(pattern,path):raise ValueError('EPUB journal path outside allowlist')
        ns,bucket=self.store._location(READER_ASSETS_BUCKET)
        if (ns,bucket)!=('vomebook','reader-assets-v2'):raise ValueError('unexpected EPUB journal bucket')
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
        if body is None:raise ValueError('EPUB journal object lacks body')
        limit=MAX_RECORD if head or '/records/' in path else MAX_INTENT
        try:
            if type(length) is not int or not 0<length<=limit:raise ValueError('EPUB journal object size invalid')
            raw=body.read(length+1)
        finally:body.close()
        if not isinstance(raw,bytes) or len(raw)!=length:raise ValueError('EPUB journal object truncated')
        return raw,response.get('ETag')
    def read_head(self,path):return self._read(path,True)
    def read_immutable(self,path):return self._read(path,False)[0]
    def create_immutable(self,path,raw):
        client,bucket=self._check(path,False)
        if not isinstance(raw,bytes) or hashlib.sha256(raw).hexdigest()!=path.rsplit('/',1)[1][:-5]:
            raise ValueError('EPUB journal object is not content addressed')
        _canonical(raw,MAX_RECORD if '/records/' in path else MAX_INTENT)
        client.put_object(Bucket=bucket,Key=path,Body=raw,ContentType='application/json',IfNoneMatch='*')
    def write_head(self,path,raw,etag):
        client,bucket=self._check(path,True);_head(_canonical(raw,MAX_RECORD))
        condition={'IfNoneMatch':'*'} if etag is None else {'IfMatch':strong_etag(etag)}
        client.put_object(Bucket=bucket,Key=path,Body=raw,ContentType='application/json',**condition)
