"""Rehearse the single-TXT chain from one caller-supplied, immutable snapshot.

This is a synthetic/offline model, not a writer, workflow, durable journal or
publication protocol. It neither acquires inputs nor stores checkpoints. Each
step reconstructs prior evidence from the same supplied bytes; inconsistent
evidence fails closed. Checkpoints and their stages are forgeable and prove
no history. This intentionally expensive replay is only suitable for small
fixtures; real CW-scale resource use is untested.

The checkpoint is an observation, not an authorization or proof of durable
remote effects. Cache inventory completeness, other index contents, writer
isolation and multi-system recovery are not established by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

try:
    from . import ccrd_single_source_cw_candidate as index
    from . import ccrd_single_source_plan as planner
    from . import ccrd_single_source_surfaces as surfaces
    from .ccrd_strict_json import loads as strict_json_loads
except ImportError:
    import ccrd_single_source_cw_candidate as index
    import ccrd_single_source_plan as planner
    import ccrd_single_source_surfaces as surfaces
    from ccrd_strict_json import loads as strict_json_loads


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


@dataclass(frozen=True)
class Snapshot:
    space_revision: str
    metadata_gz: bytes
    pointer_bytes: bytes
    activation_bytes: bytes
    current_txt: bytes
    next_txt: bytes
    database_bytes: bytes
    inventory_json: bytes
    cache_payloads: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class Checkpoint:
    # Stored externally only by a caller, if at all. Forgeable, not a receipt.
    snapshot_sha256: str
    stage: str
    plan_sha256: str
    cw_sha256: str | None = None
    cache_sha256: tuple[tuple[str, str], ...] = ()
    changed_cache_paths: tuple[str, ...] = ()
    publication_ready: bool = False


def capture(*, space_revision, metadata_gz, pointer_bytes, activation_bytes,
            current_txt, next_txt, database_bytes, specifications, payloads) -> Snapshot:
    """Freeze already supplied inputs; never fetch or stage a production file."""
    if (not isinstance(space_revision, str)
            or any(type(value) is not bytes for value in
                   (metadata_gz, pointer_bytes, activation_bytes, current_txt, next_txt, database_bytes))):
        raise ValueError('snapshot requires a revision and complete immutable bytes')
    if (not isinstance(specifications, dict) or not isinstance(payloads, dict)
            or not specifications or set(specifications) != set(payloads)
            or any(not isinstance(name, str) for name in specifications)
            or any(not isinstance(name, str) or type(raw) is not bytes for name, raw in payloads.items())):
        raise ValueError('snapshot requires an explicit matching cache inventory and bytes')
    # Freeze the caller's nested lists/dicts too. Reject coercion of non-string
    # keys, so JSON encoding cannot silently change or merge inventory fields.
    def validate_json(value):
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError('inventory object keys must be strings')
            for item in value.values():
                validate_json(item)
        elif isinstance(value, list):
            for item in value:
                validate_json(item)
        elif type(value) not in (str, int, float, bool, type(None)):
            raise ValueError('inventory must contain only JSON values')

    validate_json(specifications)
    try:
        inventory_json = _json_bytes(specifications)
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError('invalid cache inventory JSON') from error
    return Snapshot(space_revision, metadata_gz, pointer_bytes, activation_bytes, current_txt, next_txt,
                    database_bytes, inventory_json, tuple(sorted(payloads.items())))


def _identity(snapshot: Snapshot) -> str:
    if not isinstance(snapshot, Snapshot):
        raise ValueError('an immutable captured snapshot is required')
    # Revalidate even directly constructed/replaced dataclasses; do not trust a
    # previously supplied snapshot ID, cache path list, or mutable container.
    if (not isinstance(snapshot.space_revision, str)
            or any(type(value) is not bytes for value in
                   (snapshot.metadata_gz, snapshot.pointer_bytes, snapshot.activation_bytes,
                    snapshot.current_txt, snapshot.next_txt, snapshot.database_bytes, snapshot.inventory_json))
            or type(snapshot.cache_payloads) is not tuple
            or any(type(item) is not tuple or len(item) != 2 or not isinstance(item[0], str)
                   or type(item[1]) is not bytes for item in snapshot.cache_payloads)):
        raise ValueError('invalid immutable snapshot')
    names = [name for name, _raw in snapshot.cache_payloads]
    if names != sorted(set(names)):
        raise ValueError('cache snapshot paths must be unique and ordered')
    try:
        inventory = strict_json_loads(snapshot.inventory_json)
        if (not isinstance(inventory, dict) or set(inventory) != set(names)
                or _json_bytes(inventory) != snapshot.inventory_json):
            raise ValueError('inventory is not canonical or does not match cache paths')
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError('invalid snapshot inventory') from error
    inputs = {name: _sha(getattr(snapshot, name)) for name in
              ('metadata_gz', 'pointer_bytes', 'activation_bytes', 'current_txt', 'next_txt',
               'database_bytes', 'inventory_json')}
    return _sha(_json_bytes({'kind': 'ccrd-single-txt-offline-snapshot-v1',
                             'space_revision': snapshot.space_revision, 'inputs': inputs,
                             'caches': [(name, _sha(raw)) for name, raw in snapshot.cache_payloads]}))


def _replay(snapshot: Snapshot, stage: str):
    """Recompute evidence and return (checkpoint, artifacts); no persistent IO."""
    if stage not in {'planned', 'indexed', 'cached', 'prepared', 'unchanged'}:
        raise ValueError('unsupported offline stage')
    identity = _identity(snapshot)
    args = {name: getattr(snapshot, name) for name in
            ('space_revision', 'metadata_gz', 'pointer_bytes', 'activation_bytes', 'current_txt', 'next_txt')}
    plan = planner.plan_update(**args)
    if (plan['content_changed'] is not (stage != 'unchanged')):
        raise ValueError('checkpoint stage conflicts with TXT change detection')
    # The binary candidate is bound by its digest in the canonical evidence.
    plan_evidence = {key: value for key, value in plan.items() if key != 'candidate_metadata_gz'}
    if (plan['candidate_metadata_sha256'] != _sha(plan['candidate_metadata_gz'])
            or plan['next_txt_sha256'] != _sha(snapshot.next_txt)
            or plan['publication_ready'] is not False or plan['remote_writes'] != 0):
        raise ValueError('offline plan did not bind the snapshot')
    plan_sha = _sha(_json_bytes(plan_evidence))
    cw, caches = None, None
    if stage in {'indexed', 'cached', 'prepared'}:
        cw = index.build_candidate(**args, database_bytes=snapshot.database_bytes)
        if (cw['plan'] != plan or cw['changed'] is not True
                or cw['candidate_sha256'] != _sha(cw['candidate_bytes'])
                or cw['publication_ready'] is not False or cw['remote_writes'] != 0):
            raise ValueError('CW candidate did not bind the same plan')
    if stage in {'cached', 'prepared'}:
        inventory = strict_json_loads(snapshot.inventory_json)
        caches = surfaces.update_payloads(inventory, dict(snapshot.cache_payloads), snapshot.current_txt,
                                          snapshot.next_txt)
        if (set(caches['payloads']) != set(inventory) or caches['explicit_deletions'] != []
                or caches['inventory_complete_proven'] is not False
                or caches['publication_ready'] is not False or caches['remote_writes'] != 0):
            raise ValueError('cache candidate did not preserve the offline boundary')
    checkpoint = Checkpoint(
        snapshot_sha256=identity, stage=stage, plan_sha256=plan_sha,
        cw_sha256=cw['candidate_sha256'] if cw else None,
        cache_sha256=tuple(sorted((name, _sha(raw)) for name, raw in caches['payloads'].items())) if caches else (),
        changed_cache_paths=tuple(caches['changed_paths']) if caches else (),
    )
    return checkpoint, (plan, cw, caches)


def step(snapshot: Snapshot, checkpoint: Checkpoint | None = None) -> Checkpoint:
    """Advance one local model stage, replaying and verifying prior evidence.

    A lost response can be retried from the preceding *local* checkpoint. No
    statement about retrying a remote write follows from this property.
    """
    identity = _identity(snapshot)
    if checkpoint is None:
        stage = 'unchanged' if snapshot.current_txt == snapshot.next_txt else 'planned'
    else:
        if not isinstance(checkpoint, Checkpoint):
            raise ValueError('invalid offline checkpoint')
        if checkpoint.snapshot_sha256 != identity:
            raise ValueError('snapshot or checkpoint evidence changed')
        expected, _ = _replay(snapshot, checkpoint.stage)
        if checkpoint != expected:
            raise ValueError('snapshot or checkpoint evidence changed')
        stage = {'planned': 'indexed', 'indexed': 'cached', 'cached': 'prepared',
                 'prepared': 'prepared', 'unchanged': 'unchanged'}[checkpoint.stage]
    return _replay(snapshot, stage)[0]


def materialize(snapshot: Snapshot, checkpoint: Checkpoint) -> dict:
    """Reconstruct a prepared offline bundle; never publish it."""
    if not isinstance(checkpoint, Checkpoint) or checkpoint.stage != 'prepared':
        raise ValueError('a prepared changed-TXT checkpoint is required')
    if checkpoint.snapshot_sha256 != _identity(snapshot):
        raise ValueError('snapshot or checkpoint evidence changed')
    expected, (plan, cw, caches) = _replay(snapshot, 'prepared')
    if checkpoint != expected:
        raise ValueError('snapshot or checkpoint evidence changed')
    return {'target_path': planner.TARGET_PATH, 'target_txt': snapshot.next_txt,
            'metadata_gz': plan['candidate_metadata_gz'], 'cw_database': cw['candidate_bytes'],
            'cw_counts': dict(cw['counts']),
            'cache_payloads': dict(caches['payloads']), 'changed_cache_paths': caches['changed_paths'],
            'checkpoint': expected, 'explicit_deletions': [], 'remote_writes': 0,
            'inventory_complete_proven': False, 'publication_ready': False}
