#!/usr/bin/env python3
"""Trigger the remote PDF workflow at a bounded polling interval."""

import argparse
import json
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


WORKFLOW_URL = "https://api.github.com/repos/anftm/pipeline/actions/workflows/pdf-assets.yml/dispatches"
RUNS_URL = "https://api.github.com/repos/anftm/pipeline/actions/workflows/pdf-assets.yml/runs?per_page=10"


def github_token() -> str:
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True, capture_output=True, check=True,
    )
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not fields.get("password"):
        raise RuntimeError("GitHub credential is unavailable")
    return fields["password"]


def request(url: str, token: str, payload: dict | None = None) -> dict | None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=60) as response:
        if response.status == 204:
            return None
        return json.loads(response.read())


def state_read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"checkpoint": 0}


def state_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def trigger(args: argparse.Namespace, token: str, state: dict) -> None:
    runs = request(RUNS_URL, token) or {}
    latest = (runs.get("workflow_runs") or [None])[0]
    if state.get("active"):
        if latest and latest.get("status") in {"queued", "in_progress", "pending"}:
            state.update({"status": "waiting", "run": latest.get("html_url")})
            state_write(args.state, state)
            return
        if latest and latest.get("conclusion") == "success":
            state["checkpoint"] = int(state.get("checkpoint", 0)) + 1
        state.pop("active", None)
        state.pop("run", None)
    if latest and latest.get("status") in {"queued", "in_progress", "pending"}:
        state.update({"status": "waiting", "run": latest.get("html_url")})
        state_write(args.state, state)
        return
    payload = {
        "ref": "main",
        "inputs": {
            "source": args.source,
            "extension": "pdf",
            "repo": args.repo,
            "limit": str(args.batch),
            "checkpoint": str(state.get("checkpoint", 0)),
            "dry_run": "false",
        },
    }
    request(WORKFLOW_URL, token, payload)
    state.update({
        "status": "triggered", "triggered_at": datetime.now(timezone.utc).isoformat(),
        "active": True,
    })
    state_write(args.state, state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("--source", choices=("generated", "upstream", "all"), default="all")
    parser.add_argument("--repo", default="")
    parser.add_argument("--state", type=Path, default=Path("output/pdf-assets/remote-controller.json"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 30 or args.batch < 1:
        raise ValueError("interval must be at least 30 seconds and batch must be positive")
    token = github_token()
    while True:
        trigger(args, token, state_read(args.state))
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
