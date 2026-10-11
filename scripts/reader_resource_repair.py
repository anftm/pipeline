#!/usr/bin/env python3
"""Restore missing Reader image/text bundles under the central writer lock."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

try:
    from . import pdf_reading_v3 as v3, pdf_text_layer as text, pdf_ocr, ocr_layout
    from . import pilot_image_page_stream as images, reader_lifecycle as lifecycle
    from .reader_bucket_store import HubBucketStore, retry_read
    from .pdf_worker_lanes import load_config
except ImportError:
    import pdf_reading_v3 as v3
    import pdf_text_layer as text
    import pdf_ocr
    import ocr_layout
    import pilot_image_page_stream as images
    import reader_lifecycle as lifecycle
    from reader_bucket_store import HubBucketStore, retry_read
    from pdf_worker_lanes import load_config

ASSETS, PDF = lifecycle.READER_ASSETS_BUCKET, lifecycle.PDF_PAGES_BUCKET
SIDECAR = "reader-index/reader_assets.json.gz"
IMAGE_MANIFEST = re.compile(r"pages/image/(jpg|jpeg|png|bmp|webp|tif|tiff)/([0-9a-f]{64})/page-manifest\.json")
TEXT_MANIFEST = re.compile(r"objects/([0-9a-f]{2})/([0-9a-f]{64})/([0-9a-f]{16})/text-layer-manifest\.json")


def assert_writer():
    repo = load_config()["publisher_repository"]
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != repo
            or os.environ.get("GITHUB_WORKFLOW_REF") != repo + "/.github/workflows/reader-resource-repair.yml@refs/heads/main"
            or not os.environ.get("GITHUB_RUN_ID")):
        raise RuntimeError("resource repair requires the central serialized workflow")


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def report_artifact(run):
    if run.get("status") != "completed":
        raise ValueError("GC source report run is incomplete")
    allowed = {".github/workflows/reader-gc-collect.yml": ("reader-gc-collection", "collection.json"),
               ".github/workflows/reader-gc.yml": ("reader-gc-report", "reader-gc-report.json")}
    if run.get("path") not in allowed:
        raise ValueError("source report is not a central GC workflow")
    return allowed[run["path"]]


def missing_references(report):
    graph = report.get("graph") or report
    if graph.get("version") != 1 or not isinstance(graph.get("missing"), list):
        raise ValueError("repair requires a complete GC report artifact")
    refs = set()
    for value in graph["missing"]:
        if not isinstance(value, str) or ":" not in value:
            raise ValueError("invalid missing resource")
        bucket, path = value.split(":", 1)
        if bucket == ASSETS and IMAGE_MANIFEST.fullmatch(path) or bucket == PDF and TEXT_MANIFEST.fullmatch(path):
            refs.add((bucket, path))
    return refs


def absent(store, bucket, path):
    try:
        retry_read(lambda: store.read_bytes(bucket, path))
    except FileNotFoundError:
        return True
    return False


def image_plan(sidecar, missing):
    result = {}
    for key, entry in sidecar["f"].items():
        ref = (entry.get("b"), entry.get("p"))
        if ref not in missing or entry.get("s") != 2 or entry.get("m") != "i":
            continue
        match = IMAGE_MANIFEST.fullmatch(ref[1])
        if not match or not isinstance(key, str) or key.count("\0") != 1:
            raise ValueError("invalid source identity for image repair")
        repo, path = key.split("\0")
        if (not re.fullmatch(r"VoiceOfML/[A-Za-z0-9._-]+", repo)
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or "\\" in path or any(ord(char) < 32 for char in path)
                or path.rsplit(".", 1)[-1].lower() != match[1]):
            raise ValueError("image repair source does not match sidecar")
        result.setdefault(ref, []).append({"key": key, "repo": repo, "path": path,
                                         "extension": match[1], "source_sha256": match[2]})
    return result


def pin_image_sources(api, plan):
    pinned = {}
    repositories = sorted({item["repo"] for items in plan.values() for item in items})
    for repo in repositories:
        revision = retry_read(lambda: api.repo_info(repo, repo_type="dataset").sha)
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("image repair source revision is not immutable")
        entries = [item for items in plan.values() for item in items if item["repo"] == repo]
        for offset in range(0, len(entries), 50):
            chunk = entries[offset:offset + 50]
            metadata = retry_read(lambda: api.get_paths_info(repo, [item["path"] for item in chunk],
                                                            repo_type="dataset", revision=revision))
            files = {entry.path: entry for entry in metadata if hasattr(entry, "size")}
            for item in chunk:
                info = files.get(item["path"])
                if info is None:
                    raise FileNotFoundError("pinned original image source is missing")
                digest = getattr(getattr(info, "lfs", None), "sha256", None)
                if digest and digest != item["source_sha256"]:
                    raise ValueError("current original image differs from referenced source checksum")
                pinned[item["key"]] = {**item, "revision": revision, "bytes": info.size}
    return pinned


def restore_text_bundle(store, path, bundle):
    match = TEXT_MANIFEST.fullmatch(path)
    if not match or match[1] != match[2][:2]:
        raise ValueError("invalid companion restoration path")
    root = Path(path).parent
    raw = retry_read(lambda: store.read_bytes(PDF, (root / "ocr-manifest.json").as_posix()))
    manifest = json.loads(raw)
    ref = manifest.get("text_layer")
    v3.resource(ref)
    if (ref["bucket"] != PDF or ref["path"] != path or manifest.get("source_sha256") != match[2]
            or manifest.get("kind") != "pdf-ocr" or manifest.get("complete") is not True
            or [page.get("p") for page in manifest.get("pages", [])] != list(range(1, manifest["page_count"] + 1))):
        raise ValueError("OCR manifest does not bind the missing companion")
    def read(resource):
        return retry_read(lambda: store.read_bytes(resource["bucket"], resource["path"]))
    book = v3.decode(v3.verified_read({"bucket": PDF, **manifest["book_text"], "role": "provenance"}, read))
    if (book.get("complete") is not True or book.get("text_layer") != ref
            or [page.get("page") for page in book["pages"]] != list(range(1, manifest["page_count"] + 1))):
        raise ValueError("published text does not bind a complete missing companion")
    def restore_page(page):
        resource = v3.resource({"bucket": PDF, "path": page["o"], "sha256": page["os"],
                                "bytes": page["ob"], "role": "provenance"})
        payload = v3.decode(v3.verified_read(resource, read))
        if (payload.get("kind") != "pdf-ocr-page" or payload.get("page") != page["p"]
                or payload.get("source") != page["source"]):
            raise ValueError("raw OCR page identity mismatch")
        config = manifest.get("layout_options", {})
        options = {**config.get("default", {}), **config.get("pages", {}).get(str(page["p"]), {})}
        if payload["source"] == "native":
            options.pop("rotation", None)
            if payload["blocks"]:
                payload.update(ocr_layout.arrange(payload["blocks"], payload["width"], payload["height"], options,
                                                  include_writing_modes=True))
        layer = text.from_page(payload, match[2], manifest.get("language", "und"), resource,
                               layout_options=options)
        published = book["pages"][page["p"] - 1]
        if layer["text"] != published["text"] or layer["generation"] != published["text_generation"]:
            raise ValueError("restored companion differs from published page text/generation")
        return layer
    with ThreadPoolExecutor(max_workers=4) as pool:
        layers = list(pool.map(restore_page, manifest["pages"]))
    actual = v3.build_text_bundle(layers, manifest["pages"], root, bundle, include_search=False)
    if actual != ref:
        raise ValueError("restored legacy companion checksum differs from immutable reference")
    def local_read(resource):
        local = bundle / resource["path"]
        return local.read_bytes() if local.is_file() else read(resource)
    v3.verify_text_bundle(ref, local_read, match[2], manifest["page_count"])
    return ref, {target.relative_to(bundle).as_posix(): target for target in (bundle / root).rglob("*")
                 if target.is_file()}


def publish_bundle(store, bucket, files, ref, *, apply):
    from huggingface_hub import batch_bucket_files
    changed = []
    for path, local in sorted(files.items()):
        try:
            old = retry_read(lambda: store.read_bytes(bucket, path))
        except FileNotFoundError:
            changed.append((str(local), path))
        else:
            if old != local.read_bytes():
                raise ValueError("immutable repair object conflicts with published bytes")
    if not apply:
        return {"new_objects": len(changed), "applied": False}
    assert_writer()
    root = ref["path"].rsplit("/", 1)[0]
    protection = lifecycle.processing_record([{"bucket": bucket, "root": root}], "reader-resource-repair",
        {"repository": os.environ.get("GITHUB_REPOSITORY"), "run_id": os.environ.get("GITHUB_RUN_ID")})
    protection_path = "reader-index/processing/repair-" + uuid.uuid4().hex + ".json"
    store.put_json(ASSETS, protection_path, protection)
    if changed:
        batch_bucket_files(bucket, add=changed, token=store.token(bucket))
    for path, local in sorted(files.items()):
        if retry_read(lambda: store.read_bytes(bucket, path)) != local.read_bytes():
            raise ValueError("repaired object readback mismatch")
    registry_path = "reader-index/resource_repairs.json"
    try:
        registry = json.loads(retry_read(lambda: store.read_bytes(ASSETS, registry_path)))
    except FileNotFoundError:
        registry = {"version": 1, "kind": "reader-resource-repairs", "files": {}}
    registry["files"][bucket + ":" + ref["path"]] = {"resources": [ref], "status": "ready"}
    store.put_json(ASSETS, registry_path, registry)
    sidecar = json.loads(gzip.decompress(retry_read(lambda: store.read_bytes(ASSETS, SIDECAR))))
    catalog = json.loads(retry_read(lambda: store.read_bytes(ASSETS, "reader-index/reader_catalog.json")))
    generation = catalog["current"]["generation"]
    sidecar_sha = hashlib.sha256(json.dumps(sidecar, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
    if catalog["generations"][generation]["sidecar_sha256"] != sidecar_sha:
        raise ValueError("repair handoff catalog differs from current sidecar")
    lifecycle.validate_processing_handoff(protection, {"path": ref["path"]})
    store.put_json(ASSETS, protection_path, lifecycle.release_processing(protection, generation))
    return {"new_objects": len(changed), "applied": True, "generation": generation,
            "processing_root": protection_path}


def run(store, report, output, *, apply=False, limit=0, mode="all"):
    if apply:
        assert_writer()
    missing = missing_references(report)
    sidecar = json.loads(gzip.decompress(retry_read(lambda: store.read_bytes(ASSETS, SIDECAR))))
    plan = image_plan(sidecar, missing) if mode != "text" else {}
    pinned = pin_image_sources(store.api, plan) if plan else {}
    selected = [("image", bucket, path) for bucket, path in sorted(plan)]
    if mode != "images":
        selected.extend(("text", bucket, path) for bucket, path in sorted(missing) if bucket == PDF)
    if limit:
        selected = selected[:limit]
    result = {"version": 1, "applied": apply, "selected": len(selected), "restored": [],
              "already_present": [], "failed": [], "started_at": lifecycle.now_iso()}
    output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        output.write_bytes(encode(result))
        if apply:
            status = {"version": 1, "selected": result["selected"],
                      "restored": len(result["restored"]), "already_present": len(result["already_present"]),
                      "failed": len(result["failed"]), "updated_at": lifecycle.now_iso()}
            store.put_json(ASSETS, "reader-index/gc/repair-" + os.environ["GITHUB_RUN_ID"] + "/progress.json", status)
    save()
    for kind, bucket, path in selected:
        try:
            if not absent(store, bucket, path):
                result["already_present"].append({"bucket": bucket, "path": path})
                save()
                continue
            with tempfile.TemporaryDirectory(prefix="reader-resource-repair-") as directory:
                bundle = Path(directory)
                if kind == "image":
                    item = pinned[plan[(bucket, path)][0]["key"]]
                    entry, files = images.build_one(item, bundle, store.token(ASSETS), ASSETS)
                    if entry["manifest"] != path:
                        raise ValueError("rebuilt image changed source-addressed path")
                    manifest_raw = Path(files[path]).read_bytes()
                    ref = {"bucket": bucket, "path": path, "bytes": len(manifest_raw),
                           "sha256": hashlib.sha256(manifest_raw).hexdigest(), "role": "runtime"}
                    files = {key: Path(local) for key, local in files.items()}
                    pages = entry["page_count"]
                else:
                    ref, files = restore_text_bundle(store, path, bundle)
                    pages = json.loads((bundle / path).read_text())["page_count"]
                publication = publish_bundle(store, bucket, files, ref, apply=apply)
                result["restored"].append({"kind": kind, "resource": ref, "pages": pages, **publication})
                print(f"restored {kind}: {len(result['restored'])}/{len(selected)}; {pages} pages", flush=True)
        except Exception as error:
            result["failed"].append({"kind": kind, "bucket": bucket, "path": path,
                                     "error_type": type(error).__name__,
                                     "http_status": getattr(getattr(error, "response", None), "status_code", None)})
            print(f"repair failed: {kind}: {path}: {type(error).__name__}", flush=True)
        save()
    result["finished_at"] = lifecycle.now_iso()
    result["complete"] = not result["failed"]
    save()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--mode", choices=("all", "images", "text"), default="all")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("limit must be non-negative")
    result = run(HubBucketStore(), json.loads(args.report.read_text()), args.output,
                 apply=args.apply, limit=args.limit, mode=args.mode)
    print(json.dumps({key: len(value) for key, value in result.items() if isinstance(value, list)}), flush=True)
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
