"""Read-only CCRD consumer evidence for the currently selected four-source index.

Ready/status, one text download and a search hit are observations at a point in
time.  They do not prove every indexed document or authorize publication.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request

BASE = 'https://vomebook-search.hf.space'
SOURCES = frozenset({'CCRD', 'CW', 'RMRB', 'PLA'})
MAX_STATUS_BYTES = 128 * 1024
MAX_TEXT_BYTES = 32 * 1024 * 1024


def get_bytes(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'pipeline-ccrd-consumer-inspection/1.0',
                                                  'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(f'CCRD consumer HTTP {response.status}')
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('CCRD consumer response exceeds inspection limit')
    return raw


def status(raw, expected_generation):
    try:
        value = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid CCRD consumer status JSON') from error
    if not isinstance(value, dict):
        raise TypeError('invalid CCRD consumer status')
    bucket = value.get('bucket')
    loaded = value.get('loaded_sources')
    building = value.get('building_sources')
    if (value.get('ready') is not True or value.get('state') != 'ready'
            or not isinstance(loaded, list) or any(not isinstance(item, str) for item in loaded)
            or set(loaded) != SOURCES or len(loaded) != len(SOURCES)
            or building != [] or not isinstance(bucket, dict) or bucket.get('state') != 'ready'
            or bucket.get('generation') != expected_generation or bucket.get('pointer_error') is not None):
        raise ValueError('CCRD consumer has not reported the expected ready four-source generation')
    return {'generation': bucket['generation'], 'loaded_sources': sorted(loaded),
            'base_metadata_sha256': bucket.get('base_metadata_sha256'), 'ready': True}


def observe(expected_generation, expected_text, *, get=get_bytes):
    if not isinstance(expected_text, dict) or type(expected_text.get('bytes')) is not int:
        raise ValueError('invalid expected CCRD text identity')
    before = status(get(BASE + '/api/index-status', MAX_STATUS_BYTES), expected_generation)
    raw = get(BASE + '/api/download/CW:0', MAX_TEXT_BYTES)
    identity = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    if identity != {key: expected_text.get(key) for key in identity}:
        raise ValueError('CCRD consumer CW:0 bytes differ from current manifest')
    after = status(get(BASE + '/api/index-status', MAX_STATUS_BYTES), expected_generation)
    if before != after:
        raise RuntimeError('CCRD consumer status changed during inspection')
    return {'generation': before['generation'], 'loaded_sources': before['loaded_sources'],
            'base_metadata_sha256': before['base_metadata_sha256'], 'cw0': identity,
            'status_observed_twice': True, 'cw0_bytes_verified': True,
            'scope': 'status and CW:0 download only; other loaded bytes and future state not verified'}
