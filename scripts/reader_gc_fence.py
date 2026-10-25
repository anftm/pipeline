#!/usr/bin/env python3
"""Verify a drained maintenance window across every configured Reader producer."""
import json
import os
import subprocess
import time
from datetime import datetime, timezone

try:
    from .pdf_worker_lanes import load_config
except ImportError:
    from pdf_worker_lanes import load_config


COLLECTOR = ".github/workflows/reader-gc-collect.yml"


def age_minutes(stamp):
    if not isinstance(stamp, str):
        return 0
    try:
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - value).total_seconds() / 60)


def producer_path(path):
    path = path.split("@", 1)[0]
    name = path.rsplit("/", 1)[-1]
    return path.startswith(".github/workflows/") and path not in {
        COLLECTOR, ".github/workflows/reader-maintenance.yml"} and (
        name.startswith(("reader-", "pdf-", "lin-native-text", "migrate-pdf-")) or
        # Retired/renamed filenames stay listed: their queued or rerun records
        # can still exist after the workflow file moved.
        name in {"force-ocr-book.yml", "publish-source-txt.yml", "publish-ccrd-index.yml", "promote-ccrd-generation.yml",
                 "update-ccrd-source.yml", "publish-ccrd-ocr.yml", "repair-chm-conversions.yml"})


def producer_repositories():
    # Retired routing lanes may still own queued or rerun workflows. A routing
    # change must not silently drop a repository from the drain fence.
    config = load_config()
    repositories = set()
    for routing in [config, *config.get("legacy_configs", [])]:
        repositories.add(routing["publisher_repository"])
        for lane in [*routing["native_lanes"], routing["converted_lane"]]:
            repositories.add(lane.get("repository", lane["owner"] + "/pipeline"))
    return sorted(repositories)


class GitHubProducerFence:
    def __init__(self, api=None):
        self.api = api or self.github_api

    @staticmethod
    def github_api(endpoint):
        env = os.environ.copy()
        if os.environ.get("GC_PRODUCER_TOKENS_JSON"):
            tokens = json.loads(os.environ["GC_PRODUCER_TOKENS_JSON"])
            parts = endpoint.split("/")
            repo = "/".join(parts[1:3]) if parts[0] == "repos" else ""
            if repo not in tokens or not isinstance(tokens[repo], str) or not tokens[repo]:
                raise RuntimeError("producer credential inventory is incomplete")
            env["GH_TOKEN"] = tokens[repo]
        for attempt in range(3):
            result = subprocess.run(["gh", "api", "--paginate", "--slurp", endpoint],
                                    capture_output=True, text=True, timeout=120, env=env)
            if not result.returncode:
                return json.loads(result.stdout)
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise RuntimeError("cannot verify GitHub producer fence")

    def single(self, endpoint):
        pages = self.api(endpoint)
        if len(pages) != 1 or not isinstance(pages[0], dict):
            raise RuntimeError("invalid producer state response")
        return pages[0]

    def isolated_residual(self, repo, run, central=False):
        """A terminal attempt is not sufficient: repository execution must be off.

        The central repository hosts this maintenance run, so its repository-wide
        Actions switch cannot be disabled by the same window. There the fence
        instead requires every producer workflow to be disabled (checked by the
        caller) and the residual run itself to have no executable job or check.
        A scheduler-stuck old run with no jobs is reported as a ghost, never as
        evidence that a producer may still write.
        """
        try:
            permission = self.single(f"repos/{repo}/actions/permissions")
        except RuntimeError:
            if not central:
                raise
            permission = {}
        if not central and permission.get("enabled") is not False:
            raise RuntimeError(f"producers have not drained: {repo}: {[run['id']]}")
        endpoint = f"repos/{repo}/actions/runs/{run['id']}"
        before = self.single(endpoint)
        attempt = before.get("run_attempt")
        if (before.get("id") != run["id"] or before.get("status") not in {"queued", "pending"}
                or type(attempt) is not int or attempt < 1):
            raise RuntimeError("residual run identity/status mismatch")
        latest = self.api(endpoint + "/jobs?filter=latest&per_page=100")
        history = self.api(endpoint + "/jobs?filter=all&per_page=100")
        if any(page.get("total_count") != 0 or page.get("jobs") != [] for page in latest):
            raise RuntimeError("queued run has a latest execution job")
        jobs = [job for page in history for job in page["jobs"]]
        if any(job.get("status") != "completed" or not job.get("completed_at")
               or job.get("run_attempt", 0) > attempt for job in jobs):
            raise RuntimeError(f"residual run history contains unfinished execution: {repo}: {run['id']}")
        checks = self.api(f"repos/{repo}/check-suites/{before['check_suite_id']}/check-runs?per_page=100")
        check_runs = [check for page in checks for check in page.get("check_runs", [])]
        terminal = self.single(endpoint + f"/attempts/{attempt}")
        terminal_ok = (terminal.get("id") == run["id"] and terminal.get("run_attempt") == attempt
                       and terminal.get("status") == "completed" and terminal.get("conclusion"))
        ghost = False
        if not terminal_ok:
            if not (central and not jobs and not check_runs and age_minutes(before.get("updated_at")) >= 30):
                raise RuntimeError("queued run has no terminal execution attempt")
            ghost = True
        after = self.single(endpoint)
        identity = ("id", "status", "run_attempt", "updated_at", "check_suite_id")
        if any(before.get(key) != after.get(key) for key in identity):
            raise RuntimeError("residual run isolation changed during verification")
        if not central and self.single(f"repos/{repo}/actions/permissions").get("enabled") is not False:
            raise RuntimeError("residual run isolation changed during verification")
        return {"id": run["id"], "status": after["status"], "run_attempt": attempt,
                "attempt_status": terminal.get("status"), "attempt_conclusion": terminal.get("conclusion"),
                "historical_jobs": len(jobs), "latest_jobs": 0, "check_runs": len(check_runs),
                "ghost_residual": ghost,
                "repository_actions_enabled": permission.get("enabled"),
                "updated_at": after.get("updated_at")}

    def check(self):
        checked = []
        central = load_config()["publisher_repository"]
        for repo in producer_repositories():
            residuals = []
            workflows = [w for page in self.api(f"repos/{repo}/actions/workflows?per_page=100")
                         for w in page["workflows"] if producer_path(w["path"])]
            if not any(w["path"].endswith("/pdf-account-worker.yml") for w in workflows):
                raise RuntimeError(f"producer workflow inventory is incomplete: {repo}")
            enabled = [w["path"] for w in workflows if w["state"] == "active"]
            if enabled:
                raise RuntimeError(f"producers are not paused: {repo}: {enabled}")
            for status in ("in_progress", "queued", "pending", "waiting", "requested"):
                pages = self.api(f"repos/{repo}/actions/runs?status={status}&per_page=100")
                active = [r for page in pages for r in page["workflow_runs"]
                          if producer_path(r["path"]) and r["status"] != "completed"]
                for run in active:
                    if run["status"] not in {"queued", "pending"}:
                        raise RuntimeError(f"producers have not drained: {repo}: {[run['id']]}")
                    residuals.append(self.isolated_residual(repo, run, central=(repo == central)))
            checked.append({"repository": repo, "disabled_workflows": [w["path"] for w in workflows],
                            "isolated_residual_runs": residuals})
        return {"kind": "reader-gc-producer-fence", "repositories": checked}


def assert_workflow_context(workflow):
    repo = load_config()["publisher_repository"]
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY") != repo
            or os.environ.get("GITHUB_WORKFLOW_REF") != repo + "/" + workflow + "@refs/heads/main"
            or not os.environ.get("GITHUB_RUN_ID")):
        raise RuntimeError("object collection requires the central maintenance workflow")


def assert_collector_context():
    assert_workflow_context(COLLECTOR)
