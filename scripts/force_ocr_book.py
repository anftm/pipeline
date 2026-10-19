#!/usr/bin/env python3
"""Explicit, single-book OCR repair for a preserved native-text PDF.

This tool is intentionally not part of automatic planning.  It accepts one
exact source identity, treats every page as a scan for this run only, renders
the OCR inputs and publishes an ordinary OCR manifest/text layer through the
existing PDF OCR contracts.  The Reader presentation is preserved: no page
stream is created for a ``preserve-pdf`` source.

Usage:
    python scripts/force_ocr_book.py --source-path PATH --expected-sha256 SHA \
        --ocr-language ch --layout-rotation 0 --mode render-ocr --apply
    python scripts/force_ocr_book.py --source-path PATH --expected-sha256 SHA \
        --mode promote --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

try:
    from . import (
        ocr_layout,
        pdf_ocr,
        pdf_ocr_stages as stages,
        plan_pdf_ocr,
        publish_pdf_ocr_assets as publication,
        shared,
    )
except ImportError:
    import ocr_layout
    import pdf_ocr
    import pdf_ocr_stages as stages
    import plan_pdf_ocr
    import publish_pdf_ocr_assets as publication
    import shared

from huggingface_hub import HfApi


def display_key(key: str) -> str:
    return key.replace("\0", "\\0")


def source_key(repo: str, path: str) -> str:
    if "\0" in repo or "\0" in path:
        raise ValueError("source repo/path must not contain NUL")
    return repo + "\0" + path


def load_entry(api: HfApi, assets_repo: str, key: str) -> dict:
    registry = stages.load_registry(api, assets_repo, publication.OCR_MANIFEST_NAME)
    entry = registry["files"].get(key)
    if not isinstance(entry, dict):
        raise SystemExit(f"OCR registry has no entry for {display_key(key)}")
    return entry


def render_item(entry: dict, probe: dict, presentation: dict, language: str,
                layout_rotation: int) -> dict:
    page_count = probe["page_count"]
    if page_count != entry.get("page_count"):
        raise ValueError("probe page count changed after publication")
    item = {k: v for k, v in entry.items()
            if k not in {
                "ocr_manifest", "ocr_manifest_sha256", "ocr_manifest_bytes",
                "text_layer", "text_revision", "text_quality",
                "render_manifest", "page_manifest", "processing_roots",
                "stream", "image_rendered", "ocr_pages", "status",
                "profile", "render_profile", "probe", "page_chars",
            }}
    # A non-zero count below MIN_NATIVE_PAGE_CHARS keeps the page out of the
    # native branch while avoiding the embedded-bitonal-image fast path.  The
    # page is rendered as a full page and recognized from that render.
    item.update({
        "probe": {"classification": "scan", "page_count": page_count,
                  "page_chars": [1] * page_count},
        "reader_presentation": presentation,
        "page_count": page_count,
        "status": "planned",
        "profile": pdf_ocr.asset_profile(),
        "render_profile": stages.render_profile(),
        "ocr_language": language,
        "layout_options": {"default": {"rotation": layout_rotation}, "pages": {}},
    })
    return item


def render(api: HfApi, assets_repo: str, key: str, entry: dict, source: Path,
           output: Path, language: str, layout_rotation: int) -> dict:
    probe = pdf_ocr.probe_pdf(source)
    presentation = pdf_ocr.reader_presentation(source)
    item = render_item(entry, probe, presentation, language, layout_rotation)
    bundle = output / "render"
    if bundle.exists():
        raise SystemExit(f"refusing to reuse render workspace: {bundle}")
    bundle.mkdir(parents=True)
    print(json.dumps({
        "event": "render-plan", "key": display_key(key),
        "page_count": probe["page_count"], "classification": probe["classification"],
        "reader_presentation": presentation["strategy"],
        "ocr_language": language, "layout_rotation": layout_rotation,
    }, ensure_ascii=False, sort_keys=True), flush=True)
    result = stages.render_book(item, source, bundle)
    result["processing_roots"] = stages.upload_objects(bundle)
    manifest = json.loads(stages.read_object(result["render_manifest"], "/render-manifest.json"))
    stages.validate_render(result, manifest)
    stages.save_registry(api, assets_repo, stages.RENDER_REGISTRY, {key: result},
                         publish_streams=True)
    print(json.dumps({
        "event": "render-ready", "key": display_key(key),
        "render_profile": result["render_profile"],
        "ocr_pages": result["ocr_pages"], "image_rendered": result["image_rendered"],
        "render_manifest": result["render_manifest"]["path"],
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return result


def recognize(api: HfApi, assets_repo: str, key: str, rendered: dict, output: Path,
              layout_rotation: int) -> tuple[dict, list[dict], dict]:
    current = stages.load_registry(api, assets_repo, publication.OCR_MANIFEST_NAME)["files"]
    progress = stages.load_ocr_progress(api, assets_repo, "reader-assets-v2")
    overrides = {key: {"default": {"rotation": layout_rotation}, "pages": {}}}
    queue = stages.plan_images({key: rendered}, current, progress, limit=1,
                               target=plan_pdf_ocr.ocr_target_pages_per_shard(),
                               overrides=overrides, retry_failed_only=False)
    if queue["shard_count"] < 1:
        raise SystemExit("force OCR plan produced no recognition tasks")
    queue_path = output / "ocr-queue.json"
    stages.pdf_ocr.write_json(queue_path, queue)
    print(json.dumps({
        "event": "ocr-plan", "key": display_key(key),
        "shards": queue["shard_count"], "pages": queue["total_ocr_pages"],
        "profile": queue["books"][0].get("profile"),
    }, ensure_ascii=False, sort_keys=True), flush=True)
    bundle = output / "ocr"
    bundle.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    updates: dict = {}
    for shard in queue["shard_ids"]:
        for task in queue["shards"][shard]["records"]:
            result = stages.recognize_task(task, bundle)
            result["processing_roots"] = stages.upload_objects(bundle)
            results.append(result)
            updates = stages.collect_progress(queue, results)
            stages.save_ocr_progress(api, assets_repo, updates)
            errors = [error for item in results for error in item.get("errors", [])]
            if errors:
                stages.pdf_ocr.write_json(output / "ocr-results.json", {
                    "version": 1, "kind": "forced-ocr-results",
                    "complete": False, "results": results})
                print(json.dumps({"event": "ocr-errors", "errors": errors[:20],
                                  "error_count": len(errors)}, ensure_ascii=False), flush=True)
                raise SystemExit("OCR failed; uploaded artifacts retained for retry")
            print(json.dumps({
                "event": "ocr-task", "key": display_key(key),
                "pages": [page["p"] for page in result["pages"]][:1] +
                         (["...", result["pages"][-1]["p"]] if len(result["pages"]) > 1 else []),
                "recognized": len(result["pages"]),
            }, ensure_ascii=False, sort_keys=True), flush=True)
    stages.pdf_ocr.write_json(output / "ocr-results.json", {
        "version": 1, "kind": "forced-ocr-results", "complete": True, "results": results})
    return queue, results, updates


def publish_ocr(api: HfApi, assets_repo: str, queue: dict, results: list[dict],
                updates: dict, output: Path) -> dict:
    publish_dir = output / "publish"
    publish_dir.mkdir(parents=True, exist_ok=True)
    stages.publish_ocr_batches(queue["books"], updates, api, assets_repo,
                               publish_dir, batch_size=1)
    stages.handoff_worker_results(api, assets_repo, results)
    entry = load_entry(api, assets_repo, queue["books"][0]["key"])
    print(json.dumps({
        "event": "ocr-published", "key": display_key(queue["books"][0]["key"]),
        "status": entry.get("status"), "ocr_pages": entry.get("ocr_pages"),
        "ocr_manifest": entry.get("ocr_manifest"),
        "text_layer": (entry.get("text_layer") or {}).get("path"),
        "language": entry.get("language"), "profile": entry.get("profile"),
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return entry


def promote(api: HfApi, assets_repo: str, key: str, expected_parent: str | None,
            output: Path) -> dict:
    try:
        from . import publish_reader_v3 as v3publish
        from .reader_v3_automation import spec_for
    except ImportError:
        import publish_reader_v3 as v3publish
        from reader_v3_automation import spec_for
    entry = load_entry(api, assets_repo, key)
    if entry.get("status") != "ready":
        raise SystemExit("OCR entry is not ready; publish OCR before promotion")
    spec, page_count = spec_for(key, entry)
    store = v3publish.CentralHubStore()
    with tempfile.TemporaryDirectory(prefix="forced-ocr-promote-") as directory:
        report = v3publish.build_stage(store, spec, Path(directory), apply=True)
    candidate = report["candidate"]
    pointer, _ = v3publish.current(store)
    parent = pointer["generation"] if pointer else None
    if expected_parent and expected_parent != "none" and expected_parent != parent:
        raise SystemExit(f"stale expected parent: current {parent} != {expected_parent}")
    result = v3publish.promote(store, candidate, parent, apply=True)
    print(json.dumps({
        "event": "reader-promoted", "key": display_key(key),
        "page_count": page_count, "candidate": candidate,
        "promotion": result,
    }, ensure_ascii=False, sort_keys=True), flush=True)
    stages.pdf_ocr.write_json(output / "promotion.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", default="VoiceOfML/MLMRL-Library")
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--assets-repo", default="vomebook/Reader-Assets")
    parser.add_argument("--ocr-language", choices=("auto", "ch", "en"), default="auto")
    parser.add_argument("--layout-rotation", type=int, choices=(0, 90, 180, 270), default=0)
    parser.add_argument("--mode", choices=("render-ocr", "promote"), default="render-ocr")
    parser.add_argument("--expected-parent", default="none")
    parser.add_argument("--output", type=Path, default=Path("output/force-ocr"))
    parser.add_argument("--local-pdf", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    key = source_key(args.source_repo, args.source_path)
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    entry = load_entry(api, args.assets_repo, key)
    if entry.get("source_sha256") != args.expected_sha256:
        raise SystemExit("OCR registry source checksum does not match --expected-sha256")
    if args.expected_sha256 != entry.get("source_sha256"):
        raise SystemExit("source checksum mismatch")

    if args.mode == "promote":
        if not args.apply:
            print(json.dumps({"event": "promote-plan", "key": display_key(key),
                              "status": entry.get("status")}, ensure_ascii=False), flush=True)
            return 0
        args.output.mkdir(parents=True, exist_ok=True)
        promote(api, args.assets_repo, key, args.expected_parent, args.output)
        return 0

    source = args.local_pdf or plan_pdf_ocr.download_source(entry)
    source = Path(source)
    digest, size = shared.hash_file(source)
    if digest != args.expected_sha256 or size != entry.get("source_bytes"):
        raise SystemExit("source PDF identity mismatch")
    language = entry.get("ocr_language") if args.ocr_language == "auto" else args.ocr_language
    if not language:
        language = pdf_ocr.detect_language(key)
    if language not in {"ch", "en"}:
        raise SystemExit(f"forced OCR only supports ch/en, got {language!r}")
    # plan_images consults this module constant, not the registry entry.
    pdf_ocr.OCR_LANG = language

    if not args.apply:
        probe = pdf_ocr.probe_pdf(source)
        presentation = pdf_ocr.reader_presentation(source)
        item = render_item(entry, probe, presentation, language, args.layout_rotation)
        print(json.dumps({
            "event": "dry-run", "key": display_key(key),
            "page_count": probe["page_count"],
            "classification": probe["classification"],
            "reader_presentation": presentation["strategy"],
            "ocr_language": language,
            "layout_rotation": args.layout_rotation,
            "asset_profile": item["profile"],
        }, ensure_ascii=False, sort_keys=True), flush=True)
        return 0

    started = time.monotonic()
    args.output.mkdir(parents=True, exist_ok=True)
    rendered = render(api, args.assets_repo, key, entry, source, args.output,
                      language, args.layout_rotation)
    queue, results, updates = recognize(api, args.assets_repo, key, rendered,
                                        args.output, args.layout_rotation)
    published = publish_ocr(api, args.assets_repo, queue, results, updates, args.output)
    print(json.dumps({
        "event": "complete", "key": display_key(key),
        "seconds": round(time.monotonic() - started, 1),
        "text_revision": published.get("text_revision"),
        "text_quality": published.get("text_quality"),
    }, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
