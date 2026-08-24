#!/usr/bin/env python3
"""Build the compact search sidecar from a Reader Assets manifest."""

import argparse
import gzip
import json
from pathlib import Path

try:
    from .reader_assets import load_json, validate_manifest
except ImportError:
    from reader_assets import load_json, validate_manifest

STATUS = {"ready": 2, "failed": 4}
MODE = {"pdf": "p", "epub": "e", "docx": "d"}


def build_index(manifest: dict) -> dict:
    files = {}
    for key, entry in manifest["files"].items():
        status = entry.get("status")
        if status not in STATUS:
            continue
        compact = {"s": STATUS[status]}
        if status == "ready":
            compact.update({"m": MODE[entry["reader_mode"]], "p": entry["path"]})
        files[key] = compact
    return {"v": 1, "f": dict(sorted(files.items()))}


def encode_index(manifest: dict) -> bytes:
    payload = json.dumps(build_index(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(payload, compresslevel=9, mtime=0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/reader_assets.json.gz"))
    args = parser.parse_args()
    manifest = validate_manifest(load_json(args.manifest))
    index = build_index(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encode_index(manifest))
    print(f"wrote {len(index['f'])} reader asset mapping(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
