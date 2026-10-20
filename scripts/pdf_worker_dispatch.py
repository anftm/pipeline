#!/usr/bin/env python3
"""Coalesce PDF worker continuations and re-kick idle lanes."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

try:
    from . import pdf_worker_lanes
    from .validate_pdf_worker_run import validate_run
except ImportError:
    import pdf_worker_lanes
    from validate_pdf_worker_run import validate_run


WORKER = "pdf-account-worker.yml"
PUBLISHER = "pdf-account-publish.yml"
ACTIVE_STATUSES = ("requested", "waiting", "pending", "queued", "in_progress")
# The worker decides: OCR rendered pages first, otherwise render new PDFs.
NEXT_BATCH = {"stage": "auto", "limit": "20"}
# A finished worker needs a moment to dispatch its central publisher.
IDLE_GRACE = timedelta(minutes=30)
PUBLISH_TITLE = re.compile(r"PDF publish (render|ocr) / ([A-Za-z0-9-]+/[A-Za-z0-9._-]+) / ([1-9][0-9]*)")


def github(method, path, payload=None):
    args = ["gh", "api", "--method", method, path]
    if payload is not None:
        args += ["--input", "-"]
    result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        # Never retry a dispatch with an unknown outcome or log credential-bearing
        # command/environment output. A failed read must not admit more work.
        raise RuntimeError(f"GitHub {method} failed; OCR continuation stopped")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def active_runs(repository, workflow, api):
    runs = {}
    for status in ACTIVE_STATUSES:
        page = 1
        while True:
            data = api("GET", f"repos/{repository}/actions/workflows/{workflow}/runs"
                       f"?status={status}&per_page=100&page={page}")
            rows = data.get("workflow_runs")
            if not isinstance(rows, list):
                raise ValueError("invalid workflow queue response")
            for run in rows:
                if (run.get("repository", {}).get("full_name", "").lower() != repository.lower()
                        or run.get("path") != ".github/workflows/" + workflow
                        or type(run.get("id")) is not int or not run.get("status")):
                    raise ValueError("workflow queue identity mismatch")
                if run["status"] != "completed":
                    runs[run["id"]] = run
            if len(rows) < 100:
                break
            page += 1
    return list(runs.values())


def assert_publisher(config, run_id):
    expected = config["publisher_repository"]
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY", "").lower() != expected.lower()
            or os.environ.get("GITHUB_WORKFLOW_REF") != expected + "/.github/workflows/" + PUBLISHER + "@refs/heads/main"
            or os.environ.get("GITHUB_RUN_ID") != str(run_id)):
        raise RuntimeError("OCR continuation requires the central serialized publisher")


def handoff(config, repository, after_run, publisher_run, *, apply=False, api=github, checkpoint=None,
            sleep=time.sleep):
    """Only the final publisher for an idle account may request its next batch.

    All automatic callers hold reader-sidecar. This does not claim an atomic
    lock against independently/manual-dispatched GitHub runs.
    """
    if type(after_run) is not int or after_run < 1 or type(publisher_run) is not int or publisher_run < 1:
        raise ValueError("positive worker and publisher run IDs required")
    owner, separator, _ = repository.partition("/")
    if not separator or pdf_worker_lanes.repository_for_owner(config, owner).lower() != repository.lower():
        raise ValueError("unconfigured PDF worker repository")
    if apply:
        assert_publisher(config, publisher_run)
    central = config["publisher_repository"]
    publisher = api("GET", f"repos/{central}/actions/runs/{publisher_run}")
    match = PUBLISH_TITLE.fullmatch(publisher.get("display_title", ""))
    if (publisher.get("id") != publisher_run
            or publisher.get("repository", {}).get("full_name", "").lower() != central.lower()
            or publisher.get("path") != ".github/workflows/" + PUBLISHER
            or publisher.get("head_branch") != "main" or publisher.get("event") != "workflow_dispatch"
            or not match or match[2].lower() != repository.lower() or int(match[3]) != after_run
            or (apply and publisher.get("status") != "in_progress")):
        raise ValueError("continuation publisher identity mismatch")
    source = api("GET", f"repos/{repository}/actions/runs/{after_run}")
    if source.get("id") != after_run:
        raise ValueError("continuation source run mismatch")
    validate_run(config, repository, source)
    report = {"version": 1, "kind": "pdf-worker-continuation", "applied": apply,
              "repository": repository, "after_run": after_run, "publisher_run": publisher_run,
              "decision": "deferred", "active_workers": [], "awaiting_publishers": []}
    try:
        pdf_worker_lanes.lane_for_owner(config, owner)
    except ValueError:
        report["reason"] = "retired-worker-account"
        if checkpoint:
            checkpoint(report)
        return report
    # A previous worker may already be complete but its page checkpoints have not
    # been published yet. Starting a new plan then would recognize those pages
    # again. Let the last queued publisher own the continuation instead.
    for run in active_runs(central, PUBLISHER, api):
        if run["id"] == publisher_run:
            continue
        title = PUBLISH_TITLE.fullmatch(run.get("display_title", ""))
        if not title:
            raise ValueError("cannot identify queued PDF publisher")
        if title[2].lower() == repository.lower():
            report["awaiting_publishers"].append(run["id"])
    # Check workers last, immediately before dispatch. Include every active
    # status/branch: all of them share this account's worker concurrency group.
    report["active_workers"] = [run["id"] for run in active_runs(repository, WORKER, api)]
    if report["active_workers"]:
        report["reason"] = "worker-already-active"
    elif report["awaiting_publishers"]:
        report["reason"] = "worker-results-await-publication"
    else:
        report.update(decision="would-dispatch", reason="account-idle-and-publications-drained")
    if checkpoint:
        checkpoint(report)
    if report["decision"] != "would-dispatch" or not apply:
        return report
    # Persist intent before the sole POST, so a timeout never invites a blind
    # automatic retry. Subsequent callbacks re-read both queues under the lock.
    report["decision"] = "dispatch-requesting"
    if checkpoint:
        checkpoint(report)
    api("POST", f"repos/{repository}/actions/workflows/{WORKER}/dispatches",
        {"ref": "main", "inputs": dict(NEXT_BATCH)})
    report["decision"] = "dispatch-requested"
    if checkpoint:
        checkpoint(report)
    # Keep the serialized handoff until GitHub exposes the accepted run. A
    # second publisher must not see the account as idle during propagation lag.
    for attempt in range(6):
        observed = active_runs(repository, WORKER, api)
        if observed:
            report["observed_workers"] = [run["id"] for run in observed]
            if checkpoint:
                checkpoint(report)
            return report
        if attempt < 5:
            sleep(5)
    raise RuntimeError("accepted OCR dispatch is not visible; inspect it before any retry")


def lane_repositories(config):
    return sorted({pdf_worker_lanes.repository_for_owner(config, lane["owner"])
                   for lane in [*config["native_lanes"], config["converted_lane"]]})


def watchdog(config, *, apply=False, api=github, now=None):
    """Restart a lane whose continuation chain broke (failed callback,
    disabled publisher, empty OCR queue). A lane with an active worker, a
    pending central publication or a recently finished worker is left alone."""
    now = now or datetime.now(timezone.utc)
    central = config["publisher_repository"]
    awaiting = set()
    for run in active_runs(central, PUBLISHER, api):
        title = PUBLISH_TITLE.fullmatch(run.get("display_title", ""))
        if not title:
            raise ValueError("cannot identify queued PDF publisher")
        awaiting.add(title[2].lower())
    report = {"version": 1, "kind": "pdf-worker-watchdog", "applied": apply, "lanes": []}
    for repository in lane_repositories(config):
        lane = {"repository": repository}
        if repository.lower() in awaiting:
            lane["decision"] = "publication-pending"
        elif active_runs(repository, WORKER, api):
            lane["decision"] = "worker-active"
        else:
            rows = api("GET", f"repos/{repository}/actions/workflows/{WORKER}/runs?per_page=1").get("workflow_runs")
            if not isinstance(rows, list):
                raise ValueError("invalid workflow history response")
            finished = rows[0].get("updated_at") if rows else None
            if finished and now - datetime.fromisoformat(finished.replace("Z", "+00:00")) < IDLE_GRACE:
                lane["decision"] = "recently-finished"
            else:
                lane["decision"] = "dispatch-requested" if apply else "would-dispatch"
                if apply:
                    api("POST", f"repos/{repository}/actions/workflows/{WORKER}/dispatches",
                        {"ref": "main", "inputs": dict(NEXT_BATCH)})
        report["lanes"].append(lane)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchdog", action="store_true", help="re-kick idle lanes instead of a continuation")
    parser.add_argument("--repository")
    parser.add_argument("--after-run", type=int)
    parser.add_argument("--publisher-run", type=int, default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.watchdog:
        report = watchdog(pdf_worker_lanes.load_config(), apply=args.apply)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    if not args.repository or not args.after_run:
        parser.error("--repository and --after-run are required for a continuation")
    def checkpoint(report):
        args.output.parent.mkdir(parents=True, exist_ok=True)
        pending = args.output.with_suffix(".pending.json")
        pending.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        pending.replace(args.output)
    report = handoff(pdf_worker_lanes.load_config(), args.repository, args.after_run, args.publisher_run,
                     apply=args.apply, checkpoint=checkpoint)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
