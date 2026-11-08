"""Capture a read-only, fixed-parent CCRD four-source CW:0 candidate plan.

This planner verifies bytes and live consumer state but does not upload,
activate, delete, change a Space, or authorize publication.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import quote

try:
    from . import ccrd_consumer_inspection as consumer
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_source_override as source
except ImportError:
    import ccrd_consumer_inspection as consumer
    import ccrd_lifecycle as lifecycle
    import ccrd_source_override as source


MAX_POINTER = 2 * 1024 * 1024
MAX_TEXT = 32 * 1024 * 1024


def _text(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_TEXT:
        raise ValueError('CCRD text is empty or oversized')
    raw.decode('utf-8')
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def plan(pointer: bytes, old: bytes, new: bytes, observed: dict, *, source_url: str, source_revision: str,
         source_observation: dict) -> dict:
    if not isinstance(pointer, bytes) or not 0 < len(pointer) <= MAX_POINTER:
        raise ValueError('invalid CCRD pointer bytes')
    manifest = json.loads(pointer)
    refs = lifecycle.references(manifest, 'current.json')
    if manifest.get('format') != 2:
        raise ValueError('CCRD candidate requires an existing four-source generation')
    old_id, new_id = _text(old), _text(new)
    override = manifest.get('source_overrides', {}).get(str(source.CORPUS_PATH))
    if not isinstance(override, dict) or {key: override.get(key) for key in old_id} != old_id:
        raise ValueError('old CW:0 text does not match the parent manifest')
    if old_id == new_id:
        raise ValueError('CCRD CW:0 source has not changed')
    if (not isinstance(source_revision, str) or len(source_revision) != 40
            or any(c not in '0123456789abcdef' for c in source_revision)):
        raise ValueError('new CCRD source must be pinned to an exact approved repository revision')
    allowed = {f'{source.UPSTREAM_REPO}/{source_revision}/{quote(path, safe="/")}' for path in source.UPSTREAM_PATHS}
    if not isinstance(source_url, str) or source_url not in allowed:
        raise ValueError('new CCRD source must be pinned to an exact approved repository revision')
    if (not isinstance(source_observation, dict) or source_observation.get('revision') != source_revision
            or source_observation.get('url') != source_url
            or source_observation.get('sha256') != new_id['sha256']
            or source_observation.get('bytes') != new_id['bytes']
            or source_observation.get('source_stable') is not True):
        raise ValueError('pinned CCRD source observation does not match the proposed bytes')
    loaded = observed.get('loaded_sources') if isinstance(observed, dict) else None
    if (not isinstance(observed, dict) or observed.get('generation') != manifest.get('generation')
            or observed.get('cw0') != old_id or observed.get('cw0_bytes_verified') is not True
            or observed.get('status_observed_twice') is not True or not isinstance(loaded, list)
            or any(not isinstance(item, str) for item in loaded)
            or len(loaded) != len(consumer.SOURCES) or set(loaded) != consumer.SOURCES):
        raise ValueError('CCRD live consumer has not verified the parent text')
    databases = manifest['databases']
    return {'version': 1, 'kind': 'ccrd-cw0-readonly-candidate-plan',
            'parent_generation': manifest['generation'], 'parent_pointer_sha256': hashlib.sha256(pointer).hexdigest(),
            'parent_cw_database': databases['CW'], 'old_text': old_id, 'new_text': new_id,
            'new_source': {'url': source_url, 'revision': source_revision,
                           'source_stable': source_observation['source_stable']},
            'preserved_database_identities': {name: databases[name] for name in ('CCRD', 'RMRB', 'PLA')},
            'parent_reference_count': len(refs), 'parent_metadata': manifest['metadata'], 'parent_archives': manifest['archives'],
            'consumer_observation': observed, 'remote_writes': 0, 'publication_ready': False,
            'remaining': ['verify candidate SQLite bytes and non-target semantics',
                          'construct four-source metadata and retained archive identities',
                          'verify immutable storage and coordinate consumer activation',
                          'establish writer isolation, recovery and exact live acceptance']}
