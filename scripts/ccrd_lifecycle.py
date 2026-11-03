"""Qualified OCR-library roots shared by publication and four-bucket GC reports."""
from __future__ import annotations

import re

try:
    from .ccrd_ocr_release import BUCKET, TOKENIZER, canonical, digest, safe_path, sha
    from .shared import READER_ASSETS_BUCKET, PDF_PAGES_BUCKET, PDF_OCR_INPUT_BUCKET
except ImportError:
    from ccrd_ocr_release import BUCKET, TOKENIZER, canonical, digest, safe_path, sha
    from shared import READER_ASSETS_BUCKET, PDF_PAGES_BUCKET, PDF_OCR_INPUT_BUCKET


BUCKETS = (READER_ASSETS_BUCKET, PDF_PAGES_BUCKET, PDF_OCR_INPUT_BUCKET, BUCKET)
MANAGED_PREFIXES = ('text/objects/', 'text/exports/', 'text/snapshots/', 'text/packs/',
                    'indexes/ocr/', 'releases/ocr-', 'staging/ocr/')
CONTROL_KINDS = {'ccrd-ocr-current', 'ccrd-ocr-intent', 'ccrd-ocr-consumer', 'ccrd-ocr-retention', 'ccrd-ocr-id-registry', 'ccrd-ocr-automation', 'ccrd-newspaper-rollout'}


def is_root(path: str) -> bool:
    return path == 'current.json' or path.startswith('control/') and path.endswith('.json')


def is_document(path: str) -> bool:
    return (is_root(path) or path.endswith('/manifest.json') and path.startswith(('text/', 'releases/ocr-'))
            or bool(re.fullmatch(r'generations/ccrd-four-[0-9a-f]{64}/manifest\.json', path)))


def resource(ref: dict) -> dict:
    if (not isinstance(ref, dict) or ref.get('bucket') not in BUCKETS
            or type(ref.get('bytes')) is not int or ref['bytes'] < 1
            or ref.get('role') not in {'runtime', 'provenance', 'review'}):
        raise ValueError('invalid qualified CCRD lifecycle resource')
    safe_path(ref.get('path')); sha(ref.get('sha256'))
    return ref


def references(payload: dict, path: str) -> list[dict]:
    """Return only declared storage references, never human-readable source paths."""
    safe_path(path)
    if not isinstance(payload, dict):
        raise ValueError('CCRD lifecycle document must be an object')
    kind = payload.get('kind')
    if (path == 'current.json' or re.fullmatch(r'generations/ccrd-four-[0-9a-f]{64}/manifest\.json', path)) and payload.get('format') == 2:
        databases = payload.get('databases')
        metadata = payload.get('metadata')
        archives = payload.get('archives')
        if (payload.get('kind') != 'ccrd-four-source-generation'
                or payload.get('tokenizer_version') != TOKENIZER
                or not isinstance(databases, dict) or set(databases) != {'CCRD', 'CW', 'RMRB', 'PLA'}
                or not isinstance(metadata, list) or not metadata
                or not isinstance(archives, list) or not archives):
            raise ValueError('unknown four-source CCRD pointer')
        sha(payload.get('base_metadata_sha256'))
        generation = payload.get('generation')
        if not isinstance(generation, str) or not re.fullmatch(r'ccrd-[a-zA-Z0-9._-]+', generation):
            raise ValueError('invalid four-source generation name')
        if path != 'current.json' and path != f'generations/{generation}/manifest.json':
            raise ValueError('immutable four-source manifest path differs from its generation')
        refs = []
        for name, item in databases.items():
            if (not isinstance(item, dict)
                    or type(item.get('documents')) is not int or item['documents'] < 1
                    or item.get('fts_rows') != item['documents']):
                raise ValueError('invalid four-source CCRD database identity')
            legacy_path = re.fullmatch(r'generations/ccrd-[^/]+/' + name + r'\.sqlite3', item.get('path', ''))
            newspaper_path = (name in {'RMRB', 'PLA'}
                and item.get('path') == f'indexes/newspapers/{name}/{item.get("sha256")}.sqlite3')
            if not legacy_path and not newspaper_path:
                raise ValueError('invalid immutable four-source database path')
            if name in {'RMRB', 'PLA'} and (item.get('source') != name
                    or item.get('index_profile') != 'newspaper-hybrid-texts-v1'
                    or item.get('text_rows') != item['documents']):
                raise ValueError('four-source newspaper database lacks complete TXT')
            refs.append(resource({'bucket': BUCKET, 'path': item['path'], 'sha256': item.get('sha256'),
                                  'bytes': item.get('bytes'), 'role': 'runtime'}))
        prefix = f'generations/{generation}'
        paths = set()
        for ref in [*metadata, *archives]:
            resource(ref)
            if ref['bucket'] != BUCKET or not ref['path'].startswith(prefix + '/') or ref['path'] in paths:
                raise ValueError('four-source metadata/archive reference outside its generation')
            paths.add(ref['path'])
            refs.append(ref)
        if not any(ref['path'] == prefix + '/data/search_data.json.gz'
                   and ref['sha256'] == payload['base_metadata_sha256'] for ref in metadata):
            raise ValueError('four-source metadata does not retain its pinned base')
        for name in ['RMRB', 'PLA']:
            selected = [ref for ref in archives if ref.get('source') == name]
            years = set()
            minimum = 1946 if name == 'RMRB' else 1956
            for ref in selected:
                year = ref.get('year')
                if (type(year) is not int or not minimum <= year <= 1976 or year in years
                        or type(ref.get('documents')) is not int or ref['documents'] < 1
                        or ref['path'] != f'{prefix}/archives/{name}/{name}-{year}.tar.gz'):
                    raise ValueError('invalid newspaper yearly archive identity')
                years.add(year)
            if sum(ref['documents'] for ref in selected) != databases[name]['documents']:
                raise ValueError('newspaper archives do not cover the complete document count')
        if any(ref.get('source') not in {'RMRB', 'PLA'} for ref in archives):
            raise ValueError('unknown newspaper archive source')
        return refs
    if path == 'current.json' and payload.get('format') == 1:
        if payload.get('tokenizer_version') != TOKENIZER or set(payload.get('databases', {})) != {'CCRD', 'CW'}:
            raise ValueError('unknown legacy CCRD pointer')
        refs = []
        for name, item in payload['databases'].items():
            if (not isinstance(item, dict) or not re.fullmatch(r'generations/ccrd-[^/]+/' + name + r'\.sqlite3', item.get('path', ''))
                    or type(item.get('documents')) is not int or item['documents'] < 1
                    or item.get('fts_rows') != item['documents']):
                raise ValueError('invalid legacy CCRD database identity')
            refs.append(resource({'bucket': BUCKET, 'path': item['path'], 'sha256': item.get('sha256'),
                                  'bytes': item.get('bytes'), 'role': 'runtime'}))
        return refs
    if kind in CONTROL_KINDS and path.startswith('control/'):
        if payload.get('version') != 1 or not isinstance(payload.get('resources'), list):
            raise ValueError('invalid CCRD control root')
        if kind == 'ccrd-newspaper-rollout':
            generation = payload.get('generation')
            if (not isinstance(generation, str) or not re.fullmatch(r'ccrd-four-[0-9a-f]{64}', generation)
                    or payload.get('status') not in {'staging', 'activated', 'committed'}
                    or not re.fullmatch(r'[0-9a-f]{40}', str(payload.get('previous_space_revision', '')))):
                raise ValueError('invalid protected newspaper rollout identity')
            sha(payload.get('base_metadata_sha256'))
            previous = payload.get('previous_ocr_pointer')
            if not isinstance(previous, dict) or previous.get('kind') != 'ccrd-ocr-current':
                raise ValueError('newspaper rollout lacks its complete previous OCR pointer')
            references(previous, 'control/ocr-current.json')
            activation = payload.get('index_activation', {})
            if (activation.get('kind') != 'ccrd-index-activation' or activation.get('version') != 1
                    or activation.get('bucket') != BUCKET
                    or activation.get('path') != f'generations/{generation}/manifest.json'):
                raise ValueError('newspaper rollout lacks its exact index activation')
            sha(activation.get('sha256'))
            ocr = payload.get('ocr_activation', {})
            if (ocr.get('kind') != 'ccrd-ocr-activation' or ocr.get('version') != 1
                    or ocr.get('bucket') != BUCKET
                    or not re.fullmatch(r'releases/ocr-[0-9a-f]{64}/manifest\.json', str(ocr.get('path', '')))):
                raise ValueError('newspaper rollout lacks its exact OCR activation')
            sha(ocr.get('sha256'))
            resources = payload['resources']
            required = [activation]
            if payload['status'] != 'committed':
                required.extend([ocr, previous['release']])
            for item in required:
                if not any(all(ref.get(key) == item.get(key) for key in ['bucket', 'path', 'sha256']) for ref in resources):
                    raise ValueError('newspaper rollout activation/rollback is not retained')
            if payload['status'] in {'activated', 'committed'} and not re.fullmatch(r'[0-9a-f]{40}', str(payload.get('space_revision', ''))):
                raise ValueError('committed newspaper rollout lacks an immutable Space revision')
        if kind == 'ccrd-ocr-current':
            sha(payload.get('generation'))
            resource(payload.get('release'))
            if payload['release'] not in payload['resources']:
                raise ValueError('current release is not retained by its root')
        if kind == 'ccrd-ocr-id-registry':
            ids = payload.get('ids')
            if (not isinstance(ids, dict) or any(not re.fullmatch(r'[0-9a-f]{64}', key)
                    or not isinstance(value, dict) or not re.fullmatch(r'OCR:[1-9][0-9]*', str(value.get('doc_id', '')))
                    for key, value in ids.items()) or len({item['doc_id'] for item in ids.values()}) != len(ids)):
                raise ValueError('invalid stable OCR ID registry')
        if kind == 'ccrd-ocr-automation':
            failures = payload.get('failures')
            if not isinstance(failures, dict):
                raise ValueError('invalid OCR quarantine state')
            for key, failure in failures.items():
                if (not isinstance(key, str) or not isinstance(failure, dict)
                        or failure.get('key') != key or not isinstance(failure.get('error_type'), str)):
                    raise ValueError('invalid exact OCR quarantine identity')
                sha(failure.get('fingerprint'))
        return [resource(ref) for ref in payload['resources']]
    if payload.get('version') != 1:
        raise ValueError('unsupported CCRD lifecycle schema')
    if kind == 'ccrd-ocr-search-release' and path.startswith('releases/ocr-'):
        identity = {key: payload[key] for key in ('version', 'kind', 'base_metadata_sha256', 'builder_sha256', 'tokenizer_version', 'exports')}
        sha(payload.get('base_metadata_sha256')); sha(payload.get('builder_sha256'))
        if (payload.get('generation') != digest(canonical(identity)) or payload.get('tokenizer_version') != TOKENIZER
                or path != f'releases/ocr-{payload["generation"]}/manifest.json'
                or not isinstance(payload.get('exports'), list) or not payload['exports']
                or payload.get('documents') != len(payload['exports'])):
            raise ValueError('CCRD release identity mismatch')
        refs = [*payload['exports'], payload['database']]
        if any(ref.get('bucket') != BUCKET for ref in refs):
            raise ValueError('CCRD release must point to qualified local objects')
        return [resource(ref) for ref in refs]
    if kind == 'ccrd-ocr-text-export' and path.startswith('text/exports/'):
        if (payload.get('complete') is not True or not isinstance(payload.get('retained_reader_resources'), list)
                or not payload['retained_reader_resources'] or not isinstance(payload.get('pages'), list)
                or type(payload.get('page_count')) is not int or payload['page_count'] != len(payload['pages'])):
            raise ValueError('incomplete CCRD OCR export retention manifest')
        sha(payload.get('book_id')); sha(payload.get('generation'))
        if path != f'text/exports/{payload["book_id"]}/{payload["generation"]}/manifest.json':
            raise ValueError('CCRD export storage identity mismatch')
        text_ref = resource(payload.get('text'))
        if text_ref['bucket'] != BUCKET or not text_ref['path'].startswith('text/objects/'):
            raise ValueError('CCRD export does not retain its actual TXT')
        refs = [text_ref, *payload['retained_reader_resources']]
        if any(ref.get('bucket') not in BUCKETS[:3] for ref in payload['retained_reader_resources']):
            raise ValueError('export provenance uses an unknown Reader bucket')
        return [resource(ref) for ref in refs]
    raise ValueError('unknown CCRD lifecycle document')
