#!/usr/bin/env python3
"""Explicit retirement of document page streams and OCR, preserving image sources.

This is a maintenance operation, not ordinary orphan GC. Withdrawal runs through
the central v3 publisher. Collection consumes its accepted retirement receipt.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
from pathlib import PurePosixPath
import re

try:
    from . import shared, reader_lifecycle as lifecycle, publish_reader_v3 as publication
except ImportError:
    import shared
    import reader_lifecycle as lifecycle
    import publish_reader_v3 as publication


ASSETS = shared.READER_ASSETS_BUCKET
PAGES = shared.PDF_PAGES_BUCKET
INPUTS = shared.PDF_OCR_INPUT_BUCKET
SIDECAR = 'reader-index/reader_assets.json.gz'
DERIVED = 'reader-index/derived_pdf_manifest.json'
RECEIPT = 'reader-index/gc/document-reset/withdrawal.json'
REGISTRIES = ('pdf_render_manifest.json', 'pdf_render_progress.json', 'pdf_ocr_manifest.json')
OBJECT = r'objects/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{16}/'
PAGE_OUTPUT = re.compile(OBJECT + r'(?:'
    r'(?:pages|preview)/page-[0-9]{6}\.(?:png|webp|jxl|svg|jpeg)|'
    r'(?:ocr|text)/(?:page-[0-9]{6}|book-text|search-[0-9]{6}-[0-9]{6})\.json\.gz|'
    r'(?:preview|text)/partition-[0-9]{6}-[0-9]{6}-manifest\.json|'
    r'(?:page|reading|render|ocr|text-layer|text-review)-manifest\.json|'
    r'render-range-[0-9]{6}-[0-9]{6}\.json|page-map\.json\.gz)$')
INPUT = re.compile(OBJECT + r'ocr-input/page-[0-9]{6}\.png$')
DOCUMENT_PRODUCERS = {
    'pdf-account-worker.yml', 'pdf-account-publish.yml', 'pdf-lane-watchdog.yml',
    'reader-v3-build.yml', 'reader-v3-publish.yml', 'force-ocr-book.yml',
    'reader-ocr-repin.yml', 'reader-ocr-language-audit.yml', 'reader-resource-repair.yml',
    'publish-ccrd-ocr.yml', 'stage-ccrd-ocr-text.yml', 'reader-static-pdf-stream.yml',
    'reader-maintenance.yml', 'reader-gc-collect.yml', 'reader-gc-ccrd.yml',
}


class DocumentProducerFence:
    """The reset owns document roots; independent image/ebook builders may run."""
    def __init__(self, api=None):
        try:
            from .reader_gc_fence import GitHubProducerFence
        except ImportError:
            from reader_gc_fence import GitHubProducerFence
        self.fence = GitHubProducerFence(api)

    def check(self):
        try:
            from .reader_gc_fence import producer_repositories
        except ImportError:
            from reader_gc_fence import producer_repositories
        current_repo = os.environ.get('GITHUB_REPOSITORY')
        current_run = os.environ.get('GITHUB_RUN_ID')
        own_path = os.environ.get('GITHUB_WORKFLOW_REF', '').split('@')[0]
        own_path = own_path.removeprefix((current_repo or '') + '/')
        reports = []
        def selected(path):
            name = path.split('@')[0].rsplit('/', 1)[-1]
            return (name in DOCUMENT_PRODUCERS or name.startswith(('pdf-', 'reader-djvu', 'migrate-pdf-', 'lin-native-text')))
        for repo in producer_repositories():
            workflows = [w for page in self.fence.api(f'repos/{repo}/actions/workflows?per_page=100')
                         for w in page['workflows'] if selected(w['path'])]
            if not any(w['path'].endswith('/pdf-account-worker.yml') for w in workflows):
                raise RuntimeError('document producer inventory is incomplete: ' + repo)
            enabled = [w['path'] for w in workflows if w['state'] == 'active'
                       and not (repo == current_repo and w['path'] == own_path)]
            if enabled:
                raise RuntimeError(f'document producers are not paused: {repo}: {enabled}')
            residual = []
            for status in ('in_progress', 'queued', 'pending', 'waiting', 'requested'):
                runs = [r for page in self.fence.api(f'repos/{repo}/actions/runs?status={status}&per_page=100')
                        for r in page['workflow_runs'] if selected(r['path']) and r['status'] != 'completed']
                for run in runs:
                    if repo == current_repo and str(run['id']) == current_run:
                        continue
                    if run['status'] not in {'queued', 'pending'}:
                        raise RuntimeError(f'document producer still active: {repo}:{run["id"]}')
                    residual.append(self.fence.isolated_residual(repo, run, central=repo == 'anftm/pipeline'))
            reports.append({'repository': repo, 'residuals': residual})
        return reports


def encode(value):
    return publication.encode(value)


def decode(raw):
    return json.loads(gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def extension(key):
    return PurePosixPath(key.split('\0')[-1]).suffix.lower().lstrip('.')


def is_djvu(entry):
    return str(entry.get('source_extension', '')).lower() == 'djvu' or extension(entry.get('key', '')) == 'djvu'


def clean_sidecar(sidecar, derived, static_indexes=()):
    """Remove document outputs without changing ordinary image/TIFF routes."""
    if sidecar.get('v') != 1 or not isinstance(sidecar.get('f'), dict):
        raise ValueError('invalid canonical sidecar')
    result = copy.deepcopy(sidecar)
    changed = []
    fallback = {}
    for entry in derived['files']:
        if not is_djvu(entry):
            fallback[entry['key']] = {'s': 2, 'm': 'p', 'p': entry['new_path'], 'b': PAGES}
    for index in static_indexes:
        for entry in index.get('files', []):
            fallback[entry['key']] = {'s': 2, 'm': 'p', 'p': entry['object'], 'b': ASSETS}
    for key, old in sidecar['f'].items():
        entry = dict(old)
        bucket = entry.get('b', ASSETS)
        path = str(entry.get('p') or '')
        stream = bucket == PAGES and path.endswith(('/page-manifest.json', '/reading-manifest.json'))
        djvu = extension(key) == 'djvu'
        ocr = entry.get('o') and entry.get('ob', bucket) == PAGES
        if stream or (djvu and bucket == PAGES and path):
            entry = dict(fallback.get(key, {}))
        elif ocr:
            for field in ('o', 'ob', 'om', 'op', 'oe'):
                entry.pop(field, None)
            if entry.get('s') == 3:
                entry = {}
        if djvu:
            entry.pop('pd', None)
            entry.pop('pdb', None)
        if entry.get('s') == 4:
            # Diagnostic fields are archived in the original registries. HF
            # deliberately exposes only the unavailable route for this status.
            for field in ('om', 'oe', 'op', 'ob', 'o'):
                entry.pop(field, None)
        if entry != old:
            changed.append(key)
            if entry:
                result['f'][key] = entry
            else:
                result['f'].pop(key)
    return result, changed


def current_base_catalog(store):
    """Keep release history without materializing historical reference arrays."""
    import ijson
    try:
        from .reader_gc_stream import HashingReader
    except ImportError:
        from reader_gc_stream import HashingReader
    import io

    def parse(chunks, compressed):
        source = HashingReader(chunks)
        stream = io.BufferedReader(source)
        decoded = gzip.GzipFile(fileobj=stream) if compressed else stream
        cursor, builder = None, ijson.ObjectBuilder()
        skipped = None
        for prefix, event, value in ijson.parse(decoded, use_float=True):
            if skipped is not None:
                if prefix == skipped or prefix.startswith(skipped + '.'):
                    continue
                skipped = None
            if prefix == 'current.generation' and event == 'string':
                cursor = value
            if prefix == 'generations' and event == 'map_key' and cursor is None:
                raise ValueError('catalog cursor must precede its generations')
            if (prefix.startswith('generations.') and prefix.count('.') == 1
                    and prefix != 'generations.' + str(cursor)
                    and event == 'map_key' and value == 'references'):
                skipped = prefix + '.references'
                continue
            builder.event(event, value)
        while stream.read(65536):
            pass
        catalog = builder.value
        if (not isinstance(catalog, dict) or catalog.get('version') != 1
                or catalog.get('kind') != 'reader-catalog'
                or cursor is None or cursor not in catalog.get('generations', {})):
            raise ValueError('catalog current generation is missing')
        return catalog, source.hash.hexdigest()

    if hasattr(store, 'read_graph_json'):
        return store.read_graph_json(ASSETS, 'reader-index/reader_catalog.json', parser=parse)
    raw = store.read_bytes(ASSETS, 'reader-index/reader_catalog.json')
    return parse([raw], raw.startswith(b'\x1f\x8b'))


def retain_withdrawal_history(legacy, before, generation, reset_id):
    """Preserve original release identities until real consumers accept replacement."""
    history = dict(legacy.get('retired_generations', {}))
    for previous_generation, entry in before['generations'].items():
        if previous_generation == generation:
            continue
        history.setdefault(previous_generation, {
            **{key: value for key, value in entry.items() if key != 'references'},
            'replacement_generation': generation,
            'replacement_acks': {'hf': False, 'pages': False},
            'withdrawal_reset_id': reset_id,
            'withdrawn_at': legacy['current'].get('published_at'),
        })
    legacy['generations'] = {generation: legacy['generations'][generation]}
    if history:
        legacy['retired_generations'] = history


def withdraw(store, options, *, apply=False):
    reset_id = options.get('reset_id')
    if not isinstance(reset_id, str) or not re.fullmatch(r'[a-z0-9-]{8,80}', reset_id):
        raise ValueError('document reset requires a stable reset_id')
    try:
        previous = decode(store.read_bytes(ASSETS, RECEIPT))
    except FileNotFoundError:
        previous = None
    if previous:
        if previous.get('reset_id') != reset_id:
            raise ValueError('an earlier document reset needs explicit archival')
        if previous.get('status') == 'withdrawn':
            if digest(store.read_bytes(ASSETS, SIDECAR)) != previous['sidecar_sha256']:
                raise ValueError('sidecar moved after document withdrawal')
            return normalize_withdrawn(store, previous, apply=apply)
        raise ValueError('unfinished reset requires its exact saved plan')
    plan_path = f'reader-index/gc/document-reset/{reset_id}/plan.json.gz'
    try:
        saved_plan = decode(store.read_bytes(ASSETS, plan_path))
    except FileNotFoundError:
        saved_plan = None
    if saved_plan:
        return apply_withdrawal(store, saved_plan) if apply else saved_plan['report']
    raw = store.read_bytes(ASSETS, SIDECAR)
    derived_raw = store.read_bytes(PAGES, DERIVED)
    derived = decode(derived_raw)
    if derived.get('kind') != 'pdf-derived-source-index' or not isinstance(derived.get('files'), list):
        raise ValueError('invalid derived PDF registry')
    static = []
    for ext in ('ppt', 'pptx', 'pps', 'wps', 'ps'):
        try:
            static.append(decode(store.read_bytes(ASSETS, f'documents/pdf/{ext}/index.json')))
        except FileNotFoundError:
            pass
    cleaned, changed = clean_sidecar(decode(raw), derived, static)
    packed = gzip.compress(encode(cleaned), mtime=0)
    filtered = {**derived, 'files': [entry for entry in derived['files'] if not is_djvu(entry)]}
    removed = [entry for entry in derived['files'] if is_djvu(entry)]
    pointer, catalog = publication.current(store)
    before, catalog_sha = current_base_catalog(store)
    legacy, generation = lifecycle.catalog_generation(cleaned, before)
    # This reset explicitly retires all prior document routes. The current
    # non-document sidecar owns everything retained after withdrawal.
    retain_withdrawal_history(legacy, before, generation, reset_id)
    snapshot_path = f'reader-index/generations/{generation}/reader_assets.json.gz'
    legacy['generations'][generation]['snapshot'] = publication.metadata(ASSETS, snapshot_path, packed)
    snapshot = publication.metadata(ASSETS, f'reader-index/snapshots/{digest(packed)}/reader_assets.json.gz', packed)
    archives = {SIDECAR: raw, DERIVED: derived_raw}
    archives['reader-index/catalog-release-history.json'] = encode({
        'version': 1, 'kind': 'reader-catalog-release-history',
        'original_catalog_sha256': catalog_sha,
        'current': before['current'], 'retired_generations': before.get('retired_generations', {}),
        'generations': {key: {field: value for field, value in entry.items() if field != 'references'}
                        for key, entry in before['generations'].items()},
    })
    if pointer:
        archives['reader-index/v3/previous-pointer.json'] = encode(pointer)
        archives['reader-index/v3/previous-catalog.json'] = encode(catalog)
    updates = {}
    for name in REGISTRIES:
        path = 'reader-index/' + name
        try:
            archives[path] = store.read_bytes(ASSETS, path)
        except FileNotFoundError:
            pass
        updates[path] = encode({'version': 1, 'files': {}})
    updates['reader-index/pdf_ocr_progress/index.json'] = encode(
        {'version': 1, 'kind': 'pdf-ocr-progress-index', 'books': {}})
    updates['reader-index/pdf_ocr_progress.json'] = encode({'version': 1, 'files': {}})
    for path, kind in [('reader-index/v3/automation.json', 'reader-v3-automation'),
                       ('reader-index/v3/corrections/state.json', 'reader-v3-correction-state')]:
        try:
            archives[path] = store.read_bytes(ASSETS, path)
        except FileNotFoundError:
            pass
        updates[path] = encode({'version': 1, 'kind': kind, 'tasks': {}, 'days': {}})
    repair_path = 'reader-index/resource_repairs.json'
    try:
        repair_raw = store.read_bytes(ASSETS, repair_path)
    except FileNotFoundError:
        pass
    else:
        repairs = decode(repair_raw)
        if not isinstance(repairs.get('files'), dict):
            raise ValueError('invalid repair registry')
        archives[repair_path] = repair_raw
        repairs['files'] = {key: value for key, value in repairs['files'].items()
                            if not any(ref.get('bucket') in {PAGES, INPUTS}
                                       for ref in value.get('resources', []))}
        updates[repair_path] = encode(repairs)
    consumer_path = 'reader-index/reader_gc_consumers.json'
    try:
        archives[consumer_path] = store.read_bytes(ASSETS, consumer_path)
    except FileNotFoundError:
        pass
    updates[consumer_path] = encode({'version': 1, 'kind': 'reader-gc-consumers', 'receipts': {},
                                     'resources': lifecycle.sidecar_references(cleaned)})
    report = {'version': 1, 'kind': 'reader-document-reset', 'reset_id': reset_id,
              'status': 'planned', 'created_at': lifecycle.now_iso(),
              'changed_keys': changed, 'removed_v3_books': len(catalog['files']),
              'djvu_derivatives': removed, 'sidecar_sha256': digest(packed),
              'previous_sidecar_sha256': digest(raw), 'previous_catalog_sha256': catalog_sha,
              'base_generation': generation, 'base_snapshot': snapshot,
              'previous_v3_generation': pointer['generation'] if pointer else None,
              'original_sources_deleted': False, 'ordinary_image_routes_changed': False}
    if not apply:
        return report
    publication.assert_writer(store)
    if store.read_bytes(ASSETS, SIDECAR) != raw or store.read_bytes(PAGES, DERIVED) != derived_raw:
        raise ValueError('reset baseline changed')
    audit_prefix = f'reader-index/gc/document-reset/{reset_id}'
    # Small metadata checkpoints retain failure/source identities, not page data.
    for path, value in archives.items():
        target = audit_prefix + '/metadata/' + path.replace('/', '--') + '.gz'
        publication.immutable_put(store, ASSETS, target, gzip.compress(value, mtime=0))
    # The saved plan is immutable and can resume after any interrupted write.
    plan = {'report': report, 'updates': {p: decode(v) for p, v in updates.items()},
            'sidecar': cleaned, 'derived': filtered, 'legacy': legacy, 'previous_pointer': pointer}
    plan_path = audit_prefix + '/plan.json.gz'
    publication.immutable_put(store, ASSETS, plan_path, gzip.compress(encode(plan), mtime=0))
    return apply_withdrawal(store, plan)


def apply_withdrawal(store, plan):
    publication.assert_writer(store)
    report = dict(plan['report'])
    packed = gzip.compress(encode(plan['sidecar']), mtime=0)
    pointer, catalog = publication.current(store)
    expected = plan['previous_pointer']
    if pointer != expected and catalog.get('document_reset') != report['reset_id']:
        raise ValueError('v3 pointer moved outside the reset')
    current_raw = store.read_bytes(ASSETS, SIDECAR)
    if digest(current_raw) not in {report['previous_sidecar_sha256'], report['sidecar_sha256']}:
        raise ValueError('canonical sidecar changed outside the reset')
    updates = {path: encode(value) for path, value in plan['updates'].items()}
    for path, value in updates.items():
        store.put_bytes(ASSETS, path, value)
        if store.read_bytes(ASSETS, path) != value:
            raise ValueError('reset registry readback mismatch')
    store.put_bytes(PAGES, DERIVED, encode(plan['derived']))
    if store.read_bytes(PAGES, DERIVED) != encode(plan['derived']):
        raise ValueError('derived registry readback mismatch')
    snapshot_path = f"reader-index/generations/{report['base_generation']}/reader_assets.json.gz"
    snapshot = report['base_snapshot']
    publication.immutable_put(store, ASSETS, snapshot_path, packed)
    publication.immutable_put(store, ASSETS, snapshot['path'], packed)
    store.put_bytes(ASSETS, SIDECAR, packed)
    store.put_bytes(ASSETS, 'reader-index/reader_catalog.json', encode(plan['legacy']))
    if catalog.get('document_reset') != report['reset_id']:
        result = publication.commit_catalog(store, {'version': 3, 'kind': 'reader-reading-catalog',
            'files': {}, 'base_sidecar': snapshot, 'parent_generation': pointer['generation'] if pointer else None,
            'document_reset': report['reset_id']}, pointer, apply=True)
        generation = result['generation']
    else:
        generation = pointer['generation']
    report.update(status='withdrawn', generation=generation, applied=True)
    store.put_bytes(ASSETS, RECEIPT, encode(report))
    if store.read_bytes(ASSETS, SIDECAR) != packed or store.read_bytes(ASSETS, RECEIPT) != encode(report):
        raise ValueError('withdrawal readback mismatch')
    return report


def normalize_withdrawn(store, receipt, *, apply=False):
    """Remove legacy failure diagnostics from the already withdrawn projection."""
    raw = store.read_bytes(ASSETS, SIDECAR)
    sidecar = decode(raw)
    changed = False
    for entry in sidecar['f'].values():
        if entry.get('s') != 4:
            continue
        for field in ('om', 'oe', 'op', 'ob', 'o'):
            if field in entry:
                entry.pop(field)
                changed = True
    if not changed:
        return receipt
    if not apply:
        return {**receipt, 'normalization_pending': True}
    publication.assert_writer(store)
    pointer, catalog = publication.current(store)
    if catalog['files'] or catalog.get('document_reset') != receipt['reset_id']:
        raise ValueError('withdrawn catalog moved')
    packed = gzip.compress(encode(sidecar), mtime=0)
    before, _ = current_base_catalog(store)
    legacy, generation = lifecycle.catalog_generation(sidecar, before)
    retain_withdrawal_history(legacy, before, generation, receipt['reset_id'])
    path = f'reader-index/generations/{generation}/reader_assets.json.gz'
    legacy['generations'][generation]['snapshot'] = publication.metadata(ASSETS, path, packed)
    snapshot = publication.metadata(ASSETS, f'reader-index/snapshots/{digest(packed)}/reader_assets.json.gz', packed)
    publication.immutable_put(store, ASSETS, path, packed)
    publication.immutable_put(store, ASSETS, snapshot['path'], packed)
    store.put_bytes(ASSETS, SIDECAR, packed)
    store.put_bytes(ASSETS, 'reader-index/reader_catalog.json', encode(legacy))
    result = publication.commit_catalog(store, {'version': 3, 'kind': 'reader-reading-catalog',
        'files': {}, 'base_sidecar': snapshot, 'parent_generation': pointer['generation'],
        'document_reset': receipt['reset_id']}, pointer, apply=True)
    report = {**receipt, 'generation': result['generation'], 'base_generation': generation,
              'base_snapshot': snapshot, 'sidecar_sha256': digest(packed)}
    store.put_bytes(ASSETS, RECEIPT, encode(report))
    return report
