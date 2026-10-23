#!/usr/bin/env python3
"""Reindex exact pinned chapter bundles without reconverting or changing bodies."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
from pathlib import Path

try:
    from . import epub_chapters
    from .reader_assets import canonical_json, validate_chapter_manifest
    from .reader_bucket_store import S3BucketStore
    from .reader_stream_index import publish_checkpoint, read_index
except ImportError:
    import epub_chapters
    from reader_assets import canonical_json, validate_chapter_manifest
    from reader_bucket_store import S3BucketStore
    from reader_stream_index import publish_checkpoint, read_index


BUCKET = "vomebook/reader-assets-v2"
KIND = "ebook-chapter-stream-index"
PROFILE = epub_chapters.SEARCH_TEXT_PROFILE
MAX_FILES = 100_000
MAX_BYTES = 500 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def targets(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError("repair requires 1-32 exact pinned source selections")
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"key", "source_sha256", "manifest", "manifest_sha256"}:
            raise ValueError("repair selection must pin source and manifest SHA")
        key = item["key"]
        if not isinstance(key, str) or key.count("\0") != 1 or key in seen:
            raise ValueError("invalid or duplicate repair source key")
        repo, path = key.split("\0")
        if (not re.fullmatch(r"VoiceOfML/[A-Za-z0-9_.-]+", repo) or not path
                or path.startswith("/") or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))):
            raise ValueError("invalid repair source path")
        match = re.fullmatch(r"chapters/ebook/(epub|mobi|azw3|fb2|chm)/([0-9a-f]{64})/[0-9a-f]{16}/chapter-manifest.json", str(item["manifest"]))
        if (not match or match[2] != item["source_sha256"]
                or not re.fullmatch("[0-9a-f]{64}", str(item["manifest_sha256"]))):
            raise ValueError("invalid pinned chapter manifest")
        seen.add(key)
    return value


def assert_writer():
    if (os.environ.get("GITHUB_REPOSITORY") != "anftm/pipeline"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_WORKFLOW_REF") != "anftm/pipeline/.github/workflows/reader-chapter-search-repair.yml@refs/heads/main"):
        raise ValueError("chapter search repair apply requires its serialized central workflow")


def verify_search(raw, manifest):
    ref = manifest["search_index"]
    if len(raw) != ref["bytes"] or digest(raw) != ref["sha256"]:
        raise ValueError("existing search index checksum mismatch")
    # The compressed/expanded Reader limits apply to repair too.
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        expanded = stream.read(256 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024 or len(expanded) > 256 * 1024 * 1024:
        raise ValueError("search index exceeds Reader bounds")
    data = json.loads(expanded)
    if (data.get("version") not in {1, 2} or data.get("kind") != "epub-search-index"
            or data.get("text_profile") != ref.get("text_profile")
            or [(c.get("index"), c.get("path")) for c in data.get("chapters", [])]
            != [(c["index"], c["path"]) for c in manifest["chapters"]]
            or any(not isinstance(c.get("text"), str) for c in data["chapters"])):
        raise ValueError("existing complete search identity/profile mismatch")
    return data


def rebuild(entry, selection, store, work):
    """Read every old object, then replace only search index/manifest locally."""
    if (entry.get("source_sha256") != selection["source_sha256"]
            or entry.get("manifest") != selection["manifest"]
            or entry.get("bucket") != BUCKET or entry.get("mode") != "chapter-stream"):
        raise ValueError("current source mapping differs from pinned repair selection")
    root = entry.get("root")
    if root != entry["manifest"].rsplit("/", 1)[0]:
        raise ValueError("chapter root differs from manifest")
    manifest_raw = store.read_bytes(BUCKET, selection["manifest"])
    if digest(manifest_raw) != selection["manifest_sha256"]:
        raise ValueError("pinned repair manifest checksum mismatch")
    manifest = validate_chapter_manifest(json.loads(manifest_raw))
    if len(manifest["chapters"]) != entry.get("chapter_count"):
        raise ValueError("canonical chapter count mismatch")
    paths = store.list_files(BUCKET, (root + "/",))
    if not paths or len(paths) > MAX_FILES:
        raise ValueError("chapter inventory missing or exceeds repair bounds")
    bundle = work / "bundle"
    bundle.mkdir(parents=True)
    total, originals = 0, {}
    for remote in sorted(paths):
        if not remote.startswith(root + "/"):
            raise ValueError("inventory escaped exact chapter root")
        relative = remote[len(root) + 1:]
        if "\\" in relative or any(p in {"", ".", ".."} for p in relative.split("/")):
            raise ValueError("unsafe chapter resource path")
        payload = store.read_bytes(BUCKET, remote)
        total += len(payload)
        if total > MAX_BYTES:
            raise ValueError("chapter repair exceeds complete bundle byte limit")
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        originals[relative] = digest(payload)
    if (bundle / "chapter-manifest.json").read_bytes() != manifest_raw:
        raise ValueError("chapter manifest changed during exact-root read")
    if epub_chapters.bundle_version(bundle) != root.rsplit("/", 1)[1]:
        raise ValueError("old chapter bundle content identity mismatch")
    old_search = verify_search((bundle / "epub-search-index.json.gz").read_bytes(), manifest)
    ref = epub_chapters.write_search_index(bundle, manifest["chapters"])
    new_search = json.loads(gzip.decompress((bundle / ref["path"]).read_bytes()))
    updated_manifest = {**manifest, "search_index": ref}
    validate_chapter_manifest(updated_manifest)
    (bundle / "chapter-manifest.json").write_bytes(canonical_json(updated_manifest, pretty=True))
    altered = [p for p, old in originals.items() if digest((bundle / p).read_bytes()) != old]
    if any(p not in {"chapter-manifest.json", "epub-search-index.json.gz"} for p in altered):
        raise ValueError("reindex changed chapter/resource bytes")
    new_root = root.rsplit("/", 1)[0] + "/" + epub_chapters.bundle_version(bundle)
    if new_root == root and "text_profile" not in manifest["search_index"]:
        raise ValueError("legacy search repair failed to create immutable identity")
    changed_chapters = [new["index"] for old, new in zip(old_search["chapters"], new_search["chapters"])
                        if old["text"] != new["text"]]
    updated = {**entry, "root": new_root, "manifest": new_root + "/chapter-manifest.json",
               "search_index": new_root + "/epub-search-index.json.gz", "search_text_profile": PROFILE}
    # Old released processing records belong to old roots; the checkpoint creates
    # a new qualified protection owner. Old canonical history/objects stay intact.
    updated.pop("processing_roots", None)
    updated["search_repair"] = {"from_manifest": selection["manifest"],
        "from_manifest_sha256": selection["manifest_sha256"], "text_profile": PROFILE}
    if entry.get("fallback"):
        if not re.fullmatch(r"native/ebook/(epub|mobi|azw3|fb2|chm)/[0-9a-f]{64}/document.epub", entry["fallback"]):
            raise ValueError("unexpected native fallback identity")
        payload = store.read_bytes(BUCKET, entry["fallback"])
        if digest(payload) != entry["fallback"].split("/")[-2]:
            raise ValueError("preserved fallback checksum mismatch")
    uploads = {new_root + "/" + p: str(bundle / p) for p in originals}
    return updated, uploads, {"key": entry["key"], "old_manifest": selection["manifest"],
        "new_manifest": updated["manifest"], "new_manifest_sha256": digest((bundle / "chapter-manifest.json").read_bytes()),
        "changed_text_chapters": changed_chapters, "chapter_and_resource_bytes_unchanged": True,
        "fallback_unchanged": entry.get("fallback"), "files": len(originals), "old_bundle_bytes": total}


def run(selections, store, work, *, apply=False):
    selections = targets(selections)
    if apply:
        assert_writer()
    report = {"kind": "chapter-search-repair", "apply": apply, "text_profile": PROFILE, "files": []}
    indexes = {}
    for i, selection in enumerate(selections):
        extension = selection["manifest"].split("/")[2]
        index_path = f"chapters/ebook/{extension}/index.json"
        if index_path not in indexes:
            indexes[index_path] = json.loads(store.read_bytes(BUCKET, index_path))
        previous = indexes[index_path]
        entry = next((e for e in previous["files"] if e["key"] == selection["key"]), None)
        if entry is None:
            raise ValueError("selected source has no canonical chapter mapping")
        marker = entry.get("search_repair", {})
        if (entry.get("search_text_profile") == PROFILE and marker.get("from_manifest") == selection["manifest"]
                and marker.get("from_manifest_sha256") == selection["manifest_sha256"]):
            manifest = validate_chapter_manifest(json.loads(store.read_bytes(BUCKET, entry["manifest"])))
            verify_search(store.read_bytes(BUCKET, entry["search_index"]), manifest)
            if manifest["search_index"].get("text_profile") != PROFILE:
                raise ValueError("repaired checkpoint lacks exact DOM text profile")
            report["files"].append({"key": entry["key"], "already_repaired": True, "manifest": entry["manifest"]})
            continue
        folder = work / str(i)
        updated, uploads, receipt = rebuild(entry, selection, store, folder)
        if apply:
            # Re-read the current canonical just before committing each selected
            # entry. Real writer serialization plus exact original selection
            # prevents rolling back other sources/checkpoints.
            current = read_index(BUCKET, index_path, os.environ.get("HF_TOKEN"), KIND)
            if current != previous:
                raise ValueError("canonical changed before repair checkpoint")
            checkpoint = folder / "checkpoint";checkpoint.mkdir()
            indexes[index_path] = publish_checkpoint(BUCKET, index_path, previous, [updated], [], uploads,
                checkpoint, os.environ.get("HF_TOKEN"), apply=True, kind=KIND)
        report["files"].append(receipt)
        print(json.dumps(receipt, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    store = S3BucketStore();store.list_workers = 4
    args.work.mkdir(parents=True, exist_ok=False)
    result = run(json.loads(args.selection.read_text()), store, args.work, apply=args.apply)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes(canonical_json(result, pretty=True))


if __name__ == "__main__":
    main()
