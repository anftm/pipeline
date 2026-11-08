"""Daily search generation staging with a fail-closed cutover guard.

The default command stages a reproducible generation without changing either
consumer or the mutable pointer. Publication must be enabled separately only
when the full promotion/recovery protocol is available.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

try:
    from . import search_snapshot
    from .search_artifact_s3 import S3SearchArtifactStore
    from .search_artifact_store import stage_generation
    from .search_generation import build_generation
    from .search_publication_pointer import decode_pointer
except ImportError:
    import search_snapshot
    from search_artifact_s3 import S3SearchArtifactStore
    from search_artifact_store import stage_generation
    from search_generation import build_generation
    from search_publication_pointer import decode_pointer


BUCKET_RESOLVE = "https://huggingface.co/buckets/vomebook/search-data-v1/resolve/"
SPACE_RECEIPT = "https://voiceofml-search.hf.space/api/search-data-generation"
PAGES_MANIFEST = "https://vomebook.github.io/search/data/generation-manifest.json"
MAX_JSON = 1024 * 1024


def bounded_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Vomebook-Daily-Search/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=40) as response:
        if response.status != 200:
            raise RuntimeError(f"search metadata HTTP {response.status}")
        raw = response.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("oversized search metadata")
    return raw


def inspect_published(*, get=bounded_get) -> dict:
    """Return only after pointer, manifest and both actual consumers agree."""
    raw_pointer = get(BUCKET_RESOLVE + "published/current.json")
    pointer = decode_pointer(raw_pointer)
    generation = pointer["desired"]
    raw_manifest = get(BUCKET_RESOLVE + f"generations/{generation}/manifest.json")
    manifest = json.loads(raw_manifest)
    if not isinstance(manifest, dict) or manifest.get("generation") != generation:
        raise ValueError("published search manifest does not match desired generation")
    excluded = {"generation", "remote_writes", "publication_ready", "object_layout", "objects", "additional_checks"}
    import hashlib
    if hashlib.sha256(search_snapshot.canonical({k: v for k, v in manifest.items() if k not in excluded})).hexdigest() != generation:
        raise ValueError("published search manifest identity mismatch")
    pages = json.loads(get(PAGES_MANIFEST))
    space = json.loads(get(SPACE_RECEIPT))
    if (not isinstance(pages, dict) or pages.get("generation") != generation
            or pages.get("files") != manifest.get("files") or pages.get("objects") != manifest.get("objects")):
        raise ValueError("Pages has not acknowledged desired generation")
    if (not isinstance(space, dict) or space.get("generation") != generation or space.get("surface") != "space"
            or space.get("verified") is not True or space.get("records") != manifest.get("record_count")):
        raise ValueError("Space has not acknowledged desired generation")
    return {"generation": generation, "source_revisions": manifest.get("source_revisions"),
            "record_count": manifest.get("record_count"), "space_api_generation": space.get("api_generation"),
            "transition": pointer["transition"], "both_consumers_observed": True}


def stage(source: Path, tree: Path, browser: Path, revisions: Path, output: Path, store) -> dict:
    report = build_generation(source, tree, browser, revisions, output)
    result = stage_generation(output, store)
    return {**result, "record_count": report["record_count"], "staged": True,
            "consumers_verified": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect")
    build = sub.add_parser("stage")
    for name in ("source", "tree", "browser", "revisions", "output"):
        build.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "inspect":
        result = inspect_published()
    else:
        result = stage(args.source, args.tree, args.browser, args.revisions, args.output, S3SearchArtifactStore())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as output:
            output.write(f"generation={result['generation']}\n")
            if args.command == "inspect":
                revisions = json.dumps(result["source_revisions"], ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"))
                output.write(f"source_revisions={revisions}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
