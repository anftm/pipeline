#!/usr/bin/env python3
"""Read-only prerequisite: let existing central producer runs finish naturally."""
import os
import time

try:
    from .reader_gc_fence import GitHubProducerFence, age_minutes, producer_path
except ImportError:
    from reader_gc_fence import GitHubProducerFence, age_minutes, producer_path


WORKFLOW = '.github/workflows/chm-epub-retirement.yml'
REPOSITORY = 'anftm/pipeline'


def waiting_producers(fence):
    """Return live producers; admit a scheduler-stuck ghost only with no jobs/checks."""
    selected = {}
    for status in ('in_progress', 'queued', 'pending', 'waiting', 'requested'):
        for page in fence.api(f'repos/{REPOSITORY}/actions/runs?status={status}&per_page=100'):
            for run in page['workflow_runs']:
                if run.get('status') != 'completed' and producer_path(run.get('path', '')):
                    selected[run['id']] = run
    waiting = []
    for run in selected.values():
        if run.get('status') not in {'queued', 'pending'} or age_minutes(run.get('updated_at')) < 30:
            waiting.append(run['id'])
            continue
        endpoint = f"repos/{REPOSITORY}/actions/runs/{run['id']}"
        before = fence.single(endpoint)
        jobs = fence.api(endpoint + '/jobs?filter=all&per_page=100')
        checks = fence.api(f"repos/{REPOSITORY}/check-suites/{before['check_suite_id']}/check-runs?per_page=100")
        after = fence.single(endpoint)
        identity = ('id', 'status', 'run_attempt', 'updated_at', 'check_suite_id')
        if (not jobs or not checks or before.get('id') != run['id']
                or before.get('status') not in {'queued', 'pending'}
                or age_minutes(before.get('updated_at')) < 30
                or type(before.get('run_attempt')) is not int or before['run_attempt'] < 1
                or any(before.get(key) != after.get(key) for key in identity)
                or any(page.get('jobs') != [] or page.get('total_count') != 0 for page in jobs)
                or any(page.get('check_runs') != [] or page.get('total_count') != 0 for page in checks)):
            waiting.append(run['id'])
    return sorted(waiting)


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
            or os.environ.get('GITHUB_WORKFLOW_REF') != f'{REPOSITORY}/{WORKFLOW}@refs/heads/main'):
        raise RuntimeError('CHM producer wait requires its central workflow')
    fence = GitHubProducerFence()
    deadline = time.monotonic() + 330 * 60
    last, idle_since = None, None
    while True:
        waiting = waiting_producers(fence)
        if waiting != last:
            print('Read-only wait for existing central producer runs:', waiting, flush=True)
            last = waiting
        if not waiting:
            if idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= 180:
                return
        else:
            idle_since = None
        if time.monotonic() >= deadline:
            raise TimeoutError('existing producer runs did not finish; no mutation was made')
        time.sleep(60)


if __name__ == '__main__':
    main()
