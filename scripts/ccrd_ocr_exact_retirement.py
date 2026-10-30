#!/usr/bin/env python3
"""Narrowly retire one withdrawn CCRD OCR release under the full GC fence.

The run owns no document/DjVu data. It may retire only the exact reviewed CCRD
targets. All other-bucket evidence cited by the original intent is transferred
to a separate immutable control root before any existing reference is changed.
No deletion is attempted until a complete *current*, all-bucket graph proves
the resulting targets unreferenced. A full maintenance producer fence is a
mandatory part of that proof, not an optional optimization.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

try:
    from . import ccrd_lifecycle as lifecycle, reader_gc_graph as graph, reader_gc_stream as graph_stream
    from . import reader_gc_collect as collector
    from .reader_gc_fence import GitHubProducerFence
    from .reader_gc_store import CcrdCollectionStore
except ImportError:
    import ccrd_lifecycle as lifecycle
    import reader_gc_graph as graph
    import reader_gc_stream as graph_stream
    import reader_gc_collect as collector
    from reader_gc_fence import GitHubProducerFence
    from reader_gc_store import CcrdCollectionStore


BUCKET = lifecycle.BUCKET
ASSETS = lifecycle.BUCKETS[0]
GENERATION = 'fb1abf531f3a630d65e77ae0167bd049ddf9a6c46b8758ac4ed0672eb992819e'
RESET_ID = 'ccrd-ocr-exact-20261030'
INTENT = f'control/intents/{GENERATION}.json'
EXTERNAL = f'control/ocr-preserved-reader-{GENERATION}.json'
IDS = 'control/ocr-ids.json'
CURRENT = 'control/ocr-current.json'
CONSUMER = 'control/ocr-consumer.json'
READER_CONSUMERS = 'reader-index/reader_gc_consumers.json'
REPORT_PREFIX = f'reports/ccrd-ocr-reset/{RESET_ID}/'
WITHDRAWAL = REPORT_PREFIX + 'withdrawal.json'
PLAN = REPORT_PREFIX + 'exact-plan.json'
RELEASE = f'releases/ocr-{GENERATION}/manifest.json'
CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config/ccrd-ocr-withdrawal-targets.json'
CONFIG_SHA = '20a6c61019e3d062f2c286e4b027d2e03ac048029a3e5400d62ea3553c713123'
INITIAL_HASHES = {
    CURRENT: 'cb9c1b35ffa33ce57ec7c1305c377a584f443b87ee1afef98a02d73517f0600d',
    INTENT: 'f1ef6ec599c7a41ff689bff679005f1737d65d9941633115f462c99c964813d2',
    IDS: '608bf0296809b12d4f4354dac2d7e13d2c59d37b32eacbb14861af341428b752',
    CONSUMER: '560a34a8728c2df38355fbf49357ad065ec8c5ca8bd790bacf79454e8e0c9a35',
}
READER_CONSUMERS_INITIAL_SHA = 'f230947e4e064e70c60017ececd7b25cd94f64bd933e30c7407613418754b7b6'
REPO = 'anftm/pipeline'
WORKFLOW = REPO + '/.github/workflows/ccrd-ocr-exact-remove.yml@refs/heads/main'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def now():
    return datetime.now(timezone.utc).isoformat()


def identity(ref):
    return (ref.get('bucket'), ref.get('path'), ref.get('bytes'), ref.get('sha256'))


def eligible_after_graph(report, references, targets, external):
    """Only an actual complete graph with no residual pins can authorize deletion."""
    if report.get('graph_complete') is not True or report.get('blockers'):
        raise ValueError('four-bucket reference graph is incomplete')
    if set(targets) & set(references):
        raise ValueError('reference graph still retains an approved OCR target')
    if not set(external) <= set(references):
        raise ValueError('reference graph does not preserve every Reader provenance reference')
    if report.get('ambiguous_paths'):
        raise ValueError('reference graph contains unresolved bucket-ambiguous paths')


def load_targets():
    raw = CONFIG_PATH.read_bytes()
    if sha(raw) != CONFIG_SHA:
        raise ValueError('the approved exact target list changed')
    value = json.loads(raw)
    rows = value.get('targets')
    if (value.get('version') != 1 or value.get('kind') != 'ccrd-exact-ocr-withdrawal-targets'
            or value.get('bucket') != BUCKET or value.get('generation') != GENERATION
            or not isinstance(rows, list) or len(rows) != 14):
        raise ValueError('invalid target list identity')
    paths = set()
    for row in rows:
        path, digest, size = row.get('path'), row.get('sha256'), row.get('bytes')
        lifecycle.resource({'bucket': BUCKET, 'path': path, 'sha256': digest, 'bytes': size, 'role': 'runtime'})
        if (path in paths or not re.fullmatch(
                r'(?:text/objects/[0-9a-f]{2}/[0-9a-f]{64}\.txt|'
                r'text/exports/[0-9a-f]{64}/[0-9a-f]{64}/manifest\.json|'
                r'indexes/ocr/[0-9a-f]{64}/OCR\.sqlite3|releases/ocr-[0-9a-f]{64}/manifest\.json)', path)):
            raise ValueError('target path is duplicated or outside the exact OCR data tier')
        paths.add(path)
    if sum(row['bytes'] for row in rows) != 19137371:
        raise ValueError('target byte total differs from the reviewed list')
    return sorted(rows, key=lambda row: row['path'])


def assert_writer():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_WORKFLOW_REF') != WORKFLOW or not os.environ.get('GITHUB_RUN_ID')):
        raise RuntimeError('exact OCR retirement must run inside its central maintenance workflow')


def read_json(store, bucket, path):
    raw = store.read_bytes(bucket, path)
    return json.loads(raw), raw


def read_optional(store, bucket, path):
    """Optional controls use a fresh read; adapters map an actual 404 to None."""
    try:
        return store.read_bytes(bucket, path)
    except FileNotFoundError:
        return None


def current_status(client, expected_base=None):
    response = client.get('https://vomebook-search.hf.space/api/index-status',
                          headers={'Cache-Control': 'no-cache'}, timeout=60)
    response.raise_for_status()
    status = response.json()
    if (status.get('ready') is not True or status.get('ocr_release') is not None
            or not {'CCRD', 'CW', 'RMRB', 'PLA'} <= set(status.get('loaded_sources') or [])):
        raise ValueError('search consumer has not withdrawn OCR while retaining base sources')
    if expected_base and (status.get('bucket') or {}).get('generation') != expected_base:
        raise ValueError('base search generation changed')
    return status


def verify_targets(store, rows):
    """All 14 sizes and SHA-256 values must match before a retirement plan exists."""
    expected = {row['path'] for row in rows}
    listed = store.list_files(BUCKET, lifecycle.MANAGED_PREFIXES)
    if listed != expected:
        raise ValueError('OCR managed object set differs from the exact 14 reviewed targets')
    for row in rows:
        raw = store.read_bytes(BUCKET, row['path'])
        if len(raw) != row['bytes'] or sha(raw) != row['sha256']:
            raise ValueError('target bytes differ: ' + row['path'])


def verify_release_members(store, pointer, rows, intent_all):
    targets = {(BUCKET, row['path'], row['bytes'], row['sha256']) for row in rows}
    release, raw = read_json(store, BUCKET, RELEASE)
    ref = pointer['release']
    if (ref['path'] != RELEASE or len(raw) != ref['bytes'] or sha(raw) != ref['sha256']
            or release.get('generation') != GENERATION):
        raise ValueError('release and pointer differ')
    lifecycle.references(release, RELEASE)
    members = {identity(ref), identity(release['database'])}
    for export in release['exports']:
        raw = store.read_bytes(BUCKET, export['path'])
        if len(raw) != export['bytes'] or sha(raw) != export['sha256']:
            raise ValueError('export checksum changed')
        value = json.loads(raw)
        linked = lifecycle.references(value, export['path'])
        for retained in linked:
            if retained['bucket'] != BUCKET and identity(retained) not in intent_all:
                raise ValueError('export provenance is absent from the preservation intent')
        members.update((identity(export), identity(value['text'])))
    if members != targets:
        raise ValueError('release members are not exactly the 14 approved targets')


def inspect_roots(store, reader_ref, expected_controls=None, require_initial=True):
    """Read every CCRD root; no unapproved root may point at any target."""
    paths = {p for p in store.list_files(BUCKET, ('control/', 'current.json')) if lifecycle.is_root(p)}
    digests, payloads = {}, {}
    for path in sorted(paths):
        value, raw = read_json(store, BUCKET, path)
        lifecycle.references(value, path)
        digests[path] = sha(raw)
        payloads[path] = value
    if expected_controls is not None and digests != expected_controls:
        raise ValueError('CCRD control root set or bytes changed')
    required = {INTENT, IDS, CONSUMER} | ({CURRENT} if require_initial else set())
    if not required <= set(digests):
        raise ValueError('required OCR roots are missing')
    return digests, payloads


def create_plan(store, client, rows, save_local, *, expected_reader_consumer_sha=READER_CONSUMERS_INITIAL_SHA):
    controls, payloads = inspect_roots(store, None)
    for path, expected in INITIAL_HASHES.items():
        if controls.get(path) != expected:
            raise ValueError('initial OCR control changed: ' + path)
    pointer, intent, ids, consumer = (payloads[p] for p in (CURRENT, INTENT, IDS, CONSUMER))
    if (pointer.get('generation') != GENERATION or pointer.get('parent') is not None
            or intent.get('generation') != GENERATION or intent.get('status') != 'committed'
            or intent.get('expected_parent') is not None
            or consumer.get('status') != 'consumer-withdrawn'
            or consumer.get('previous_generation') != GENERATION
            or consumer.get('pointer_sha256') != controls[CURRENT] or consumer.get('resources') != []):
        raise ValueError('the withdrawn OCR control state is not the reviewed one')
    existing_targets = {(BUCKET, row['path'], row['bytes'], row['sha256']) for row in rows}
    intent_targets = {identity(ref) for ref in intent['resources'] if ref.get('bucket') == BUCKET}
    if intent_targets != existing_targets:
        raise ValueError('OCR intent does not own exactly the approved 14 target identities')
    external = [copy.deepcopy(ref) for ref in intent['resources'] if ref.get('bucket') != BUCKET]
    if len(external) != 2531 or len({(x['bucket'], x['path']) for x in external}) != len(external):
        raise ValueError('external Reader provenance set changed')
    if EXTERNAL in controls:
        raise ValueError('reserved external provenance control already exists')
    for path, value in payloads.items():
        if path in (CURRENT, INTENT, IDS):
            continue
        overlap = {identity(ref) for ref in lifecycle.references(value, path)} & existing_targets
        if overlap:
            raise ValueError('another CCRD control retains an approved target: ' + path)
    if [identity(ref) for ref in ids.get('resources', [])] != [identity(pointer['release'])]:
        raise ValueError('OCR ID registry has an unexpected release reference')
    verify_targets(store, rows)
    verify_release_members(store, pointer, rows, {identity(ref) for ref in intent['resources']})
    status = current_status(client, consumer.get('base_generation'))
    catalog, consumers = collector.observe_projections(store, client)
    receipt = consumers.get('receipts', {}).get('ccrd', {})
    if not receipt.get('consumer_withdrawn') or receipt.get('generation') != GENERATION:
        raise ValueError('both Reader surfaces and withdrawn OCR consumer were not acknowledged')
    current_consumer, old_consumer_raw = read_json(store, ASSETS, READER_CONSUMERS)
    if expected_reader_consumer_sha and sha(old_consumer_raw) != expected_reader_consumer_sha:
        raise ValueError('Reader consumer receipt changed since the approved snapshot')
    old_refs = current_consumer.get('resources')
    if (current_consumer.get('kind') != 'reader-gc-consumers' or not isinstance(old_refs, list)
            or not isinstance(current_consumer.get('receipts'), dict)):
        raise ValueError('unexpected existing Reader consumer receipt')
    release_id = identity(pointer['release'])
    if sum(identity(ref) == release_id for ref in old_refs) != 1:
        raise ValueError('existing Reader consumer does not name the exact OCR release once')
    if any((ref.get('bucket'), ref.get('path')) in {(row[0], row[1]) for row in existing_targets}
           and identity(ref) != release_id for ref in old_refs):
        raise ValueError('Reader consumer directly references another approved target')
    retained = [copy.deepcopy(ref) for ref in old_refs if identity(ref) != release_id]
    new_consumers = copy.deepcopy(current_consumer)
    new_consumers['resources'] = retained
    new_consumers['receipts']['ccrd'] = {
        'checked_url': 'https://vomebook-search.hf.space/api/index-status',
        'checked_at': now(), 'projection_verified': True, 'consumer_withdrawn': True,
        'previous_generation': GENERATION, 'release_sha256': pointer['release']['sha256'],
    }
    new_intent = copy.deepcopy(intent)
    new_intent.update(status='retired', resources=[], retirement_reason='explicit-document-reset', reset_id=RESET_ID)
    new_ids = copy.deepcopy(ids)
    new_ids.update(status='withdrawn', resources=[], reset_id=RESET_ID)
    external_control = {'version': 1, 'kind': 'ccrd-ocr-retention', 'status': 'committed',
        'resources': external, 'reason': 'preserve Reader provenance after OCR search retirement',
        'generation': GENERATION}
    lifecycle.references(external_control, EXTERNAL)
    retirement = {'version': 1, 'kind': 'ccrd-ocr-reset', 'reset_id': RESET_ID,
        'status': 'withdrawn', 'applied': True, 'consumer': status, 'previous_pointer': pointer,
        'retired_control': [{'path': INTENT, 'generation': GENERATION,
                             'sha256': controls[INTENT], 'bytes': len(store.read_bytes(BUCKET, INTENT)),
                             'status': 'committed'}],
        'preserved_reader_provenance': {'path': EXTERNAL, 'resources': len(external)},
        'targets': rows}
    plan = {'version': 1, 'kind': 'ccrd-exact-ocr-retirement-plan', 'created_at': now(),
        'generation': GENERATION, 'target_config_sha256': CONFIG_SHA, 'targets': rows,
        'control_hashes_before': controls, 'reader_consumer_sha256_before': sha(old_consumer_raw),
        'pointer_before': pointer, 'base_generation': consumer['base_generation'],
        'catalog_identity': catalog, 'reader_acknowledgments': consumers['receipts'],
        'updates': {'external': external_control, 'reader_consumers': new_consumers,
                    'intent': new_intent, 'ids': new_ids, 'withdrawal': retirement},
        'external_resource_count': len(external), 'removed_reader_reference_count': 1,
        'remote_data_deletions': 0}
    save_local(plan)
    return plan


def verify_saved_plan(store, client, rows, saved):
    """Reconstruct a saved dry-run plan from current state before using it.

    The durable plan is not authority by itself. Apart from observation times,
    its complete proposed update must match a freshly checked intent, external
    provenance set, Reader consumer receipt, and live consumer projections.
    Partially mutated controls fail create_plan and need explicit recovery.
    """
    fresh = create_plan(store, client, rows, lambda _: None)

    def normalize(value):
        result = copy.deepcopy(value)
        result.pop('created_at', None)
        for receipt in result.get('reader_acknowledgments', {}).values():
            receipt.pop('checked_at', None)
        result['updates']['reader_consumers']['receipts']['ccrd'].pop('checked_at', None)
        return result

    if normalize(saved) != normalize(fresh):
        raise ValueError('saved plan differs from the freshly verified controls and consumers')


def post_control_hashes(plan):
    value = dict(plan['control_hashes_before'])
    value.pop(CURRENT)
    value[EXTERNAL] = sha(encode(plan['updates']['external']))
    value[INTENT] = sha(encode(plan['updates']['intent']))
    value[IDS] = sha(encode(plan['updates']['ids']))
    return value


def simulate_retired_graph(store, plan, fence, *, deadline_minutes=300, workers=8):
    """Full four-bucket traversal of the post-retirement control view, read-only."""
    overrides = {(BUCKET, EXTERNAL): encode(plan['updates']['external']),
                 (BUCKET, INTENT): encode(plan['updates']['intent']),
                 (BUCKET, IDS): encode(plan['updates']['ids']),
                 (ASSETS, READER_CONSUMERS): encode(plan['updates']['reader_consumers'])}
    hidden = {(BUCKET, CURRENT)}

    class View:
        def __init__(self):
            self.object_sizes = store.object_sizes
            self.read_etags = {}
            self.root_etags = {}

        def list_files(self, bucket, prefixes):
            files = set(store.list_files(bucket, prefixes))
            files.difference_update(path for b, path in hidden if b == bucket)
            files.update(path for b, path in overrides if b == bucket and path.startswith(prefixes))
            return files

        def list_roots(self, bucket, parents):
            # The wrapped store may not have the graph's root predicate set:
            # filter explicitly so telemetry and retired snapshots are not
            # accidentally interpreted as new live roots by the simulation.
            roots = {path: etag for path, etag in store.list_roots(bucket, parents).items()
                     if graph.is_root(path, bucket)}
            for b, path in hidden:
                if b == bucket:
                    roots.pop(path, None)
            for b, path in overrides:
                if b == bucket and graph.is_root(path, bucket):
                    roots[path] = None
            return roots

        def read_bytes(self, bucket, path):
            if (bucket, path) in hidden:
                raise FileNotFoundError(path)
            if (bucket, path) in overrides:
                return overrides[(bucket, path)]
            return store.read_bytes(bucket, path)

        def read_graph_json(self, bucket, path, parser=None):
            if (bucket, path) in hidden:
                raise FileNotFoundError(path)
            if (bucket, path) in overrides:
                raw = overrides[(bucket, path)]
                if parser is None:
                    parser = graph_stream.graph_json
                return parser([raw], path.endswith('.gz'))
            return store.read_graph_json(bucket, path, parser=parser)

        def digest(self, bucket, path):
            if (bucket, path) in overrides:
                return sha(overrides[(bucket, path)])
            return store.digest(bucket, path)

    fence.check()
    ref_graph = graph.ReferenceGraph(View(), workers=workers, max_rounds=40,
                                     deadline=time.monotonic() + deadline_minutes * 60)
    report = ref_graph.build()
    external = {(r['bucket'], r['path']) for r in plan['updates']['external']['resources']}
    eligible_after_graph(report, ref_graph.references,
                         {(BUCKET, row['path']) for row in plan['targets']}, external)
    fence.check()
    # Re-list every root before checking fingerprints: checking previously
    # listed ETags alone cannot detect newly added roots or a stale listing.
    collector.verify_roots(ref_graph.store, ref_graph)
    controls, _ = inspect_roots(store, None)
    if controls != plan['control_hashes_before']:
        raise ValueError('CCRD controls changed while the reference graph was scanned')
    if sha(store.read_bytes(ASSETS, READER_CONSUMERS)) != plan['reader_consumer_sha256_before']:
        raise ValueError('Reader consumer receipt changed while the reference graph was scanned')
    return {'graph_complete': True, 'scanned_at': report['scanned_at'],
            'bucket_object_counts': {b: v['objects'] for b, v in report['buckets'].items()},
            'root_count': len(report['roots']), 'target_references': 0,
            'external_provenance_references': len(external),
            'graph_snapshot_sha256': sha(encode(report)), 'deletion_authorized': False}


def ensure_bytes(store, bucket, path, expected, replacement):
    current = read_optional(store, bucket, path)
    if current == replacement:
        return
    if current is None and expected != 'never' or current is not None and sha(current) != expected:
        raise ValueError('root changed before retirement: ' + bucket + ':' + path)
    store.put_bytes(bucket, path, replacement)
    if store.read_bytes(bucket, path) != replacement:
        raise ValueError('root readback differs after retirement: ' + path)


def apply_reference_retirement(store, plan, fence, client, checkpoint):
    """Preserve outside references, retire only target refs, then remove pointer."""
    fence.check()
    current_status(client, plan['base_generation'])
    controls, _ = inspect_roots(store, None, require_initial=False)
    allowed = dict(plan['control_hashes_before'])
    allowed[EXTERNAL] = sha(encode(plan['updates']['external']))
    if set(controls) - set(allowed) or set(allowed) - set(controls) - {EXTERNAL, CURRENT}:
        raise ValueError('CCRD control inventory changed before retirement')
    for path, actual in controls.items():
        expected = allowed[path]
        if path == INTENT:
            valid = {expected, sha(encode(plan['updates']['intent']))}
        elif path == IDS:
            valid = {expected, sha(encode(plan['updates']['ids']))}
        else:
            valid = {expected}
        if actual not in valid:
            raise ValueError('CCRD root changed before retirement: ' + path)
    reader_bytes = store.read_bytes(ASSETS, READER_CONSUMERS)
    if sha(reader_bytes) not in {plan['reader_consumer_sha256_before'],
                                 sha(encode(plan['updates']['reader_consumers']))}:
        raise ValueError('Reader consumers changed before retirement')
    verify_targets(store, plan['targets'])
    # Reader's catalog can exceed 2 GB: verify the exact digest by streaming,
    # never materialize the shared catalog in memory.
    if collector.snapshot_digest(store, ASSETS, collector.CATALOG) != plan['catalog_identity']['digest']:
        raise ValueError('Reader catalog changed after reference graph verification')
    saved_withdrawal = read_optional(store, BUCKET, WITHDRAWAL)
    if saved_withdrawal is not None and saved_withdrawal != encode(plan['updates']['withdrawal']):
        raise ValueError('an unrelated OCR withdrawal receipt already exists')
    # Re-prove Reader surfaces immediately before reference mutation, not just
    # before a potentially hours-long graph scan.
    if CURRENT in controls:
        current_catalog, fresh_consumers = collector.observe_projections(store, client)
        if current_catalog != plan['catalog_identity']:
            raise ValueError('Reader catalog moved before retirement')
        for surface in ('hf', 'pages'):
            old = plan['reader_acknowledgments'][surface]
            fresh = fresh_consumers['receipts'][surface]
            for field in ('generation', 'catalog_sha256', 'base_sidecar_sha256', 'deployed_projection_sha256'):
                if old.get(field) != fresh.get(field):
                    raise ValueError('Reader consumer changed before retirement: ' + surface)
    updates = plan['updates']
    external_raw = encode(updates['external'])
    existing = read_optional(store, BUCKET, EXTERNAL)
    if existing is None:
        store.put_bytes(BUCKET, EXTERNAL, external_raw)
    else:
        if existing != external_raw:
            raise ValueError('external provenance preservation root conflicts')
    if store.read_bytes(BUCKET, EXTERNAL) != external_raw:
        raise ValueError('external provenance preservation readback failed')
    checkpoint('external-provenance-preserved')
    ensure_bytes(store, BUCKET, WITHDRAWAL, 'never', encode(updates['withdrawal']))
    ensure_bytes(store, ASSETS, READER_CONSUMERS, plan['reader_consumer_sha256_before'], encode(updates['reader_consumers']))
    checkpoint('reader-consumer-reference-retired')
    ensure_bytes(store, BUCKET, INTENT, plan['control_hashes_before'][INTENT], encode(updates['intent']))
    ensure_bytes(store, BUCKET, IDS, plan['control_hashes_before'][IDS], encode(updates['ids']))
    checkpoint('ocr-intent-and-id-reference-retired')
    fence.check()
    current_status(client, plan['base_generation'])
    current_raw = read_optional(store, BUCKET, CURRENT)
    if current_raw is not None:
        if sha(current_raw) != plan['control_hashes_before'][CURRENT]:
            raise ValueError('OCR current pointer changed before retirement')
        store.delete(BUCKET, [CURRENT])
    if read_optional(store, BUCKET, CURRENT) is not None:
        raise ValueError('OCR current pointer still exists')
    inspect_roots(store, None, expected_controls=post_control_hashes(plan), require_initial=False)
    checkpoint('ocr-current-pointer-retired')


def verify_reference_retirement(store, plan):
    controls, payloads = inspect_roots(store, None, expected_controls=post_control_hashes(plan), require_initial=False)
    if CURRENT in controls:
        raise ValueError('OCR current pointer was recreated')
    targets = {(BUCKET, row['path'], row['bytes'], row['sha256']) for row in plan['targets']}
    for path, value in payloads.items():
        if {identity(ref) for ref in lifecycle.references(value, path)} & targets:
            raise ValueError('a control root still pins an approved target: ' + path)
    if store.read_bytes(ASSETS, READER_CONSUMERS) != encode(plan['updates']['reader_consumers']):
        raise ValueError('Reader consumer references changed after retirement')
    if store.read_bytes(BUCKET, WITHDRAWAL) != encode(plan['updates']['withdrawal']):
        raise ValueError('OCR withdrawal receipt changed')
    if store.read_bytes(BUCKET, EXTERNAL) != encode(plan['updates']['external']):
        raise ValueError('Reader provenance preservation root changed')


def delete_exact(store, plan, fence, client, checkpoint):
    """Delete only targets following graph completion and immediately rechecked roots."""
    verify_reference_retirement(store, plan)
    rows = plan['targets']
    existing = store.list_files(BUCKET, lifecycle.MANAGED_PREFIXES)
    targets = {row['path'] for row in rows}
    if not existing <= targets:
        raise ValueError('unexpected OCR managed objects appeared after retirement')
    reference_graph = graph.ReferenceGraph(store, workers=8, max_rounds=40,
                                           deadline=time.monotonic() + 300 * 60)
    actual_graph = reference_graph.build()
    external = {(r['bucket'], r['path']) for r in plan['updates']['external']['resources']}
    eligible_after_graph(actual_graph, reference_graph.references,
                         {(BUCKET, path) for path in targets}, external)
    if not existing <= set(actual_graph['buckets'][BUCKET]['candidates']):
        raise ValueError('current complete graph has not admitted every existing target as a candidate')
    verify_reference_retirement(store, plan)
    # The graph must stay current across the potentially long scan. Producer
    # isolation is checked again below; each recorded root is independently
    # revalidated against its last observed hash or listed S3 ETag.
    fence.check()
    current_status(client, plan['base_generation'])
    collector.verify_roots(store, reference_graph)
    for row in rows:
        if row['path'] in existing:
            raw = store.read_bytes(BUCKET, row['path'])
            if len(raw) != row['bytes'] or sha(raw) != row['sha256']:
                raise ValueError('target changed before deletion: ' + row['path'])
    fence.check()
    current_status(client, plan['base_generation'])
    checkpoint('delete-submitted')
    store.delete(BUCKET, sorted(existing))
    remaining = store.list_files(BUCKET, lifecycle.MANAGED_PREFIXES)
    if remaining:
        raise ValueError('OCR managed objects remain after deletion: ' + repr(sorted(remaining)))
    # Listing alone is not enough: check every exact key through a fresh GET.
    for row in rows:
        if read_optional(store, BUCKET, row['path']) is not None:
            raise ValueError('deleted OCR object is still directly readable: ' + row['path'])
    public_sample = 'https://huggingface.co/buckets/' + BUCKET + '/resolve/' + next(
        row['path'] for row in rows if row['path'].endswith('.txt'))
    public = client.get(public_sample, headers={'Range': 'bytes=0-0', 'Cache-Control': 'no-cache'}, timeout=60)
    if public.status_code not in {404, 410}:
        raise ValueError('an OCR text object remains directly publicly readable')
    verify_reference_retirement(store, plan)
    fence.check()
    current_status(client, plan['base_generation'])
    checkpoint('14-objects-verified-absent')
    return {'confirmed_deleted_objects': len(rows), 'confirmed_deleted_bytes': sum(r['bytes'] for r in rows)}


def run(output: Path, client, *, delete=False, resume=False, graph_deadline=300, workers=8):
    assert_writer()
    rows = load_targets()
    store = CcrdCollectionStore()
    fence = GitHubProducerFence()
    state = {'kind': 'ccrd-exact-ocr-retirement-run', 'started_at': now(), 'complete': False,
             'delete_requested': delete, 'phase': 'starting', 'confirmed_deleted_objects': 0,
             'target_count': len(rows), 'config_sha256': CONFIG_SHA}
    output.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint(phase):
        state['phase'] = phase
        state['updated_at'] = now()
        output.write_bytes(encode(state))
        store.put_bytes(BUCKET, REPORT_PREFIX + 'progress.json', encode(state))
        if store.read_bytes(BUCKET, REPORT_PREFIX + 'progress.json') != encode(state):
            raise ValueError('progress checkpoint readback failed')

    def save_plan(plan):
        ensure_bytes(store, BUCKET, PLAN, 'never', encode(plan))

    try:
        fence.check()
        if resume:
            if not delete:
                raise ValueError('resume requires the previously approved delete operation')
            plan, _ = read_json(store, BUCKET, PLAN)
            prior, _ = read_json(store, BUCKET, REPORT_PREFIX + 'progress.json')
            if (plan.get('kind') != 'ccrd-exact-ocr-retirement-plan' or plan.get('targets') != rows
                    or plan.get('target_config_sha256') != CONFIG_SHA
                    or plan.get('generation') != GENERATION or plan.get('version') != 1):
                raise ValueError('saved plan does not match the exact approved targets')
            verify_saved_plan(store, client, rows, plan)
            state['resumed'] = True
            state['prior_phase'] = prior.get('phase')
            checkpoint('four-bucket-simulation')
            # Never use a saved dry-run result as deletion authority. A resumed
            # operation must prove the current closure with this code version.
            state['graph'] = simulate_retired_graph(store, plan, fence, deadline_minutes=graph_deadline, workers=workers)
            checkpoint('full-graph-verified')
        else:
            checkpoint('planning')
            plan = create_plan(store, client, rows, save_plan)
            checkpoint('four-bucket-simulation')
            state['graph'] = simulate_retired_graph(store, plan, fence, deadline_minutes=graph_deadline, workers=workers)
            checkpoint('full-graph-verified')
        if delete:
            apply_reference_retirement(store, plan, fence, client, checkpoint)
            verify_reference_retirement(store, plan)
            state['result'] = delete_exact(store, plan, fence, client, checkpoint)
            state['confirmed_deleted_objects'] = state['result']['confirmed_deleted_objects']
        state.update(phase='complete', complete=True, finished_at=now())
        checkpoint('complete')
        return state
    except Exception as error:
        state.update(complete=False, error_type=type(error).__name__, error=str(error), failed_at=now())
        output.write_bytes(encode(state))
        try:
            store.put_bytes(BUCKET, REPORT_PREFIX + 'progress.json', encode(state))
        except Exception:
            pass
        raise
