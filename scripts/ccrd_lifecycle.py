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
CONTROL_KINDS = {'ccrd-ocr-current', 'ccrd-ocr-intent', 'ccrd-ocr-consumer', 'ccrd-ocr-retention', 'ccrd-ocr-id-registry', 'ccrd-ocr-automation'}


def is_root(path: str) -> bool:
    return path == 'current.json' or path.startswith('control/') and path.endswith('.json')


def is_document(path: str) -> bool:
    return is_root(path) or path.endswith('/manifest.json') and path.startswith(('text/', 'releases/ocr-'))


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
