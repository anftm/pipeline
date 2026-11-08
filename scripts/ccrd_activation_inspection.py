"""Read exact live CCRD Space activation without treating status as byte proof."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request

try:
    from . import ccrd_lifecycle as lifecycle
except ImportError:
    import ccrd_lifecycle as lifecycle


SPACE = 'vomebook/Search'
ACTIVATION = 'data/index-release.json'
MAX_ACTIVATION = 64 * 1024
REVISION = re.compile(r'[0-9a-f]{40}\Z')
GENERATION = re.compile(r'ccrd-four-[0-9a-f]{64}\Z')


def _get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'pipeline-ccrd-activation-inspection/1.0',
                                                   'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(request, timeout=45) as response:
        if response.status != 200:
            raise RuntimeError(f'CCRD activation HTTP {response.status}')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('CCRD activation is oversized')
    return data


def decode(raw: bytes, generation: str, manifest_sha256: str) -> dict:
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        raise ValueError('invalid CCRD generation')
    lifecycle.sha(manifest_sha256)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ACTIVATION:
        raise ValueError('CCRD activation bytes are missing or oversized')
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid CCRD activation JSON') from error
    if (not isinstance(value, dict) or set(value) != {'version', 'kind', 'bucket', 'path', 'sha256'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'ccrd-index-activation' or value['bucket'] != lifecycle.BUCKET
            or value['path'] != f'generations/{generation}/manifest.json' or value['sha256'] != manifest_sha256):
        raise ValueError('CCRD Space activation differs from the expected manifest')
    return value


def observe(api, *, generation: str, manifest_sha256: str, get=_get) -> dict:
    """Pin Space revision and ensure running code and activation bytes stay stable."""
    info = api.space_info(SPACE, timeout=20)
    revision = getattr(info, 'sha', None)
    if not isinstance(revision, str) or not REVISION.fullmatch(revision):
        raise ValueError('invalid CCRD Space revision')
    state = api.get_space_runtime(SPACE)
    if state.stage != 'RUNNING' or not isinstance(state.raw, dict) or state.raw.get('sha') != revision:
        raise ValueError('CCRD Space runtime is not running the observed repository revision')
    url = f'https://huggingface.co/spaces/{SPACE}/resolve/{revision}/{ACTIVATION}'
    raw = get(url, MAX_ACTIVATION)
    value = decode(raw, generation, manifest_sha256)
    if api.space_info(SPACE, timeout=20).sha != revision:
        raise RuntimeError('CCRD Space repository changed during activation observation')
    final = api.get_space_runtime(SPACE)
    if final.stage != 'RUNNING' or not isinstance(final.raw, dict) or final.raw.get('sha') != revision:
        raise RuntimeError('CCRD Space runtime changed during activation observation')
    return {'version': 1, 'kind': 'ccrd-space-activation-observation', 'space': SPACE,
            'revision': revision, 'generation': generation, 'manifest_sha256': manifest_sha256,
            'activation_sha256': hashlib.sha256(raw).hexdigest(), 'activation': value,
            'runtime_observed_twice': True,
            'scope': 'repository activation and runtime identity only; loaded index bytes not proven'}
