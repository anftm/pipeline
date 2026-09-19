#!/usr/bin/env python3
"""Continue an explicitly bounded backfill after its publication succeeded."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def continuation_command(root, env):
    remaining = int(env.get("FOLLOWUPS", "0"))
    if not 0 <= remaining <= 20:
        raise ValueError("followups must be 0..20")
    result_files = list(root.rglob("results.json"))
    if not remaining or not any(json.loads(path.read_text()) for path in result_files):
        return None
    command = ["gh", "workflow", "run", "pdf-range-assets.yml", "--ref", "main"]
    values = {"followups": str(remaining - 1), "repo": env.get("SOURCE_REPO", ""),
              "path": env.get("SOURCE_PATH", ""), "limit": env.get("BATCH_LIMIT", "30"),
              "checkpoints": env.get("CHECKPOINTS", "4"), "dry_run": "false",
              "retry_failed": env.get("RETRY_FAILED", "false"),
              "retry_blocked": env.get("RETRY_BLOCKED", "false"),
              "retry_stale_blocked": env.get("RETRY_STALE_BLOCKED", "false")}
    for key, value in values.items():
        command.extend(["-f", f"{key}={value}"])
    return command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles", type=Path, required=True)
    args = parser.parse_args()
    command = continuation_command(args.bundles, os.environ)
    if command:
        subprocess.run(command, check=True, timeout=60)
        print("Started next PDF batch after successful publication", flush=True)
    else:
        print("PDF continuation stopped: no work or no remaining batch budget", flush=True)


if __name__ == "__main__":
    main()
