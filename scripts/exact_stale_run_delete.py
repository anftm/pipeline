"""Delete two explicitly selected stale queued v3 runs, with local evidence.

No repository, workflow, secret, storage object or other run may be changed.
The workflow runs without a shared writer lock and never authorizes GC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re

import httpx


CENTRAL = 'anftm/pipeline'
REPO_ID = 1260985830
WORKFLOW_ID = 380225964
WORKFLOW_PATH = '.github/workflows/reader-v3-publish.yml'
OWNER_WORKFLOW = CENTRAL + '/.github/workflows/exact-stale-run-delete.yml@refs/heads/main'
TARGETS = {
    39305430594: '248fb20fabf39f6fb02f195cc4d9d6a72dd1f44e',
    39261848246: '88337e7c2228004713d0847a4dc72fd4d3c02ed2',
}
BASE = 'https://api.github.com/repos/' + CENTRAL
OUTPUT = Path('output/exact-stale-run-delete')


def require_context():
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_REPOSITORY') != CENTRAL
            or os.environ.get('GITHUB_WORKFLOW_REF') != OWNER_WORKFLOW
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or not re.fullmatch(r'[1-9][0-9]*', os.environ.get('GITHUB_RUN_ID', ''))):
        raise RuntimeError('exact run cleanup requires its central manual workflow')


def persist(directory, filename, value):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()
    with (directory / filename).open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(raw).hexdigest()


def fetch(client, path, params=None):
    response = client.get(BASE + path, params=params)
    if response.status_code != 200:
        raise RuntimeError('GitHub GET failed: HTTP ' + str(response.status_code))
    return response.json()


def snapshot(client, run_id, expected_sha, *, now=None):
    path = f'/actions/runs/{run_id}'
    run = fetch(client, path)
    attempt = fetch(client, path + '/attempts/1')
    jobs = fetch(client, path + '/jobs', {'filter': 'all', 'per_page': 100})
    latest = fetch(client, path + '/jobs', {'filter': 'latest', 'per_page': 100})
    artifacts = fetch(client, path + '/artifacts', {'per_page': 100})
    checks = fetch(client, f"/check-suites/{run['check_suite_id']}/check-runs", {'per_page': 100})
    try:
        created = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError('target run has no valid creation timestamp') from error
    now = now or datetime.now(timezone.utc)
    if (run.get('id') != run_id or run.get('repository', {}).get('id') != REPO_ID
            or run.get('path', '').split('@', 1)[0] != WORKFLOW_PATH
            or run.get('workflow_id') != WORKFLOW_ID or run.get('event') != 'workflow_dispatch'
            or run.get('head_sha') != expected_sha or run.get('status') != 'queued'
            or run.get('conclusion') is not None or run.get('run_attempt') != 1
            or attempt.get('id') != run_id or attempt.get('status') != 'queued'
            or attempt.get('run_attempt') != 1 or created.tzinfo is None or now - created <= timedelta(days=14)
            or any(type(value.get('total_count')) is not int or value['total_count'] != 0
                   for value in (jobs, latest, artifacts, checks))
            or jobs.get('jobs') != [] or latest.get('jobs') != []
            or artifacts.get('artifacts') != [] or checks.get('check_runs') != []):
        raise RuntimeError('target run identity or eligibility changed: ' + str(run_id))
    return {'run': run, 'attempt': attempt, 'jobs_all': jobs, 'jobs_latest': latest,
            'artifacts': artifacts, 'check_runs': checks}


def run(client, directory=OUTPUT):
    require_context()
    identity = fetch(client, '')
    if identity.get('id') != REPO_ID or identity.get('full_name') != CENTRAL:
        raise RuntimeError('central repository identity differs')
    before = {str(run_id): snapshot(client, run_id, sha) for run_id, sha in TARGETS.items()}
    snapshot_sha = persist(directory, 'before.json', {'repository': CENTRAL, 'repository_id': REPO_ID,
                                                       'runs': before})
    persist(directory, 'intent.json', {'kind': 'exact-stale-run-delete', 'target_run_ids': list(TARGETS),
                                       'repository_id': REPO_ID, 'snapshot_sha256': snapshot_sha,
                                       'credential_type': 'workflow-github-token',
                                       'gc_authorized': False})
    for run_id, sha in TARGETS.items():
        snapshot(client, run_id, sha)
        try:
            response = client.delete(BASE + f'/actions/runs/{run_id}')
        except httpx.TransportError as error:
            persist(directory, f'{run_id}-delete.json', {'run_id': run_id, 'outcome': 'ambiguous',
                                                        'error': type(error).__name__})
            raise RuntimeError('run delete outcome ambiguous; inspect before retry') from error
        # Log only a tightly constrained GitHub diagnostic, never response
        # bodies that could contain credential or unrelated private material.
        diagnostic = None
        if response.status_code != 204:
            try:
                candidate = response.json().get('message')
            except (ValueError, AttributeError):
                candidate = None
            if isinstance(candidate, str) and re.fullmatch(r'[A-Za-z0-9 .,;:()/_-]{1,200}', candidate):
                diagnostic = candidate
        persist(directory, f'{run_id}-delete.json', {'run_id': run_id, 'http_status': response.status_code,
                                                      'diagnostic': diagnostic})
        if response.status_code != 204:
            raise RuntimeError('run delete not accepted: HTTP ' + str(response.status_code))
        readback = client.get(BASE + f'/actions/runs/{run_id}')
        persist(directory, f'{run_id}-readback.json', {'run_id': run_id, 'http_status': readback.status_code})
        if readback.status_code != 404:
            raise RuntimeError('deleted run could not be independently confirmed absent')
    result = {'kind': 'exact-stale-run-delete-result', 'repository_id': REPO_ID,
              'deleted_run_ids': list(TARGETS), 'gc_authorized': False, 'bucket_deletions': 0}
    persist(directory, 'result.json', result)
    return result


def main():
    require_context()
    token = os.environ.get('EXACT_RUN_GITHUB_TOKEN')
    if not token:
        raise RuntimeError('workflow GitHub Actions token unavailable')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'User-Agent': 'exact-stale-queued-run-removal'}
    with httpx.Client(headers=headers, timeout=45, follow_redirects=False) as client:
        result = run(client)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
