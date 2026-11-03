"""Read-only shadow evaluation for the post-retirement central producer fence.

This deliberately does not change `producer_repositories()` or authorize a GC
operation. It validates immutable audit bytes and central workflow/run state,
reports external-writer uncertainty, and refuses to interpret stale queued jobs
without current execution checks as permanently safe.
"""
from __future__ import annotations

try:
    from . import reader_producer_audit_runner as audit
    from . import reader_producer_scope_transition as scope
    from .reader_gc_fence import GitHubProducerFence, producer_path
except ImportError:
    import reader_producer_audit_runner as audit
    import reader_producer_scope_transition as scope
    from reader_gc_fence import GitHubProducerFence, producer_path


CENTRAL = 'anftm/pipeline'
CENTRAL_ID = 1260985830


def verify_evidence(store):
    """Require exact archived receipt bytes; this is still not a write lock."""
    candidate, receipts, writer = audit.load_inputs()
    plan = audit.publication.intent.plan(candidate, receipts, writer)
    if plan['candidate_sha256'] != audit.CANDIDATE_SHA or plan['intent_sha256'] != audit.INTENT_SHA:
        raise RuntimeError('producer retirement audit identity differs')
    for path, expected in sorted(plan['objects'].items()):
        if store.read(path) != expected:
            raise RuntimeError('producer retirement audit bytes changed: ' + path)
    return {'object_count': len(plan['objects']), 'candidate_sha256': plan['candidate_sha256'],
            'intent_sha256': plan['intent_sha256']}


def check_deleted_names(observe):
    """Reject recreated/renamed exact repositories; absence is not isolation.

    The injected read-only adapter returns HTTP statuses without treating 404
    as success; 401/403/5xx and a live old/new identity all fail closed.
    """
    results = []
    for repository, repository_id in {**scope.EXPECTED, **scope.ADDITIONAL}.items():
        named_status, numeric_status = observe(repository, repository_id)
        if named_status != 404 or numeric_status != 404:
            raise RuntimeError('retired producer repository identity no longer absent: ' + repository)
        results.append({'repository': repository, 'repository_id': repository_id,
                        'name_status': named_status, 'id_status': numeric_status})
    return {'repositories': results, 'absence_is_isolation_proof': False}


def check_central(api):
    """Check central identity and all selected workflows/runs, GET only."""
    fence = GitHubProducerFence(api)
    identity = fence.single('repos/' + CENTRAL)
    if identity.get('id') != CENTRAL_ID or identity.get('full_name') != CENTRAL:
        raise RuntimeError('central producer repository identity differs')
    workflow_pages = api('repos/' + CENTRAL + '/actions/workflows?per_page=100')
    if not isinstance(workflow_pages, list) or not workflow_pages:
        raise RuntimeError('central workflow inventory unavailable')
    workflows = [w for page in workflow_pages for w in page['workflows'] if producer_path(w['path'])]
    if not any(w['path'].endswith('/pdf-account-worker.yml') for w in workflows):
        raise RuntimeError('central producer workflow inventory is incomplete')
    enabled = [w['path'] for w in workflows if w['state'] == 'active']
    if enabled:
        raise RuntimeError('central producers are not paused: ' + ', '.join(enabled))
    residual = []
    for status in ('in_progress', 'queued', 'pending', 'waiting', 'requested'):
        pages = api(f'repos/{CENTRAL}/actions/runs?status={status}&per_page=100')
        active = [run for page in pages for run in page['workflow_runs']
                  if producer_path(run['path']) and run['status'] != 'completed']
        for run in active:
            if run['status'] not in {'queued', 'pending'}:
                raise RuntimeError('central producers have not drained')
            residual.append(fence.isolated_residual(CENTRAL, run, central=True))
    return {'repository': CENTRAL, 'repository_id': CENTRAL_ID,
            'workflow_count': len(workflows), 'enabled_workflows': enabled,
            'isolated_residual_runs': residual}


def check(store, api):
    prepared = scope.prepare()
    evidence = verify_evidence(store)
    central = check_central(api)
    return {'kind': 'reader-producer-v2-shadow', 'scope_sha256': prepared['sha256'],
            'evidence': evidence, 'central': central, 'external_writers_fenced': False,
            'source_credentials_revoked': False, 'scope_activated': False, 'gc_authorized': False}
