#!/usr/bin/env python3
"""Journalled producer isolation, collector dispatch and unconditional restoration."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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


def api(repo, path, *, method="GET", body=None):
    tokens = json.loads(os.environ["GC_PRODUCER_TOKENS_JSON"])
    token = tokens.get(repo)
    if not isinstance(token, str) or not token:
        raise RuntimeError("incomplete maintenance credentials")
    command = ["gh", "api", f"repos/{repo}/{path}", "--method", method]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
        env={**os.environ, "GH_TOKEN": token}, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"maintenance API {method} failed: {repo}:{path}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


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
                api(repo, "actions/permissions", method="PUT", body=record["permission"])
                if api(repo, "actions/permissions").get("enabled") != record["permission"].get("enabled"):
                    raise RuntimeError("Actions restoration readback mismatch")
            for workflow in record["workflows"]:
                if workflow["state"] == "active":
                    api(repo, f"actions/workflows/{workflow['id']}/enable", method="PUT")
                    if api(repo, f"actions/workflows/{workflow['id']}")["state"] != "active":
                        raise RuntimeError("workflow restoration readback mismatch")
            for run in record["cancelled"]:
                if run.get("restored"):
                    continue
                current = api(repo, f"actions/runs/{run['id']}")
                if current["status"] == "completed" and current.get("conclusion") == "cancelled":
                    if run.get("restoration_requested"):
                        if current.get("run_attempt", 1) <= run.get("restoration_attempt", 1):
                            raise RuntimeError("ambiguous rerun response requires readback before retry")
                        run["restored"] = True
                        journal(path, state)
                        continue
                    run["restoration_requested"] = True
                    run["restoration_attempt"] = current.get("run_attempt", 1)
                    journal(path, state)
                    api(repo, f"actions/runs/{run['id']}/rerun", method="POST")
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


def run(path):
    central = load_config()["publisher_repository"]
    if path.exists() and not json.loads(path.read_text()).get("restored"):
        raise RuntimeError("unfinished maintenance journal requires restoration first")
    state = {"version": 1, "kind": "reader-maintenance-journal", "repositories": {},
             "maintenance_id": uuid.uuid4().hex, "started_at": datetime.now(timezone.utc).isoformat()}
    journal(path, state)
    try:
        for repo in producer_repositories():
            workflows = [w for w in pages(repo, "actions/workflows", "workflows") if producer_path(w["path"])]
            record = {"permission": api(repo, "actions/permissions"), "workflows": [
                {"id": w["id"], "state": w["state"], "path": w["path"]} for w in workflows], "cancelled": []}
            state["repositories"][repo] = record
            journal(path, state)
            for workflow in workflows:
                if workflow["state"] == "active":
                    api(repo, f"actions/workflows/{workflow['id']}/disable", method="PUT")
            for status in ACTIVE:
                for task in pages(repo, f"actions/runs?status={status}", "workflow_runs"):
                    if not producer_path(task["path"]) or task["status"] != "queued":
                        continue
                    record["cancelled"].append({"id": task["id"]})
                    journal(path, state)
                    try:
                        api(repo, f"actions/runs/{task['id']}/cancel", method="POST")
                    except RuntimeError:
                        attempt = api(repo, f"actions/runs/{task['id']}/attempts/{task.get('run_attempt', 1)}")
                        if attempt.get("status") != "completed":
                            raise
            if repo != central and record["permission"].get("enabled"):
                record["permission_changed"] = True
                journal(path, state)
                api(repo, "actions/permissions", method="PUT",
                    body={**record["permission"], "enabled": False})
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
            "ref": "main", "inputs": {"apply": "true", "grace_days": "0", "scope": "unreferenced",
                                       "maintenance_id": identifier}})
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
    parser.add_argument("operation", choices=["run", "restore"])
    parser.add_argument("--journal", type=Path, required=True)
    arguments = parser.parse_args()
    (run if arguments.operation == "run" else restore)(arguments.journal)
