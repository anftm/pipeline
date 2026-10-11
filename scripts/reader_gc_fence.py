#!/usr/bin/env python3
"""Verify a drained maintenance window across every configured Reader producer."""
import json
import os
import subprocess
import time

try:
    from .pdf_worker_lanes import load_config
except ImportError:
    from pdf_worker_lanes import load_config


COLLECTOR = ".github/workflows/reader-gc-collect.yml"


def producer_path(path):
    path = path.split("@", 1)[0]
    name = path.rsplit("/", 1)[-1]
    return path.startswith(".github/workflows/") and path != COLLECTOR and (
        name.startswith(("reader-", "pdf-", "lin-native-text", "migrate-pdf-")))


def producer_repositories():
    config = load_config()
    return sorted({config["publisher_repository"], *[
        lane.get("repository", lane["owner"] + "/pipeline")
        for lane in [*config["native_lanes"], config["converted_lane"]]]})


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

    def isolated_residual(self, repo, run):
        """A terminal attempt is not sufficient: repository execution must be off."""
        permission = self.single(f"repos/{repo}/actions/permissions")
        if permission.get("enabled") is not False:
            raise RuntimeError(f"producers have not drained: {repo}: {[run['id']]}")
        endpoint = f"repos/{repo}/actions/runs/{run['id']}"
        before = self.single(endpoint)
        attempt = before.get("run_attempt")
        if (before.get("id") != run["id"] or before.get("status") not in {"queued", "pending"}
                or type(attempt) is not int or attempt < 1):
            raise RuntimeError("residual run identity/status mismatch")
        terminal = self.single(endpoint + f"/attempts/{attempt}")
        if (terminal.get("id") != run["id"] or terminal.get("run_attempt") != attempt
                or terminal.get("status") != "completed" or not terminal.get("conclusion")):
            raise RuntimeError("queued run has no terminal execution attempt")
        latest = self.api(endpoint + "/jobs?filter=latest&per_page=100")
        history = self.api(endpoint + "/jobs?filter=all&per_page=100")
        if any(page.get("total_count") != 0 or page.get("jobs") != [] for page in latest):
            raise RuntimeError("queued run has a latest execution job")
        jobs = [job for page in history for job in page["jobs"]]
        if (not jobs or any(job.get("status") != "completed" or not job.get("completed_at")
                            or job.get("run_attempt", 0) > attempt for job in jobs)):
            raise RuntimeError("residual run history contains unfinished execution")
        after = self.single(endpoint)
        identity = ("id", "status", "run_attempt", "updated_at", "check_suite_id")
        if (any(before.get(key) != after.get(key) for key in identity)
                or self.single(f"repos/{repo}/actions/permissions").get("enabled") is not False):
            raise RuntimeError("residual run isolation changed during verification")
        return {"id": run["id"], "status": after["status"], "run_attempt": attempt,
                "attempt_status": terminal["status"], "attempt_conclusion": terminal["conclusion"],
                "historical_jobs": len(jobs), "latest_jobs": 0, "repository_actions_enabled": False,
                "updated_at": after.get("updated_at")}

    def check(self):
        checked = []
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
                    residuals.append(self.isolated_residual(repo, run))
            checked.append({"repository": repo, "disabled_workflows": [w["path"] for w in workflows],
                            "isolated_residual_runs": residuals})
        return {"kind": "reader-gc-producer-fence", "repositories": checked}


def assert_collector_context():
    repo = load_config()["publisher_repository"]
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY") != repo
            or os.environ.get("GITHUB_WORKFLOW_REF") != repo + "/" + COLLECTOR + "@refs/heads/main"
            or not os.environ.get("GITHUB_RUN_ID")):
        raise RuntimeError("object collection requires the central maintenance workflow")
