#!/usr/bin/env python3
"""Journalled producer isolation, collector dispatch and unconditional restoration."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

try:
    from .reader_gc_fence import producer_repositories, producer_path
    from .pdf_worker_lanes import load_config
except ImportError:
    from reader_gc_fence import producer_repositories, producer_path
    from pdf_worker_lanes import load_config


ACTIVE = ("queued", "pending", "in_progress", "waiting", "requested")


class MaintenanceAPIError(RuntimeError):
    def __init__(self, repo, path, method, status):
        self.status = status
        super().__init__(f'maintenance API {method} failed: {repo}:{path} '
                         f'(HTTP {status or "unknown"})')


def api(repo, path, *, method="GET", body=None):
    tokens = json.loads(os.environ["GC_PRODUCER_TOKENS_JSON"])
    token = tokens.get(repo)
    if not isinstance(token, str) or not token:
        raise RuntimeError("incomplete maintenance credentials")
    command = ["gh", "api", f"repos/{repo}/{path}", "--method", method]
    if body is not None:
        command += ["--input", "-"]
    for attempt in range(3):
        try:
            result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                env={**os.environ, "GH_TOKEN": token}, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            error = MaintenanceAPIError(repo, path, method, None)
        else:
            if not result.returncode:
                return json.loads(result.stdout) if result.stdout.strip() else {}
            match = re.search(r'\bHTTP (\d{3})\b', result.stderr or '')
            error = MaintenanceAPIError(repo, path, method, int(match[1]) if match else None)
        # Never blindly retry a mutation: cancellation, dispatch and rerun are not
        # idempotent. Only a read may be repeated.
        if method != "GET" or error.status not in {None, 408, 429, 500, 502, 503, 504} \
                or attempt == 2:
            raise error
        time.sleep(2 ** attempt)


def set_state(repo, path, body, expected):
    """Idempotent PUT: read back an ambiguous outcome before any bounded retry."""
    for attempt in range(3):
        if expected(api(repo, path)):
            return
        try:
            api(repo, path, method="PUT", body=body)
        except MaintenanceAPIError as error:
            if expected(api(repo, path)):
                return
            if error.status not in {None, 408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
            time.sleep(2 ** attempt)
            continue
        if not expected(api(repo, path)):
            raise RuntimeError("maintenance state mutation readback mismatch")
        return


def permissions(repo, desired):
    # GET may expose read-only or newly added fields that PUT does not accept.
    body = {key: desired[key] for key in ("enabled", "allowed_actions") if key in desired}
    # GitHub rejects allowed_actions when Actions is being disabled. Keep the
    # original policy only for re-enabling; the disabled state ignores it.
    if body.get("enabled") is False:
        body.pop("allowed_actions", None)
    set_state(repo, "actions/permissions", body,
              lambda value: all(value.get(key) == item for key, item in body.items()))


def disable_actions(repo, record):
    """Best-effort repository isolation; workflow disable is the primary fence.

    A producer token may have Actions write but no Administration write. That is
    enough to disable every producer workflow and cancel queued runs, which is
    the normal drain. The collector fence still rejects any residual queued run
    while repository Actions stays enabled, so a denied PUT cannot authorize a
    deletion race.
    """
    try:
        permissions(repo, {**record["permission"], "enabled": False})
    except MaintenanceAPIError as error:
        if error.status not in {401, 403, 404}:
            raise
        record["permission_denied"] = error.status
        return False
    record["permission_changed"] = True
    return True


def workflow_state(repo, workflow, enabled):
    path = f"actions/workflows/{workflow['id']}"
    desired = "active" if enabled else "disabled_manually"
    for attempt in range(3):
        if api(repo, path)["state"] == desired:
            return
        try:
            api(repo, path + ("/enable" if enabled else "/disable"), method="PUT")
        except MaintenanceAPIError as error:
            if api(repo, path)["state"] == desired:
                return
            if error.status not in {None, 408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
            time.sleep(2 ** attempt)
            continue
        if api(repo, path)["state"] != desired:
            raise RuntimeError("workflow state readback mismatch")
        return


def preflight():
    """Check every credential and inventory before disabling the first producer."""
    central = load_config()["publisher_repository"]
    result = {}
    errors = []
    for repo in producer_repositories():
        try:
            inventory = pages(repo, "actions/workflows", "workflows")
            workflows = [w for w in inventory if producer_path(w["path"])]
            if not any(w["path"].endswith("/pdf-account-worker.yml") for w in workflows):
                raise RuntimeError("incomplete producer inventory")
            # The central repository is never repository-disabled: it hosts the
            # maintenance run itself and only needs its producer workflows
            # stopped. Some central worker credentials intentionally cannot read
            # or write the repository Actions policy.
            permission = {} if repo == central else api(repo, "actions/permissions")
            result[repo] = {"permission": permission, "workflows": [
                {"id": w["id"], "state": w["state"], "path": w["path"]} for w in workflows],
                "cancelled": []}
        except (MaintenanceAPIError, RuntimeError) as error:
            # Keep the GET-only failure readable without exposing any credential.
            errors.append({"repository": repo, "error": type(error).__name__,
                           "status": getattr(error, "status", None)})
    if errors:
        raise RuntimeError(f"maintenance preflight failed for {len(errors)} repositories: "
                           + json.dumps(errors, sort_keys=True))
    return result


def pages(repo, path, field):
    result, page = [], 1
    while True:
        data = api(repo, path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        items = data[field]
        result.extend(items)
        if len(items) < 100:
            return result
        page += 1


def journal(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def restore(path):
    if not path.exists():
        return
    state = json.loads(path.read_text())
    if state.get("preflight_only"):
        return
    if state.get("dispatch_requested"):
        central = load_config()["publisher_repository"]
        run_id = state.get("collector_run_id")
        if not run_id:
            matching = [run for run in pages(central,
                "actions/workflows/reader-gc-collect.yml/runs", "workflow_runs")
                if state["maintenance_id"] in run.get("display_title", "")]
            if not matching:
                raise RuntimeError("collector dispatch outcome unknown; do not restore producers yet")
            run_id = matching[0]["id"]
            state["collector_run_id"] = run_id
            journal(path, state)
        current = api(central, f"actions/runs/{run_id}")
        if current["status"] != "completed":
            raise RuntimeError("collector still active; do not restore producers")
    errors = []
    for repo, record in state["repositories"].items():
        try:
            if record.get("permission_changed"):
                permissions(repo, record["permission"])
            for workflow in record["workflows"]:
                if workflow["state"] == "active":
                    workflow_state(repo, workflow, True)
            for run in record["cancelled"]:
                if run.get("restored"):
                    continue
                current = api(repo, f"actions/runs/{run['id']}")
                if current["status"] == "completed" and current.get("conclusion") == "cancelled":
                    # Never re-run a cancellation performed by this window.
                    # GitHub can leave a re-run of a queued/cancelled attempt in
                    # a permanent "waiting for pending jobs" state, and a
                    # partially executed build must not be replayed behind the
                    # next publication. Record it for explicit re-dispatch.
                    run["rerun_skipped"] = True
                run["restored"] = True
                journal(path, state)
            record["restored"] = True
            journal(path, state)
        except Exception as error:
            errors.append({"repository": repo, "error": type(error).__name__})
    state["restore_errors"] = errors
    state["restored"] = not errors
    journal(path, state)
    if errors:
        raise RuntimeError("maintenance restoration requires recovery; see journal")


def run(path, *, limit=0, apply=True):
    central = load_config()["publisher_repository"]
    if path.exists() and not json.loads(path.read_text()).get("restored"):
        raise RuntimeError("unfinished maintenance journal requires restoration first")
    state = {"version": 1, "kind": "reader-maintenance-journal", "repositories": {},
             "maintenance_id": uuid.uuid4().hex, "started_at": datetime.now(timezone.utc).isoformat()}
    journal(path, state)
    try:
        # A read-only preflight checks every credential and inventory before the
        # first disable. A missing worker token must fail here, not mid-isolation.
        inventory = preflight()
        for repo, record in inventory.items():
            state["repositories"][repo] = record
            journal(path, state)
            for workflow in record["workflows"]:
                if workflow["state"] == "active":
                    workflow_state(repo, workflow, False)
            for status in ACTIVE:
                for task in pages(repo, f"actions/runs?status={status}", "workflow_runs"):
                    if not producer_path(task["path"]) or task["status"] != "queued":
                        continue
                    record["cancelled"].append({"id": task["id"]})
                    journal(path, state)
                    try:
                        api(repo, f"actions/runs/{task['id']}/cancel", method="POST")
                    except RuntimeError as error:
                        attempt = api(repo, f"actions/runs/{task['id']}/attempts/{task.get('run_attempt', 1)}")
                        if attempt.get("status") == "completed":
                            continue
                        # GitHub rejects cancellation of a run that its
                        # scheduler has not admitted yet (409). The fence
                        # decides whether that old run has any executable job.
                        if getattr(error, "status", None) == 409:
                            record["cancelled"][-1]["cancel_deferred"] = True
                            journal(path, state)
                            continue
                        raise
            if repo != central and record["permission"].get("enabled"):
                disable_actions(repo, record)
                journal(path, state)
        deadline = time.monotonic() + 3600
        while True:
            active = []
            for repo in state["repositories"]:
                active.extend(task for status in ("in_progress", "waiting", "requested")
                    for task in pages(repo, f"actions/runs?status={status}", "workflow_runs")
                    if producer_path(task["path"]))
            if not active:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("producer drain deadline exceeded")
            time.sleep(30)
        identifier = state["maintenance_id"]
        state["dispatch_requested"] = True
        journal(path, state)
        api(central, "actions/workflows/reader-gc-collect.yml/dispatches", method="POST", body={
            "ref": "main", "inputs": {"apply": str(apply).lower(), "grace_days": "0",
                                      "scope": "unreferenced", "reader_only": "true",
                                      "limit": str(limit), "maintenance_id": identifier}})
        deadline = time.monotonic() + 360 * 60
        while True:
            runs = pages(central, "actions/workflows/reader-gc-collect.yml/runs", "workflow_runs")
            matching = [task for task in runs if identifier in task.get("display_title", "")]
            if matching:
                task = matching[0]
                state["collector_run_id"] = task["id"]
                journal(path, state)
                if task["status"] == "completed":
                    if task["conclusion"] != "success":
                        raise RuntimeError("collector failed; restoring producers")
                    state["collected"] = True
                    journal(path, state)
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError("collector deadline exceeded")
            time.sleep(30)
    finally:
        if state.get("dispatch_requested") and not state.get("collector_run_id"):
            matching = [task for task in pages(central,
                "actions/workflows/reader-gc-collect.yml/runs", "workflow_runs")
                if state["maintenance_id"] in task.get("display_title", "")]
            if not matching:
                raise RuntimeError("collector dispatch outcome unknown; recover journal before restoring producers")
            state["collector_run_id"] = matching[0]["id"]
            journal(path, state)
        if state.get("collector_run_id"):
            task = api(central, f"actions/runs/{state['collector_run_id']}")
            if task["status"] != "completed":
                api(central, f"actions/runs/{task['id']}/cancel", method="POST")
                deadline = time.monotonic() + 600
                while api(central, f"actions/runs/{task['id']}")["status"] != "completed":
                    if time.monotonic() >= deadline:
                        raise RuntimeError("collector still running; keep producers isolated and recover journal")
                    time.sleep(10)
        restore(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["run", "restore", "preflight"])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--plan-only", action="store_true")
    arguments = parser.parse_args()
    if arguments.limit < 0:
        parser.error("limit must be non-negative")
    if arguments.operation == "preflight":
        inventory = preflight()
        journal(arguments.journal, {"preflight_only": True, "repositories": inventory,
                                    "remote_writes": 0})
        print(f"GET-only maintenance preflight passed for {len(inventory)} repositories")
    elif arguments.operation == "run":
        run(arguments.journal, limit=arguments.limit, apply=not arguments.plan_only)
    else:
        restore(arguments.journal)
