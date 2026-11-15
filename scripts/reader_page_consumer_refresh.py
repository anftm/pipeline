"""Refresh only the Reader-owned portion of the existing consumer root.

The caller must hold the central publication serialized writer window across
apply, including independent writers. A single-root CAS cannot lock projection
sources. prepare is read-only; neither phase publishes ACKs or grants GC rights.

No production adapter or CLI is installed here. The future narrow adapter must
allow read_for_update/put_if_match only for (ASSETS, CONSUMERS), require an
existing root and an opaque conditional token, and disable transport retries.
immutable_put is restricted to content-addressed JSON under INTENTS, with atomic
create-if-absent, identical-existing acceptance and conflict rejection. Its
transport must also disable retries. Unknown outcomes require inspection.
"""
import base64
import copy
import hashlib
import json
import re

try:
    from . import reader_gc_collect as collector, publish_reader_v3 as publication
except ImportError:
    import reader_gc_collect as collector
    import publish_reader_v3 as publication

ASSETS = collector.ASSETS
CONSUMERS = collector.CONSUMERS
INTENTS = 'reader-index/gc/consumer-refresh/'
READER_BUCKETS = collector.graph.READER_BUCKETS
SHA = re.compile(r'[0-9a-f]{64}')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _document(raw):
    value = json.loads(raw)
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1 or value.get('kind') != 'reader-gc-consumers'
            or not isinstance(value.get('resources'), list)
            or not isinstance(value.get('receipts'), dict)):
        raise ValueError('unsupported consumer root schema')
    for ref in value['resources']:
        if (not isinstance(ref, dict) or not isinstance(ref.get('bucket'), str)
                or not ref['bucket'] or not any(isinstance(ref.get(key), str)
                    and ref[key] for key in ('path', 'root'))):
            raise ValueError('invalid consumer resource identity')
    for surface in ('hf', 'pages'):
        if surface in value['receipts'] and not isinstance(value['receipts'][surface], dict):
            raise ValueError('invalid Reader consumer receipt')
    return value


def _optional_hash(store, bucket, path):
    try:
        return _sha(store.read_bytes(bucket, path))
    except FileNotFoundError:
        return None


def _sources(store, generation):
    pointer_raw = store.read_bytes(ASSETS, publication.POINTER)
    pointer, _ = publication.current(store)
    if not pointer or pointer['generation'] != generation or json.loads(pointer_raw) != pointer:
        raise ValueError('current pointer differs from requested generation')
    ccrd = collector.graph.ccrd_lifecycle.BUCKET
    return {
        'pointer_raw': pointer_raw,
        'canonical_sidecar_sha256': collector.snapshot_digest(store, ASSETS, collector.SIDECAR),
        'canonical_catalog_sha256': collector.snapshot_digest(store, ASSETS, collector.CATALOG),
        'ccrd_pointer_sha256': _optional_hash(store, ccrd, 'control/ocr-current.json'),
        'ccrd_consumer_sha256': _optional_hash(store, ccrd, 'control/ocr-consumer.json'),
    }


def _verify(store, plan):
    if _sources(store, plan['current_generation']) != plan['sources']:
        raise ValueError('consumer refresh source drift')
    if store.read_bytes(ASSETS, CONSUMERS) != plan['before']:
        raise ValueError('consumer root changed from reviewed bytes')


def prepare(store, httpclient, *, current_generation, old_root_sha256):
    """Read-only plan bound to explicit current generation and old raw root SHA.

    Observations always come from the collector's complete deployed projections,
    including its independent current CCRD acceptance. CCRD evidence is recorded
    in the plan, but its old root references and receipt remain untouched.
    """
    for value in (current_generation, old_root_sha256):
        if not isinstance(value, str) or not SHA.fullmatch(value):
            raise ValueError('exact current generation and old root SHA256 are required')
    before = store.read_bytes(ASSETS, CONSUMERS)
    if _sha(before) != old_root_sha256:
        raise ValueError('consumer root SHA256 differs from requested old root')
    old = _document(before)
    sources = _sources(store, current_generation)
    identity, observed = collector.observe_projections(store, httpclient)
    _document(collector.encode(observed))
    if identity['digest'] != sources['canonical_catalog_sha256']:
        raise ValueError('canonical catalog changed during consumer refresh')
    for surface in ('hf', 'pages'):
        receipt = observed['receipts'].get(surface)
        if (not isinstance(receipt, dict) or receipt.get('projection_verified') is not True
                or not receipt.get('checked_at') or not receipt.get('checked_url')
                or receipt.get('generation') != current_generation):
            raise ValueError('fresh current Reader projection evidence required')
        publication.acknowledge(store, surface, receipt, apply=False)
    after = copy.deepcopy(old)
    after['resources'] = [copy.deepcopy(ref) for ref in old['resources']
                          if ref['bucket'] not in READER_BUCKETS]
    after['resources'].extend(copy.deepcopy(ref) for ref in observed['resources']
                              if ref['bucket'] in READER_BUCKETS)
    for surface in ('hf', 'pages'):
        # Retain extensions to the known receipt schema; fresh proof replaces
        # the fields it actually supplies. Other domains are never rewritten.
        after['receipts'][surface] = {**after['receipts'].get(surface, {}),
                                      **copy.deepcopy(observed['receipts'][surface])}
    plan = {'version': 1, 'kind': 'reader-page-consumer-refresh-plan',
            'current_generation': current_generation, 'old_root_sha256': old_root_sha256,
            'before': before, 'after': collector.encode(after), 'sources': sources,
            'observations': copy.deepcopy(observed['receipts']),
            'deletion_authorized': False}
    _verify(store, plan)
    return plan


def _comparison(plan):
    """Only observation times may advance between review and apply."""
    value = copy.deepcopy(plan)
    after = _document(value['after'])
    for surface in ('hf', 'pages'):
        after['receipts'][surface].pop('checked_at', None)
    value['after'] = collector.encode(after)
    for surface in ('hf', 'pages', 'ccrd'):
        if surface in value['observations']:
            value['observations'][surface].pop('checked_at', None)
    return value


def apply(store, plan, httpclient, *, current_generation, old_root_sha256):
    """Re-derive the reviewed plan, attempt one CAS and report exact readback.

    Lost response + exact after bytes is applied. Before bytes or failed readback
    is unknown, even when PutObject returned normally; foreign bytes is conflict.
    The immutable audit intent precedes the sole mutable consumer-root write.
    Neither write is retried and no rollback is performed.
    """
    reviewed = copy.deepcopy(plan)
    publication.assert_writer(store)
    fresh = prepare(store, httpclient, current_generation=current_generation,
                    old_root_sha256=old_root_sha256)
    if _comparison(reviewed) != _comparison(fresh):
        raise ValueError('consumer refresh plan differs from fresh evidence')
    before, token = store.read_for_update(ASSETS, CONSUMERS)
    if before != fresh['before'] or token is None:
        raise ValueError('consumer conditional read differs from reviewed root')
    intent = {'version': 1, 'kind': 'reader-page-consumer-refresh-intent',
              'current_generation': current_generation,
              'before_base64': base64.b64encode(before).decode('ascii'),
              'after_base64': base64.b64encode(fresh['after']).decode('ascii'),
              'before_sha256': old_root_sha256, 'after_sha256': _sha(fresh['after']),
              'sources': {**fresh['sources'], 'pointer_raw':
                  base64.b64encode(fresh['sources']['pointer_raw']).decode('ascii')},
              'fresh_consumer_observations': fresh['observations'],
              'deletion_authorized': False}
    intent_raw = publication.encode(intent)
    intent_ref = publication.metadata(ASSETS, INTENTS + _sha(intent_raw) + '.json', intent_raw)
    store.immutable_put(ASSETS, intent_ref['path'], intent_raw)
    if store.read_bytes(ASSETS, intent_ref['path']) != intent_raw:
        raise ValueError('consumer refresh intent readback mismatch')
    observed, latest_token = store.read_for_update(ASSETS, CONSUMERS)
    if observed != before or latest_token != token:
        raise ValueError('consumer root changed before CAS')
    _verify(store, fresh)
    # Serialization covers the interval from these independent source reads to
    # the conditional write. The root token alone is not a multi-object lock.
    publication.assert_writer(store)
    cas_error = None
    try:
        store.put_if_match(ASSETS, CONSUMERS, fresh['after'], token)
    except Exception as error:
        cas_error = type(error).__name__
    try:
        actual, _ = store.read_for_update(ASSETS, CONSUMERS)
        outcome = ('applied' if actual == fresh['after'] else
                   'unknown' if actual == before else 'conflict')
    except Exception:
        outcome = 'unknown'
    return {'version': 1, 'kind': 'reader-page-consumer-refresh-result',
            'outcome': outcome, 'applied': outcome == 'applied', 'cas_attempts': 1,
            'cas_error': cas_error, 'intent_ref': intent_ref,
            'current_generation': current_generation,
            'old_root_sha256': old_root_sha256, 'new_root_sha256': _sha(fresh['after']),
            'deletion_authorized': False}
