"""One historical image: physical owning shard CAS, then canonical CAS.

There is no dispatcher or publication. 'applied' proves observation of the two
index targets, not sidecar, Reader/Search, rollback, or GC authorization.

The caller must hold an external fence that excludes independent writers and
competing transitions (including other keys), and protects against ABA, across
all phases and recovery. The callback checks that fence; it does not acquire
one. Use fresh checkpoint reads and a single-attempt checkpoint PUT transport
(including when injecting a store into ConditionalCheckpointStore). Exact
strong parent ETags are sealed; opaque target ETags are learned from exact byte
readbacks and then pinned in the journal.
"""
from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass
from typing import Callable, Literal, Protocol

from . import reader_image_history as history, reader_image_history_shard as physical
from . import reader_image_history_store as wire
from .reader_stream_checkpoint_cas import ConditionalCheckpointStore, Snapshot, strong_etag


@dataclass(frozen=True)
class Transition:
    canonical: history.Intent
    shard_pin: physical.ShardPin
    shard_parent: bytes
    shard_target: bytes

    @property
    def claim_key(self) -> str:
        # Same object as the canonical-only claim, not a key derived from the
        # shard, operation ID, or wire version.
        return self.canonical.claim_key

    @property
    def sha256(self) -> str:
        return history._sha(history._encode(['reader-image-history-transition-v3', self.canonical.sha256,
                                            asdict(self.shard_pin), history._sha(self.shard_parent),
                                            history._sha(self.shard_target)]))


@dataclass(frozen=True)
class Record:
    intent_sha256: str
    state: Literal['shard-permitted', 'canonical-permitted', 'applied']
    shard_etag: str | None = None
    canonical_etag: str | None = None


class Journal(Protocol):
    """Linearizable durable journal at the legacy claim key; no reset or delete.

    Missing means absent, never unsupported or corrupt. Claim stores the entire
    seal atomically. Advance preserves it and CASes the exact previous record.
    True means a confirmed win by THIS caller, never an identical readback
    after an ambiguous write. Ambiguity raises; False grants no permission.
    """

    def load(self, key: str) -> tuple[Transition, Record] | None: ...
    def claim(self, key: str, intent: Transition, record: Record) -> bool: ...
    def advance(self, key: str, expected: Record, record: Record) -> bool: ...


Phase = Literal['parents', 'shard-target', 'both-targets']
# Every call must independently observe the historical/current immutable source
# and Search pins, retained/protected WebP and immutable/protected AVIF. Bind
# the reviewed protection evidence in rollback_sha256 and pins_sha256. Return
# both whole shadow projections with fresh pins; not copies of sealed values.
Revalidate = Callable[[Transition, Phase], history.Checks]
RequireFence = Callable[[Transition, Phase], bool]


def _splice(canonical, shard, pin):
    index = history._parent_index(shard.raw)
    _, old = history._selected(index, canonical.key)
    # The physical row need not have identical checkpoint metadata, but must
    # refer to the SAME protected historical source and WebP.
    history._static(old, canonical.key, canonical.preflight.source, canonical.preflight.old, 'webp')
    return physical.splice(key=canonical.key, old_row_sha256=history.row_digest(old),
                           canonical_target=canonical.candidate, canonical_sha256=history._sha(canonical.candidate),
                           shard=shard, pin=pin).target


def seal(canonical: history.Intent, *, shard: Snapshot, pin: physical.ShardPin) -> Transition:
    """Seal from a reviewed physical snapshot, never a merged shard baseline."""
    if type(canonical) is not history.Intent or type(pin) is not physical.ShardPin:
        raise ValueError('expected a sealed canonical intent and an exact owning shard pin')
    history._validate(canonical)
    return Transition(canonical, pin, shard.raw, _splice(canonical, shard, pin))


def _validate(intent):
    if type(intent) is not Transition or type(intent.canonical) is not history.Intent:
        raise ValueError('expected a sealed two-index image history intent')
    canonical = history._validate(intent.canonical)
    pin = intent.shard_pin
    if type(pin) is not physical.ShardPin:
        raise ValueError('expected exact owning shard pin')
    shard = Snapshot(history._parent_index(intent.shard_parent), pin.etag, pin.sha256, intent.shard_parent)
    if intent.shard_target != _splice(intent.canonical, shard, pin):
        raise ValueError('sealed physical shard delta or target bytes differ')
    return history._index(intent.shard_target), canonical


def _guard(intent, phase, checkpoints, revalidate, require_fence, *, shard_etag=None, canonical_etag=None):
    def fence():
        if require_fence(intent, phase) is not True:
            raise ValueError('image history writer fence not confirmed')

    def observe(path, raw, etag, target, parent_etag):
        snapshot = checkpoints.read(history.BUCKET, path)
        if (snapshot.raw != raw or (etag is not None and snapshot.etag != etag)
                or (target and snapshot.etag == parent_etag)):
            raise ValueError(f'exact image history {"target" if target else "parent"} differs: {path}; do not replay')
        return strong_etag(snapshot.etag)

    canonical, pin = intent.canonical, intent.shard_pin
    shard_target, canonical_target = phase != 'parents', phase == 'both-targets'
    sraw = intent.shard_target if shard_target else intent.shard_parent
    craw = canonical.candidate if canonical_target else canonical.parent
    fence()
    s = observe(pin.path, sraw, shard_etag if shard_target else pin.etag, shard_target, pin.etag)
    c = observe(history.CANONICAL, craw, canonical_etag if canonical_target else canonical.parent_etag,
                canonical_target, canonical.parent_etag)
    history._fresh(canonical, lambda *_: revalidate(intent, phase), 'after' if canonical_target else 'before')
    observe(pin.path, sraw, s, shard_target, pin.etag)
    observe(history.CANONICAL, craw, c, canonical_target, canonical.parent_etag)
    fence()
    return s, c


def _readback(journal, intent, record):
    if journal.load(intent.claim_key) != (intent, record):
        raise RuntimeError('image history journal readback differs; inspect before recovery')
    return record


def _advance(journal, intent, expected, record):
    if journal.advance(intent.claim_key, expected, record) is not True:
        raise RuntimeError('image history journal advance lost; no write permit')
    return _readback(journal, intent, record)


def start(journal: Journal, intent: Transition, *, checkpoints: ConditionalCheckpointStore,
          revalidate: Revalidate, require_fence: RequireFence) -> None:
    """New durable claim grants only its creator one shard write attempt."""
    shard, _ = _validate(intent)
    key = intent.claim_key
    if journal.load(key) is not None:
        raise ValueError('image history write already claimed; reconcile instead')
    _guard(intent, 'parents', checkpoints, revalidate, require_fence)
    record = Record(intent.sha256, 'shard-permitted')
    if journal.claim(key, intent, record) is not True:
        raise RuntimeError('image history journal claim lost; no write permit')
    _readback(journal, intent, record)
    _guard(intent, 'parents', checkpoints, revalidate, require_fence)
    checkpoints.write_once(history.BUCKET, intent.shard_pin.path, shard, intent.shard_pin.etag,
                           operation_id=intent.canonical.operation)


def reconcile(journal: Journal, intent: Transition, *, checkpoints: ConditionalCheckpointStore,
              revalidate: Revalidate, require_fence: RequireFence) -> Record:
    """Observe spent permits, never replay them. May win a NEW canonical permit.

    A permit with parent or foreign bytes requires operator intervention. A
    lost *journal* advance before commit leaves the previous stage intact; if
    committed, nobody may use that permit by merely loading it. A lost index
    response is recoverable only by exact target observation under the fence.
    """
    _, canonical = _validate(intent)
    loaded = journal.load(intent.claim_key)
    if loaded is None or loaded[0] != intent:
        raise ValueError('no matching two-index image history claim')
    current = loaded[1]
    _bind_record(intent, current)
    if current.state == 'shard-permitted':
        s, _ = _guard(intent, 'shard-target', checkpoints, revalidate, require_fence)
        current = _advance(journal, intent, current, Record(current.intent_sha256, 'canonical-permitted', s))
        _guard(intent, 'shard-target', checkpoints, revalidate, require_fence, shard_etag=s)
        checkpoints.write_once(history.BUCKET, history.CANONICAL, canonical, intent.canonical.parent_etag,
                               operation_id=intent.canonical.operation)
    s, c = _guard(intent, 'both-targets', checkpoints, revalidate, require_fence,
                  shard_etag=current.shard_etag, canonical_etag=current.canonical_etag)
    if current.state == 'applied':
        return _readback(journal, intent, current)
    return _advance(journal, intent, current, Record(current.intent_sha256, 'applied', s, c))


def _bind_record(intent, record):
    if type(record) is not Record or record.intent_sha256 != intent.sha256:
        raise ValueError('image history transition record digest differs')
    if record.state == 'shard-permitted' and record.shard_etag is None and record.canonical_etag is None:
        return
    if record.state in ('canonical-permitted', 'applied'):
        if strong_etag(record.shard_etag) != intent.shard_pin.etag:
            if record.state == 'canonical-permitted' and record.canonical_etag is None:
                return
            if record.state == 'applied' and strong_etag(record.canonical_etag) != intent.canonical.parent_etag:
                return
    raise ValueError('invalid image history transition state or target ETags')


def _encode(intent, record):
    fields = asdict(intent.canonical)
    for name in ('parent', 'candidate'):
        fields[name] = base64.b64encode(fields[name]).decode('ascii')
    return history._encode({'version': 3, 'kind': wire.KIND, 'intent': fields, 'record': asdict(record),
                            'shard_pin': asdict(intent.shard_pin),
                            'shard_parent': base64.b64encode(intent.shard_parent).decode('ascii'),
                            'shard_target': base64.b64encode(intent.shard_target).decode('ascii')})


def _decode(key, raw):
    try:
        value = json.loads(raw)
        if type(value['version']) is not int or value['version'] != 3 or value['kind'] != wire.KIND:
            raise ValueError('unsupported image history claim wire version or kind')
        fields = value['intent']
        pf = fields['preflight']
        canonical = history.Intent(**{**fields, 'preflight': history.Preflight(**{
            **pf, 'source': history.Source(**pf['source']), 'current_source': history.Source(**pf['current_source']),
            'old': history.Asset(**pf['old']), 'new': history.Asset(**pf['new'])}),
            **{name: base64.b64decode(fields[name], validate=True) for name in ('parent', 'candidate')}})
        intent = Transition(canonical, physical.ShardPin(**value['shard_pin']),
                            base64.b64decode(value['shard_parent'], validate=True),
                            base64.b64decode(value['shard_target'], validate=True))
        record = Record(**value['record'])
        if intent.claim_key != key or _encode(intent, record) != raw:
            raise ValueError('noncanonical image history transition envelope or wrong key')
        _validate(intent)
        _bind_record(intent, record)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError('invalid or unsupported image history transition envelope') from error
    return intent, record


class S3TransitionJournal:
    """V3 codec over the existing single-attempt S3 journal transport.

    Requires an explicit client. Retains the old envelope size limit: plans
    exceeding it fail before any index CAS. This namespace isn't GC protection.
    """

    def __init__(self, client):
        if client is None:
            raise ValueError('explicit image history journal client required')
        self._transport = wire.S3ClaimStore(client)

    def load(self, key):
        raw, _ = self._transport._get(wire._path(key))
        return None if raw is None else _decode(key, raw)

    def claim(self, key, intent, record):
        _validate(intent)
        _bind_record(intent, record)
        if key != intent.claim_key or record.state != 'shard-permitted':
            raise ValueError('invalid initial image history transition claim')
        return self._transport._put(wire._path(key), _encode(intent, record), IfNoneMatch='*')

    def advance(self, key, expected, record):
        path = wire._path(key)
        raw, etag = self._transport._get(path)
        if raw is None:
            return False
        intent, current = _decode(key, raw)
        if current != expected:
            return False
        _bind_record(intent, record)
        if ((current.state, record.state) not in {('shard-permitted', 'canonical-permitted'),
                                                 ('canonical-permitted', 'applied')}
                or (current.shard_etag is not None and current.shard_etag != record.shard_etag)):
            raise ValueError('invalid image history transition journal advance')
        return self._transport._put(path, _encode(intent, record), IfMatch=etag)
