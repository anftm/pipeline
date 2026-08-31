#!/usr/bin/env python3
"""Coordinate remote PDF batches and local DJVU page-stream batches."""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from local_pdf_controller import run_batch as run_djvu_batch
from local_pdf_controller import write_state as write_djvu_state
from remote_pdf_controller import github_token, state_read, state_write, trigger


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--pdf-batch", type=int, default=10)
    parser.add_argument("--djvu-batch", type=int, default=1)
    parser.add_argument("--repo", default="")
    parser.add_argument("--pdf-state", type=Path, default=Path("output/pdf-assets/remote-controller.json"))
    parser.add_argument("--djvu-state", type=Path, default=Path("output/pdf-assets/local-controller.json"))
    parser.add_argument("--djvu-bundle", type=Path, default=Path("output/pdf-assets/local-djvu"))
    parser.add_argument("--pipeline-dir", type=Path, default=Path("/tmp/opencode/pipeline-latest"))
    args = parser.parse_args()
    if args.interval < 30 or args.pdf_batch < 1 or args.djvu_batch < 1:
        raise ValueError("invalid controller interval or batch size")
    token = github_token()
    remote_args = argparse.Namespace(
        source="all", repo=args.repo, batch=args.pdf_batch, state=args.pdf_state,
    )
    while True:
        trigger(remote_args, token, state_read(args.pdf_state))
        djvu_args = argparse.Namespace(
            batch=args.djvu_batch, repo=args.repo, state=args.djvu_state,
            bundle_root=args.djvu_bundle, pipeline_dir=args.pipeline_dir,
        )
        run_djvu_batch(djvu_args)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
