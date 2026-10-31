#!/usr/bin/env python3
"""Retire only the reviewed CHM EPUB list under durable producer isolation."""
import json
import os
import sys
import time
from pathlib import Path

import httpx

try:
    from . import chm_epub_journal as journal, chm_epub_publication_preflight as chm
    from . import chm_epub_publication_safety as safety, reader_gc_collect as collector
    from . import reader_gc_maintenance as maintenance, reader_gc_graph as graph
    from . import publish_reader_v3 as publication
    from .reader_bucket_store import S3BucketStore, retry_s3
    from .reader_gc_fence import GitHubProducerFence, assert_workflow_context
    from .reader_gc_store import CollectionStore
except ImportError:
    import chm_epub_journal as journal
    import chm_epub_publication_preflight as chm
    import chm_epub_publication_safety as safety
    import reader_gc_collect as collector
    import reader_gc_maintenance as maintenance
    import reader_gc_graph as graph
    import publish_reader_v3 as publication
    from reader_bucket_store import S3BucketStore, retry_s3
    from reader_gc_fence import GitHubProducerFence, assert_workflow_context
    from reader_gc_store import CollectionStore


WORKFLOW = '.github/workflows/chm-epub-retirement.yml'
ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / 'config/chm-history-inventory-20261030.json'
OUTPUT = Path('output/chm-epub-retirement')
MAINTENANCE = OUTPUT / 'maintenance.json'


class ExactStore(CollectionStore):
    """Reject every bucket deletion outside the independently hashed list."""
    def __init__(self, rows):
        super().__init__()
        self.read_cache = None
        self.allowed = {(row['bucket'], row['path']) for row in rows}

    def assert_buckets_available(self):
        for bucket in graph.BUCKETS:
            namespace, name = self._location(bucket)
            retry_s3(lambda: self._client(namespace).head_bucket(Bucket=name))

    def assert_chm_bucket_available(self):
        namespace, name = self._location(chm.ASSETS)
        retry_s3(lambda: self._client(namespace).head_bucket(Bucket=name))
        return True

    def read_bytes(self, bucket, path):
        try:
            return super().read_bytes(bucket, path)
        except FileNotFoundError:
            namespace, name = self._location(bucket)
            retry_s3(lambda: self._client(namespace).head_bucket(Bucket=name))
            raise

    def delete(self, bucket, paths):
        if (not paths or len(paths) > 100 or len(paths) != len(set(paths))
                or any((bucket, path) not in self.allowed for path in paths)):
            raise ValueError('CHM delete request exceeds reviewed exact inventory')
        S3BucketStore.delete(self, bucket, paths)


def request_pages_deploy(store, fence, plan):
    """Dispatch the existing Pages build; it fetches only the active projection."""
    repository = os.environ.get('PAGES_REPO', '')
    if repository != 'vomebook/search':
        raise ValueError('unexpected Pages repository')
    if publication.current(store)[0] != json.loads(plan['overrides'][publication.POINTER]):
        raise ValueError('CHM pointer changed before Pages dispatch')
    fence.check()
    token=os.environ.get('PAGES_TOKEN','')
    if not token:raise RuntimeError('Pages deployment credential is unavailable')
    response=httpx.post(f'https://api.github.com/repos/{repository}/actions/workflows/static.yml/dispatches',
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                 'User-Agent':'exact-chm-epub-retirement'},json={'ref':'main'},timeout=45)
    response.raise_for_status()


def recover_pages_handoff(store, fence, client, plan, before, checkpoint):
    """Never replay a dispatch whose POST may already have been accepted."""
    try:
        return safety.observe_planned_projection(store,client,plan)
    except (ValueError,OSError,httpx.HTTPError):
        pointer=json.loads(plan['overrides'][publication.POINTER])
        current,_=publication.current(store)
        if current == pointer:
            raise RuntimeError('CHM pointer already moved but both sites are not confirmed; '
                               'Pages dispatch outcome is unknown and must not be repeated') from None
        safety.apply_pre_ack_publication(store,plan,before,fence,checkpoint,
            on_pointer_committed=lambda:request_pages_deploy(store,fence,plan))
        return None


def initial(store, fence, client, rows, maintenance_state):
    if journal.read_optional(store, journal.PLAN) is not None or journal.read_optional(store, journal.PROGRESS) is not None:
        raise ValueError('existing CHM transaction requires its recovery path')
    fence.check()
    store.assert_buckets_available()
    identity, observations = collector.observe_projections(store, client)
    generations = {site:observations['receipts'][site]['generation'] for site in ('hf','pages')}
    plan = chm.plan_chm_withdrawal(store, INVENTORY, acknowledged_generations=generations)
    if identity['digest'] != plan['catalog_sha256_before']:
        raise ValueError('Reader catalog changed during CHM planning')
    protection = chm.prepare_processing_protection_updates(store, rows, fence)
    closure = chm.verify_chm_reading_closure(store, plan['chapter_manifests_to_preserve'], workers=4,
        checkpoint=lambda done,total:print(json.dumps({'chapter_manifests_checked':done,'total':total}),flush=True))
    overlays = {**plan['overrides'], **{p:v['after'] for p,v in protection['updates'].items()}}
    simulation_details = {}
    preliminary = chm.simulate_complete_withdrawal(store, overlays, rows, fence,
                                                     immutable=plan['immutable'], workers=8,
                                                     details=simulation_details)
    result = {}
    def save(plan_value, before, updates):
        result['plan_sha256'] = journal.save_plan(store, plan_value, before, updates, OUTPUT)
    def persist(phase, state, before):
        state.update(plan_sha256=result['plan_sha256'], inventory_sha256=chm.preview.INVENTORY_SHA256,
                     maintenance_id=maintenance_state['maintenance_id'])
        journal.save_progress(store, phase, state, OUTPUT)
        if phase == 'plan-and-preconditions-verified':
            maintenance_state['callback_mutation_started'] = True
            maintenance_state['callback_plan_sha256'] = result['plan_sha256']
            maintenance.journal(MAINTENANCE, maintenance_state)
    state = safety.run_guarded_retirement(store, fence, client, plan, rows, preflight=preliminary,
        persist=persist, save_plan=save, publish_pages=lambda value:request_pages_deploy(store,fence,value),
        chapter_evidence=closure, processing_updates=protection['updates'],
        simulation_details=simulation_details, stop_after_handoff=True)
    if state.get('handoff_complete') is not True or state.get('phase') != 'handoff-complete':
        raise RuntimeError('CHM staged handoff did not durably complete')
    maintenance_state['callback_recovery_required'] = True
    maintenance_state['callback_handoff_stage'] = 'references-retired'
    maintenance_state['callback_handoff_owner_run_id'] = maintenance_state['callback_owner_run_id']
    maintenance_state['callback_handoff_ready'] = True
    maintenance.journal(MAINTENANCE, maintenance_state)
    return state


def resume_isolated(store, fence, client, rows, maintenance_state):
    """Resume reference publication only; later jobs rebuild the actual graph."""
    plan, before, updates, digest = journal.load_plan(store, rows)
    state = journal.load_progress(store, digest)
    if state.get('maintenance_id') != maintenance_state.get('maintenance_id'):
        raise ValueError('CHM recovery maintenance identity changed')
    if maintenance_state.get('callback_plan_sha256') != digest:
        raise ValueError('CHM recovery fence does not bind the saved plan')
    journal.verify_initial_chapter_evidence(plan, updates, state)
    safety.validate_publication_order(plan, state)
    reconciled=safety.reconcile_epub_deletions(store, rows, state.get('batches', []))
    fence.check()
    journal.classify_recovery_controls(store, plan, before, updates, state)
    old_catalogs = {g:publication.validate_catalog(json.loads(store.read_bytes(chm.ASSETS,
        f'reader-index/v3/generations/{g}/catalog.json'))) for g in
        (plan['current_v3_generation'], plan['old_v3_generation'])}
    safety.validate_preserved_catalog_roots(plan,old_catalogs,rows)
    safety.validate_old_base_routes(store,plan,old_catalogs,rows)
    safety.require_initial_chapter_inventory(store,state['chapter_bundles'])
    def persist(phase):journal.save_progress(store,phase,state,OUTPUT)
    def checkpoint(phase,path):
        if phase == 'pre-ack-control-verified' and path not in state['staged_controls']:
            state['staged_controls'].append(path)
        if phase == 'control-verified' and path not in state['post_ack_controls']:
            state['post_ack_controls'].append(path)
        if phase == 'processing-update-verified' and path not in state['processing_updates_verified']:
            state['processing_updates_verified'].append(path)
        safety.validate_publication_order(plan,state)
        persist(phase)
    try:
        if not state.get('both_surfaces_accepted'):
            staged=safety.staged_pre_ack_controls(plan,before)
            reports=recover_pages_handoff(store,fence,client,plan,before,checkpoint)
            if reports is None:
                deadline=time.monotonic()+1200
                while True:
                    try:
                        reports=safety.observe_planned_projection(store,client,plan)
                        break
                    except (ValueError,OSError,httpx.HTTPError):
                        if time.monotonic()>=deadline:raise
                        time.sleep(15)
            state['both_surfaces_accepted']=True
            state['consumer_receipts']=reports
            persist('both-readers-accepted')
        staged=safety.staged_pre_ack_controls(plan,before)
        if not state.get('retirement_started'):
            # A failure prior to the durable start can restart only after a
            # complete, fresh proposed graph over the still-live controls.
            overlays={**plan['overrides'],**{p:v['after'] for p,v in updates.items()}}
            details={}
            proof=chm.simulate_complete_withdrawal(store,overlays,rows,fence,
                                                   immutable=plan['immutable'],details=details)
            if proof.get('graph_complete') is not True or proof.get('retained_targets') != 0:
                raise ValueError('fresh CHM recovery simulation retains targets')
            versions=safety.capture_graph_versions(store,details['graph'],omitted=details['overlay_paths'])
            safety.recheck_simulation_by_version(store,details,fence,versions=versions,
                                                 applied={**plan['immutable'],**staged})
            state['retirement_started']=True
            persist('retirement-started')
        if state.get('delete_started'):
            raise ValueError('partial deletion must resume through the staged actual-graph path')
        safety.apply_acknowledged_controls(store,plan,before,staged,client,fence,checkpoint)
        safety.apply_processing_protection_updates(store,rows,fence,client,plan,updates,checkpoint,
                                                   deleted={p for bucket,p in reconciled['missing']})
        safety.verify_live_chm_controls(store,plan)
        safety.verify_live_processing_controls(store,updates)
        safety.verify_predelete_chapter_inventory(store,state,updates)
        safety.verify_retired_rollback_aliases(store,plan)
        safety.observe_planned_projection(store,client,plan)
        state['handoff_complete']=True
        state.pop('error',None);state.pop('error_type',None);state.pop('reconciliation_error',None)
        persist('handoff-complete')
        maintenance_state.update(callback_handoff_ready=True,callback_handoff_stage='references-retired',
                                 callback_handoff_owner_run_id=maintenance_state['callback_owner_run_id'],
                                 callback_recovery_required=True)
        maintenance.journal(MAINTENANCE,maintenance_state)
        return state
    except Exception as error:
        state.update(complete=False,error_type=type(error).__name__,error=str(error))
        try:
            count=safety.reconcile_epub_deletions(store,rows,state.get('batches',[]))
            state['confirmed_deleted_objects']=count['confirmed_absent_objects']
            state['confirmed_deleted_bytes']=count['confirmed_absent_bytes']
        except Exception as failure:state['reconciliation_error']=type(failure).__name__
        persist('failed')
        raise


def continue_staged(store, fence, client, rows, maintenance_state, *, max_batches=1, batch_size=100):
    """Rebuild the actual graph in this job, then retire a bounded exact batch set."""
    plan, before, updates, digest = journal.load_plan(store, rows)
    state = journal.load_progress(store, digest)
    if state.get('complete') is True:
        if (state.get('maintenance_id') != maintenance_state.get('maintenance_id')
                or maintenance_state.get('callback_plan_sha256') != digest
                or state.get('barrier_policy') != 'full-closure-sha256-and-four-bucket-root-inventory-per-batch'):
            raise ValueError('completed CHM transaction identity changed')
        counts = safety.reconcile_epub_deletions(store, rows, state.get('batches', []))
        if counts['present_objects'] or counts['confirmed_absent_objects'] != len(rows):
            raise ValueError('completed CHM target reappeared before isolation restore')
        safety.verify_live_chm_controls(store, plan)
        safety.verify_live_processing_controls(store, updates)
        safety.verify_predelete_chapter_inventory(store, state, updates)
        safety.verify_retired_rollback_aliases(store, plan)
        safety.observe_planned_projection(store, client, plan)
        fence.check()
        maintenance_state.update(callback_committed=True, callback_recovery_required=False,
                                 callback_handoff_ready=False)
        maintenance.journal(MAINTENANCE, maintenance_state)
        return state
    if (state.get('maintenance_id') != maintenance_state.get('maintenance_id')
            or maintenance_state.get('callback_plan_sha256') != digest
            or state.get('handoff_complete') is not True or state.get('retirement_started') is not True
            or state.get('both_surfaces_accepted') is not True):
        raise ValueError('CHM staged continuation does not bind a completed reference handoff')
    if (state.get('delete_started') and state.get('barrier_policy') !=
            'full-closure-sha256-and-four-bucket-root-inventory-per-batch'):
        raise ValueError('partial CHM deletion lacks the required SHA-256 barrier policy')
    journal.verify_initial_chapter_evidence(plan, updates, state)
    safety.validate_publication_order(plan, state)
    counts = safety.reconcile_epub_deletions(store, rows, state.get('batches', []))
    fence.check()
    safety.verify_live_chm_controls(store, plan)
    safety.verify_live_processing_controls(store, updates)
    safety.verify_predelete_chapter_inventory(store, state, updates)
    safety.verify_retired_rollback_aliases(store, plan)
    reports = safety.observe_planned_projection(store, client, plan)
    safety.validate_document_reset_identity(plan, reports)
    safety.verify_document_reset_prerequisites(store, plan)
    actual, traversal = safety.actual_graph_evidence(store, fence, client, rows, plan)
    safety.require_actual_zero_graph(actual, rows)
    previous = state.get('actual_graph')
    if state.get('delete_started'):
        safety.require_actual_zero_graph(previous or {}, rows)
        if (previous.get('root_members') != actual.get('root_members')
                or previous.get('root_sha256') != actual.get('root_sha256')
                or not {(item['bucket'],item['path']) for item in previous.get('retired_roots', [])}
                       <= {(item['bucket'],item['path']) for item in actual.get('retired_roots', [])}):
            raise ValueError('CHM active root closure changed after partial deletion')
        old_closure = {(item['bucket'], item['path']):item['sha256'] for item in previous['closure_sha256']}
        new_closure = {(item['bucket'], item['path']):item['sha256'] for item in actual['closure_sha256']}
        if (len(old_closure) != len(previous['closure_sha256'])
                or len(new_closure) != len(actual['closure_sha256'])
                or any(old_closure.get(ref) != digest for ref,digest in new_closure.items())):
            raise ValueError('CHM reference closure grew or changed after partial deletion')
    versions = safety.capture_graph_versions(store, traversal)
    state['actual_graph_verified'] = True
    state['actual_graph'] = actual
    state['confirmed_deleted_objects'] = counts['confirmed_absent_objects']
    state['confirmed_deleted_bytes'] = counts['confirmed_absent_bytes']
    state['barrier_policy'] = 'full-closure-sha256-and-four-bucket-root-inventory-per-batch'
    journal.save_progress(store, 'stage-actual-graph-verified', state, OUTPUT)
    safety.verify_predelete_chapter_inventory(store, state, updates)
    state['delete_started'] = True
    safety.validate_publication_order(plan, state)
    journal.save_progress(store, 'deletion-started', state, OUTPUT)

    def checkpoint(phase, batches, status):
        state['batches'] = batches
        state['confirmed_deleted_objects'] = status.get('confirmed_absent_objects', 0)
        state['confirmed_deleted_bytes'] = status.get('confirmed_absent_bytes', 0)
        journal.save_progress(store, phase, state, OUTPUT)

    batches, final = safety.delete_exact_epub_batches(store, rows, fence, checkpoint,
        batches=state.get('batches', []), max_new_batches=max_batches, batch_size=batch_size,
        barrier=lambda:safety.recheck_actual_graph_by_version(store, traversal, actual, rows,
                                                               fence, client, plan, versions))
    state['batches'] = batches
    state['confirmed_deleted_objects'] = final['confirmed_absent_objects']
    state['confirmed_deleted_bytes'] = final['confirmed_absent_bytes']
    safety.finish_actual_graph_barrier(store, traversal, actual, rows, fence, client, plan, versions)
    safety.verify_live_chm_controls(store, plan)
    safety.verify_live_processing_controls(store, updates)
    safety.verify_predelete_chapter_inventory(store, state, updates)
    safety.verify_retired_rollback_aliases(store, plan)
    state.pop('error', None)
    state.pop('error_type', None)
    state.pop('reconciliation_error', None)
    if final['confirmed_absent_objects'] == len(rows):
        if final['confirmed_absent_bytes'] != sum(row['bytes'] for row in rows):
            raise ValueError('CHM final byte count differs from exact inventory')
        state['complete'] = True
        journal.save_progress(store, 'complete', state, OUTPUT)
        maintenance_state.update(callback_committed=True, callback_recovery_required=False,
                                 callback_handoff_ready=False)
        maintenance.journal(MAINTENANCE, maintenance_state)
        return state
    state['complete'] = False
    journal.save_progress(store, 'deletion-stage-complete', state, OUTPUT)
    return state


def require_owned_continuation(current, owner, run_id, stage, *, allow_committed=False):
    """A stage may use only its own active owner or a terminal predecessor."""
    owner_id = str(current.get('callback_owner_run_id') or '')
    if (not str(run_id).isdigit() or not owner_id.isdigit()
            or owner.get('id') != int(owner_id)
            or owner.get('path', '').split('@',1)[0] != WORKFLOW
            or owner.get('head_branch') != 'main' or owner.get('event') != 'workflow_dispatch'
            or current.get('kind') != 'reader-maintenance-journal' or current.get('restored') is True
            or current.get('callback_mutation_started') is not True
            or current.get('callback_committed') and not allow_committed):
        raise ValueError('CHM recovery does not own an interrupted isolated window')
    if current.get('callback_committed') and stage == 'continue' and (
            owner_id != str(run_id) or current.get('callback_recovery_required')):
        raise ValueError('committed CHM continuation must belong to the current verified owner')
    if owner_id == str(run_id):
        if owner.get('status') != 'in_progress':
            raise ValueError('CHM current owner is not active')
    elif owner.get('status') != 'completed':
        raise ValueError('previous CHM owner is still active')
    if stage == 'continue' and (owner_id != str(run_id) or
            not (current.get('callback_handoff_ready') is True or current.get('callback_committed') is True)):
        raise ValueError('CHM continuation must share the original staged Actions run')
    return True


def main():
    assert_workflow_context(WORKFLOW)
    rows=chm.preview.load_inventory(INVENTORY)['targets']
    OUTPUT.mkdir(parents=True,exist_ok=True)
    if sys.argv[1:] == ['--restore-committed']:
        store, fence = ExactStore(rows), GitHubProducerFence()
        current = json.loads(store.read_bytes(chm.ASSETS, maintenance.DURABLE_JOURNAL))
        if (str(current.get('callback_owner_run_id')) != os.environ.get('GITHUB_RUN_ID')
                or current.get('callback_recovery_required')
                or not (current.get('callback_committed') is True or current.get('restored') is True)):
            raise ValueError('CHM final restoration requires this run and a committed transaction')
        plan, before, updates, digest = journal.load_plan(store, rows)
        state = journal.load_progress(store, digest)
        if (state.get('maintenance_id') != current.get('maintenance_id') or state.get('complete') is not True
                or current.get('callback_plan_sha256') != digest
                or state.get('barrier_policy') != 'full-closure-sha256-and-four-bucket-root-inventory-per-batch'):
            raise ValueError('CHM final restoration does not bind the completed exact plan')
        counts = safety.reconcile_epub_deletions(store, rows, state.get('batches', []))
        if counts['present_objects'] or counts['confirmed_absent_objects'] != len(rows):
            raise ValueError('CHM target reappeared before final restoration')
        fence.check()
        MAINTENANCE.write_text(json.dumps(current,sort_keys=True,indent=2)+'\n')
        maintenance.restore(MAINTENANCE)
        return state
    if sys.argv[1:]:
        raise ValueError('unexpected CHM retirement arguments')
    resume=os.environ.get('RESUME','false').lower()=='true'
    stage=os.environ.get('CHM_STAGE','initial')
    store,fence=ExactStore(rows),GitHubProducerFence()
    with httpx.Client(timeout=120,follow_redirects=True) as client:
        if stage == 'initial' and not resume:
            if journal.read_optional(store,journal.PLAN) is not None or journal.read_optional(store,journal.PROGRESS) is not None:
                raise ValueError('existing CHM transaction requires its recovery path')
            return maintenance.run(MAINTENANCE, preserve_queued_runs=True, suppress_resume_dispatch=True,
                require_callback_commit=True, allow_staged_handoff=True,
                isolated_callback=lambda state:initial(store,fence,client,rows,state))
        current=json.loads(store.read_bytes(chm.ASSETS,maintenance.DURABLE_JOURNAL))
        run_id = os.environ.get('GITHUB_RUN_ID', '')
        owner_id = current.get('callback_owner_run_id')
        if not str(run_id).isdigit() or not str(owner_id).isdigit():
            raise ValueError('CHM continuation has no valid Actions owner identity')
        owner=fence.single(f'repos/anftm/pipeline/actions/runs/{owner_id}')
        require_owned_continuation(current,owner,run_id,stage,
                                   allow_committed=stage == 'continue' or resume and stage == 'initial')
        MAINTENANCE.write_text(json.dumps(current,sort_keys=True,indent=2)+'\n')
        if stage == 'continue':
            if current.get('callback_committed'):
                plan, before, updates, digest = journal.load_plan(store, rows)
                completed = journal.load_progress(store, digest)
                if (completed.get('complete') is not True or completed.get('maintenance_id') != current.get('maintenance_id')
                        or current.get('callback_plan_sha256') != digest):
                    raise ValueError('committed CHM continuation lacks its completed exact transaction')
            return continue_staged(store,fence,client,rows,current)
        if not resume:
            raise ValueError('unexpected CHM retirement stage')
        state = json.loads(store.read_bytes(chm.ASSETS, journal.PROGRESS))
        if state.get('handoff_complete') is True:
            plan, before, updates, digest = journal.load_plan(store, rows)
            state = journal.load_progress(store, digest)
            if (state.get('maintenance_id') != current.get('maintenance_id')
                    or current.get('callback_plan_sha256') != digest
                    or state.get('handoff_complete') is not True):
                raise ValueError('CHM recovery owner cannot adopt a different transaction')
            if current.get('callback_committed') and state.get('complete') is not True:
                raise ValueError('committed CHM maintenance lacks a completed transaction')
            safety.validate_publication_order(plan, state)
            safety.reconcile_epub_deletions(store, rows, state.get('batches', []))
            journal.verify_initial_chapter_evidence(plan, updates, state)
            fence.check()
            previous = {'run_id': str(owner_id), 'status': owner.get('status'),
                        'resumed_by': run_id, 'at': publication.reader_lifecycle.now_iso()}
            current.setdefault('callback_owner_history', []).append(previous)
            current.update(callback_owner_run_id=run_id, callback_handoff_owner_run_id=run_id,
                           callback_handoff_ready=True, callback_recovery_required=True)
            maintenance.journal(MAINTENANCE,current)
            return continue_staged(store,fence,client,rows,current)
        plan, before, updates, digest = journal.load_plan(store, rows)
        checked = journal.load_progress(store, digest)
        if checked.get('maintenance_id') != current.get('maintenance_id') or current.get('callback_plan_sha256') != digest:
            raise ValueError('CHM recovery owner cannot adopt a different transaction')
        safety.validate_publication_order(plan,checked)
        safety.reconcile_epub_deletions(store,rows,checked.get('batches',[]))
        journal.verify_initial_chapter_evidence(plan,updates,checked)
        fence.check()
        current.setdefault('callback_owner_history',[]).append({
            'run_id':str(owner_id),'status':owner.get('status'),'resumed_by':run_id,
            'at':publication.reader_lifecycle.now_iso()})
        current.update(callback_owner_run_id=run_id,callback_recovery_required=True)
        current.pop('callback_handoff_ready',None)
        current.pop('callback_handoff_owner_run_id',None)
        maintenance.journal(MAINTENANCE,current)
        return resume_isolated(store,fence,client,rows,current)


if __name__=='__main__':
    main()
