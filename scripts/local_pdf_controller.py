#!/usr/bin/env python3
"""Run local DJVU page-stream conversion on a bounded polling loop."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_INTERVAL = 300
DEFAULT_BATCH = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--state", type=Path, default=Path("output/pdf-assets/local-controller.json"))
    parser.add_argument("--bundle-root", type=Path, default=Path("output/pdf-assets/local-djvu"))
    parser.add_argument("--repo", default="")
    return parser.parse_args()


def write_state(path: Path, **values: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
    current.update(values)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_batch(args: argparse.Namespace) -> int:
    command = [
        sys.executable, "scripts/pdf_assets.py",
        "--source", "upstream", "--extension", "djvu", "--failed-only",
        "--limit", str(args.batch), "--checkpoint", "0",
        "--bundle", str(args.bundle_root),
    ]
    if args.repo:
        command += ["--repo", args.repo]
    started = datetime.now(timezone.utc).isoformat()
    write_state(args.state, status="running", started_at=started, batch=args.batch)
    result = subprocess.run(command, check=False)
    write_state(
        args.state,
        status="success" if result.returncode == 0 else "failed",
        finished_at=datetime.now(timezone.utc).isoformat(),
        returncode=result.returncode,
    )
    return result.returncode


def main() -> int:
    args = parse_args()
    if args.interval < 30 or args.batch < 1:
        raise ValueError("interval must be at least 30 seconds and batch must be positive")
    lock_path = args.state.with_suffix(args.state.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("local PDF controller is already running", file=sys.stderr)
            return 2
        while True:
            run_batch(args)
            if args.once:
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
