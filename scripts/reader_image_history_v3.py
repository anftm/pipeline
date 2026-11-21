"""One exact v3 promotion permit. Pages dispatch remains a separate lane."""
from __future__ import annotations

import json

from . import publish_reader_v3 as v3, reader_image_history as history
from .reader_image_history_store import S3ClaimStore

PREFIX = 'reader-index/gc/image-history-v3/v1/'
LIMIT = 8192


def promote(*, store, client, parent_raw, target_pointer, canonical_sha256, legacy_seal, guard,
            observe_only=False):
    """Verify the reviewed v3 successor or win one durable promotion attempt.

    The caller must check the continuous migration fence and all prior-stage
    readbacks via guard(). No loaded or ambiguous permit can authorize a PUT.
    No Pages dispatch or deletion is performed here.
    """
    history._hex(canonical_sha256)
    history._hex(legacy_seal)
    if not isinstance(parent_raw, bytes) or not 0 < len(parent_raw) <= LIMIT or not isinstance(target_pointer, dict):
        raise ValueError('expected exact reviewed Reader pointers')
    parent = json.loads(parent_raw)
    for pointer in (parent, target_pointer):
        history._hex(pointer['generation'])
        if (pointer.get('version'), pointer.get('kind'), pointer.get('catalog', {}).get('path')) != (
                1, 'reader-reading-pointer', f"reader-index/v3/generations/{pointer['generation']}/catalog.json"):
            raise ValueError('invalid reviewed v3 pointer')
    if target_pointer['generation'] == parent['generation']:
        raise ValueError('expected a distinct reviewed v3 successor')
    claim = PREFIX + history._sha(parent_raw) + '.json'
    permit = history._encode({'version': 1, 'kind': 'reader-image-v3-permit', 'parent_sha256': history._sha(parent_raw),
                              'pointer': target_pointer, 'canonical_sha256': canonical_sha256, 'legacy_seal': legacy_seal})
    if len(permit) > LIMIT:
        raise ValueError('oversized Reader v3 permit')
    if client is None:
        raise ValueError('explicit single-attempt S3 client required')
    journal = S3ClaimStore(client)

    def check():
        if guard() is not True:
            raise ValueError('Reader image migration fence or legacy proof not confirmed')
        if history._sha(store.read_bytes(v3.ASSETS, 'reader-index/reader_assets.json.gz')) != canonical_sha256:
            raise ValueError('canonical sidecar changed before v3 promotion')
        raw = store.read_bytes(v3.ASSETS, v3.POINTER)
        pointer, catalog = v3.current(store)
        if json.loads(raw) != pointer or store.read_bytes(v3.ASSETS, v3.POINTER) != raw:
            raise ValueError('Reader pointer views differ')
        return raw, pointer, catalog

    def inspect():
        raw, pointer, catalog = check()
        if (raw != v3.encode(target_pointer) or pointer != target_pointer
                or catalog.get('parent_generation') != parent['generation']):
            raise ValueError('Reader exact v3 successor not observed; do not replay')
        return {'generation': pointer['generation'], 'pointer': pointer, 'legacy_seal': legacy_seal,
                'confirmed': True, 'pages_dispatched': False}

    prior, _ = journal._get(claim, maximum=LIMIT)
    if prior is not None:
        if prior != permit:
            raise ValueError('Reader v3 parent already claimed for different bytes')
        return inspect()
    if observe_only:
        raise ValueError('no matching v3 attempt to observe')
    raw, pointer, _ = check()
    if raw != parent_raw or pointer != parent:
        raise ValueError('exact v3 parent changed before claim')
    options = dict(expected_pointer=parent, expected_canonical=canonical_sha256, conditional_pointer=True)
    if v3.unify(store, apply=False, **options).get('pointer') != target_pointer:
        raise ValueError('v3 successor differs from the reviewed pointer')
    if not journal._put(claim, permit, maximum=LIMIT, IfNoneMatch='*'):
        raise RuntimeError('v3 permit already claimed; observe the existing attempt')
    fresh, _, _ = check()
    if fresh != parent_raw:
        raise ValueError('v3 parent changed after claim; do not replay')
    if v3.unify(store, apply=True, **options).get('pointer') != target_pointer:
        raise ValueError('v3 publication did not produce the reviewed pointer')
    return inspect()
