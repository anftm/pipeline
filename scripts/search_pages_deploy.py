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
    if (type(run.get("id")) is not int or run["id"] <= 0 or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main" or run.get("head_sha") != expected_sha
            or not isinstance(path, str) or path.split("@", 1)[0] != ".github/workflows/static.yml"
            or _timestamp(run.get("created_at")) < start):
        raise ValueError("Pages run does not match its operation intent")
    if run.get("status") not in {"requested", "queued", "waiting", "pending", "in_progress", "completed"}:
        raise ValueError("unknown Pages run status")
    if run["status"] == "completed" and not isinstance(run.get("conclusion"), str):
        raise ValueError("completed Pages run has no conclusion")
    return run


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


def dispatch(session, *, generation: str, operation: str) -> None:
    """Send exactly once; callers persist the intent before calling this method."""
    _generation(generation)
    _operation(operation)
    response = session.post(f"{BASE}/actions/workflows/{WORKFLOW}/dispatches", timeout=25,
                            json={"ref": "main", "inputs": {"search_generation": generation,
                                                              "search_transition": operation}})
    if response.status_code != 204:
        raise RuntimeError("Pages dispatch result is not confirmed; inspect before any retry")
