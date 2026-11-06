"""Read-only current CCRD target download/preview and source-count check.

GET only, fixed public endpoints, no credentials or persistent files. The
actual served target bytes must match an independently observed immutable
Space file and four-source manifest. API state is sampled before/after but is
not a writer lock, proof of no ABA, or exhaustive full-text acceptance.

Run explicitly; a successful result is not authorization to update anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from urllib.parse import quote

try:
    from . import check_ccrd_txt_only as base
except ImportError:
    import check_ccrd_txt_only as base


HOST = 'https://vomebook-search.hf.space'
TARGET_ID = 'CW:0'
MAX_PREVIEW_BYTES = 8 * base.MAX_TXT_BYTES + base.MAX_METADATA_BYTES


def _status(read, manifest):
    state = base.json_object(read(HOST + '/api/index-status', limit=base.MAX_METADATA_BYTES), 'index status')
    base._index_status(state, manifest)


def check(*, read=base.read_url):
    observation = base.observe(read=read)
    report = observation['report']
    manifest = base.json_object(observation['manifest_bytes'], 'observed manifest')
    expected = observation['site_txt']
    if len(expected) > base.MAX_TXT_BYTES or hashlib.sha256(expected).hexdigest() != report['site_txt_sha256']:
        raise ValueError('observed Space TXT does not match its manifest')
    counts = {}
    for source, details in manifest['databases'].items():
        if not isinstance(details, dict) or type(details.get('documents')) is not int or details['documents'] < 1:
            raise ValueError('manifest lacks exact source counts')
        counts[source] = details['documents']

    _status(read, manifest)
    sources_raw = read(HOST + '/api/sources', limit=base.MAX_METADATA_BYTES)
    # Reuse the strict parser while accepting this endpoint's JSON array.
    sources = base.json_object(b'{"sources":' + sources_raw + b'}', 'running sources')['sources']
    if not isinstance(sources, list) or len(sources) != len(counts):
        raise ValueError('running sources do not match the four-source manifest')
    seen = {}
    for item in sources:
        if (not isinstance(item, dict) or not isinstance(item.get('slug'), str)
                or item['slug'] in seen or type(item.get('count')) is not int):
            raise ValueError('invalid or duplicate running source')
        seen[item['slug']] = item['count']
    if seen != counts:
        raise ValueError('running source counts do not match the activated manifest')

    encoded_id = quote(TARGET_ID, safe='')
    downloaded = read(HOST + '/api/download/' + encoded_id, limit=base.MAX_TXT_BYTES)
    if downloaded != expected:
        raise ValueError('running target download differs from the pinned Space TXT')
    preview = base.json_object(read(HOST + '/api/preview/' + encoded_id, limit=MAX_PREVIEW_BYTES), 'target preview')
    if (preview.get('doc_id') != TARGET_ID or preview.get('source') != 'CW'
            or not isinstance(preview.get('text'), str) or preview['text'].encode('utf-8') != expected):
        raise ValueError('running target preview differs from the pinned Space TXT')
    folder = base.json_object(read(HOST + '/api/folders/CW/contents', limit=base.MAX_METADATA_BYTES), 'CW folder')
    files = folder.get('files')
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ValueError('running CW folder is invalid')
    target = [item for item in files if item.get('doc_id') == TARGET_ID]
    if len(target) != 1 or type(target[0].get('size')) is not int or target[0]['size'] != len(expected):
        raise ValueError('running CW folder target is missing, duplicated or has the wrong size')
    _status(read, manifest)
    state = base.json_object(read(f'https://huggingface.co/api/spaces/{base.SPACE}',
                                  limit=base.MAX_METADATA_BYTES), 'Space status reread')
    if base._head(state) != report['space_revision']:
        raise ValueError('Space changed during live target check')
    return {'read_only': True, 'space_revision': report['space_revision'],
            'index_generation': report['index_generation'], 'index_manifest_sha256': report['index_manifest_sha256'],
            'target_sha256': report['site_txt_sha256'], 'upstream_matches_site': report['upstream_matches_site'],
            'target_download_matches': True, 'target_preview_matches': True, 'folder_target_size_matches': True,
            'four_source_counts_match': True, 'fulltext_query_verified': False, 'all_index_bytes_verified': False,
            'external_writers_fenced': False, 'aba_excluded': False, 'remote_writes': 0,
            'publication_ready': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = check()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result['upstream_matches_site'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
