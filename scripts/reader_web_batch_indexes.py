"""Commit three sealed web indexes under one writer window; never replay a spent index permit.

The indexes are not a transaction on the object store. The caller holds a
verified writer window until the legacy catalog/v3 bundle advances or a safe
state is established. The shared route roots are not changed by this module.
"""
from . import reader_web_batch_plan as plan, reader_web_exact_plan as web, reader_web_index_guard as index
from .reader_stream_checkpoint_cas import strong_etag


def canonical_set(*, checkpoints, journal, hub, parents, pairs, operation, etags,
                  prerequisites, guard, recover=False, managed=False):
    """A fresh batch or explicit continuation of an observed exact prefix.

    Recover never writes a root if that root has a spent permit and old bytes.
    All pending roots are preflighted before any per-index permit is spent.
    """
    targets=plan.canonical(parents,pairs,operation,managed=managed)
    order=tuple(sorted(targets))
    if set(etags)!=set(order) or type(recover) is not bool:
        raise ValueError('an exact ETag for each web index is required')
    for value in etags.values():strong_etag(value)
    paths={ext:f'documents/web/{ext}/index.json' for ext in order}
    keys={ext:frozenset(old['key'] for old,_ in pairs if old['extension']==ext) for ext in order}
    seals={ext:index._permit(paths[ext],plan.digest(parents[ext]),plan.digest(targets[ext]),prerequisites) for ext in order}

    def read(ext):
        current=checkpoints.read(web.BUCKET,paths[ext]); resolved=hub.read_bytes(web.BUCKET,paths[ext])
        if current.raw!=resolved:
            raise RuntimeError('canonical web S3/resolver readback mismatch: '+ext)
        return current.raw,current.etag

    def call_guard(_):
        if guard() is not True:raise RuntimeError('exact web batch writer window not verified')
        return True

    def preflight():
        state={}; seen_parent=False
        for ext in order:
            call_guard('classify')
            raw,etag=read(ext); permitted=journal.read(plan.digest(parents[ext]),guard=call_guard)
            if (not seen_parent and recover and raw==targets[ext] and permitted==seals[ext]):
                state[ext]='observed'
            elif raw==parents[ext] and etag==etags[ext] and permitted is None:
                state[ext]='pending';seen_parent=True
            else:
                raise RuntimeError('mixed, changed or spent-but-unconfirmed canonical index: '+ext)
        if recover and ('observed' not in state.values()):
            raise RuntimeError('no exact canonical progress observed; do not replay the batch')
        return state

    state=preflight(); expected={e:targets[e] if state[e]=='observed' else parents[e] for e in order}
    out={e:'observed' for e in order if state[e]=='observed'}
    for ext in order:
        if state[ext]=='observed':continue
        def check(phase):
            call_guard(phase)
            for other in order:
                # Internal target readback may be either parent or target until
                # CAS outcome is known. No other root may drift while this PUT runs.
                if other==ext:
                    continue
                raw,etag=read(other)
                if raw!=expected[other] or (raw==parents[other] and etag!=etags[other]):
                    raise RuntimeError('noncurrent web index drifted during batch CAS: '+other)
            return True
        out[ext]=index.apply(checkpoints,journal,path=paths[ext],parent_sha256=plan.digest(parents[ext]),
                           target_raw=targets[ext],prerequisites=prerequisites,guard=check,
                           expected_keys=keys[ext],parent_etag=etags[ext],managed=managed)
        if read(ext)[0]!=targets[ext]:raise RuntimeError('exact index target not observable: '+ext)
        expected[ext]=targets[ext]
    call_guard('final-index-set')
    for ext in order:
        if read(ext)[0]!=targets[ext] or journal.read(plan.digest(parents[ext]),guard=call_guard)!=seals[ext]:
            raise RuntimeError('exact three-index successor not observable: '+ext)
    return out
