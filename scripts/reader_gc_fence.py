#!/usr/bin/env python3
"""Verify a drained maintenance window across every configured Reader producer."""
import json
import os
import subprocess

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
        result = subprocess.run(["gh", "api", "--paginate", "--slurp", endpoint],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError("cannot verify GitHub producer fence")
        return json.loads(result.stdout)

    def check(self):
        checked = []
        for repo in producer_repositories():
            workflows = [w for page in self.api(f"repos/{repo}/actions/workflows?per_page=100")
                         for w in page["workflows"] if producer_path(w["path"])]
            if not any(w["path"].endswith("/pdf-account-worker.yml") for w in workflows):
                raise RuntimeError(f"producer workflow inventory is incomplete: {repo}")
            enabled = [w["path"] for w in workflows if w["state"] == "active"]
            if enabled:
                raise RuntimeError(f"producers are not paused: {repo}: {enabled}")
            for status in ("in_progress", "queued", "pending", "waiting", "requested"):
                pages = self.api(f"repos/{repo}/actions/runs?status={status}&per_page=100")
                active = [r["id"] for page in pages for r in page["workflow_runs"]
                          if producer_path(r["path"]) and r["status"] != "completed"]
                if active:
                    raise RuntimeError(f"producers have not drained: {repo}: {active}")
            checked.append({"repository": repo, "disabled_workflows": [w["path"] for w in workflows]})
        return {"kind": "reader-gc-producer-fence", "repositories": checked}


def assert_collector_context():
    repo = load_config()["publisher_repository"]
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY") != repo
            or os.environ.get("GITHUB_WORKFLOW_REF") != repo + "/" + COLLECTOR + "@refs/heads/main"
            or not os.environ.get("GITHUB_RUN_ID")):
        raise RuntimeError("object collection requires the central maintenance workflow")
