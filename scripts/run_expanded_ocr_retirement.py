#!/usr/bin/env python3
"""Withdraw only the reviewed OCR/PS inventory under durable isolation."""
from __future__ import annotations

import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx

try:
    from . import ccrd_ocr_exact_retirement as exact, expanded_ocr_retirement as expanded
    from . import reader_gc_collect as collector, reader_gc_maintenance as maintenance
    from .reader_bucket_store import S3BucketStore, retry_s3
    from .reader_gc_fence import GitHubProducerFence, assert_workflow_context
    from .reader_gc_store import CcrdCollectionStore
except ImportError:
    import ccrd_ocr_exact_retirement as exact
    import expanded_ocr_retirement as expanded
    import reader_gc_collect as collector
    import reader_gc_maintenance as maintenance
    from reader_bucket_store import S3BucketStore, retry_s3
    from reader_gc_fence import GitHubProducerFence, assert_workflow_context
    from reader_gc_store import CcrdCollectionStore


WORKFLOW = '.github/workflows/expanded-ocr-retirement.yml'
OUTPUT = Path('output/expanded-ocr-retirement')
INVENTORY = Path(__file__).resolve().parents[1] / 'config/expanded-ocr-ps-inventory-20261030.json'
PREFIX = 'reports/ccrd-ocr-reset/expanded-ocr-ps-20261030/'
PLAN = PREFIX + 'plan.json'
PLAN_DIGEST = PREFIX + 'plan.sha256'
PROGRESS = PREFIX + 'progress.json'


class ExactStore(CcrdCollectionStore):
    """Permit only the fixed inventory and the one retired CCRD pointer."""
    def __init__(self, rows):
        super().__init__()
        # Deletion authority must not reuse parsed cache entries selected by
        # stale object listings, including the potentially large Reader catalog.
        self.read_cache = None
        self.allowed = {(row['bucket'], row['path']) for row in rows}
        self.allowed.add((expanded.CCRD, exact.CURRENT))

    def delete(self, bucket, paths):
        if not paths or len(paths) != len(set(paths)) or any((bucket, path) not in self.allowed for path in paths):
            raise ValueError('deletion exceeds the reviewed OCR/PS inventory')
        # Bypass CcrdCollectionStore's single-bucket restriction only after the
        # exact four-bucket identity check above; S3 still reports per-key errors.
        S3BucketStore.delete(self, bucket, paths)

    def assert_buckets_available(self):
        for bucket in expanded.graph.BUCKETS:
            namespace, name = self._location(bucket)
            retry_s3(lambda: self._client(namespace).head_bucket(Bucket=name))

    def read_bytes(self, bucket, path):
        try:
            return super().read_bytes(bucket, path)
        except FileNotFoundError as error:
            cause = error.__cause__
            response = getattr(cause, 'response', {})
            code = response.get('Error', {}).get('Code')
            if code == 'NoSuchBucket' or (code is None and self._bucket_missing(bucket)):
                raise RuntimeError('bucket unavailable while checking object absence: ' + bucket) from error
            raise

    def _bucket_missing(self, bucket):
        namespace, name = self._location(bucket)
        try:
            retry_s3(lambda: self._client(namespace).head_bucket(Bucket=name))
            return False
        except FileNotFoundError:
            return True


def save_plan(store, plan):
    raw = expanded.encode(plan)
    exact.ensure_bytes(store, expanded.CCRD, PLAN, 'never', raw)
    exact.ensure_bytes(store, expanded.CCRD, PLAN_DIGEST, 'never', (expanded.sha(raw) + '\n').encode())
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'plan.json').write_bytes(raw)


def load_plan(store, rows):
    raw = store.read_bytes(expanded.CCRD, PLAN)
    digest_raw = exact.read_optional(store, expanded.CCRD, PLAN_DIGEST)
    digest = digest_raw.decode('ascii').strip() if digest_raw else None
    if digest is not None and expanded.sha(raw) != digest:
        raise ValueError('saved OCR/PS plan digest mismatch')
    plan = json.loads(raw)
    expanded.assert_same_inventory(plan, rows)
    return plan


def save_progress(store, state, phase):
    state['phase'] = phase
    state['updated_at'] = exact.now()
    raw = expanded.encode(state)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'result.json').write_bytes(raw)
    store.put_bytes(expanded.CCRD, PROGRESS, raw)
    if store.read_bytes(expanded.CCRD, PROGRESS) != raw:
        raise ValueError('OCR/PS progress checkpoint readback failed')
    print(json.dumps({'phase': phase, 'confirmed_deleted_objects':
                      state.get('deletion', {}).get('confirmed_deleted_objects', 0)}), flush=True)


def check_reader_consumers(store, client, plan):
    exact.current_status(client, plan['base_generation'])
    catalog, consumers = collector.observe_projections(store, client)
    if catalog != plan['catalog_identity']:
        raise ValueError('Reader catalog changed since retirement planning')
    for surface in ('hf', 'pages'):
        old = plan['reader_acknowledgments'][surface]
        fresh = consumers['receipts'][surface]
        for field in ('generation', 'catalog_sha256', 'base_sidecar_sha256', 'deployed_projection_sha256'):
            if old.get(field) != fresh.get(field):
                raise ValueError('Reader consumer changed: ' + surface)


def check_current_reader_coverage(store, plan):
    """Fresh consumer metadata must retain every current Reader resource."""
    current, _ = exact.read_json(store, expanded.ASSETS, exact.READER_CONSUMERS)
    actual = {(r['bucket'], r['path']) for r in current.get('resources', [])}
    projected = set()
    for surface in ('hf', 'pages'):
        receipt = plan['reader_acknowledgments'][surface]
        if not receipt.get('projection_verified', True):
            raise ValueError('Reader surface projection is not verified: ' + surface)
    base = expanded.graph.decode(store.read_bytes(expanded.ASSETS, collector.SIDECAR))
    _, catalog = collector.current(store)
    projection = collector.project_sidecar(base, catalog)
    projected.update((r['bucket'], r['path']) for r in collector.lifecycle.sidecar_references(projection))
    if not projected <= actual:
        raise ValueError('Reader consumer root does not retain the current projected resources')


def check_simulated_roots_before_mutation(store, plan, details, *, allow_partial=False):
    """Bind the long simulated traversal to fresh, unchanged live controls."""
    ref_graph = details['reference_graph']
    collector.verify_roots(ref_graph.store, ref_graph)
    simulated_overrides = {(expanded.CCRD, exact.EXTERNAL),
                           (expanded.CCRD, exact.INTENT), (expanded.CCRD, exact.IDS),
                           (expanded.ASSETS, exact.READER_CONSUMERS)}
    simulated_overrides.update((expanded.ASSETS, path) for path in plan['processing_updates'])
    before_controls, _ = exact.inspect_roots(store, None, require_initial=False)
    if (not set(before_controls) <= (set(plan['control_hashes_before']) | {exact.EXTERNAL})
            or not (set(plan['control_hashes_before']) - {exact.CURRENT}) <= set(before_controls)):
        raise ValueError('live CCRD control inventory changed after simulation')
    def check_live_root(ref):
        bucket, path = ref
        try:
            current_sha = store.digest(bucket, path)
        except FileNotFoundError:
            raise ValueError('live root disappeared after simulation: ' + bucket + ':' + path) from None
        if current_sha != ref_graph.root_hashes[ref]:
            raise ValueError('live root changed after simulation: ' + bucket + ':' + path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(check_live_root, sorted(set(ref_graph.root_hashes) - simulated_overrides)))
    if allow_partial:
        expanded.check_partial_controls(store, plan)
        return
    controls, _ = exact.inspect_roots(store, None, expected_controls=plan['control_hashes_before'])
    if controls != plan['control_hashes_before']:
        raise ValueError('CCRD controls changed after simulation')
    if expanded.sha(store.read_bytes(expanded.ASSETS, exact.READER_CONSUMERS)) != plan['reader_consumer_sha256_before']:
        raise ValueError('Reader consumer root changed after simulation')
    for path, change in plan['processing_updates'].items():
        if expanded.sha(store.read_bytes(expanded.ASSETS, path)) != change['sha256_before']:
            raise ValueError('processing root changed after simulation: ' + path)


def require_simulation(proof, rows):
    if (proof.get('graph_complete') is not True or proof.get('target_count') != len(rows)
            or proof.get('target_exclusivity_proven') is not True or proof.get('retained_target_count') != 0
            or proof.get('non_target_origins') or proof.get('tree_roots_without_origin') != 0
            or proof.get('actual_retired_controls_checked') is not False):
        raise ValueError('fresh simulated four-bucket graph did not prove zero target references')


def run(store, fence, client, rows, *, resume=False):
    """The caller holds both workflow locks and the durable maintenance fence."""
    store.assert_buckets_available()
    fence.check()
    if resume:
        plan = load_plan(store, rows)
        plan_digest = exact.read_optional(store, expanded.CCRD, PLAN_DIGEST)
        raw_progress = exact.read_optional(store, expanded.CCRD, PROGRESS)
        if raw_progress is not None and plan_digest is None:
            raise ValueError('saved OCR/PS progress lacks its immutable plan digest')
        if raw_progress is None:
            # The immutable plan may have been saved immediately before a
            # runner died. With no progress or deletions, reverify and continue.
            expanded.verify_exact_targets(store, rows)
            exact.inspect_roots(store, None, expected_controls=plan['control_hashes_before'])
            if expanded.sha(store.read_bytes(expanded.ASSETS, exact.READER_CONSUMERS)) != plan['reader_consumer_sha256_before']:
                raise ValueError('Reader consumers changed since the saved plan')
            state = {'version': 1, 'kind': 'expanded-ocr-retirement', 'started_at': exact.now(),
                     'inventory_sha256': expanded.INVENTORY_SHA256,
                     'plan_sha256': expanded.sha(expanded.encode(plan)), 'target_count': len(rows),
                     'target_bytes': sum(row['bytes'] for row in rows), 'complete': False,
                     'deletion': {'batches': [], 'confirmed_deleted_objects': 0, 'confirmed_deleted_bytes': 0}}
            save_plan(store, plan)
            save_progress(store, state, 'plan-saved')
        else:
            state = json.loads(raw_progress)
        if (state.get('kind') != 'expanded-ocr-retirement' or state.get('inventory_sha256') != expanded.INVENTORY_SHA256
                or state.get('plan_sha256') != expanded.sha(expanded.encode(plan)) or state.get('complete')):
            raise ValueError('saved OCR/PS progress does not match an interrupted run')
        expanded.submitted_targets(state.get('deletion', {}), rows)
        state['resumed_at'] = exact.now()
    else:
        if exact.read_optional(store, expanded.CCRD, PLAN) is not None or exact.read_optional(
                store, expanded.CCRD, PROGRESS) is not None:
            raise ValueError('a prior OCR/PS retirement exists; use its reviewed recovery path')
        plan = expanded.prepare(store, client, rows)
        expanded.assert_same_inventory(plan, rows)
        save_plan(store, plan)
        state = {'version': 1, 'kind': 'expanded-ocr-retirement', 'started_at': exact.now(),
                 'inventory_sha256': expanded.INVENTORY_SHA256, 'plan_sha256': expanded.sha(expanded.encode(plan)),
                 'target_count': len(rows), 'target_bytes': sum(row['bytes'] for row in rows),
                 'complete': False, 'deletion': {'batches': [], 'confirmed_deleted_objects': 0,
                                                  'confirmed_deleted_bytes': 0}}
        save_progress(store, state, 'plan-saved')
    try:
        if not state.get('references_retired'):
            expanded.verify_saved_plan_controls(store, plan)
            if not state.get('reference_mutation_started'):
                check_reader_consumers(store, client, plan)
                expanded.verify_exact_targets(store, rows)
                details = {}
                proof = expanded.simulate(store, plan, fence, workers=8, deadline_minutes=300,
                                          omit_internal_target_manifests=True, details=details)
                require_simulation(proof, rows)
                state['simulation'] = proof
                save_progress(store, state, 'simulation-verified')
                check_simulated_roots_before_mutation(store, plan, details)
                check_reader_consumers(store, client, plan)
                # Write the checkpoint before the first potentially partial mutation.
                state['reference_mutation_started'] = True
                save_progress(store, state, 'reference-mutation-started')
            else:
                # A resumed partial control update must not silently adopt changed
                # books or Reader versions; idempotent writes are checked below.
                check_reader_consumers(store, client, plan)
                expanded.verify_exact_targets(store, rows)
                # Re-run the current hypothetical closure even when some control
                # writes completed before the prior runner stopped.
                details = {}
                proof = expanded.simulate(store, plan, fence, workers=8, deadline_minutes=300,
                                          omit_internal_target_manifests=True, details=details,
                                          allow_partial=True)
                require_simulation(proof, rows)
                check_simulated_roots_before_mutation(store, plan, details, allow_partial=True)
                check_reader_consumers(store, client, plan)
                state['simulation'] = proof
                save_progress(store, state, 'resumed-simulation-verified')
            expanded.apply_planned_reference_retirement(
                store, plan, fence, client, lambda phase: save_progress(store, state, phase))
            state['references_retired'] = True
            save_progress(store, state, 'references-retired')
        else:
            exact.verify_reference_retirement(store, plan)
        check_reader_consumers(store, client, plan)
        check_current_reader_coverage(store, plan)
        state.pop('error', None)
        state.pop('error_type', None)
        reconciled = expanded.reconcile_targets(store, rows, state['deletion'])
        state['deletion']['confirmed_deleted_objects'] = reconciled['confirmed_absent']
        state['deletion']['confirmed_deleted_bytes'] = reconciled['confirmed_absent_bytes']
        save_progress(store, state, 'target-identities-reconciled')
        details = {}
        proof = expanded.simulate(store, plan, fence, workers=8, deadline_minutes=300,
                                  omit_internal_target_manifests=True, actual=True, details=details)
        expanded.require_actual_zero_proof(proof, rows)
        state['actual_graph'] = proof
        save_progress(store, state, 'actual-graph-verified')
        expanded.recheck_roots_and_controls(store, plan, proof, fence, client)
        collector.verify_roots(details['reference_graph'].store, details['reference_graph'])
        check_reader_consumers(store, client, plan)
        check_current_reader_coverage(store, plan)
        save_progress(store, state, 'pre-delete-barrier-verified')

        def checkpoint(phase, result):
            state['deletion'] = result
            save_progress(store, state, phase)

        state['deletion'] = expanded.delete_approved_batches(store, rows, fence, client, checkpoint,
                                                              progress=state['deletion'])
        store.assert_buckets_available()
        final = expanded.reconcile_targets(store, rows, state['deletion'])
        if final['present'] or final['confirmed_absent'] != len(rows):
            raise ValueError('not every approved target is absent')
        expanded.recheck_roots_and_controls(store, plan, proof, fence, client)
        check_reader_consumers(store, client, plan)
        check_current_reader_coverage(store, plan)
        state.update(complete=True, finished_at=exact.now())
        save_progress(store, state, 'complete')
        return state
    except Exception as error:
        failed_phase = state.get('phase')
        try:
            store.assert_buckets_available()
            reconciled = expanded.reconcile_targets(store, rows, state['deletion'])
            state['deletion']['confirmed_deleted_objects'] = reconciled['confirmed_absent']
            state['deletion']['confirmed_deleted_bytes'] = reconciled['confirmed_absent_bytes']
        except Exception as recount_error:
            state['reconciliation_error'] = type(recount_error).__name__
        failure = {'phase': failed_phase, 'error_type': type(error).__name__, 'error': str(error), 'at': exact.now()}
        state.setdefault('failures', []).append(failure)
        state.update(complete=False, error_type=type(error).__name__, error=str(error))
        save_progress(store, state, 'failed')
        raise


def main():
    assert_workflow_context(WORKFLOW)
    rows = expanded.inventory(INVENTORY)['targets']
    resume = os.environ.get('RESUME', 'false').lower() == 'true'
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        return maintenance.run(OUTPUT / 'maintenance.json', suppress_resume_dispatch=True,
                               preserve_queued_runs=True,
                               isolated_callback=lambda _: run(ExactStore(rows), GitHubProducerFence(),
                                                                client, rows, resume=resume))


if __name__ == '__main__':
    main()
