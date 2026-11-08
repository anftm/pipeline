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
    from .search_adopt import MAX_CORPUS, observe_consumers
    from .search_artifact_s3 import S3SearchArtifactStore
    from .search_artifact_store import stage_generation
    from .search_generation import build_generation
    from .search_publication_pointer import decode_pointer
except ImportError:
    import search_snapshot
    from search_adopt import MAX_CORPUS, observe_consumers
    from search_artifact_s3 import S3SearchArtifactStore
    from search_artifact_store import stage_generation
    from search_generation import build_generation
    from search_publication_pointer import decode_pointer


BUCKET_RESOLVE = "https://huggingface.co/buckets/vomebook/search-data-v1/resolve/"
SPACE_RECEIPT = "https://voiceofml-search.hf.space/api/search-data-generation"
PAGES_MANIFEST = "https://vomebook.github.io/search/data/generation-manifest.json"
MAX_JSON = 1024 * 1024


def bounded_get(url: str, limit: int = MAX_JSON) -> bytes:
    if not 0 < limit <= MAX_CORPUS:
        raise ValueError("invalid search read limit")
    request = urllib.request.Request(url, headers={"User-Agent": "Vomebook-Daily-Search/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=40) as response:
        if response.status != 200:
            raise RuntimeError(f"search metadata HTTP {response.status}")
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("oversized search response")
    return raw


def inspect_published(*, get=bounded_get) -> dict:
    """Return only after pointer, manifest and both actual consumers agree."""
    raw_pointer = get(BUCKET_RESOLVE + "published/current.json", MAX_JSON)
    pointer = decode_pointer(raw_pointer)
    generation = pointer["desired"]
    raw_manifest = get(BUCKET_RESOLVE + f"generations/{generation}/manifest.json", MAX_JSON)
    manifest = json.loads(raw_manifest)
    if (not isinstance(manifest, dict) or manifest.get("generation") != generation
            or manifest.get("version") != 1 or manifest.get("kind") != "search-snapshot-local"
            or manifest.get("object_layout") != "objects/sha256/<first-two>/<sha256>"
            or type(manifest.get("record_count")) is not int or manifest["record_count"] < 1):
        raise ValueError("published search manifest does not match desired generation")
    excluded = {"generation", "remote_writes", "publication_ready", "object_layout", "objects", "additional_checks"}
    import hashlib
    if hashlib.sha256(search_snapshot.canonical({k: v for k, v in manifest.items() if k not in excluded})).hexdigest() != generation:
        raise ValueError("published search manifest identity mismatch")
    observed = observe_consumers(generation, manifest, get=get)
    if get(BUCKET_RESOLVE + "published/current.json", MAX_JSON) != raw_pointer:
        raise ValueError("published pointer changed during consumer verification")
    return {"generation": generation, "source_revisions": manifest.get("source_revisions"),
            "record_count": manifest.get("record_count"), "space_api_generation": observed["space_api_generation"],
            "pages_corpus_sha256": observed["pages_corpus_sha256"],
            "transition": pointer["transition"], "both_consumers_observed": True}


def verify_unchanged(previous: dict, current: dict) -> None:
    """Check the transition, not only the potentially identical source revisions."""
    if previous != current:
        raise ValueError("published pointer or consumer receipt changed during the run")


def stage(source: Path, tree: Path, browser: Path, revisions: Path, output: Path, store) -> dict:
    report = build_generation(source, tree, browser, revisions, output)
    result = stage_generation(output, store)
    return {**result, "record_count": report["record_count"], "staged": True,
            "consumers_verified": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--baseline-output", type=Path)
    inspect.add_argument("--receipt-output", type=Path)
    inspect.add_argument("--expected-receipt", type=Path)
    build = sub.add_parser("stage")
    for name in ("source", "tree", "browser", "revisions", "output"):
        build.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "inspect":
        result = inspect_published()
        if args.expected_receipt is not None:
            verify_unchanged(json.loads(args.expected_receipt.read_bytes()), result)
        if args.baseline_output is not None:
            with args.baseline_output.open("x", encoding="utf-8") as output:
                json.dump(result["source_revisions"], output, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        if args.receipt_output is not None:
            with args.receipt_output.open("x", encoding="utf-8") as output:
                json.dump(result, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
