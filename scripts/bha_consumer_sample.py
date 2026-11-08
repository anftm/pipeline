"""Compare bounded pinned BHA source articles with live consumer previews.

This samples explicit documents.  It does not prove all index bytes or grant
permission to rebuild or publish. Missing tags, unknown revisions and moving
source heads fail closed instead of being treated as empty data.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request

try:
    from . import check_bha_revisions as revisions
except ImportError:
    import check_bha_revisions as revisions


OWNER = 'anftm'
BASE = 'https://vomebook-bha-search.hf.space'
MAX_SOURCE = 12 * 1024 * 1024
MAX_PREVIEW = 24 * 1024 * 1024
MAX_HEALTH = 128 * 1024
SHA = re.compile(r'[0-9a-f]{40}\Z')
ARTICLE = re.compile(r'[0-9a-f]{10}\Z')
PART = re.compile(r'[A-Za-z0-9._-]+\Z')


def get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'pipeline-bha-consumer-sample/1.0',
                                                   'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(f'BHA sample response HTTP {response.status}')
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('BHA sample response exceeds limit')
    return raw


def search(doc_id: str, archive: int, title: str) -> bytes:
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 256:
        raise ValueError('invalid BHA sample title')
    body = json.dumps({'q': title, 'exact': True, 'fulltext': False, 'page': 1,
                       'page_size': 100, 'archive_id': archive}, ensure_ascii=False).encode()
    request = urllib.request.Request(BASE + '/api/search', data=body, method='POST',
                                     headers={'Content-Type': 'application/json',
                                              'User-Agent': 'pipeline-bha-consumer-sample/1.0'})
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(f'BHA sample search HTTP {response.status}')
        raw = response.read(MAX_PREVIEW + 1)
    if len(raw) > MAX_PREVIEW:
        raise ValueError('BHA sample search response exceeds limit')
    return raw


def sample_path(archive: int, commit: str, path: str) -> tuple[str, str]:
    if type(archive) is not int or not 0 <= archive <= 31 or not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError('invalid BHA archive or pinned commit')
    if not isinstance(path, str) or '\\' in path or any(ord(c) < 32 for c in path):
        raise ValueError('invalid BHA sample path')
    parts = path.split('/')
    if len(parts) != 4 or any(not PART.fullmatch(part) or part in {'.', '..'} for part in parts):
        raise ValueError('invalid BHA article path')
    article = parts[-1].removesuffix('.json')
    if not parts[-1].endswith('.json') or not ARTICLE.fullmatch(article) or parts[-2] != article[:3]:
        raise ValueError('invalid BHA article identity')
    doc_id = f'{archive}:{len(article)}:{article}:{parts[1]}'
    return doc_id, urllib.parse.quote(path, safe='/')


def _object(raw: bytes, label: str):
    try:
        return json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'invalid BHA {label} JSON') from error


def health(raw: bytes) -> dict:
    value = _object(raw, 'health')
    if (not isinstance(value, dict) or value.get('ok') is not True or value.get('index_ready') is not True
            or value.get('index_status') != 'ready' or value.get('es') is not True
            or type(value.get('document_count')) is not int or value['document_count'] < 1):
        raise ValueError('BHA consumer is not ready')
    return {'document_count': value['document_count'], 'index_status': value['index_status']}


def observe_one(archive: int, commit: str, path: str, *, fetch=get, query=search) -> dict:
    doc_id, escaped_path = sample_path(archive, commit, path)
    prefix = f'https://raw.githubusercontent.com/{OWNER}/banned-historical-archives{archive}/{commit}/'
    article_raw = fetch(prefix + escaped_path, MAX_SOURCE)
    tags_raw = fetch(prefix + escaped_path[:-5] + '.tags', MAX_SOURCE)
    article, tags = _object(article_raw, 'article'), _object(tags_raw, 'tags')
    if not isinstance(article, dict) or 'tags' in article or not isinstance(tags, list):
        raise ValueError('invalid BHA parsed article or tags')
    title = article.get('title')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 256:
        raise ValueError('invalid BHA sample article title')
    preview = _object(fetch(BASE + '/api/preview/' + urllib.parse.quote(doc_id, safe=':'), MAX_PREVIEW), 'preview')
    if (not isinstance(preview, dict) or preview.get('doc_id') != doc_id or preview.get('archive_id') != archive
            or preview.get('article_id') != path.split('/')[-1][:-5]
            or preview.get('publication_id') != path.split('/')[1] or preview.get('article') != {**article, 'tags': tags}):
        raise ValueError('BHA preview differs from the pinned parsed article and tags')
    hits = _object(query(doc_id, archive, title), 'search')
    if (not isinstance(hits, dict) or hits.get('indexing') is not False or hits.get('index_status') != 'ready'
            or type(hits.get('total')) is not int or not 1 <= hits['total'] <= 100
            or not isinstance(hits.get('results'), list)
            or not any(isinstance(item, dict) and item.get('doc_id') == doc_id for item in hits['results'])):
        raise ValueError('BHA exact-title search did not return the sampled article')
    return {'archive': archive, 'commit': commit, 'path': path, 'doc_id': doc_id,
            'source_sha256': hashlib.sha256(article_raw).hexdigest(), 'tags_sha256': hashlib.sha256(tags_raw).hexdigest(),
            'preview_article_equal': True, 'exact_title_search_found': True}


def observe(token: str, samples: list[dict], *, fetch=get, lookup=revisions.archive_identity, query=search) -> dict:
    if not token or not isinstance(samples, list) or not 1 <= len(samples) <= 16:
        raise ValueError('a credential and one to sixteen BHA samples are required')
    if any(not isinstance(row, dict) or set(row) != {'archive', 'path'}
           or type(row['archive']) is not int or not isinstance(row['path'], str) for row in samples):
        raise ValueError('invalid BHA sample selection')
    for row in samples:
        sample_path(row['archive'], 'a' * 40, row['path'])
    if len({(row['archive'], row['path']) for row in samples}) != len(samples):
        raise ValueError('duplicate BHA sample selection')
    before = health(fetch(BASE + '/api/health', MAX_HEALTH))
    identities = {archive: lookup(token, archive) for archive in sorted({row['archive'] for row in samples})}
    rows = []
    for row in samples:
        key, commit, tree = identities[row['archive']]
        if key != str(row['archive']) or not isinstance(tree, str) or not SHA.fullmatch(tree):
            raise ValueError('invalid BHA pinned tree identity')
        result = observe_one(row['archive'], commit, row['path'], fetch=fetch, query=query)
        result['tree'] = tree
        rows.append(result)
    for archive, old in identities.items():
        if lookup(token, archive) != old:
            raise RuntimeError('BHA parsed revision changed during sampling')
    after = health(fetch(BASE + '/api/health', MAX_HEALTH))
    if before != after:
        raise RuntimeError('BHA health changed during sampling')
    return {'version': 1, 'kind': 'bha-consumer-sample', 'samples': rows,
            'consumer_health_observed_twice': True, 'remote_writes': 0,
            'scope': 'explicit pinned articles, tags and exact-title searches only; whole index bytes unverified'}
