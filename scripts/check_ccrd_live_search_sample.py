"""Optional bounded, read-only CW search query against an observed TXT.

The fixed public Space search endpoint uses POST as a read-only query (not a
publication endpoint). At most four bounded pages are inspected, with a strict
response-size limit. Positive queries must be literal normalized substrings of
the captured TXT; negative queries must be absent from it. If the total is
larger than the bounded pages, absence of CW:0 is never treated as success.

This is selective runtime evidence, not exhaustive index verification, writer
isolation, deployment authority or a persistent receipt. It performs no file,
repository, bucket, or user-data writes. Only explicit CLI invocation queries
the network; no workflow calls this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from . import check_ccrd_live_target as target
    from . import check_ccrd_txt_only as base
except ImportError:
    import check_ccrd_live_target as target
    import check_ccrd_txt_only as base


URL = target.HOST + '/api/search/CW'
PAGE_SIZE = 200
MAX_PAGES = 4
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('search query redirect refused')


def post_search(payload: dict) -> bytes:
    """Call only the fixed read-only search endpoint, with no credentials."""
    parsed = urlparse(URL)
    if (parsed.scheme != 'https' or parsed.hostname != 'vomebook-search.hf.space'
            or parsed.port is not None or parsed.username or parsed.password or parsed.path != '/api/search/CW'):
        raise ValueError('search endpoint is outside the fixed read-only scope')
    data = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    request = Request(URL, data=data, method='POST', headers={'Content-Type': 'application/json',
                                                               'Accept': 'application/json',
                                                               'User-Agent': base.USER_AGENT,
                                                               'Cache-Control': 'no-cache'})
    try:
        with build_opener(NoRedirects()).open(request, timeout=90) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        raise RuntimeError(f'read-only search failed: HTTP {status}') from None
    except (URLError, OSError, TimeoutError):
        raise RuntimeError('read-only search failed: network error') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError('search response exceeds the configured byte limit')
    return raw


def check(*, observation: dict, phrase: str, expect_present: bool, read=base.read_url, post=post_search) -> dict:
    if not isinstance(observation, dict) or not isinstance(observation.get('report'), dict):
        raise ValueError('a read-only Space observation is required')
    report, site_txt = observation['report'], observation.get('site_txt')
    manifest_raw = observation.get('manifest_bytes')
    if (type(site_txt) is not bytes or not site_txt or len(site_txt) > base.MAX_TXT_BYTES
            or type(manifest_raw) is not bytes or len(manifest_raw) > base.MAX_METADATA_BYTES
            or hashlib.sha256(site_txt).hexdigest() != report.get('site_txt_sha256')):
        raise ValueError('observed TXT or manifest is missing, oversize or differs from its pinned SHA')
    if (not isinstance(phrase, str) or not 2 <= len(phrase) <= 128 or phrase != phrase.strip()
            or any(ord(char) < 32 for char in phrase) or '*' in phrase or '?' in phrase
            or type(expect_present) is not bool):
        raise ValueError('search phrase must be a bounded literal')
    normalized = lambda text: unicodedata.normalize('NFKC', text).casefold().replace('\0', ' ')
    contains = normalized(phrase) in normalized(site_txt.decode('utf-8'))
    if contains != expect_present:
        raise ValueError('expected phrase presence differs from the captured target TXT')
    manifest = base.json_object(manifest_raw, 'observed index manifest')
    if (hashlib.sha256(manifest_raw).hexdigest() != report.get('index_manifest_sha256')
            or manifest.get('generation') != report.get('index_generation')):
        raise ValueError('observed manifest identity differs from the report')
    target._status(read, manifest)
    total = generation = None
    ids = set()
    target_found, pages = False, 0
    for page in range(1, MAX_PAGES + 1):
        body = {'q': phrase, 'page': page, 'page_size': PAGE_SIZE, 'exact': True,
                'search_paths': False, 'fulltext': True}
        result = base.json_object(post(body), 'search response')
        actual_total, rows, current_generation = result.get('total'), result.get('results'), result.get('index_generation')
        if (result.get('indexing') is not False or type(actual_total) is not int or actual_total < 0
                or result.get('page') != page or result.get('page_size') != PAGE_SIZE
                or type(current_generation) is not int or current_generation < 0 or not isinstance(rows, list)
                or len(rows) != min(PAGE_SIZE, max(0, actual_total - (page - 1) * PAGE_SIZE))):
            raise ValueError('search response is incomplete or indexing')
        if total is None:
            total, generation = actual_total, current_generation
        elif (actual_total, current_generation) != (total, generation):
            raise ValueError('search total or runtime index changed during paging')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('doc_id'), str) or row['doc_id'] in ids:
                raise ValueError('search results contain invalid or duplicate document IDs')
            ids.add(row['doc_id'])
        pages = page
        target_found = target.TARGET_ID in ids
        if target_found or len(ids) == total:
            break
    complete = len(ids) == total
    if not target_found and not complete:
        raise ValueError('target search absence cannot be established within the page bound')
    if target_found != expect_present:
        raise ValueError('live search target membership differs from the captured TXT')
    target._status(read, manifest)
    state = base.json_object(read(f'https://huggingface.co/api/spaces/{base.SPACE}',
                                  limit=base.MAX_METADATA_BYTES), 'Space status reread')
    if base._head(state) != report['space_revision']:
        raise ValueError('Space changed during the bounded search query')
    return {'read_only_query': True, 'space_revision': report['space_revision'],
            'index_generation': report['index_generation'], 'target_sha256': report['site_txt_sha256'],
            'phrase_sha256': hashlib.sha256(phrase.encode('utf-8')).hexdigest(),
            'expected_target_match': expect_present, 'target_match_observed': target_found,
            'pages_checked': pages, 'total_results': total, 'all_results_checked': complete,
            'runtime_index_generation': generation, 'full_index_semantics_verified': False,
            'external_writers_fenced': False, 'remote_writes': 0, 'publication_ready': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phrase', help='literal phrase to check without printing it in the result')
    parser.add_argument('--expect-absent', action='store_true')
    args = parser.parse_args()
    observation = base.observe()
    print(json.dumps(check(observation=observation, phrase=args.phrase,
                           expect_present=not args.expect_absent), ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
