#!/usr/bin/env python3
"""Read-only preflight for the single interrupted 2026-11-03 CHM handoff.

This deliberately has no apply path. It does not change a route, processing
record, historical journal, retirement hint, or any remote object.
"""

import gzip
import hashlib
import json
import os
import re
import sys

import httpx
from huggingface_hub import HfFileSystem

try:
    from . import build_reader_assets_v2_sidecar as sidecar, publish_reader_v3 as publication
    from . import reader_gc_maintenance as maintenance, reader_lifecycle, shared
    from .reader_bucket_store import S3BucketStore
except ImportError:
    import build_reader_assets_v2_sidecar as sidecar
    import publish_reader_v3 as publication
    import reader_gc_maintenance as maintenance
    import reader_lifecycle
    import shared
    from reader_bucket_store import S3BucketStore


SOURCE_RUN = 41582039143
SOURCE_SHA = 'ad70f4b348c6b1a8994c7c9a3657065bf215286bcb65071f7e2ba5e520d94d77'
SOURCE_KEY = 'VoiceOfML/MLMRL-Hub\0' + '000005/1870520043_362_马恩列斯毛论反对修正主义.chm'
OLD_PROFILE = '74476c3a49723cb1'
NEW_PROFILE = 'e671bff4cbba0b2b'
ROOT = f'chapters/ebook/chm/{SOURCE_SHA}/{NEW_PROFILE}'
OLD_ROOT = f'chapters/ebook/chm/{SOURCE_SHA}/{OLD_PROFILE}'
PROCESSING = 'reader-index/processing/stream-upload-d48339131485ba72583ee6ad2e3aed41.json'
PROCESSING_SHA256 = '55cb813324437ddbf470f603b673c29fb0d145e2a33a64665af4998da12fbb2e'
MANIFEST_SHA256 = '9d021bf045007d54bd663aa9b2a63564e4f7afe522420da2c546da4f5fc0f8cb'
PARENT_V3 = '6baf0df0604fef83c05fab27360665a504800a1352eb976368d01c5637c2c0cd'
MAINTENANCE_ID = '540c4557103649188becdadea5716832'
MAINTENANCE_SHA256 = 'b51ac0533ab95d8d298615c48f54451bdd6ad11d96c59f50563f3aecbe3d4dcc'
SIDECAR_SHA256 = '3962d363e73360b484c333ae8f55948d3e21fd4b9aee420c536f14c8db34115a'
INDEX_SHA256 = 'e93346edff0ad6bc61906d71fe35f4f582dd49845efcf2c076c18d211f1755ec'
POINTER_SHA256 = 'c1e08968bf30d3caf9723c37bc5e9c2de3eca3df98c9d8f522e10a5316cbc560'
ASSETS = shared.READER_ASSETS_BUCKET
INDEX = 'chapters/ebook/chm/index.json'
WORKFLOW = '.github/workflows/chm-single-interrupted-preflight.yml'
MAX_OBJECT_BYTES = 8 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def route(root):
    manifest = root + '/chapter-manifest.json'
    return {'s': 2, 'm': 'e', 'p': manifest, 'b': ASSETS, 'c': manifest, 'cb': ASSETS}


def validate_route_change(before, after):
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or before.get('v') != 1 or after.get('v') != 1
            or not isinstance(before.get('f'), dict) or not isinstance(after.get('f'), dict)):
        raise ValueError('invalid Reader sidecar')
    old, new = before['f'], after['f']
    if {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)} != {SOURCE_KEY}:
        raise ValueError('sidecar differs outside the exact CHM route')
    if old.get(SOURCE_KEY) != route(OLD_ROOT) or new.get(SOURCE_KEY) != route(ROOT):
        raise ValueError('exact CHM before/after route changed')


def validate_processing(raw):
    if digest(raw) != PROCESSING_SHA256:
        raise ValueError('CHM processing protection changed')
    value = json.loads(raw)
    if (value.get('version') != 1 or value.get('kind') != 'reader-processing-roots'
            or value.get('status') != 'processing' or value.get('operation') != 'reader-stream-upload'
            or value.get('release_policy') != 'explicit-publisher-handoff'
            or value.get('run') != {'repository': 'anftm/pipeline', 'run_id': str(SOURCE_RUN), 'attempt': '1'}
            or value.get('resources') != [{'bucket': ASSETS, 'root': ROOT}]):
        raise ValueError('CHM processing protection identity differs')


def validate_entry(index):
    matches = [item for item in index.get('files', []) if isinstance(item, dict) and item.get('key') == SOURCE_KEY]
    if (len(matches) != 1 or matches[0].get('root') != ROOT
            or matches[0].get('manifest') != ROOT + '/chapter-manifest.json'
            or matches[0].get('source_sha256') != SOURCE_SHA
            or matches[0].get('processing_roots') != [PROCESSING]):
        raise ValueError('canonical CHM index differs from interrupted upload')
    return matches[0]


def validate_manifest(store):
    raw = store.read_bytes(ASSETS, ROOT + '/chapter-manifest.json')
    if digest(raw) != MANIFEST_SHA256 or len(raw) > MAX_OBJECT_BYTES:
        raise ValueError('CHM manifest checksum or size differs')
    manifest = json.loads(raw)
    if manifest.get('kind') != 'epub-chapters' or len(manifest.get('chapters', [])) != 2:
        raise ValueError('CHM chapter manifest differs')
    for item in [*manifest['chapters'], manifest.get('search_index')]:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str):
            raise ValueError('invalid CHM bundle member')
        path = item['path']
        if (path.startswith('/') or '\\' in path or any(part in {'', '.', '..'} for part in path.split('/'))
                or type(item.get('bytes')) is not int or not 0 <= item['bytes'] <= MAX_OBJECT_BYTES
                or not re.fullmatch(r'[0-9a-f]{64}', str(item.get('sha256', '')))):
            raise ValueError('invalid CHM bundle member identity')
        raw_item = store.read_bytes(ASSETS, ROOT + '/' + path)
        if len(raw_item) != item['bytes'] or digest(raw_item) != item['sha256']:
            raise ValueError('CHM bundle member checksum differs')


def controls():
    return [(sidecar.PDF_BUCKET, 'reader-index/derived_pdf_manifest.json'),
            (ASSETS, 'documents/text/index.json'), *sidecar.stream_processing_indexes(ASSETS),
            *sidecar.static_pdf_processing_indexes(ASSETS), *sidecar.ebook_chapter_indexes(ASSETS)]


def validate_github(api):
    repo = api('')
    run = api(f'/actions/runs/{SOURCE_RUN}')
    jobs = api(f'/actions/runs/{SOURCE_RUN}/attempts/1/jobs?per_page=100')
    if (repo.get('id') != 1443726000 or repo.get('full_name') != 'anftm/pipeline'
            or run.get('id') != SOURCE_RUN or run.get('repository', {}).get('id') != repo['id']
            or run.get('path') != '.github/workflows/reader-chm-repair.yml'
            or run.get('head_sha') != '82018d6f5f34dcb4f12da21e967679a0a508841d'
            or run.get('event') != 'workflow_dispatch' or run.get('run_attempt') != 1
            or run.get('status') != 'completed' or run.get('conclusion') != 'failure'):
        raise ValueError('interrupted CHM source run identity differs')
    rows = jobs.get('jobs')
    if (not isinstance(rows, list) or jobs.get('total_count') != len(rows) or len(rows) != 2
            or any(row.get('status') != 'completed' for row in rows)):
        raise ValueError('interrupted CHM source jobs are incomplete')
    rebuild = next((row for row in rows if row.get('name') == 'rebuild'), None)
    steps = {step.get('name'): step.get('conclusion') for step in (rebuild or {}).get('steps', [])}
    if (not rebuild or rebuild.get('conclusion') != 'failure'
            or steps.get('Build chapter streams from pinned CHMs (EPUB is temporary)') != 'success'
            or steps.get('Rebuild the canonical Reader sidecar and release handed-off roots') != 'failure'
            or steps.get('Unify the updated Reader projection') != 'skipped'):
        raise ValueError('interrupted CHM source stages differ')


def github_api(path):
    token = os.environ.get('GH_TOKEN', '')
    if not token:
        raise RuntimeError('GH_TOKEN is required')
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.get('https://api.github.com/repos/anftm/pipeline' + path,
                              headers={'Authorization': 'Bearer ' + token,
                                       'Accept': 'application/vnd.github+json',
                                       'User-Agent': 'chm-interrupted-readonly-preflight'})
    if response.status_code != 200:
        raise RuntimeError('GitHub source run read failed: HTTP ' + str(response.status_code))
    return response.json()


def validate_context():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != 'anftm/pipeline/' + WORKFLOW + '@refs/heads/main'
            or not re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', ''))):
        raise RuntimeError('preflight requires its central locked workflow')


def main():
    if sys.argv[1:]:
        raise RuntimeError('read-only preflight accepts no arguments')
    validate_context()
    validate_github(github_api)
    token = os.environ.get('HF_TOKEN')
    if not token:
        raise RuntimeError('HF_TOKEN is required')
    store = S3BucketStore()
    maintenance_raw = store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL)
    journal = json.loads(maintenance_raw)
    if (digest(maintenance_raw) != MAINTENANCE_SHA256
            or journal.get('maintenance_id') != MAINTENANCE_ID or journal.get('restored') is not True
            or journal.get('restore_errors') or journal.get('callback_recovery_required')
            or (journal.get('callback_mutation_started') and not journal.get('callback_committed'))):
        raise ValueError('maintenance journal is not the restored historical window')
    pointer, catalog = publication.current(store)
    pointer_raw = store.read_bytes(ASSETS, publication.POINTER)
    if (not pointer or pointer.get('generation') != PARENT_V3 or digest(pointer_raw) != POINTER_SHA256
            or json.loads(pointer_raw) != pointer):
        raise ValueError('Reader v3 parent changed')
    raw_sidecar = store.read_bytes(ASSETS, sidecar.SIDECAR_PATH)
    if digest(raw_sidecar) != SIDECAR_SHA256:
        raise ValueError('canonical Reader sidecar changed')
    before = json.loads(gzip.decompress(raw_sidecar))
    processing_raw = store.read_bytes(ASSETS, PROCESSING)
    validate_processing(processing_raw)
    index_raw = store.read_bytes(ASSETS, INDEX)
    if digest(index_raw) != INDEX_SHA256:
        raise ValueError('canonical CHM index changed')
    entry = validate_entry(json.loads(index_raw))
    reader_lifecycle.validate_processing_handoff(json.loads(processing_raw), entry)
    validate_manifest(store)
    after = sidecar.build(HfFileSystem(token=token), ASSETS, sidecar.PDF_BUCKET,
                          'reader-index/derived_pdf_manifest.json', 'reader-index/pdf_render_manifest.json',
                          'reader-index/pdf_ocr_manifest.json')
    validate_route_change(before, after)
    indexes = []
    for bucket, path in controls():
        try:
            indexes.append(sidecar.read_bucket_json(path, token, bucket=bucket))
        except FileNotFoundError:
            continue
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        _, evidence, missing = sidecar.historical_processing_guard(indexes, after, store, client)
        projections = {site: publication.observe_consumer(store, site, client) for site in ('hf', 'pages')}
    if any(value.get('generation') != PARENT_V3 or value.get('projection_verified') is not True
           for value in projections.values()):
        raise ValueError('live Reader projections changed')
    validate_github(github_api)
    if (store.read_bytes(ASSETS, maintenance.DURABLE_JOURNAL) != maintenance_raw
            or store.read_bytes(ASSETS, sidecar.SIDECAR_PATH) != raw_sidecar
            or store.read_bytes(ASSETS, PROCESSING) != processing_raw
            or store.read_bytes(ASSETS, INDEX) != index_raw
            or store.read_bytes(ASSETS, publication.POINTER) != pointer_raw
            or publication.current(store)[0] != pointer):
        raise RuntimeError('Reader or maintenance state changed during preflight')
    print(json.dumps({'outcome': 'read-only-preflight-passed', 'source_run': SOURCE_RUN,
                      'changed_routes': 1, 'protected_processing': True,
                      'historical_missing_proved': len(missing), 'historical_evidence': evidence,
                      'v3_generation': catalog['generation'], 'remote_writes': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
