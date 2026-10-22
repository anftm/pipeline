#!/usr/bin/env python3
"""Read-only production identity, metadata IDs, selective search and TXT checks.

This pins full production generation/metadata, but does not pretend selected API
queries or first/last pages enumerate every possible search combination.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
from pathlib import Path
import time
import unicodedata
from urllib.parse import quote
import zipfile

try:
    from . import ccrd_ocr_release as contract, verify_ccrd_ocr_live as ocr_live
    from .publish_ccrd_newspapers import SpaceCommitter
except ImportError:
    import ccrd_ocr_release as contract
    import verify_ccrd_ocr_live as ocr_live
    from publish_ccrd_newspapers import SpaceCommitter


def verify(root, receipt, *, wait_seconds=1800, space=None):
    space = space or SpaceCommitter()
    generation = receipt['generation']
    manifest = json.loads((root / f'generations/{generation}/manifest.json').read_text())
    metadata_ref = next(ref for ref in manifest['metadata'] if ref['path'].endswith('/data/search_data.json.gz'))
    raw = (root / metadata_ref['path']).read_bytes()
    if contract.digest(raw) != manifest['base_metadata_sha256']:
        raise ValueError('local full publication metadata differs from its manifest')
    expected = json.loads(gzip.decompress(raw))
    expected_ids = {record['doc_id'] for record in expected['records']}
    deadline = time.monotonic() + wait_seconds
    while True:
        status = ocr_live.json_request('/api/index-status')
        bucket = status.get('bucket') or {}
        if (status.get('ready') is True and status.get('ocr_release') == receipt['OCR_generation']
                and bucket.get('source') == 'space-activation' and bucket.get('generation') == generation
                and bucket.get('base_metadata_sha256') == manifest['base_metadata_sha256']
                and space.runtime_revision() == receipt['space_revision']):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError('production has not loaded the exact pinned four-source/OCR generation')
        time.sleep(15)
    if space.head() != receipt['space_revision']:
        raise ValueError('Space repository advanced before exact metadata acceptance')
    # The runtime deliberately does not expose compressed metadata as an API.
    # Verify the full bytes from the exact repository revision that the HF API
    # confirms is currently RUNNING, not a nonexistent SPA-fallback data URL.
    live_raw = space.read('data/search_data.json.gz', receipt['space_revision'])
    if contract.digest(live_raw) != manifest['base_metadata_sha256']:
        raise ValueError('production compressed base metadata SHA differs')
    live_metadata = json.loads(gzip.decompress(live_raw))
    if {record['doc_id'] for record in live_metadata['records']} != expected_ids or live_metadata != expected:
        raise ValueError('production complete metadata ID/content set differs')
    sources = {item['slug']: item['count'] for item in ocr_live.json_request('/api/sources')}
    for source, details in manifest['databases'].items():
        if sources.get(source) != details['documents']:
            raise ValueError('production base source count differs')
    checked, query_checks, selected = [], [], []
    body_by_id = {}
    import sqlite3
    for source in ['RMRB', 'PLA']:
        details = manifest['databases'][source]
        records = [record for record in expected['records'] if record['source'] == source]
        total = len(records)
        for page in sorted({1, (total + 199) // 200}):
            response = ocr_live.json_request('/api/search/' + source,
                {'q': '', 'page_size': 200, 'page': page, 'fulltext': True})
            if response.get('indexing') is not False or response['total'] != total:
                raise ValueError('production newspaper paging is not ready or has a different total')
            wanted = [row['doc_id'] for row in records[(page - 1) * 200:page * 200]]
            if [row['doc_id'] for row in response['results']] != wanted:
                raise ValueError('production newspaper first/last page IDs differ')
        with sqlite3.connect(f'file:{root / details["path"]}?mode=ro&immutable=1', uri=True) as db:
            numbers = [int(records[0]['doc_id'].split(':')[1]), int(records[-1]['doc_id'].split(':')[1]),
                       db.execute('SELECT number FROM source_files ORDER BY bytes DESC LIMIT 1').fetchone()[0]]
            for number in dict.fromkeys(numbers):
                doc_id = f'{source}:{number}'
                rec = next(row for row in records if row['doc_id'] == doc_id)
                body = db.execute('SELECT body FROM texts WHERE doc_id=?', (doc_id,)).fetchone()[0].encode('utf-8')
                if len(body) != rec['size'] or contract.digest(body) != rec['text_sha256']:
                    raise ValueError('local acceptance TXT differs from the admitted metadata')
                for path in ['/api/download/' + doc_id, '/txt/' + source + '/' + quote(rec['storage_rel_path'], safe='/')]:
                    if ocr_live.request(path) != body:
                        raise ValueError('production original newspaper TXT bytes differ')
                if ocr_live.json_request('/api/preview/' + doc_id)['text'].encode() != body:
                    raise ValueError('production newspaper preview bytes differ')
                checked.append(doc_id)
                selected.append(doc_id)
                body_by_id[doc_id] = body
            for phrase in ['手机', '手。机']:
                normalized = unicodedata.normalize('NFKC', phrase).casefold()
                wanted = {f'{source}:{number}' for number, in db.execute(
                    'SELECT rowid FROM content_fts WHERE instr(content,?)>0', (normalized,))}
                for rec in records:
                    text = ' '.join([rec['display_name'], rec['source_name'], rec['display_rel_path']])
                    if normalized in unicodedata.normalize('NFKC', text).casefold():
                        wanted.add(rec['doc_id'])
                results = ocr_live.all_results({'sources': [source], 'q': phrase, 'exact': True, 'fulltext': True})
                if {row['doc_id'] for row in results} != wanted:
                    raise ValueError('production selected complete newspaper search IDs differ')
                query_checks.append({'source': source, 'query': phrase, 'total': len(wanted)})
    prepared = ocr_live.json_request('/api/zip-prepare', {'doc_ids': selected})
    if prepared['file_count'] != len(selected):
        raise ValueError('production ZIP prepare lost selected newspaper IDs')
    with zipfile.ZipFile(io.BytesIO(ocr_live.request(prepared['download_url']))) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(selected):
            raise ValueError('production mixed newspaper ZIP is invalid')
        for member in archive.namelist():
            doc_id = member.rsplit('[', 1)[1].split(']', 1)[0].replace('_', ':')
            if archive.read(member) != body_by_id[doc_id]:
                raise ValueError('production mixed newspaper ZIP TXT differs')
    # OCR verifier checks every live OCR ID; body/phrase probes can be bounded to
    # unchanged first/last exports because this rebind changes no OCR TXT.
    ocr_receipt = {'generation': receipt['OCR_generation'],
                   'release': receipt['OCR_publication']['release']}
    rebase_root = root / 'ocr-rebase'
    ocr_manifest = json.loads((rebase_root / ocr_receipt['release']['path']).read_text())
    exports = [json.loads((rebase_root / ref['path']).read_text()) for ref in ocr_manifest['exports']]
    ocr_receipt['processed'] = [{'doc_id': row['doc_id']} for row in [exports[0], exports[-1]]]
    ocr_result = ocr_live.verify(rebase_root, ocr_receipt, metadata=root / metadata_ref['path'], wait_seconds=0)
    final = ocr_live.json_request('/api/index-status')
    if (final.get('ocr_release') != receipt['OCR_generation'] or (final.get('bucket') or {}).get('generation') != generation
            or space.head() != receipt['space_revision'] or space.runtime_revision() != receipt['space_revision']):
        raise ValueError('production generation changed during acceptance')
    return {'kind': 'ccrd-newspaper-live-acceptance', 'generation': generation,
            'base_metadata_sha256': manifest['base_metadata_sha256'], 'full_base_metadata_ids': len(expected_ids),
            'first_last_API_pages_checked': True, 'selected_original_TXT_ids': checked,
            'selective_complete_search_ID_sets': query_checks, 'mixed_zip_verified': True,
            'OCR': ocr_result, 'remote_writes': 0,
            'scope': 'full pinned base/metadata plus selected API/body queries; no exhaustive newspaper API pagination'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.root, json.loads(args.receipt.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(contract.canonical(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
