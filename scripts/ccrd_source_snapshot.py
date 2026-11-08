"""Capture a bounded CCRD source at an exact stable upstream Git revision."""
from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request

try:
    from . import ccrd_source_override as source
except ImportError:
    import ccrd_source_override as source


REF = 'https://api.github.com/repos/ProletRevDicta/Prolet/git/ref/heads/master'
SHA = re.compile(r'[0-9a-f]{40}\Z')


def read_revision(token: str) -> str:
    import json
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'pipeline-ccrd-source-snapshot/1.0'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(REF, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f'CCRD source revision HTTP {response.status}')
        raw = response.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError('CCRD source revision response too large')
    value = json.loads(raw)
    revision = value.get('object', {}).get('sha') if isinstance(value, dict) else None
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError('invalid CCRD source revision')
    return revision


def read_text(url: str) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'pipeline-ccrd-source-snapshot/1.0',
                                                   'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f'CCRD source text HTTP {response.status}')
        raw = response.read(source.MAX_OVERRIDE_BYTES + 1)
    if not raw or len(raw) > source.MAX_OVERRIDE_BYTES:
        raise ValueError('CCRD source text is empty or oversized')
    raw.decode('utf-8')
    return raw


def capture(token: str, *, revision=read_revision, read=read_text) -> tuple[bytes, dict]:
    before = revision(token)
    if not isinstance(before, str) or not SHA.fullmatch(before):
        raise ValueError('invalid CCRD source revision')
    selected = None
    for path in source.UPSTREAM_PATHS:
        url = f'{source.UPSTREAM_REPO}/{before}/{urllib.parse.quote(path, safe="/")}'
        try:
            raw = read(url)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        selected = (url, raw)
        break
    if selected is None:
        raise ValueError('none of the approved CCRD source paths exists at the captured revision')
    url, raw = selected
    if not isinstance(raw, bytes) or not 0 < len(raw) <= source.MAX_OVERRIDE_BYTES:
        raise ValueError('invalid CCRD source snapshot bytes')
    raw.decode('utf-8')
    if revision(token) != before:
        raise RuntimeError('CCRD upstream source revision changed during capture')
    return raw, {'url': url, 'revision': before, 'sha256': hashlib.sha256(raw).hexdigest(),
                 'bytes': len(raw), 'source_stable': True, 'remote_writes': 0}
