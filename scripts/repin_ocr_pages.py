#!/usr/bin/env python3
"""Re-pin a ready OCR book whose page objects no longer match its manifest.

Before `upload_objects` refused overwrites, an unpublished re-recognition could
replace pages at a book's deterministic recognition root. The registry then
pinned digests that no bucket object has any more, so every v3 build failed
with a checksum mismatch. Recognition is not byte-reproducible, so re-running
OCR into the same root cannot repair it.

This tool performs no OCR. For each explicitly named key it:

- requires a ready registry entry whose render entry is ready for the same
  source/profile, and an unchanged complete OCR manifest;
- reads every OCR page object as it currently exists, requires the payload to be
  a `pdf-ocr-page` for exactly that page/source and size, and adopts its bytes;
- assembles a new manifest/book text/text layer under a distinct `repin` root
  and publishes through the normal OCR publisher, which reads every object back
  before the registry/catalog changes.

Use only under the central `reader-sidecar` lock (reader-ocr-repin.yml).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

try:
    from . import pdf_ocr_stages as stages, shared
except ImportError:
    import pdf_ocr_stages as stages
    import shared


def current_bytes(path: str) -> bytes:
    stages.pdf_ocr.validate_ocr_object_path(path, ".json.gz")

    def download():
        response = stages.get_session().get(
            f"https://huggingface.co/buckets/{shared.PDF_PAGES_BUCKET}/resolve/{quote(path, safe='/')}",
            headers={"Cache-Control": "no-cache"}, follow_redirects=True, timeout=120)
        stages.hf_raise_for_status(response)
        return response.content
    return stages.retry(download)


def adopt_pages(manifest: dict, read=current_bytes):
    """Return (saved pages keyed by number, number of adopted pages)."""
    saved, adopted = {}, 0
    for page in manifest["pages"]:
        if page.get("source") != "ocr":
            continue
        raw = read(page["o"])
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.loads(gzip.decompress(raw))
        if (payload.get("kind") != "pdf-ocr-page" or payload.get("page") != page["p"]
                or payload.get("source") != "ocr" or payload.get("width") != page.get("width")
                or payload.get("height") != page.get("height")
                or not isinstance(payload.get("text"), str) or not isinstance(payload.get("blocks"), list)):
            raise ValueError(f"page {page['p']} payload is not this page's OCR result")
        result = {**page, "chars": len(payload["text"]), "text": payload["text"],
                  "text_spans": payload.get("text_spans", []), "layout": payload.get("layout", {})}
        stages.set_page_meta(result, "o", {"path": page["o"], "sha256": digest, "bytes": len(raw)})
        adopted += digest != page["os"] or len(raw) != page["ob"]
        saved[str(page["p"])] = result
    return saved, adopted


def plan(api, repo, keys, read=current_bytes):
    ocr = stages.load_registry(api, repo, stages.publication.OCR_MANIFEST_NAME)["files"]
    rendered = stages.load_registry(api, repo, stages.RENDER_REGISTRY)["files"]
    books, report = [], []
    for key in keys:
        entry, render = ocr.get(key), rendered.get(key)
        if not entry or entry.get("status") != "ready":
            raise ValueError(f"{key!r}: OCR entry is not ready")
        if (not render or render.get("status") != "ready" or render.get("render_manifest") != entry.get("render_manifest")
                or render.get("source_sha256") != entry.get("source_sha256")):
            raise ValueError(f"{key!r}: render entry changed since OCR publication")
        manifest = json.loads(stages.read_object(
            {"path": entry["ocr_manifest"], "sha256": entry["ocr_manifest_sha256"],
             "bytes": entry["ocr_manifest_bytes"]}, "/ocr-manifest.json"))
        if (manifest.get("kind") != "pdf-ocr" or manifest.get("complete") is not True
                or manifest.get("profile") != entry.get("profile")
                or manifest.get("source_sha256") != entry["source_sha256"]
                or len(manifest.get("pages", [])) != entry.get("page_count")):
            raise ValueError(f"{key!r}: OCR manifest identity mismatch")
        render_manifest = stages.validate_render(render, json.loads(
            stages.read_object(render["render_manifest"], "/render-manifest.json")))
        if [p["p"] for p in render_manifest["pages"]] != [p["p"] for p in manifest["pages"]]:
            raise ValueError(f"{key!r}: render/OCR page sequence differs")
        saved, adopted = adopt_pages(manifest, read)
        if not adopted:
            report.append({"key": key, "adopted_pages": 0, "action": "already-consistent"})
            continue
        repin = hashlib.sha256("\0".join(saved[n]["os"] for n in sorted(saved, key=int)).encode()).hexdigest()[:16]
        book = {**{k: v for k, v in entry.items() if k not in {
                    "ocr_manifest", "ocr_manifest_sha256", "ocr_manifest_bytes", "text_layer", "processing_roots"}},
                "status": "ready", "render_manifest": render["render_manifest"],
                "page_manifest": render.get("page_manifest"),
                "layout_options": manifest.get("layout_options", entry.get("layout_options", {})),
                "ocr_language": manifest.get("language"), "ocr_backend": manifest.get("backend"),
                "pages": render_manifest["pages"], "saved": saved, "repin": repin}
        if stages.pdf_ocr.book_ocr_config(book)[0] != manifest.get("language"):
            raise ValueError(f"{key!r}: recognition language would change")
        books.append(book)
        report.append({"key": key, "adopted_pages": adopted, "pages": len(saved), "repin": repin,
                       "old_manifest": entry["ocr_manifest"], "action": "repin"})
    return books, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", action="append", required=True, help="exact repo\\0path key; repeatable")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("output/ocr-repin"))
    parser.add_argument("--assets-repo", default="vomebook/Reader-Assets")
    args = parser.parse_args()
    if args.apply and (os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("GITHUB_WORKFLOW_REF", "").endswith(
            "/.github/workflows/reader-ocr-repin.yml@refs/heads/main")):
        raise SystemExit("apply requires the central reader-ocr-repin.yml workflow")
    keys = [key.replace("\\0", "\0") for key in args.key]
    api = stages.HfApi(token=os.environ.get("HF_TOKEN"))
    books, report = plan(api, args.assets_repo, keys)
    args.output.mkdir(parents=True, exist_ok=True)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    if args.apply and books:
        updates = {book["key"]: {"generation": stages.generation_for(book), "pages": book["saved"]} for book in books}
        progress = stages.publish_ocr_batches(books, updates, api, args.assets_repo, args.output, batch_size=4)
        failed = [batch for batch in progress["batches"] if batch["failed"]]
        stages.save_ocr_progress(api, args.assets_repo, updates)
        report = {"books": report, "publication": progress}
        if failed:
            raise SystemExit(f"re-pin publication failed: {failed}")
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
