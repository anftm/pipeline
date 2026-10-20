#!/usr/bin/env python3
"""Read-only OCR deployment acceptance: generation, complete ID set and full bytes."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from . import ccrd_ocr_release as contract, publish_ccrd_ocr as publisher
except ImportError:
    import ccrd_ocr_release as contract
    import publish_ccrd_ocr as publisher


BASE = 'https://vomebook-search.hf.space'


def request(path, payload=None):
    data = contract.canonical(payload) if payload is not None else None
    headers = {'Content-Type': 'application/json'} if data else {}
    for attempt in range(4):
        try:
            with urlopen(Request(BASE + path, data=data, headers=headers), timeout=90) as response:
                return response.read()
        except HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 3:
                raise
            delay = int(error.headers.get('Retry-After', '0')) if error.headers.get('Retry-After', '').isdigit() else 0
            error.close()
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 3:
                raise
            delay = 0
        time.sleep(max(delay, 2 ** attempt))


def json_request(path, payload=None):
    return json.loads(request(path, payload))


def all_results(payload):
    first = json_request('/api/search', {**payload, 'page': 1, 'page_size': 200})
    total = first['total']
    results = list(first['results'])
    for page in range(2, (total + 199) // 200 + 1):
        part = json_request('/api/search', {**payload, 'page': page, 'page_size': 200})
        if part['total'] != total:
            raise ValueError('live OCR total changed during acceptance')
        results.extend(part['results'])
    if len(results) != total or len({item['doc_id'] for item in results}) != total:
        raise ValueError('live OCR pagination omitted or duplicated IDs')
    return results


def metadata_counts(path: Path, expected_sha256: str) -> dict[str, int]:
    """Read exact legacy counts from the archived base metadata, never constants."""
    raw = path.read_bytes()
    if contract.digest(raw) != expected_sha256:
        raise ValueError('base metadata archive differs from the OCR release base')
    try:
        data = json.loads(gzip.decompress(raw))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid base metadata archive') from error
    sources = data.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('base metadata lacks sources')
    counts = {}
    for source in sources:
        slug, count = source.get('slug'), source.get('count')
        if (not isinstance(slug, str) or not slug or slug in counts
                or type(count) is not int or count < 0):
            raise ValueError('invalid base metadata source counts')
        counts[slug] = count
    if 'OCR' in counts:
        raise ValueError('base metadata must not already contain the OCR source')
    return counts


def verify(root, receipt, *, metadata=None, wait_seconds=1200):
    generation, ref = receipt['generation'], receipt['release']
    raw = (root / contract.safe_path(ref['path'])).read_bytes()
    if len(raw) != ref['bytes'] or contract.digest(raw) != ref['sha256']:
        raise ValueError('live acceptance receipt/release checksum mismatch')
    manifest = json.loads(raw)
    database = manifest['database']
    if not (root / database['path']).is_file():
        # Recovery may only need to reactivate an already committed generation.
        target = root / contract.safe_path(database['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(publisher.verified_remote(publisher.HubBucketStore(), database))
    loaded = contract.load_release(root, ref['path'], ref['sha256'], manifest['base_metadata_sha256'])
    if loaded['manifest']['generation'] != generation:
        raise ValueError('acceptance generation differs from the published release')
    deadline = time.monotonic() + wait_seconds
    while True:
        status = json_request('/api/index-status')
        if status.get('ready') is True and status.get('ocr_release') == generation:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError('CCRD has not loaded the exact complete OCR generation')
        time.sleep(15)
    if metadata is None:
        raise ValueError('base metadata archive is required for exact legacy counts')
    expected = metadata_counts(metadata, manifest['base_metadata_sha256'])
    expected['OCR'] = len(loaded['records'])
    counts = {item['slug']: item['count'] for item in json_request('/api/sources')}
    if counts != expected:
        raise ValueError(f'live legacy/OCR counts differ: {counts!r} != {expected!r}')
    results = all_results({'q': '', 'sources': ['OCR'], 'sort': 'name'})
    by_id = {item['doc_id']: item for item in results}
    if set(by_id) != set(loaded['text_paths']):
        raise ValueError('live OCR complete ID set differs from the generated index')
    for value in loaded['exports']:
        item = by_id[value['doc_id']]
        if (item['TextQuality'] != value['quality'] or item['TextGeneration'] != value['text_generation']
                or item['DisplayPath'] != value['readable_rel_path']):
            raise ValueError('live OCR quality/path/generation mapping mismatch')
    checked, queries = [], []
    processed = receipt.get('processed') or [{'doc_id': value['doc_id']} for value in loaded['exports']]
    for processed in processed:
        number = processed['doc_id']
        body = loaded['text_paths'][number].read_bytes()
        item = by_id[number]
        for path in ('/api/download/' + quote(number), item['TextUrl']):
            if request(path) != body:
                raise ValueError('live OCR full text/download differs from the indexed text')
        if json_request('/api/preview/' + quote(number))['text'] != body.decode('utf-8'):
            raise ValueError('live OCR preview differs from the complete text')
        normalized = unicodedata.normalize('NFKC', body.decode('utf-8')).casefold()
        phrase = next((match[0] for match in re.finditer(r'[\u4e00-\u9fff]{4,12}|[a-z]{5,12}', normalized)
                       if all(match[0] not in unicodedata.normalize('NFKC', record['display_name']).casefold()
                              for record in loaded['records'])), None)
        if phrase:
            expected = {doc_id for doc_id, target in loaded['text_paths'].items()
                        if phrase in unicodedata.normalize('NFKC', target.read_text(encoding='utf-8')).casefold()}
            found = all_results({'q': phrase, 'sources': ['OCR'], 'fulltext': True, 'exact': True, 'search_paths': False})
            if {item['doc_id'] for item in found} != expected:
                raise ValueError('live exact full-text IDs/totals differ from complete OCR text')
            queries.append({'q': phrase, 'total': len(expected)})
        checked.append(number)
    if json_request('/api/index-status').get('ocr_release') != generation:
        raise ValueError('live OCR generation changed during acceptance')
    return {'generation': generation, 'documents': len(results), 'complete_id_set_verified': True,
            'full_text_preview_and_download_verified': checked, 'exact_fulltext_queries': queries, 'remote_writes': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True,
                        help='Exact base data/search_data.json.gz archived with the release')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.root, json.loads(args.receipt.read_text()), metadata=args.metadata)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(contract.canonical(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
