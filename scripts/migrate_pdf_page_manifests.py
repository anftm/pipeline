#!/usr/bin/env python3
"""Migrate existing v1 PDF page manifests to the compact v2 shape.

Only the manifest object in the PDF pages bucket is replaced. Page images,
pdf_manifest.json, and the reader sidecar keep their existing paths.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from huggingface_hub import HfApi, HfFileSystem, hf_hub_download, sync_bucket

try:
    from . import pdf_assets
except ImportError:
    import pdf_assets


BUCKET = "vomebook/pdf-pages"


def load_pdf_manifest(api: HfApi, repo: str) -> dict:
    path = hf_hub_download(repo_id=repo, repo_type="dataset", filename=pdf_assets.MANIFEST_NAME,
                            token=os.environ.get("HF_TOKEN"))
    return json.loads(Path(path).read_text(encoding="utf-8"))


def v1_candidates(manifest: dict) -> list[tuple[str, dict]]:
    candidates = []
    for key, entry in sorted(manifest.get("files", {}).items()):
        if not isinstance(entry, dict) or entry.get("status") != "ready":
            continue
        page_manifest = entry.get("page_manifest")
        if not isinstance(page_manifest, dict):
            continue
        if page_manifest.get("version", 1) != 1:
            continue
        path = page_manifest.get("path")
        if isinstance(path, str) and path.endswith("/page-manifest.json"):
            candidates.append((key, entry))
    return candidates


def read_bucket_manifest(fs: HfFileSystem, path: str) -> dict:
    with fs.open(f"hf://buckets/{BUCKET}/{path}", "rb") as stream:
        return json.loads(stream.read().decode("utf-8"))


def migrate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or manifest.get("kind") != "pdf-pages":
        raise ValueError("expected v1 PDF page manifest")
    pages = manifest.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("v1 PDF page manifest has no pages")
    return pdf_assets.compact_page_manifest(
        str(manifest.get("source_sha256") or ""),
        str(manifest.get("profile") or ""),
        pages,
        manifest.get("toc"),
    )


def plan(manifest: dict, *, limit: int = 0, checkpoint: int = 0) -> list[tuple[str, dict]]:
    if limit < 0 or checkpoint < 0:
        raise ValueError("limit and checkpoint must be non-negative")
    candidates = v1_candidates(manifest)
    start = checkpoint * limit if limit else 0
    return candidates[start:start + limit if limit else None]


def migrate(api: HfApi, repo: str, *, limit: int = 0, checkpoint: int = 0,
            apply: bool = False) -> dict:
    manifest = load_pdf_manifest(api, repo)
    selected = plan(manifest, limit=limit, checkpoint=checkpoint)
    fs = HfFileSystem(token=os.environ.get("HF_TOKEN"))
    converted = []
    skipped = []
    with tempfile.TemporaryDirectory(prefix="pdf-manifest-v2-") as root:
        root_path = Path(root)
        include = []
        for key, entry in selected:
            path = entry["page_manifest"]["path"]
            try:
                old = read_bucket_manifest(fs, path)
                if old.get("version") == pdf_assets.PAGE_MANIFEST_VERSION:
                    converted.append({"key": key, "path": path,
                                      "page_count": old.get("page_count"),
                                      "already_v2": True})
                    continue
                new = migrate_manifest(old)
                target = root_path / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(new, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                                  encoding="utf-8")
                include.append(path)
                converted.append({"key": key, "path": path, "page_count": new["page_count"]})
            except Exception as exc:
                skipped.append({"key": key, "path": path, "error": f"{type(exc).__name__}: {exc}"})
        if apply and include:
            sync_bucket(str(root_path), f"hf://buckets/{BUCKET}", include=include,
                        token=os.environ.get("HF_TOKEN"), quiet=False)
    return {"selected": len(selected), "converted": converted, "skipped": skipped,
            "apply": apply}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", pdf_assets.READER_ASSETS_REPO))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--checkpoint", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("output/pdf-manifest-migration/report.json"))
    args = parser.parse_args()
    if args.apply and not os.environ.get("HF_TOKEN"):
        raise RuntimeError("HF_TOKEN is required for migration")
    report = migrate(HfApi(token=os.environ.get("HF_TOKEN")), args.assets_repo,
                     limit=args.limit, checkpoint=args.checkpoint, apply=args.apply)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                            encoding="utf-8")
    print(f"selected={report['selected']} converted={len(report['converted'])} skipped={len(report['skipped'])} apply={args.apply}")
    return 1 if report["skipped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
