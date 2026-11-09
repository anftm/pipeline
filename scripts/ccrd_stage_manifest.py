"""Verify a staged CCRD generation, then conditionally create its manifest.

This is not publication: current.json and the Space are never changed. Exact
member readbacks, parent and upstream observations are independently checked;
unknown write outcomes are read back once, never blindly retried. Caller must
coordinate actual writers; this module does not provide a bucket-wide lock.
"""
from __future__ import annotations

import hashlib
import json
from typing import Protocol

try:
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_source_override as source
    from . import ccrd_stage_objects as objects
    from . import ccrd_staging_plan as planning
except ImportError:
    import ccrd_lifecycle as lifecycle
    import ccrd_source_override as source
    import ccrd_stage_objects as objects
    import ccrd_staging_plan as planning


MAX_MANIFEST = 2 * 1024 * 1024


class ManifestStore(Protocol):
    def parent_pointer(self) -> bytes: ...
    def inspect(self, key: str) -> dict | None: ...
    def inspect_preserved(self, key: str) -> dict | None: ...
    def read_manifest(self) -> bytes | None: ...
    def create_manifest(self, raw: bytes) -> None: ...


def _source_check(plan: dict, observation: dict) -> None:
    identity = plan['identity']
    text = identity['new_text']
    if (not isinstance(observation, dict) or observation.get('revision') != identity['source_revision']
            or observation.get('url') != identity['source_url'] or observation.get('source_stable') is not True
            or observation.get('bytes') != text['bytes'] or observation.get('sha256') != text['sha256']):
        raise ValueError('live CCRD source does not match the exact candidate')


def _consumer_check(parent: dict, observed: dict) -> None:
    original = parent['source_overrides'][str(source.CORPUS_PATH)]
    identity = {key: original[key] for key in ('bytes', 'sha256')}
    if (not isinstance(observed, dict) or observed.get('generation') != parent['generation']
            or observed.get('cw0') != identity or observed.get('cw0_bytes_verified') is not True
            or observed.get('status_observed_twice') is not True):
        raise ValueError('CCRD consumer has not verified the expected parent')
    sources = observed.get('loaded_sources')
    if (not isinstance(sources, list) or any(not isinstance(name, str) for name in sources)
            or len(sources) != 4 or set(sources) != {'CCRD', 'CW', 'RMRB', 'PLA'}):
        raise ValueError('CCRD consumer has not verified all four sources')


def _members(plan: dict, store: ManifestStore) -> int:
    for entry in plan['objects']:
        expected = {'bytes': entry['bytes'], 'sha256': entry['sha256']}
        if store.inspect(entry['target']) != expected:
            raise ValueError('CCRD staged member missing or mismatched: ' + entry['target'])
        if entry['source'] == 'parent' and store.inspect_preserved(entry['source_path']) != expected:
            raise ValueError('CCRD parent member missing or mismatched: ' + entry['source_path'])
    for details in plan['identity']['preserved_databases'].values():
        expected = {'bytes': details['bytes'], 'sha256': details['sha256']}
        if store.inspect_preserved(details['path']) != expected:
            raise ValueError('CCRD preserved database missing or mismatched: ' + details['path'])
    return len(plan['objects']) + len(plan['identity']['preserved_databases'])


def _verify(plan: dict, description: dict, store: ManifestStore, capture, observe) -> tuple[bytes, int]:
    objects._validate(plan)
    parent_raw = store.parent_pointer()
    if planning.make(parent_raw, description) != plan:
        raise ValueError('CCRD stage plan differs from the approved candidate')
    parent = json.loads(parent_raw)
    _source_check(plan, capture())
    _consumer_check(parent, observe())
    count = _members(plan, store)
    _source_check(plan, capture())
    _consumer_check(parent, observe())
    if store.parent_pointer() != parent_raw:
        raise ValueError('CCRD parent pointer changed during staged verification')
    return parent_raw, count


def stage_manifest(plan: dict, *, approved_description: dict, store: ManifestStore,
                   capture, observe, apply: bool = False, before_create=None) -> dict:
    """Re-read every referenced object, then optionally create one immutable manifest."""
    parent, count = _verify(plan, approved_description, store, capture, observe)
    raw = lifecycle.canonical(plan['manifest'])
    if store.parent_pointer() != parent:
        raise ValueError('CCRD parent pointer changed before staged manifest operation')
    if not 0 < len(raw) <= MAX_MANIFEST or hashlib.sha256(raw).hexdigest() != plan['manifest_sha256']:
        raise ValueError('CCRD manifest bytes differ from plan')
    key = f"generations/{plan['generation']}/manifest.json"
    lifecycle.references(json.loads(raw), key)
    prior = store.read_manifest()
    wrote = False
    outcome_unknown = False
    if prior is not None and prior != raw:
        raise ValueError('existing CCRD generation manifest has different bytes')
    if apply and prior is None:
        if before_create is None:
            raise ValueError('CCRD manifest creation requires a fresh publication guard')
        before_create()
        try:
            store.create_manifest(raw)
        except Exception:
            # A conflict or lost response is unknown. Read the exact key once;
            # do not replay, even if the observed state is still absent.
            after = store.read_manifest()
            if after != raw:
                raise
            outcome_unknown = True
        else:
            after = store.read_manifest()
        if after != raw:
            raise ValueError('CCRD generation manifest readback differs')
        wrote = True
    if store.parent_pointer() != parent:
        raise ValueError('CCRD parent pointer changed after staged manifest operation')
    return {'version': 1, 'kind': 'ccrd-cw0-staged-manifest', 'generation': plan['generation'],
            'manifest_sha256': plan['manifest_sha256'], 'references_verified': count,
            'manifest_present': prior == raw or wrote, 'manifest_written': wrote,
            'manifest_write_response_unknown': outcome_unknown, 'pointer_written': False, 'consumer_switched': False, 'publication_ready': False,
            'external_writers_verified': False}
