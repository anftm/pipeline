"""Correlate one explicitly dispatched Pages deployment with a durable intent.

Dispatch and run discovery are separate on purpose: if a response is lost,
inspect the unique operation ID and live site before considering any recovery.
A GitHub run result alone is not proof of the site's loaded generation.
"""
from __future__ import annotations

import re
from datetime import datetime

try:
    from .search_publication_pointer import _generation
except ImportError:
    from search_publication_pointer import _generation


REPO = "vomebook/search"
BASE = f"https://api.github.com/repos/{REPO}"
WORKFLOW = "static.yml"
OPERATION = re.compile(r"[0-9a-f]{32}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
# Reviewed topology: source-checks has no deployment permissions; deploy is a
# separate needs: source-checks job. A change requires a new code review.
RECOVERY_WORKFLOW_BLOBS = frozenset({'3e13f0e4c9ca38d4019151cf07becc756d8da1f0'})


def _operation(value: str) -> str:
    if not isinstance(value, str) or not OPERATION.fullmatch(value):
        raise ValueError("invalid Pages operation identity")
    return value


def run_name(operation: str) -> str:
    return f"search-transition/{_operation(operation)}"


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid UTC timestamp")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("invalid UTC timestamp") from error


def matching_run(runs: list, *, operation: str, not_before: str, expected_sha: str) -> dict | None:
    """Fail closed on duplicate, malformed, stale or differently pinned runs."""
    name = run_name(operation)
    start = _timestamp(not_before)
    if not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha):
        raise ValueError("invalid expected Pages revision")
    if not isinstance(runs, list):
        raise TypeError("invalid Pages run list")
    matches = [run for run in runs if isinstance(run, dict) and run.get("display_title") == name]
    if len(matches) > 1:
        raise ValueError("duplicate Pages runs for one operation; inspect before recovery")
    if not matches:
        return None
    run = matches[0]
    path = run.get("path")
    if (type(run.get("id")) is not int or run["id"] <= 0
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main" or run.get("head_sha") != expected_sha
            or not isinstance(path, str) or path.split("@", 1)[0] != ".github/workflows/static.yml"
            or _timestamp(run.get("created_at")) < start):
        raise ValueError("Pages run does not match its operation intent")
    if run.get("status") not in {"requested", "queued", "waiting", "pending", "in_progress", "completed"}:
        raise ValueError("unknown Pages run status")
    if run["status"] == "completed" and not isinstance(run.get("conclusion"), str):
        raise ValueError("completed Pages run has no conclusion")
    return run


def assert_idle(session) -> None:
    """Never dispatch into a Pages queue whose ordering can cancel pending work."""
    for state in ("in_progress", "queued", "waiting", "pending", "requested"):
        response = session.get(f"{BASE}/actions/workflows/{WORKFLOW}/runs",
                               params={"per_page": 100, "status": state}, timeout=20)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("workflow_runs"), list):
            raise TypeError("invalid Pages workflow runs response")
        if getattr(response, "links", {}).get("next") or body["workflow_runs"]:
            raise ValueError("Pages deployment is active, queued or incomplete to inspect")


def failed_before_deploy(session, run: dict, *, expected_sha: str) -> None:
    """Accept only the known pinned workflow and an unstarted deployment job."""
    run_id = run.get('id') if isinstance(run, dict) else None
    if (type(run_id) is not int or run_id <= 0 or run.get('run_attempt') != 1
            or not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha)):
        raise ValueError('invalid failed Pages run identity')
    # Job names alone do not prove the topology: another workflow version could
    # deploy from a different job. Refuse new revisions until independently
    # reviewed and explicitly added; never execute or evaluate remote YAML.
    response = session.get(f'{BASE}/contents/.github/workflows/static.yml',
                           params={'ref': expected_sha}, timeout=20)
    response.raise_for_status()
    metadata = response.json()
    if (not isinstance(metadata, dict) or metadata.get('sha') not in RECOVERY_WORKFLOW_BLOBS
            or metadata.get('type') != 'file'):
        raise ValueError('Pages workflow topology is not reviewed for failed-run recovery')
    response = session.get(f'{BASE}/actions/runs/{run_id}/attempts/1/jobs', params={'per_page': 100}, timeout=20)
    response.raise_for_status()
    body = response.json()
    if (not isinstance(body, dict) or not isinstance(body.get('jobs'), list)
            or type(body.get('total_count')) is not int or body['total_count'] != len(body['jobs'])
            or getattr(response, 'links', {}).get('next')):
        raise ValueError('Pages failed-run job list is incomplete')
    jobs = body['jobs']
    if (len(jobs) != 2 or any(not isinstance(job, dict) for job in jobs)
            or {job.get('name') for job in jobs} != {'source-checks', 'deploy'}):
        raise ValueError('Pages failed-run jobs differ from the pinned workflow')
    source = next(job for job in jobs if job['name'] == 'source-checks')
    deployment = next(job for job in jobs if job['name'] == 'deploy')
    if (source.get('status') != 'completed' or source.get('conclusion') not in {'failure', 'cancelled', 'timed_out'}
            or deployment.get('status') != 'completed' or deployment.get('conclusion') != 'skipped'):
        raise ValueError('Pages deployment may have started; keep intent pending')
    steps = deployment.get('steps')
    if not isinstance(steps, list) or any(not isinstance(step, dict) or step.get('conclusion') != 'skipped' for step in steps):
        raise ValueError('Pages deploy step status is unknown; keep intent pending')


def inspect_runs(session, *, operation: str, not_before: str, expected_sha: str) -> dict | None:
    """Read a bounded run set; pagination or errors are unknown, never absent."""
    _timestamp(not_before)
    response = session.get(f"{BASE}/actions/workflows/{WORKFLOW}/runs",
                           params={"per_page": 100, "created": ">=" + not_before}, timeout=20)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("workflow_runs"), list):
        raise TypeError("invalid Pages workflow runs response")
    if getattr(response, "links", {}).get("next"):
        raise ValueError("Pages run list is paginated; narrow the inspection window")
    return matching_run(body["workflow_runs"], operation=operation, not_before=not_before, expected_sha=expected_sha)


def dispatch(session, *, generation: str, operation: str, expected_sha: str) -> None:
    """Send once after checking main and queue; these reads do not fence other writers."""
    _generation(generation)
    _operation(operation)
    if not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha):
        raise ValueError("invalid expected Pages revision")
    response = session.get(f"{BASE}/branches/main", timeout=20)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("commit"), dict) or body["commit"].get("sha") != expected_sha:
        raise ValueError("Pages main changed before dispatch")
    assert_idle(session)
    response = session.post(f"{BASE}/actions/workflows/{WORKFLOW}/dispatches", timeout=25,
                            json={"ref": "main", "inputs": {"search_generation": generation,
                                                              "search_transition": operation}})
    if response.status_code != 204:
        raise RuntimeError("Pages dispatch result is not confirmed; inspect before any retry")
