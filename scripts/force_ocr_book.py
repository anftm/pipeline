#!/usr/bin/env python3
"""Build only the two approved PS OCR repairs; publish under reader-sidecar.

Preparation uploads protected immutable objects but never changes a registry or
Reader pointer. Publication verifies the artifact and the original OCR identity.
Reader candidate building and explicit promotion use reader-v3-publish.yml, not
this producer. No correction model is called.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

try:
    from . import pdf_ocr, pdf_ocr_stages as stages, plan_pdf_ocr
    from . import publish_pdf_ocr_assets as publication, shared
except ImportError:
    import pdf_ocr
    import pdf_ocr_stages as stages
    import plan_pdf_ocr
    import publish_pdf_ocr_assets as publication
    import shared


REPO = "VoiceOfML/MLMRL-Library"
PREFIX = "安全培训/维基备份/软件/综合性资料/编程随想的博客/IT（信息技术）/IT/软件开发/编程语言/Lisp/"
APPROVED = {
    REPO + "\0" + PREFIX + "Paul Graham：Lisp 之根源@2003.ps": (
        "f30bd0621bc6f78a0b3c6ea588b749bc5e15d0a60ce95912305a1a667929c7f8", 11, "ch"),
    REPO + "\0" + PREFIX + "Guy Steele：Common Lisp——The Language@1989 (第2版).ps": (
        "a2caeac5f8ee2e07e7b0985cf4cc81b30cbcce7ca161499ed5743fbfc5d1b584", 1096, "en"),
}
ASSETS_REPO = "vomebook/Reader-Assets"
PROTOCOL = "explicit-ps-ocr-v1"
CHECKPOINT_PAGES = 25
READER_MANIFEST_BYTES = 8 * 1024 * 1024


def approve(key, digest, language="auto"):
    expected = APPROVED.get(key)
    if expected is None or expected[0] != digest:
        raise ValueError("source key/SHA is outside the two explicitly approved repairs")
    if language not in {"auto", expected[2]}:
        raise ValueError("recognition language differs from the approved page language")
    return expected


def identity(entry):
    return {k: entry.get(k) for k in (
        "key", "source_sha256", "source_revision", "source_bytes", "page_count",
        "ocr_manifest", "ocr_manifest_sha256", "ocr_manifest_bytes", "text_layer")}


def load_entry(api, key):
    entry = stages.load_registry(api, ASSETS_REPO, publication.OCR_MANIFEST_NAME)["files"].get(key)
    if not isinstance(entry, dict) or entry.get("key") != key:
        raise ValueError("approved source is missing from the OCR registry")
    digest, pages, language = approve(key, entry.get("source_sha256"))
    if entry.get("page_count") != pages or entry.get("status") != "ready":
        raise ValueError("approved source is not a complete ready OCR entry")
    if entry.get("text_revision") != "raw" or entry.get("text_quality") != "unreviewed":
        raise ValueError("forced raw OCR cannot replace reviewed/effective text")
    return entry


def render_item(entry, probe, presentation, language):
    """Keep the real probe; the renderer's explicit keyword forces recognition."""
    digest, pages, expected_language = approve(entry["key"], entry["source_sha256"], language)
    if probe["page_count"] != pages or entry["page_count"] != pages:
        raise ValueError("approved page count changed")
    if presentation.get("strategy") != "preserve-pdf":
        raise ValueError("approved repairs must preserve the original PDF presentation")
    fields = ("key", "repo", "path", "source_kind", "source_revision", "source_sha256",
              "source_bytes", "source_extension", "extension", "routing_source_bytes",
              "reader_assets_bucket", "reader_assets_path", "reader_assets_repo",
              "reader_assets_revision", "source_url", "worker_lane")
    return {**{k: entry[k] for k in fields if k in entry}, "probe": probe,
            "reader_presentation": presentation, "ocr_language": expected_language,
            "ocr_backend": "rapidocr_onnxruntime", "force_ocr_approval": PROTOCOL}


def write_report(output, report):
    path = output / "result.json"
    pending = path.with_suffix(".pending.json")
    pdf_ocr.write_json(pending, report)
    pending.replace(path)


def prepare(api, key, source, output, *, apply=False):
    entry = load_entry(api, key)
    digest, size = shared.hash_file(source)
    if (digest, size) != (entry["source_sha256"], entry["source_bytes"]):
        raise ValueError("source PDF differs from the pinned registry identity")
    probe = pdf_ocr.probe_pdf(source)
    item = render_item(entry, probe, pdf_ocr.reader_presentation(source), APPROVED[key][2])
    report = {"version": 1, "kind": PROTOCOL, "complete": False,
              "source": identity(entry), "pages": entry["page_count"],
              "ocr_language": APPROVED[key][2], "processing_roots": [], "results": []}
    output.mkdir(parents=True, exist_ok=True)
    if not apply:
        write_report(output, report)
        return report
    roots = report["processing_roots"]
    with tempfile.TemporaryDirectory(dir=output) as directory:
        bundle = Path(directory)
        rendered = stages.render_book(item, source, bundle, force_ocr_native=True)
        roots.extend(stages.upload_objects(bundle))
        manifest = json.loads(stages.read_object(rendered["render_manifest"], "/render-manifest.json"))
        stages.validate_render(rendered, manifest)
    # Deliberately do not publish-render or touch a shared progress registry.
    queue = stages.plan_images({key: rendered}, {}, {}, limit=1, target=CHECKPOINT_PAGES)
    if (len(queue["books"]) != 1 or queue["total_ocr_pages"] != entry["page_count"]):
        raise ValueError("forced OCR must cover every approved page")
    report["queue"] = queue
    write_report(output, report)
    for shard in queue["shards"]:
        for task in shard:
            with tempfile.TemporaryDirectory(dir=output) as directory:
                bundle = Path(directory)
                result = stages.recognize_task(task, bundle)
                result["processing_roots"] = stages.upload_objects(bundle)
            roots.extend(result["processing_roots"])
            report["results"].append(result)
            write_report(output, report)
            if result.get("errors"):
                raise ValueError("recognition failed; protected checkpoints retained in the artifact")
    updates = stages.collect_progress(queue, report["results"])
    with tempfile.TemporaryDirectory(dir=output) as directory:
        bundle = Path(directory)
        result = stages.assemble_book(queue["books"][0], updates[key]["pages"], bundle)
        if result["status"] != "ready":
            raise ValueError("refusing an incomplete OCR result")
        roots.extend(stages.upload_objects(bundle))
    result["processing_roots"] = list(roots)
    publication.verify_ready_objects(result)
    report.update(complete=True, entry=result)
    write_report(output, report)
    return report


def assert_publisher():
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY") != "anftm/pipeline"
            or os.environ.get("GITHUB_WORKFLOW_REF") !=
            "anftm/pipeline/.github/workflows/force-ocr-book.yml@refs/heads/main"
            or not os.environ.get("GITHUB_RUN_ID")
            or os.environ.get("FORCE_OCR_PUBLICATION_PROTOCOL") != PROTOCOL):
        raise RuntimeError("forced OCR registry writes require the serialized central publish job")


def handoff_owner(result):
    """Prove render/recognition roots against the verified final page closure.

    The compact registry entry directly references only the assembled text and
    render manifest. Recognition objects are referenced *inside* the final OCR
    manifest, so passing the compact entry to the generic handoff validator
    incorrectly fails after publication. Expand only for this ownership check;
    never duplicate all page text in the registry or relax the shared validator.
    """
    meta = {"path": result["ocr_manifest"], "sha256": result["ocr_manifest_sha256"],
            "bytes": result["ocr_manifest_bytes"]}
    manifest = json.loads(stages.read_object(meta, "/ocr-manifest.json"))
    pages = manifest.get("pages", [])
    if (manifest.get("kind") != "pdf-ocr" or manifest.get("complete") is not True
            or manifest.get("source_sha256") != result["source_sha256"]
            or manifest.get("page_count") != result["page_count"]
            or manifest.get("text_layer") != result["text_layer"]
            or [p.get("p") for p in pages] != list(range(1, result["page_count"] + 1))
            or any(p.get("source") != "ocr" for p in pages)):
        raise ValueError("processing handoff requires the complete approved OCR page closure")
    owner = {**result, "pages": pages}
    # Read-only preflight before publication: a foreign root must never cause a
    # late failure after updating the registry/catalog.
    publication.reader_lifecycle.processing_handoffs(
        [owner], "ownership-preflight", lambda path: publication.read_bucket_json(path, os.environ.get("HF_TOKEN")))
    return owner


def compact_reader_manifest(result, bundle, *, limit=READER_MANIFEST_BYTES):
    """Keep every page/reference while removing duplicate inline OCR payloads.

    Reader loads the complete text from the existing checked text bundle and
    page objects. Large legacy manifests duplicate that text/spans/layout and
    exceed Reader's unchanged manifest budget. Retain the original manifest as
    qualified provenance too; never truncate the page sequence or source text.
    """
    if result["ocr_manifest_bytes"] <= limit:
        return dict(result)
    original = {"bucket": shared.PDF_PAGES_BUCKET, "path": result["ocr_manifest"],
                "sha256": result["ocr_manifest_sha256"], "bytes": result["ocr_manifest_bytes"],
                "role": "provenance"}
    manifest = json.loads(stages.read_object(original, "/ocr-manifest.json"))
    if (not manifest.get("text_layer") or manifest["text_layer"] != result["text_layer"]
            or manifest.get("complete") is not True
            or manifest.get("source_sha256") != result["source_sha256"]
            or manifest.get("page_count") != result["page_count"]
            or [p.get("p") for p in manifest.get("pages", [])] != list(range(1, result["page_count"] + 1))):
        raise ValueError("compaction requires a complete independent text bundle")
    pages = [{k: value for k, value in page.items() if k not in {"text", "text_spans", "layout"}}
             for page in manifest["pages"]]
    compact = {**manifest, "pages": pages, "resources": [*manifest.get("resources", []), original]}
    root = stages.root_for(result["source_sha256"], result["key"],
                           original["sha256"] + "|reader-compact-ocr-manifest-v1")
    target = bundle / root / "ocr-manifest.json"
    pdf_ocr.write_json(target, compact)
    meta = stages.metadata(target, bundle)
    if meta["bytes"] > limit:
        raise ValueError("complete reference-only manifest still exceeds Reader budget")
    return {**result, "ocr_manifest": meta["path"], "ocr_manifest_sha256": meta["sha256"],
            "ocr_manifest_bytes": meta["bytes"]}


def publish(api, key, report, *, apply=False):
    current = load_entry(api, key)
    if (report.get("version") != 1 or report.get("kind") != PROTOCOL
            or report.get("complete") is not True):
        raise ValueError("a complete prepared OCR artifact is required")
    source, result = report["source"], report["entry"]
    digest, pages, language = approve(key, source.get("source_sha256"))
    if (source.get("key") != key or result.get("key") != key
            or result.get("source_sha256") != digest or result.get("page_count") != pages
            or result.get("ocr_pages") != pages or result.get("language") != language
            or result.get("force_ocr_approval") != PROTOCOL
            or result.get("status") != "ready" or result.get("text_revision") != "raw"
            or result.get("text_quality") != "unreviewed"
            or result.get("image_rendered") or result.get("page_manifest")
            or not pdf_ocr.preserves_reader_pdf(result)):
        raise ValueError("prepared OCR result differs from its explicit approval")
    original = result
    with tempfile.TemporaryDirectory(prefix="reader-ocr-compact-") as directory:
        bundle = Path(directory)
        result = compact_reader_manifest(original, bundle)
        # The exact prepared full manifest may have committed before a handoff
        # failure. Accept that identity or this deterministic compact result,
        # never an unrelated descendant or reviewed generation.
        already = current.get("ocr_manifest_sha256") == result.get("ocr_manifest_sha256")
        prepared = current.get("ocr_manifest_sha256") == original.get("ocr_manifest_sha256")
        if not already and not prepared and identity(current) != source:
            raise ValueError("OCR entry changed during preparation; refusing to overwrite it")
        if apply:
            assert_publisher()
            extra_roots = stages.upload_objects(bundle) if result != original else []
            result = {**result, "processing_roots": [*result.get("processing_roots", []), *extra_roots]}
            owner = handoff_owner(result)
            # Only checked page/text refs go to the compact registry entry;
            # expanded page ownership is used solely for safe root handoff.
            publication.publish(api, ASSETS_REPO, [{**result, "processing_roots": []}])
            stages.handoff_worker_results(api, ASSETS_REPO, [owner])
    return {"applied": apply, "unchanged": already, "source_key": key,
            "text_layer": result["text_layer"], "page_count": pages,
            "ocr_manifest": result["ocr_manifest"], "manifest_bytes": result["ocr_manifest_bytes"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", default=REPO)
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--ocr-language", choices=("auto", "ch", "en"), default="auto")
    parser.add_argument("--mode", choices=("prepare", "publish"), default="prepare")
    parser.add_argument("--output", type=Path, default=Path("output/force-ocr"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    key = args.source_repo + "\0" + args.source_path
    approve(key, args.expected_sha256, args.ocr_language)
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    if args.mode == "prepare":
        entry = load_entry(api, key)
        report = prepare(api, key, plan_pdf_ocr.download_source(entry), args.output, apply=args.apply)
        summary = {k: report[k] for k in ("kind", "complete", "pages", "ocr_language")}
    else:
        report = json.loads((args.output / "result.json").read_text(encoding="utf-8"))
        summary = publish(api, key, report, apply=args.apply)
        pdf_ocr.write_json(args.output / "publication.json", summary)
    print(json.dumps(summary, ensure_ascii=True, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
