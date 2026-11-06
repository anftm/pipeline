#!/usr/bin/env python3
"""Read-only check of the one CCRD source TXT; never updates any resource.

Checks one bounded upstream TXT against the currently deployed Space file and
its active index manifest. A mismatch is a report, not an update or approval.
The source can change between reads; this check cannot fence external writers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from .ccrd_source_override import CORPUS_PATH, source_urls
except ImportError:
    from ccrd_source_override import CORPUS_PATH, source_urls


SPACE = 'vomebook/Search'
TARGET = CORPUS_PATH.as_posix()
BUCKET = 'vomebook/ccrd-index'
USER_AGENT = 'ccrd-single-txt-readonly-check/1.0'
MAX_TXT_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024


class ReadOnlyRedirects(HTTPRedirectHandler):
    """Allow the specific HF CDN hop, never forwarding Authorization to it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlparse(req.full_url), urlparse(newurl)
        allowed_cdn = old.hostname == 'huggingface.co' and new.hostname == 'us.aws.cdn.hf.co'
        if (old.scheme != 'https' or new.scheme != 'https' or old.port is not None or new.port is not None
                or old.username or old.password or new.username or new.password
                or old.hostname not in {'huggingface.co', 'raw.githubusercontent.com', 'us.aws.cdn.hf.co',
                                        'vomebook-search.hf.space'}
                or (old.hostname != new.hostname and not allowed_cdn)):
            raise RuntimeError('cross-origin or non-HTTPS redirect refused')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and old.hostname != new.hostname:
            redirected.remove_header('Authorization')
        return redirected


def read_url(url: str, *, limit: int) -> bytes:
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'huggingface.co', 'raw.githubusercontent.com',
                                                            'vomebook-search.hf.space'}
            or parsed.username or parsed.password or parsed.port is not None):
        raise ValueError('URL is outside the fixed read-only origins')
    headers = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
    request = Request(url, headers=headers, method='GET')
    opener = build_opener(ReadOnlyRedirects())
    try:
        with opener.open(request, timeout=45) as response:
            raw = response.read(limit + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        raise RuntimeError(f'read-only GET failed: HTTP {status}') from None
    except (URLError, OSError, TimeoutError):
        raise RuntimeError('read-only GET failed: network error') from None
    if len(raw) > limit:
        raise ValueError('read-only response exceeds configured byte limit')
    return raw


def upstream_snapshot(read=read_url) -> tuple[str, bytes]:
    """Capture exactly one selected upstream response, including its URL."""
    for url in source_urls():
        try:
            data = read(url, limit=MAX_TXT_BYTES)
        except RuntimeError as error:
            if str(error) == 'read-only GET failed: HTTP 404':
                continue
            raise
        if not data:
            raise ValueError('upstream TXT is empty')
        data.decode('utf-8')
        return url, data
    raise ValueError('neither configured upstream TXT path exists')


def upstream(read=read_url) -> bytes:
    return upstream_snapshot(read)[1]


def json_object(raw: bytes, label: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('invalid JSON constant')))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(label + ' is not valid JSON') from error
    if not isinstance(value, dict):
        raise ValueError(label + ' must be a JSON object')
    return value


def _head(state: dict) -> str:
    revision = state.get('sha')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Space does not expose an immutable commit SHA')
    runtime = state.get('runtime')
    if not isinstance(runtime, dict) or runtime.get('stage') != 'RUNNING' or runtime.get('sha') != revision:
        raise ValueError('Space running revision is not the pinned repository commit')
    return revision


def _index_status(status: dict, manifest: dict) -> None:
    loaded = status.get('bucket')
    if (status.get('ready') is not True or not isinstance(loaded, dict)
            or loaded.get('state') != 'ready' or loaded.get('source') != 'space-activation'
            or loaded.get('generation') != manifest['generation']
            or loaded.get('base_metadata_sha256') != manifest.get('base_metadata_sha256')
            or not isinstance(loaded.get('sources'), list)
            or len(loaded['sources']) != 4
            or not all(isinstance(source, str) for source in loaded['sources'])
            or set(loaded['sources']) != {'CCRD', 'CW', 'RMRB', 'PLA'}):
        raise ValueError('running Space has not loaded the activated four-source generation')


def observe(*, read=read_url) -> dict:
    """Return a report and the exact observed TXT bytes, with no writes."""
    # GETting Space state/resolved files and immutable bucket manifests below
    # must never be confused with a lock or deployment acknowledgment.
    api = f'https://huggingface.co/api/spaces/{SPACE}'
    state = json_object(read(api, limit=MAX_METADATA_BYTES), 'Space status')
    revision = _head(state)
    base = f'https://huggingface.co/spaces/{SPACE}/resolve/{revision}/'
    activation_bytes = read(base + 'data/index-release.json', limit=MAX_METADATA_BYTES)
    activation = json_object(activation_bytes, 'Space index activation')
    status_url = 'https://vomebook-search.hf.space/api/index-status'
    status = json_object(read(status_url, limit=MAX_METADATA_BYTES), 'running index status')
    manifest_path = activation.get('path')
    if (type(activation.get('version')) is not int or activation['version'] != 1
            or activation.get('kind') != 'ccrd-index-activation'
            or activation.get('bucket') != BUCKET or not isinstance(manifest_path, str)
            or not re.fullmatch(r'generations/ccrd-four-[0-9a-f]{64}/manifest\.json', manifest_path)
            or not re.fullmatch(r'[0-9a-f]{64}', str(activation.get('sha256', '')))):
        raise ValueError('Space activation does not pin a supported four-source index')
    manifest_raw = read(f'https://huggingface.co/buckets/{BUCKET}/resolve/{manifest_path}',
                        limit=MAX_METADATA_BYTES)
    if hashlib.sha256(manifest_raw).hexdigest() != activation['sha256']:
        raise ValueError('Space activation and immutable index manifest differ')
    manifest = json_object(manifest_raw, 'index manifest')
    if (type(manifest.get('format')) is not int or manifest['format'] != 2
            or manifest.get('kind') != 'ccrd-four-source-generation'
            or manifest_path != f"generations/{manifest.get('generation')}/manifest.json"
            or not re.fullmatch(r'[0-9a-f]{64}', str(manifest.get('base_metadata_sha256', '')))
            or not isinstance(manifest.get('databases'), dict)
            or set(manifest['databases']) != {'CCRD', 'CW', 'RMRB', 'PLA'}):
        raise ValueError('index manifest is not the expected four-source generation')
    _index_status(status, manifest)
    overrides = manifest.get('source_overrides')
    if not isinstance(overrides, dict) or not isinstance(overrides.get(TARGET), dict):
        raise ValueError('index manifest does not pin the target TXT')
    expected = overrides[TARGET].get('sha256')
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('index manifest does not pin the target TXT')
    site_text = read(base + quote(TARGET, safe='/'), limit=MAX_TXT_BYTES)
    if not site_text:
        raise ValueError('Space target TXT is empty')
    site_text.decode('utf-8')
    if hashlib.sha256(site_text).hexdigest() != expected:
        raise ValueError('Space TXT and activated index manifest differ')
    selected_url, current_upstream = upstream_snapshot(read)
    status_after = json_object(read(status_url, limit=MAX_METADATA_BYTES), 'running index status reread')
    _index_status(status_after, manifest)
    after = json_object(read(api, limit=MAX_METADATA_BYTES), 'Space status reread')
    if _head(after) != revision:
        raise ValueError('Space changed during the read-only check')
    upstream_sha = hashlib.sha256(current_upstream).hexdigest()
    report = {'space_revision': revision, 'index_generation': manifest['generation'],
              'index_manifest_sha256': activation['sha256'],
              'site_txt_sha256': expected, 'upstream_txt_sha256': upstream_sha,
              'upstream_matches_site': upstream_sha == expected,
              'consumer_status_matches_generation': True, 'complete_index_contents_verified': False,
              'external_writers_fenced': False,
              'remote_writes': 0, 'publication_ready': False}
    return {'report': report, 'site_txt': site_text, 'upstream_txt': current_upstream,
            'upstream_url': selected_url, 'manifest_bytes': manifest_raw,
            'activation_bytes': activation_bytes}


def check(*, read=read_url) -> dict:
    """Maintain the metadata-only result; never print TXT bodies."""
    return observe(read=read)['report']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = check()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['upstream_matches_site'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
