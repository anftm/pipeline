#!/usr/bin/env python3
"""Resume exactly 51 already-published ebook processing handoffs; no collection."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re

import httpx

try:
    from . import publish_reader_v3 as publication, reader_gc_collect as collector
    from . import reader_lifecycle as lifecycle
    from . import reader_retired_processing as history
    from .reader_gc_fence import GitHubProducerFence, producer_path
    from .reader_gc_maintenance import preflight as producer_inventory
    from .reader_bucket_store import S3BucketStore
    from .reader_processing_cas import ProcessingWriter
    from .report_office_chapter_coverage import verify_bundle_payload
except ImportError:
    import publish_reader_v3 as publication
    import reader_gc_collect as collector
    import reader_lifecycle as lifecycle
    import reader_retired_processing as history
    from reader_gc_fence import GitHubProducerFence, producer_path
    from reader_gc_maintenance import preflight as producer_inventory
    from reader_bucket_store import S3BucketStore
    from reader_processing_cas import ProcessingWriter
    from report_office_chapter_coverage import verify_bundle_payload


ASSETS = lifecycle.READER_ASSETS_BUCKET
SIDECAR = 'reader-index/reader_assets.json.gz'
CATALOG = 'reader-index/reader_catalog.json'
RECEIPT_PREFIX = 'reader-index/gc/ebook-processing-handoff/41172648658/'
MANIFEST_PATH = Path(__file__).resolve().parents[1] / 'config/ebook-processing-handoff-20261031.json'
MANIFEST_SHA256 = '27f2271286f97bddf008477487cc2538c389ffbca4a9f741118ecd74887de7ca'
SOURCE_RUN = '41172648658'
SOURCE_ATTEMPT = '1'
V3_GENERATION = '8327d60feb95c848298f916e97b39ea5f6c8c36a16bc78e6613eb91082c6f0ec'
OLD_V3_GENERATION = 'b0e4a2aec277264932fde052ab512a24de5fe1bf07d842f03af430f93f0d6ca8'
BASE_GENERATION = '2e623caa2ea3831d4678c7a4272f63b9'
SIDECAR_SHA256 = '827b673662c9d888983158e4eae53f9660464010d7cd281cd20e0ccb4cedb5ea'
WORKFLOW = 'anftm/pipeline/.github/workflows/reader-ebook-processing-handoff.yml@refs/heads/main'
ROOT_RE = re.compile(r'chapters/ebook/(epub|azw3|fb2)/([0-9a-f]{64})/([0-9a-f]{16})')
HEX64 = re.compile(r'[0-9a-f]{64}')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def assert_workflow_context():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'anftm/pipeline'
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW
            or not re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', ''))):
        raise RuntimeError('ebook processing handoff requires its central locked workflow')


def assert_no_foreign_producers():
    """Only GET checks; queued work is preserved, and any active worker blocks."""
    inventory = producer_inventory()
    fence = GitHubProducerFence()
    for repo in sorted(inventory):
        for status in ('in_progress', 'waiting', 'requested'):
            pages = fence.api(f'repos/{repo}/actions/runs?status={status}&per_page=100')
            active = [run for page in pages for run in page.get('workflow_runs', [])
                      if run.get('status') != 'completed' and run.get('id') != int(os.environ['GITHUB_RUN_ID'])
                      and (run.get('path', '').startswith('.github/workflows/reader-maintenance.yml')
                           or run.get('path', '').startswith('.github/workflows/reader-gc-collect.yml')
                           or run.get('path', '').startswith('.github/workflows/chm-epub-retirement.yml')
                           or producer_path(run.get('path', '')))]
            if active:
                raise RuntimeError('foreign Reader producer or maintenance is active: ' + repo)


def load_manifest(path=MANIFEST_PATH, expected_sha256=MANIFEST_SHA256):
    raw = path.read_bytes()
    if digest(raw) != expected_sha256:
        raise ValueError('exact ebook handoff inventory checksum differs')
    value = json.loads(raw)
    expected = {'version': 1, 'kind': 'reader-exact-ebook-processing-handoff',
                'source_run_id': SOURCE_RUN, 'source_attempt': SOURCE_ATTEMPT,
                'previous_v3_generation': OLD_V3_GENERATION, 'v3_generation': V3_GENERATION,
                'base_generation': BASE_GENERATION, 'sidecar_sha256': SIDECAR_SHA256, 'expected_count': 51}
    if any(value.get(k) != v for k, v in expected.items()) or not isinstance(value.get('entries'), list):
        raise ValueError('exact ebook handoff inventory header differs')
    rows = value['entries']
    if len(rows) != 51:
        raise ValueError('ebook handoff inventory must name exactly 51 entries')
    keys, paths = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid ebook handoff row')
        key, before, after = row.get('key'), row.get('before'), row.get('after')
        if (not isinstance(key, str) or key.count('\0') != 1 or not all(key.split('\0'))
                or key in keys or not isinstance(before, dict) or not isinstance(after, dict)):
            raise ValueError('invalid ebook source identity')
        keys.add(key)
        if (set(before) != {'s', 'm', 'p', 'b', 'c', 'cb'} or set(after) != set(before)
                or any(before.get(field) != after.get(field) for field in ('s', 'm', 'b', 'cb'))
                or after.get('s') != 2 or after.get('m') != 'e'
                or after.get('b') != ASSETS or after.get('cb') != ASSETS
                or after.get('p') != after.get('c') or before.get('p') != before.get('c')
                or before.get('p') == after.get('p')):
            raise ValueError('ebook handoff changes a non-chapter route')
        after_path = after.get('p')
        old_path = before.get('p')
        if not isinstance(after_path, str) or not isinstance(old_path, str):
            raise ValueError('invalid ebook chapter manifest')
        new_root = after_path.removesuffix('/chapter-manifest.json')
        old_root = old_path.removesuffix('/chapter-manifest.json')
        match, old_match = ROOT_RE.fullmatch(new_root), ROOT_RE.fullmatch(old_root)
        if (not after_path.endswith('/chapter-manifest.json') or not old_path.endswith('/chapter-manifest.json')
                or not match or not old_match or not key.lower().endswith('.' + match[1])
                or match[1] != old_match[1] or match[2] != row.get('source_sha256')
                or not re.fullmatch(r'[0-9a-f]{40}', str(row.get('source_revision', '')))
                or type(row.get('chapter_count')) is not int or row['chapter_count'] < 1
                or any(not HEX64.fullmatch(str(row.get(name, ''))) for name in
                       ('source_sha256', 'manifest_sha256', 'search_index_sha256', 'first_chapter_sha256'))):
            raise ValueError('ebook handoff asset identity is invalid')
        path = row.get('processing_path')
        entry = {'key': key, 'root': new_root, 'manifest': after_path}
        if (path != history.stream_identity(entry)[0] or path in paths):
            raise ValueError('ebook processing path differs from its exact producer identity')
        paths.add(path)
    return value


def observe_state(store, client, plan):
    maintenance = json.loads(store.read_bytes(ASSETS, 'reader-index/gc/maintenance/current.json'))
    if (maintenance.get('restored') is not True or maintenance.get('restore_errors')
            or maintenance.get('callback_recovery_required') or maintenance.get('callback_mutation_started')
            and not maintenance.get('callback_committed')):
        raise RuntimeError('maintenance isolation has not been restored')
    pointer, catalog = publication.current(store)
    raw = store.read_bytes(ASSETS, SIDECAR)
    if (pointer.get('generation') != plan['v3_generation'] or digest(raw) != plan['sidecar_sha256']
            or catalog.get('base_sidecar', {}).get('sha256') != plan['sidecar_sha256']):
        raise ValueError('ebook sidecar or v3 generation changed')
    sidecar = json.loads(gzip.decompress(raw))
    if sidecar.get('v') != 1 or not isinstance(sidecar.get('f'), dict):
        raise ValueError('invalid canonical Reader sidecar')
    identity, consumers = collector.observe_projections(store, client)
    acknowledged, reason = collector.catalog_acknowledged(store, identity)
    if identity['current'] != plan['base_generation'] or not acknowledged:
        raise ValueError('ebook base generation lacks exact two-site acknowledgment: ' + reason)
    if any(consumers['receipts'].get(surface, {}).get('generation') != plan['v3_generation']
           or consumers['receipts'][surface].get('projection_verified') is not True
           for surface in ('hf', 'pages')):
        raise ValueError('both Readers must accept the exact ebook generation')
    consumer_refs = {(item.get('bucket'), item.get('path')) for item in consumers.get('resources', [])
                     if isinstance(item, dict)}
    for row in plan['entries']:
        if (ASSETS, row['after']['p']) not in consumer_refs:
            raise ValueError('accepted consumers do not protect an ebook chapter manifest')
    base = json.loads(store.read_bytes(ASSETS, CATALOG))
    base_entry = base.get('generations', {}).get(plan['base_generation'], {})
    references = {(item.get('bucket'), item.get('path')) for item in base_entry.get('references', [])
                  if isinstance(item, dict)}
    if any((ASSETS, row['after']['p']) not in references for row in plan['entries']):
        raise ValueError('accepted base catalog does not protect an ebook chapter manifest')
    promotion = json.loads(store.read_bytes(ASSETS, f"reader-index/v3/promotions/{plan['v3_generation']}.json"))
    if promotion.get('acks') != {'hf': True, 'pages': True}:
        raise ValueError('ebook v3 promotion lacks both acknowledgments')
    old = json.loads(store.read_bytes(ASSETS,
        f"reader-index/v3/generations/{plan['previous_v3_generation']}/catalog.json"))
    publication.validate_catalog(old)
    previous = publication.project_sidecar(publication.pinned_base(store, old, {}), old)['f']
    current = publication.project_sidecar(sidecar, catalog)['f']
    expected = {row['key']: row for row in plan['entries']}
    changed = {key for key in set(previous) | set(current) if previous.get(key) != current.get(key)}
    if changed != set(expected):
        raise ValueError('ebook projection differs outside the exact reviewed 51 rows')
    for key, row in expected.items():
        if previous.get(key) != row['before'] or current.get(key) != row['after']:
            raise ValueError('reviewed ebook route changed')
    return {'pointer': pointer, 'catalog_identity': identity, 'sidecar_sha256': digest(raw),
            'maintenance_id': maintenance.get('maintenance_id'),
            'consumers': {surface: consumers['receipts'][surface] for surface in ('hf', 'pages')}}


def _indexes(store):
    result = {}
    for ext in ('epub', 'azw3', 'fb2'):
        path = f'chapters/ebook/{ext}/index.json'
        raw = store.read_bytes(ASSETS, path)
        payload = json.loads(raw)
        rows = payload.get('files')
        if not isinstance(rows, list) or any(not isinstance(x, dict) or not isinstance(x.get('key'), str) for x in rows):
            raise ValueError('invalid canonical ebook index')
        mapping = {x['key']: x for x in rows}
        if len(mapping) != len(rows):
            raise ValueError('duplicate ebook index source')
        result[ext] = {'path': path, 'sha256': digest(raw), 'entries': mapping}
    return result


def prepare(store, client, plan):
    state = observe_state(store, client, plan)
    indexes = _indexes(store)
    writer = ProcessingWriter(store)
    actions, records = [], []
    for row in sorted(plan['entries'], key=lambda item: item['processing_path']):
        ext = row['key'].rsplit('.', 1)[-1].lower()
        entry = indexes[ext]['entries'].get(row['key'])
        root = row['after']['p'].rsplit('/', 1)[0]
        if (not isinstance(entry, dict) or entry.get('root') != root or entry.get('manifest') != row['after']['p']
                or entry.get('source_revision') != row['source_revision']
                or entry.get('source_sha256') != row['source_sha256']
                or entry.get('chapter_count') != row['chapter_count']
                or entry.get('processing_roots') != [row['processing_path']]):
            raise ValueError('canonical ebook entry differs from reviewed inventory')
        raw = store.read_bytes(ASSETS, entry['manifest'])
        if digest(raw) != row['manifest_sha256']:
            raise ValueError('ebook manifest differs from reviewed bytes')
        manifest = json.loads(raw)
        search = store.read_bytes(ASSETS, root + '/' + manifest['search_index']['path'])
        first = store.read_bytes(ASSETS, root + '/' + manifest['chapters'][0]['path'])
        checked = verify_bundle_payload(entry, raw, search, first)
        if (checked['search_index_sha256'] != row['search_index_sha256']
                or checked['first_chapter_sha256'] != row['first_chapter_sha256']):
            raise ValueError('ebook search or first chapter differs from reviewed bytes')
        path = row['processing_path']
        record = writer.read(path)
        if (record.get('kind') != 'reader-processing-roots' or record.get('version') != 1
                or record.get('operation') != 'reader-stream-upload'
                or record.get('release_policy') != 'explicit-publisher-handoff'
                or record.get('run') != {'repository': 'anftm/pipeline', 'run_id': SOURCE_RUN,
                                         'attempt': SOURCE_ATTEMPT}
                or record.get('resources') != [{'bucket': ASSETS, 'root': root}]):
            raise ValueError('ebook processing protection belongs to a different upload')
        lifecycle.validate_processing_handoff(record, entry)
        if record.get('status') == 'released':
            if record.get('generation') != plan['base_generation']:
                raise ValueError('ebook processing record released to a different generation')
            records.append({'path': path, 'status': 'already-released', 'generation': record['generation']})
            continue
        released = lifecycle.release_processing(record, plan['base_generation'])
        # Do not publish generic retirement hints here. They broaden a chapter
        # version to the whole source bundle and could invite later deletion of
        # older Reader chapters. This recovery changes only the 51 protections.
        actions.append((path, released))
        records.append({'path': path, 'status': 'ready', 'generation': plan['base_generation'],
                        'record_sha256': digest(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                                           separators=(',', ':')).encode())})
    for info in indexes.values():
        if digest(store.read_bytes(ASSETS, info['path'])) != info['sha256']:
            raise RuntimeError('canonical ebook index changed during handoff preflight')
    again = observe_state(store, client, plan)
    if (again['pointer'] != state['pointer'] or again['catalog_identity'] != state['catalog_identity']
            or again['sidecar_sha256'] != state['sidecar_sha256']
            or again['maintenance_id'] != state['maintenance_id']):
        raise RuntimeError('Reader or maintenance state changed during ebook handoff preflight')
    return writer, actions, {'version': 1, 'kind': 'reader-exact-ebook-processing-handoff-result',
        'inventory_sha256': MANIFEST_SHA256, 'source_run_id': SOURCE_RUN, 'v3_generation': V3_GENERATION,
        'base_generation': BASE_GENERATION, 'sidecar_sha256': SIDECAR_SHA256,
        'catalog_identity': again['catalog_identity'], 'maintenance_id': again['maintenance_id'],
        'index_sha256': {value['path']: value['sha256'] for value in indexes.values()},
        'consumers': again['consumers'], 'records': records, 'planned': len(actions),
        'already_released': len(records) - len(actions), 'complete': False, 'remote_mutations': 0}


def run(store, client, plan, *, apply=False, output=None):
    writer, actions, result = prepare(store, client, plan)
    if output:
        output.mkdir(parents=True, exist_ok=True)
        (output / 'result.json').write_bytes(publication.encode(result))
    if not apply:
        return result
    assert_workflow_context()
    run_id = os.environ['GITHUB_RUN_ID']
    path = RECEIPT_PREFIX + run_id + '.json'
    result['phase'] = 'prepared'
    store.put_bytes(ASSETS, path, publication.encode(result))
    if json.loads(store.read_bytes(ASSETS, path)) != result:
        raise RuntimeError('ebook handoff prepared receipt readback failed')
    result['receipt_updates'] = 1
    # Verify all fixed control identities after writing only a retained receipt.
    assert_no_foreign_producers()
    state = observe_state(store, client, plan)
    if state['catalog_identity'] != result['catalog_identity'] or state['maintenance_id'] != result['maintenance_id']:
        raise RuntimeError('Reader catalog or maintenance changed before ebook handoff')
    for index, expected in result['index_sha256'].items():
        if digest(store.read_bytes(ASSETS, index)) != expected:
            raise RuntimeError('ebook index changed before the first handoff')
    if state['pointer']['generation'] != V3_GENERATION:
        raise RuntimeError('ebook generation changed before handoff')
    for number, (record_path, released) in enumerate(actions, 1):
        if (number - 1) % 10 == 0:
            assert_no_foreign_producers()
            active = json.loads(store.read_bytes(ASSETS, 'reader-index/gc/maintenance/current.json'))
            if active.get('restored') is not True or active.get('maintenance_id') != result['maintenance_id']:
                raise RuntimeError('maintenance changed during ebook processing handoff')
        writer.publish(record_path, released)
        result['remote_mutations'] += 1
        result['released'] = number
        result['phase'] = 'releasing'
        if number % 10 == 0 or number == len(actions):
            result['receipt_updates'] += 1
            store.put_bytes(ASSETS, path, publication.encode(result))
            if json.loads(store.read_bytes(ASSETS, path)) != result:
                raise RuntimeError('ebook handoff progress readback failed')
            if output:
                (output / 'result.json').write_bytes(publication.encode(result))
    assert_no_foreign_producers()
    final = observe_state(store, client, plan)
    if final['catalog_identity'] != result['catalog_identity'] or final['maintenance_id'] != result['maintenance_id']:
        raise RuntimeError('Reader catalog or maintenance changed during ebook handoff')
    for index, expected in result['index_sha256'].items():
        if digest(store.read_bytes(ASSETS, index)) != expected:
            raise RuntimeError('ebook index changed during handoff')
    for row in plan['entries']:
        record = writer.read(row['processing_path'])
        if (record.get('status') != 'released' or record.get('generation') != plan['base_generation']
                or record.get('run') != {'repository': 'anftm/pipeline', 'run_id': SOURCE_RUN,
                                        'attempt': SOURCE_ATTEMPT}):
            raise RuntimeError('ebook processing final readback mismatch')
    result['receipt_updates'] += 1
    result.update(phase='complete', complete=True, verified=len(plan['entries']),
                  consumers_after=final['consumers'])
    store.put_bytes(ASSETS, path, publication.encode(result))
    if json.loads(store.read_bytes(ASSETS, path)) != result:
        raise RuntimeError('ebook handoff completion receipt readback failed')
    if output:
        (output / 'result.json').write_bytes(publication.encode(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('output/ebook-processing-handoff'))
    args = parser.parse_args()
    if args.apply:
        assert_workflow_context()
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        result = run(S3BucketStore(), client, load_manifest(), apply=args.apply, output=args.output)
    print(json.dumps({k: result.get(k) for k in ('planned', 'already_released', 'released',
                                                  'verified', 'complete', 'remote_mutations')}), flush=True)


if __name__ == '__main__':
    main()
