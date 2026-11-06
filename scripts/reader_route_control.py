#!/usr/bin/env python3
"""Withdraw or restore one Reader route through the existing v3 publisher.

The source/search record, canonical format indexes and bucket files are not
modified. A v3 catalog carries suppressed source keys; compatible clients read
an ordinary filtered base snapshot and need no new wire format.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import re
import time

try:
    from . import publish_reader_v3 as publication, reader_route_plan as plan
except ImportError:
    import publish_reader_v3 as publication
    import reader_route_plan as plan


FIELD = 'suppressed_sources'


def validated_sources(catalog: dict) -> list[str]:
    keys = catalog.get(FIELD, [])
    if not isinstance(keys, list) or any(not isinstance(key, str) or key.count('\0') != 1 for key in keys):
        raise ValueError('invalid suppressed source list')
    for key in keys:
        repo, path = key.split('\0')
        if plan.source_key(repo, path) != key:
            raise ValueError('invalid suppressed source identity')
    if keys != sorted(set(keys)):
        raise ValueError('suppressed source list must be sorted and unique')
    if any(key in catalog.get('files', {}) for key in keys):
        raise ValueError('suppressed source still has a v3 override')
    return keys


def filter_sidecar(base: dict, keys: list[str]) -> dict:
    if base.get('v') != 1 or not isinstance(base.get('f'), dict):
        raise ValueError('invalid base sidecar')
    result = copy.deepcopy(base)
    for key in keys:
        result['f'].pop(key, None)
    return result


def prepare_base(catalog: dict, raw: bytes) -> tuple[bytes, dict]:
    """Filter authoritative current base and return an immutable v3 snapshot."""
    base = plan.decode_sidecar(raw)
    keys = validated_sources(catalog)
    if keys and any(key in base['f'] for key in keys):
        raw = gzip.compress(publication.encode(filter_sidecar(base, keys)), mtime=0)
    digest = hashlib.sha256(raw).hexdigest()
    ref = publication.metadata(publication.ASSETS,
        f'reader-index/snapshots/{digest}/reader_assets.json.gz', raw)
    publication.base_sidecar_ref(ref)
    return raw, ref


def change(store, options: dict, expected_parent: str | None, *, action: str, apply=False) -> dict:
    if not isinstance(options, dict) or set(options) != {'repo', 'path'}:
        raise ValueError('provide only exact repo and path')
    key = plan.source_key(options['repo'], options['path'])
    if action not in {'remove', 'restore'}:
        raise ValueError('invalid route action')
    if apply:
        publication.assert_writer(store)
    pointer, catalog = publication.current(store)
    if not pointer or pointer['generation'] != expected_parent:
        raise ValueError('v3 generation changed; inspect and use the current generation')
    keys = validated_sources(catalog)
    if not catalog.get('base_sidecar'):
        raise ValueError('a pinned base is required before changing a Reader route')
    canonical = store.read_bytes(publication.ASSETS, plan.SIDECAR)
    base = plan.decode_sidecar(canonical)
    pinned = publication.pinned_base(store, catalog, {})
    files = copy.deepcopy(catalog['files'])
    if action == 'remove':
        if key not in keys and key not in base['f'] and key not in pinned['f'] and key not in files:
            raise ValueError('source has no Reader route; nothing was changed')
        removed_override = files.pop(key, None)
        keys = sorted(set(keys) | {key})
    else:
        if key not in keys:
            # After a successful pointer write, retry publication/verification
            # without creating a second generation or replacing a v3 override.
            if key not in pinned['f'] or key in files:
                raise ValueError('source is not suppressed')
            if publication.current(store)[0] != pointer or store.read_bytes(publication.ASSETS, plan.SIDECAR) != canonical:
                raise ValueError('Reader roots changed before route update')
            return {'applied': apply, 'unchanged': True, 'action': action, 'source_key': key,
                    'previous_generation': pointer['generation'], 'generation': pointer['generation'],
                    'objects_deleted': False, 'search_record_removed': False}
        if key not in base['f']:
            raise ValueError('canonical source route is unavailable; cannot restore')
        # Do not restore a stale v3 override. The current canonical route is used.
        removed_override = None
        keys = [item for item in keys if item != key]
    revised = {**catalog, 'files': files, FIELD: keys}
    packed, ref = prepare_base(revised, canonical)
    new_catalog = {'version': 3, 'kind': 'reader-reading-catalog', 'files': files,
                   'base_sidecar': ref, FIELD: keys, 'parent_generation': pointer['generation']}
    same = catalog.get('base_sidecar') == ref and catalog.get('files') == files and validated_sources(catalog) == keys
    if same:
        if publication.current(store)[0] != pointer or store.read_bytes(publication.ASSETS, plan.SIDECAR) != canonical:
            raise ValueError('Reader roots changed before route update')
        return {'applied': apply, 'unchanged': True, 'action': action, 'source_key': key,
                'previous_generation': pointer['generation'], 'generation': pointer['generation'],
                'objects_deleted': False, 'search_record_removed': False}
    report = publication.commit_catalog(store, copy.deepcopy(new_catalog), pointer, apply=False)
    report.update(action=action, source_key=key, previous_generation=pointer['generation'],
                  removed_override=removed_override is not None, objects_deleted=False, search_record_removed=False)
    if not apply:
        return report
    if publication.current(store)[0] != pointer or store.read_bytes(publication.ASSETS, plan.SIDECAR) != canonical:
        raise ValueError('Reader roots changed before route update')
    publication.immutable_put(store, publication.ASSETS, ref['path'], packed)
    result = publication.commit_catalog(store, new_catalog, pointer, apply=True,
                                        restored_source=key if action == 'restore' else None)
    result.update(action=action, source_key=key, previous_generation=pointer['generation'],
                  removed_override=removed_override is not None, objects_deleted=False, search_record_removed=False)
    return result


def verify_consumers(store, client, options: dict, expected_generation: str, *, action='remove',
                     timeout_seconds=0, sleep_fn=time.sleep, clock=time.monotonic) -> dict:
    """Verify both deployed projections for the exact generation and route."""
    if not isinstance(options, dict) or set(options) != {'repo', 'path'}:
        raise ValueError('provide only exact repo and path')
    key = plan.source_key(options['repo'], options['path'])
    if not re.fullmatch(r'[0-9a-f]{64}', expected_generation):
        raise ValueError('an exact generation is required')
    pointer, catalog = publication.current(store)
    if not pointer or pointer['generation'] != expected_generation:
        raise ValueError('v3 generation changed before verification')
    suppressed = validated_sources(catalog)
    if action not in {'remove', 'restore'}:
        raise ValueError('invalid route verification action')
    if action == 'remove' and key not in suppressed:
        raise ValueError('source is not suppressed in this generation')
    if action == 'restore' and key in suppressed:
        raise ValueError('source remains suppressed')
    base = publication.pinned_base(store, catalog, {})
    projected = publication.project_sidecar(base, catalog)['f']
    if action == 'remove' and key in projected:
        raise ValueError('suppressed route remains in expected projection')
    if action == 'restore' and key not in projected:
        raise ValueError('restored route is absent from expected projection')
    import httpx
    pending, consumers, errors = {'hf', 'pages'}, {}, {}
    deadline = clock() + max(0, timeout_seconds)
    while pending:
        for surface in sorted(pending):
            try:
                receipt = publication.observe_consumer(store, surface, client,
                    absent_source=key if action == 'remove' else None)
                if receipt.get('generation') != expected_generation or receipt.get('projection_verified') is not True:
                    raise ValueError(f'{surface} has not accepted the expected projection')
                consumers[surface] = receipt
                pending.remove(surface)
                errors.pop(surface, None)
            except (ValueError, httpx.HTTPError, OSError) as error:
                errors[surface] = type(error).__name__
        if not pending:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            raise RuntimeError(f'route verification incomplete: {errors}')
        if publication.current(store)[0] != pointer:
            raise ValueError('v3 generation changed during verification')
        sleep_fn(min(20, remaining))
    if publication.current(store)[0] != pointer:
        raise ValueError('v3 generation changed during verification')
    return {'version': 1, 'kind': 'reader-route-verification', 'source_key': key,
            'generation': expected_generation, 'complete': True,
            'status': 'verified-withdrawn' if action == 'remove' else 'verified-restored', 'consumers': consumers,
            'objects_deleted': False, 'search_record_removed': False}
