#!/usr/bin/env python3
"""Prepare a local four-source publication plan; no uploads or pointer writes.

Reuse existing pinned CCRD/CW databases and hash-address newspaper databases.
Only the canonical publisher may eventually promote the resulting manifest,
together with an OCR rebase and exact Space metadata/activation commit.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

try:
    from . import ccrd_lifecycle as lifecycle, shared
    from .ccrd_index_contract import require_legacy_manifest
except ImportError:
    import ccrd_lifecycle as lifecycle
    import shared
    from ccrd_index_contract import require_legacy_manifest


PROFILE = 'newspaper-hybrid-texts-v1'
NEWSPAPERS = {'RMRB', 'PLA'}


def file_identity(path):
    digest, size = shared.hash_file(path)
    return {'sha256': digest, 'bytes': size}


def checked_file(root, ref):
    relative = ref.get('path')
    if not isinstance(relative, str):
        raise ValueError('local candidate reference lacks path')
    lifecycle.safe_path(relative)
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError('local candidate path escapes the package or is missing')
    if file_identity(path) != {key: ref[key] for key in ['sha256', 'bytes']}:
        raise ValueError('local candidate reference checksum mismatch')
    return path


def inspect_newspaper(path, source, ids, receipt):
    identity = receipt['identity']
    if (identity.get('source') != source or identity.get('profile') != PROFILE
            or identity.get('documents') != len(ids) or receipt['documents'] != len(ids)):
        raise ValueError('newspaper source receipt differs from merged metadata')
    with sqlite3.connect(f'file:{path.resolve()}?mode=ro&immutable=1', uri=True) as db:
        metadata = dict(db.execute('SELECT key,value FROM metadata'))
        phase = json.loads(db.execute("SELECT value FROM build_state WHERE key='phase'").fetchone()[0])
        saved_identity = json.loads(db.execute("SELECT value FROM build_state WHERE key='identity'").fetchone()[0])
        if (metadata.get('tokenizer_version') != lifecycle.TOKENIZER or metadata.get('index_profile') != PROFILE
                or metadata.get('build_complete') != 'true' or phase != 'complete' or saved_identity != identity):
            raise ValueError('newspaper database is not a verified completed source')
        if any(row[0] not in ids for row in db.execute('SELECT doc_id FROM documents')):
            raise ValueError('newspaper DB IDs differ from merged metadata')
        if (any(row[0] not in ids for row in db.execute('SELECT doc_id FROM texts'))
                or any(f'{source}:{row[0]}' not in ids for row in db.execute('SELECT rowid FROM content_fts'))):
            raise ValueError('newspaper text/FTS IDs differ from merged metadata')
        counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                  for table in ['documents', 'texts', 'content_fts']}
        if set(counts.values()) != {len(ids)}:
            raise ValueError('newspaper complete document counts differ')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('newspaper SQLite integrity check failed')
        postings = db.execute('SELECT count(*) FROM postings').fetchone()[0]
    return {'source': source, 'documents': len(ids), 'fts_rows': len(ids), 'text_rows': len(ids),
            'postings': postings, 'index_profile': PROFILE, **file_identity(path)}


def prepare(newspapers, merged, base_manifest_path, approved_base_sha, *, allow_sample=False):
    newspapers, merged = newspapers.resolve(), merged.resolve()
    lifecycle.sha(approved_base_sha)
    base = json.loads(base_manifest_path.read_text())
    require_legacy_manifest(base)
    if (base.get('format') != 1 or base.get('tokenizer_version') != lifecycle.TOKENIZER
            or set(base.get('databases', {})) != {'CCRD', 'CW'}):
        raise ValueError('first newspaper rollout must pin the exact existing two-source index manifest')
    lifecycle.references(base, 'current.json')
    merge = json.loads((merged / 'merge-receipt.json').read_text())
    if (merge.get('kind') != 'ccrd-four-source-local-metadata'
            or merge.get('previous_base_metadata_sha256') != approved_base_sha
            or merge.get('OCR_rebase_required') is not True or merge.get('OCR_activation_copied') is not False):
        raise ValueError('four-source metadata lacks the approved legacy base/OCR rebase requirement')
    metadata_paths = [checked_file(merged, ref) for ref in merge['files']]
    base_metadata = merged / 'data/search_data.json.gz'
    if shared.hash_file(base_metadata)[0] != merge['base_metadata_sha256']:
        raise ValueError('merged compressed metadata SHA mismatch')
    payload = json.loads(gzip.decompress(base_metadata.read_bytes()))
    ids, count = {}, 0
    for record in payload['records']:
        source = record['source']
        doc_id = record['doc_id']
        if doc_id in ids.setdefault(source, set()):
            raise ValueError('duplicate merged document ID')
        ids[source].add(doc_id)
        count += 1
    declared = {source['slug']: source['count'] for source in payload['sources']}
    if ({source: len(values) for source, values in ids.items()} != declared
            or set(ids) != {'CCRD', 'CW', *NEWSPAPERS} or count != merge['documents']):
        raise ValueError('merged four-source IDs/counts are inconsistent')
    for source in ['CCRD', 'CW']:
        if len(ids[source]) != base['databases'][source]['documents']:
            raise ValueError('legacy metadata counts differ from the pinned existing database')
    del payload
    package = json.loads((newspapers / 'package-receipt.json').read_text())
    if (package.get('kind') != 'newspaper-local-package'
            or shared.hash_file(newspapers / 'package-receipt.json')[0] != merge['newspaper_package_receipt_sha256']):
        raise ValueError('merged candidate was made from a different newspaper package')
    archive_manifest = json.loads((newspapers / 'archives/manifest.json').read_text())
    if archive_manifest['archives'] != package['archives'] or not package['archives']:
        raise ValueError('newspaper package lacks its checked yearly TXT archives')
    newspapers_db, databases, complete_audits = {}, dict(base['databases']), True
    package_databases = {ref['source']: ref for ref in package.get('databases', [])}
    for source in sorted(NEWSPAPERS):
        path = newspapers / 'data/fulltext' / (source + '.sqlite3')
        receipt = json.loads((newspapers / 'receipts' / (source + '.json')).read_text())
        if file_identity(path) != {key: receipt[key] for key in ['sha256', 'bytes']}:
            raise ValueError('newspaper database receipt checksum mismatch')
        expected = package_databases.get(source, {})
        if any(expected.get(key) != receipt[key] for key in ['documents', 'bytes', 'sha256']):
            raise ValueError('newspaper database differs from the packaged metadata generation')
        details = inspect_newspaper(path, source, ids[source], receipt)
        details['path'] = f'indexes/newspapers/{source}/{details["sha256"]}.sqlite3'
        databases[source] = details
        newspapers_db[source] = path
        complete_audits &= receipt['identity'].get('audit_documents') == len(ids[source])
    if not complete_audits and not allow_sample:
        raise ValueError('sample newspapers cannot be prepared for full publication; --allow-sample is local only')
    archives = []
    archive_paths = []
    for ref in package['archives']:
        local = checked_file(newspapers, ref)
        source, year = ref['source'], int(ref['year'])
        archives.append({'source': source, 'year': year, 'documents': ref['identity']['documents'],
                         'local_relative_path': ref['path'], **file_identity(local)})
        archive_paths.append(local)
    identity = {'kind': 'ccrd-four-source-generation', 'base_metadata_sha256': merge['base_metadata_sha256'],
                'previous_index_manifest_sha256': shared.hash_file(base_manifest_path)[0],
                'databases': databases, 'metadata': merge['files'], 'archives': archives}
    generation = 'ccrd-four-' + hashlib.sha256(lifecycle.canonical(identity)).hexdigest()
    prefix = 'generations/' + generation
    upload, metadata_refs, archive_refs = [], [], []
    for path in metadata_paths:
        relative = path.relative_to(merged).as_posix()
        ref = {'bucket': lifecycle.BUCKET, 'path': prefix + '/' + relative, 'role': 'runtime', **file_identity(path)}
        metadata_refs.append(ref)
        upload.append({'local_path': str(path), **ref})
    for local, entry in zip(archive_paths, archives):
        ref = {'bucket': lifecycle.BUCKET, 'path': prefix + '/' + entry['local_relative_path'], 'role': 'runtime',
               **{key: entry[key] for key in ['source', 'year', 'documents', 'sha256', 'bytes']}}
        archive_refs.append(ref)
        upload.append({'local_path': str(local), **ref})
    for source, path in newspapers_db.items():
        upload.append({'local_path': str(path), 'bucket': lifecycle.BUCKET, 'role': 'runtime',
                       **{key: databases[source][key] for key in ['path', 'sha256', 'bytes']}})
    manifest = {'format': 2, 'kind': 'ccrd-four-source-generation', 'generation': generation,
                'tokenizer_version': lifecycle.TOKENIZER, 'base_metadata_sha256': merge['base_metadata_sha256'],
                'previous_base_metadata_sha256': approved_base_sha,
                'previous_index_manifest_sha256': shared.hash_file(base_manifest_path)[0],
                'databases': databases, 'metadata': metadata_refs, 'archives': archive_refs,
                'builder': base.get('builder'), 'source_archive': base.get('source_archive'),
                'source_overrides': base.get('source_overrides'), 'corpus_migration': base.get('corpus_migration')}
    lifecycle.references(manifest, 'current.json')
    plan = {'version': 1, 'kind': 'ccrd-four-source-local-publication-plan', 'generation': generation,
            'manifest': manifest, 'upload_files': upload,
            'reuse_existing_indexes': {source: base['databases'][source] for source in ['CCRD', 'CW']},
            'complete_newspaper_audits': complete_audits, 'sample_only': not complete_audits,
            'documents': declared, 'remote_writes': 0, 'publication_ready': False,
            'remaining_gates': ['canonical serialized publication and full immutable readback',
                'OCR live-export rebase with exact parent under the same writer',
                'one Space commit for runtime/metadata/rebased OCR activation',
                'approved OCR consumer pin update and exact production acceptance']}
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--newspapers', type=Path, required=True)
    parser.add_argument('--merged', type=Path, required=True)
    parser.add_argument('--base-manifest', type=Path, required=True)
    parser.add_argument('--approved-base-sha', required=True)
    parser.add_argument('--allow-sample', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = prepare(args.newspapers, args.merged, args.base_manifest, args.approved_base_sha, allow_sample=args.allow_sample)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + '.tmp')
    temporary.write_bytes(lifecycle.canonical(result))
    temporary.replace(args.output)
    print(json.dumps({key: result[key] for key in ['generation', 'documents', 'sample_only', 'publication_ready', 'remote_writes']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
