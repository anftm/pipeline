#!/usr/bin/env python3
"""Durable PDF rendering followed by independent, resumable image-only OCR.

Only the rendering stage downloads PDFs or uses Poppler/cjxl. The OCR stage
uses checksummed PNG objects; neither Reader WebP nor a PDF is an OCR input.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, nullcontext, closing
from pathlib import Path
from urllib.parse import quote

import httpx
from huggingface_hub import HfApi, CommitOperationAdd
from huggingface_hub.errors import HfHubHTTPError
from huggingface_hub.utils import get_session, hf_raise_for_status

try:
    from . import pdf_ocr, pdf_assets, lin_pdf_text, pdf_render_schedule, pdf_worker_lanes, plan_pdf_ocr, publish_pdf_ocr_assets as publication, shared, ocr_layout, reader_lifecycle, pdf_text_layer, pdf_reading_v3
    from . import pdf_ocr_progress, djvu_pages, ocr_quality, ndl_ocr, native_text_quality, ocr_mixed_text, ocr_script_evidence, ocr_latin_spacing, ocr_source_review, ocr_aligned_table, ocr_regions, ocr_symbolic_table, ocr_formula_scripts, ocr_fraction_layout, ocr_grouped_table, ocr_flow_diagram, ocr_chronology_table, ocr_formula_sums
    from .run_pdf_ocr import source_path, _bucket_retry_delay
    from .reader_bucket import index_path, publish_bytes, publish_json, publish_json_batch, publish_catalog, read_json as read_bucket_json
except ImportError:
    import pdf_ocr, pdf_assets, lin_pdf_text, plan_pdf_ocr, publish_pdf_ocr_assets as publication, shared, ocr_layout, reader_lifecycle, pdf_text_layer, pdf_reading_v3
    import pdf_render_schedule
    import pdf_worker_lanes
    import pdf_ocr_progress
    import djvu_pages
    import ocr_quality
    import ndl_ocr
    import native_text_quality
    import ocr_mixed_text
    import ocr_script_evidence
    import ocr_latin_spacing
    import ocr_source_review
    import ocr_aligned_table
    import ocr_regions
    import ocr_symbolic_table
    import ocr_formula_scripts
    import ocr_fraction_layout
    import ocr_grouped_table
    import ocr_flow_diagram
    import ocr_chronology_table
    import ocr_formula_sums
    from run_pdf_ocr import source_path, _bucket_retry_delay
    from reader_bucket import index_path, publish_bytes, publish_json, publish_json_batch, publish_catalog, read_json as read_bucket_json

RENDER_REGISTRY = "pdf_render_manifest.json"
PROGRESS_REGISTRY = "pdf_ocr_progress.json"
RENDER_PROGRESS_REGISTRY = "pdf_render_progress.json"
RENDER_RANGE_PAGES = 250
RENDER_RANGE_THRESHOLD = 500
MIN_OCR_IMAGE_SIDE_PIXELS = 8
BUCKET = f"hf://buckets/{shared.PDF_PAGES_BUCKET}"
OCR_INPUT_BUCKET = f"hf://buckets/{shared.PDF_OCR_INPUT_BUCKET}"
SMALL_RENDER_MAX_SOURCE_BYTES = 100 * 1024 * 1024
RENDER_SAMPLE_TIMEOUT = 60


def render_profile() -> str:
    return (f"pdf-render-v2-text-png-dpi-{pdf_ocr.OCR_DPI}-maxpix-{pdf_ocr.MAX_PAGE_PIXELS}"
            f"-webp-{pdf_ocr.WEBP_QUALITY}-{pdf_ocr.WEBP_MAX_DIMENSION}"
            f"-native-{pdf_ocr.MIN_NATIVE_PAGE_CHARS}-jxl-{int(pdf_ocr.JXL_ENABLED)}"
            "-adaptive-reader-source-pixels-v2-clean-webp-80-native-stream-v1-preserve-bitonal-v1-png6"
            + (f"-{pdf_ocr.reader_page_codec.PROFILE}" if pdf_ocr.JXL_ENABLED else "")
            + f'-{native_text_quality.PROFILE}-bitmap-text-v1-native-geometry-v1-native-raster-text-v3-rule-evidence'
            + '-pdf-primary-only-v1')


def render_profile_for(item: dict) -> str:
    """PDF and native DjVu renders never share a profile namespace."""
    return (djvu_pages.render_profile(render_profile().removesuffix('-pdf-primary-only-v1'))
            if djvu_pages.is_native_item(item) else render_profile())


def djvu_fallback_current(previous, item):
    """A ``.djvu`` whose bytes are a PDF stays on its current derived-PDF render.

    Native planning recorded that decision on the derived render entry. Do not
    download and sniff the same pinned source again on every plan.
    """
    return bool(djvu_pages.is_native_item(item) and isinstance(previous, dict)
                and previous.get("djvu_pdf_fallback") == "pdf-magic"
                and previous.get("djvu_source_revision") == item.get("source_revision")
                and previous.get("status") in {"ready", "skipped"}
                and previous.get("render_profile") == render_profile())


def root_for(source_sha: str, key: str, identity: str) -> Path:
    suffix = hashlib.sha256(f"{key}\0{identity}".encode()).hexdigest()[:16]
    return Path("objects") / source_sha[:2] / source_sha / suffix


def metadata(path: Path, bundle: Path) -> dict:
    sha, size = shared.hash_file(path)
    return {"path": path.relative_to(bundle).as_posix(), "sha256": sha, "bytes": size}


def public_item(item: dict) -> dict:
    return {k: v for k, v in item.items()
            if not k.startswith("_") and k not in {"probe", "page_chars", "bundle_root"}}


def retry(operation):
    for attempt in range(15):
        try:
            return operation()
        except HfHubHTTPError as exc:
            if shared.hf_status_code(exc) not in {408, 429, 500, 502, 503, 504} or attempt == 14:
                raise
            delay = _bucket_retry_delay(exc, attempt)
        except (httpx.TransportError, ConnectionError, OSError):
            if attempt == 14:
                raise
            delay = min(600, 5 * 2 ** min(attempt, 7))
        print(f"temporary object transfer failure; retry in {delay}s", flush=True)
        time.sleep(delay)


def read_existing_object(path: str, bucket: str = shared.PDF_PAGES_BUCKET) -> bytes:
    """Current bytes of an object whose digest is not yet known (adoption only)."""
    def download():
        response = get_session().get(
            f"https://huggingface.co/buckets/{bucket}/resolve/{quote(path, safe='/')}",
            headers={"Cache-Control": "no-cache"}, follow_redirects=True, timeout=120)
        hf_raise_for_status(response)
        return response.content
    return retry(download)


def remote_sizes(bucket: str, prefix: str, token) -> dict[str, int]:
    sizes = {}
    def listing():
        for item in HfApi(token=token).list_bucket_tree(bucket, prefix=prefix, recursive=True, token=token):
            if item.type == "file":
                sizes[item.path] = getattr(item, "size", None)
    retry(listing)
    return sizes


def remote_sha256(bucket: str, path: str, token) -> str:
    """Hash one existing bucket object for immutable JSON descriptor checks."""
    def download():
        headers = {"Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = get_session().get(
            f"https://huggingface.co/buckets/{bucket}/resolve/{quote(path, safe='/')}",
            headers=headers, follow_redirects=True, timeout=120)
        hf_raise_for_status(response)
        return hashlib.sha256(response.content).hexdigest()
    return retry(download)


def fresh_additions(bucket: str, relative: str, additions: list, token) -> list:
    """Never overwrite an existing immutable object.

    Object paths are deterministic per source/profile, but recognition output is
    not byte-reproducible. Re-uploading over a published page silently changed
    bytes that registries, v3 text bundles and CCRD exports had pinned. An
    identical-size binary object is kept as is; a differing one is a hard
    conflict. Small JSON descriptors are additionally checked by SHA because a
    revision-only change can keep exactly the same byte length while changing
    the payload, which previously let a stale range object masquerade as fresh.
    """
    if not additions:
        return []
    existing = remote_sizes(bucket, relative + "/", token)
    fresh, conflicts = [], []
    for local, path in additions:
        size = existing.get(path)
        if size is None:
            fresh.append((local, path))
        elif type(size) is int and size != Path(local).stat().st_size:
            conflicts.append(path)
        elif path.endswith(".json"):
            local_sha = shared.hash_file(Path(local))[0]
            if remote_sha256(bucket, path, token) != local_sha:
                conflicts.append(path)
    if conflicts:
        raise ValueError(f"immutable object conflict for {len(conflicts)} object(s), e.g. {bucket}:{conflicts[0]}")
    return fresh


def upload_objects(bundle: Path) -> list[str]:
    """Sync only one book's immutable prefix, never recursively list the whole bucket."""
    # This uploader is exclusively for document/OCR artifacts. A stale account
    # worker must not get as far as creating even a protected reading-page
    # object before the central publisher rejects its old render contract.
    for root in (bundle / "objects").glob("*/*/*"):
        if root.is_dir() and ((root / "pages").exists() or (root / "page-manifest.json").exists()):
            raise ValueError("document reader page images are retired")
    records, roots = {}, []
    for root in sorted((bundle / "objects").glob("*/*/*")):
        if root.is_dir():
            relative = root.relative_to(bundle).as_posix()
            resources = [{"bucket": shared.PDF_PAGES_BUCKET, "root": relative}]
            if (root / "ocr-input").is_dir():
                resources.append({"bucket": shared.PDF_OCR_INPUT_BUCKET, "root": relative})
            record = reader_lifecycle.processing_record(resources, "pdf-object-upload", {
                "repository": os.environ.get("GITHUB_REPOSITORY", "local"),
                "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            })
            protection_path = index_path(f"processing/{uuid.uuid4().hex}.json")
            records[protection_path] = record
            roots.append((root, relative))
    if not records:
        return []
    # Protect all prefixes before transferring any bytes. Batch metadata writes
    # avoid two separate small-object uploads per book in central assembly.
    token = os.environ.get("HF_TOKEN")
    publish_json_batch(records, token)
    def additions(root, relative, input_bucket=False):
        result = []
        for local in sorted(root.rglob("*")):
            if not local.is_file():
                continue
            rel = local.relative_to(root)
            if input_bucket:
                if rel.parts[0] != "ocr-input":
                    continue
            else:
                if rel.parts[0] == "ocr-input":
                    continue
                if not (rel.parts[0] in {"ocr", "text"}
                        or rel.name in {"text-layer-manifest.json", "text-review-manifest.json",
                                         "ocr-manifest.json", "render-manifest.json"}
                        or rel.name.startswith("render-range-") and rel.suffix == ".json"):
                    continue
            result.append((str(local), f"{relative}/{rel.as_posix()}"))
        return result

    with ExitStack() as heartbeats:
        for path, record in records.items():
            heartbeats.enter_context(reader_lifecycle.processing_heartbeat(record,
                lambda value, path=path: publish_json(path, value, token)))
        input_token = os.environ.get("HF_INPUT_TOKEN") or os.environ.get("HF_TOKEN")
        for root, relative in roots:
            pdf_additions = fresh_additions(shared.PDF_PAGES_BUCKET, relative, additions(root, relative), token)
            if pdf_additions:
                shared.batch_bucket_files_with_retry(shared.PDF_PAGES_BUCKET, pdf_additions, token)
            input_additions = fresh_additions(shared.PDF_OCR_INPUT_BUCKET, relative,
                                              additions(root, relative, input_bucket=True), input_token)
            if input_additions:
                shared.batch_bucket_files_with_retry(shared.PDF_OCR_INPUT_BUCKET, input_additions, input_token)
    uploaded = {path: {**reader_lifecycle.heartbeat_processing(record), "status": "uploaded"}
                for path, record in records.items()}
    publish_json_batch(uploaded, token)
    return list(records)


def release_processing_roots(paths: list[str], generation: str, result: dict) -> None:
    token = os.environ.get("HF_TOKEN")
    updates = reader_lifecycle.processing_handoffs([{**result, "processing_roots": paths}], generation,
                                                  lambda path: read_bucket_json(path, token))
    if updates:
        publish_json_batch(updates, token)


def handoff_worker_results(api, repo: str, results: list[dict]) -> None:
    """Progress registries take ownership before worker upload roots are released."""
    if type(api) is not HfApi:
        return
    protected = [result for result in results if result.get("processing_roots")]
    if not protected:
        return
    token = os.environ.get("HF_TOKEN")
    generation = publish_catalog(publication.load_sidecar(api, repo), token)
    publication.release_processing_roots(protected, generation, token)


def read_object(meta: dict, suffix: str | None = None) -> bytes:
    path = pdf_ocr.validate_ocr_object_path(meta["path"], suffix)
    bucket = meta.get("bucket") or (
        shared.PDF_OCR_INPUT_BUCKET if "/ocr-input/" in path or path.endswith(".jxl")
        else shared.PDF_PAGES_BUCKET)
    if bucket not in {shared.PDF_PAGES_BUCKET, shared.PDF_OCR_INPUT_BUCKET}:
        raise ValueError("invalid PDF object bucket")
    for attempt in range(3):
        def download():
            # Public resolve avoids per-page metadata calls. Revalidate a short
            # or stale response without relaxing the pinned size/SHA contract.
            response = get_session().get(
                f"https://huggingface.co/buckets/{bucket}/resolve/{quote(path, safe='/')}",
                **({"headers": {"Cache-Control": "no-cache"}} if attempt else {}),
                follow_redirects=True, timeout=120)
            hf_raise_for_status(response)
            return response.content
        data = retry(download)
        if len(data) == meta["bytes"] and hashlib.sha256(data).hexdigest() == meta["sha256"]:
            return data
        if attempt < 2:
            print("PDF object identity mismatch; retrying verified read", flush=True)
            time.sleep(2 ** (attempt + 1))
    raise ValueError(f"object checksum mismatch: {path}")


def page_meta(page: dict, field: str) -> dict:
    return {"path": page[field], "sha256": page[field + "s"], "bytes": page[field + "b"],
            **({"bucket": page[field + "bucket"]} if page.get(field + "bucket") else {})}


def set_page_meta(page: dict, field: str, meta: dict) -> None:
    page.update({field: meta["path"], field + "s": meta["sha256"], field + "b": meta["bytes"]})
    if meta.get('bucket'):
        page[field + 'bucket'] = meta['bucket']


def reading_meta(page):
    return page_meta(page, 'r' if 'r' in page else 'j' if 'j' in page else 'w')


def load_registry(api, repo, name, revision=None):
    if type(api) is HfApi:
        try:
            data = read_bucket_json(index_path(name), os.environ.get("HF_TOKEN"))
        except FileNotFoundError:
            return {"version": 1, "files": {}}
        if data.get("version") != 1 or not isinstance(data.get("files"), dict):
            raise ValueError(f"invalid {name}")
        return data
    try:
        path = retry(lambda: api.hf_hub_download(repo_id=repo, repo_type="dataset", filename=name,
                                                revision=revision))
    except HfHubHTTPError as exc:
        if shared.hf_status_code(exc) == 404:
            return {"version": 1, "files": {}}
        raise
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("files"), dict):
        raise ValueError(f"invalid {name}")
    return data


def load_pdf_sources(api, repo: str) -> dict:
    """Merge current-bucket PDF categories and derivatives into the source inventory."""
    assets = load_registry(api, repo, "manifest.json")
    if type(api) is not HfApi:
        return assets
    files = dict(assets["files"])
    for bucket, path in [
            *[(shared.READER_ASSETS_BUCKET, f"documents/pdf/{ext}/index.json")
              for ext in ("ppt", "pptx", "pps", "wps", "ps")],
            (shared.PDF_PAGES_BUCKET, "reader-index/derived_pdf_manifest.json")]:
        try:
            payload = read_bucket_json(path, os.environ.get("HF_TOKEN"), bucket=bucket)
        except FileNotFoundError:
            continue
        entries = payload.get("files")
        if not isinstance(entries, list):
            raise ValueError(f"invalid PDF source index: {bucket}:{path}")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
                raise ValueError(f"invalid PDF source entry: {bucket}:{path}")
            object_path = entry.get("new_path") or entry.get("object") or entry.get("path")
            size = entry.get("derived_bytes", entry.get("bytes"))
            if (not isinstance(object_path, str) or not object_path.endswith("/document.pdf")
                    or type(size) is not int or size < 1):
                raise ValueError(f"invalid PDF source object: {bucket}:{path}")
            files[entry["key"]] = {**entry, "status": "ready", "reader_mode": "pdf",
                                    "path": object_path, "bytes": size, "bucket": bucket}
    return {**assets, "files": files}


def save_registry(api, repo, name, updates, merge=None, publish_streams=False):
    if not updates:
        return
    if type(api) is HfApi:
        data = load_registry(api, repo, name)
        for key, value in updates.items():
            data["files"][key] = merge(data["files"].get(key), value) if merge else value
        publish_json(index_path(name), data, os.environ.get("HF_TOKEN"))
        if publish_streams:
            ocr_state = load_registry(api, repo, publication.OCR_MANIFEST_NAME)
            sidecar = publication.load_sidecar(api, repo)
            for key, value in updates.items():
                if value.get("status") != "ready":
                    continue
                previous = ocr_state["files"].get(key, {})
                if previous.get("status") != "ready":
                    ocr_state["files"][key] = {**value, "status": "rendered"}
                elif (pdf_ocr.preserves_reader_pdf(value) and same_source(previous, value)
                      and previous.get("source_sha256") == value.get("source_sha256")):
                    ocr_state["files"][key] = {**previous, "page_manifest": None, "stream": False,
                                              "render_manifest": value["render_manifest"],
                                              "render_profile": value["render_profile"], "image_rendered": False,
                                              "reader_presentation": value["reader_presentation"]}
                elif (value.get("range_status") == "failed" and value.get("classification") == "native-text"
                      and value.get("page_manifest") and same_source(previous, value)
                      and previous.get("source_sha256") == value.get("source_sha256")):
                    ocr_state["files"][key] = {**previous, "page_manifest": value["page_manifest"],
                                                "render_manifest": value["render_manifest"],
                                                "range_status": "failed", "classification": "native-text"}
                entry = shared.preserve_pdf_sidecar_entry(sidecar["f"].get(key), value)
                if entry:
                    sidecar["f"][key] = entry
            token = os.environ.get("HF_TOKEN")
            publish_json(index_path(publication.OCR_MANIFEST_NAME), ocr_state, token)
            publish_bytes(index_path(publication.SIDECAR_NAME), publication.encode_sidecar(sidecar), token)
            if token:
                generation = publish_catalog(sidecar, token)
                for value in updates.values():
                    paths = value.get("processing_roots", [])
                    if paths:
                        release_processing_roots(paths, generation, value)
        return
    for attempt in range(20):
        info = retry(lambda: api.repo_info(repo_id=repo, repo_type="dataset"))
        data = load_registry(api, repo, name, info.sha)
        for key, value in updates.items():
            data["files"][key] = merge(data["files"].get(key), value) if merge else value
        content = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        operations = [CommitOperationAdd(path_in_repo=name, path_or_fileobj=content)]
        if publish_streams:
            ocr_state = load_registry(api, repo, publication.OCR_MANIFEST_NAME, info.sha)
            try:
                sidecar_path = retry(lambda: api.hf_hub_download(
                    repo_id=repo, repo_type="dataset", filename=publication.SIDECAR_NAME, revision=info.sha))
                with gzip.open(sidecar_path, "rt", encoding="utf-8") as stream:
                    sidecar = json.load(stream)
            except HfHubHTTPError as exc:
                if shared.hf_status_code(exc) != 404:
                    raise
                sidecar = {"v": 1, "f": {}}
            if sidecar.get("v") != 1 or not isinstance(sidecar.get("f"), dict):
                raise ValueError("invalid Reader sidecar")
            for key, value in updates.items():
                if value.get("status") == "ready":
                    previous = ocr_state["files"].get(key, {})
                    if previous.get("status") != "ready":
                        ocr_state["files"][key] = {**value, "status": "rendered"}
                    elif (pdf_ocr.preserves_reader_pdf(value) and same_source(previous, value)
                          and previous.get("source_sha256") == value.get("source_sha256")):
                        ocr_state["files"][key] = {**previous, "page_manifest": None, "stream": False,
                                                  "render_manifest": value["render_manifest"],
                                                  "render_profile": value["render_profile"], "image_rendered": False,
                                                  "reader_presentation": value["reader_presentation"]}
                    elif (value.get("range_status") == "failed" and value.get("classification") == "native-text"
                          and value.get("page_manifest") and same_source(previous, value)
                          and previous.get("source_sha256") == value.get("source_sha256")):
                        ocr_state["files"][key] = {**previous, "page_manifest": value["page_manifest"],
                                                   "render_manifest": value["render_manifest"],
                                                   "range_status": "failed", "classification": "native-text"}
                    entry = shared.preserve_pdf_sidecar_entry(sidecar["f"].get(key), value)
                    if entry:
                        sidecar["f"][key] = entry
            operations.append(CommitOperationAdd(path_in_repo=publication.SIDECAR_NAME,
                                                  path_or_fileobj=publication.encode_sidecar(sidecar)))
            operations.append(CommitOperationAdd(
                path_in_repo=publication.OCR_MANIFEST_NAME,
                path_or_fileobj=(json.dumps(ocr_state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()))
        try:
            api.create_commit(repo_id=repo, repo_type="dataset", parent_commit=info.sha,
                              commit_message=f"Publish {name}",
                              operations=operations)
            token = os.environ.get("HF_TOKEN")
            if token and type(api) is HfApi:
                publish_json(index_path(name), data, token)
                if publish_streams:
                    publish_json(index_path(publication.OCR_MANIFEST_NAME), ocr_state, token)
                    publish_bytes(index_path(publication.SIDECAR_NAME), publication.encode_sidecar(sidecar), token)
            return
        except HfHubHTTPError as exc:
            if not shared.is_retryable_hf_status(shared.hf_status_code(exc)) or attempt == 19:
                raise
            time.sleep(shared.hf_retry_delay(attempt))


def same_source(entry, item):
    """Same source bytes, not merely the same repository revision.

    A repository-wide revision bump can leave a DJVU/PDF byte-identical. Render
    roots are keyed by source SHA-256, so treating that revision as a new source
    would requeue the book and collide with the immutable range objects already
    published under the same root. Fall back to the revision only for legacy
    entries that do not carry a source SHA.
    """
    if not entry or entry.get("source_kind") != item.get("source_kind"):
        return False
    if entry.get("reader_assets_path") != item.get("reader_assets_path"):
        return False
    source_sha = item.get("source_sha256")
    if isinstance(source_sha, str) and len(source_sha) == 64:
        return entry.get("source_sha256") == source_sha
    return entry.get("source_revision") == item.get("source_revision")


def pin_unchanged_render_sources(api, records, rendered, ocr=None):
    """Prove old renders' LFS source bytes before the unprobed batch is selected."""
    groups = {}
    for item in records:
        previous = rendered.get(item["key"], {})
        preserved = (ocr or {}).get(item["key"], {})
        completed_djvu = (djvu_pages.is_native_item(item) and preserved.get('status') in {'ready', 'skipped', 'failed'}
                          and preserved.get('source_sha256') == previous.get('source_sha256'))
        revision = item.get("source_revision", "")
        if (item.get("source_kind") != "upstream" or item.get("source_sha256")
                or previous.get("source_kind") != "upstream"
                or previous.get("reader_assets_path") != item.get("reader_assets_path")
                or previous.get("status") not in {"ready", "skipped", "failed"}
                or (previous.get("render_profile") != render_profile_for(item) and not completed_djvu)
                or not re.fullmatch(r"[0-9a-f]{64}", str(previous.get("source_sha256", "")))
                or not re.fullmatch(r"[0-9a-f]{40}", str(revision))
                or previous.get("source_revision") == revision):
            continue
        groups.setdefault((item["repo"], revision), []).append(item)
    verified = {}
    for (repo, revision), items in groups.items():
        for start in range(0, len(items), 100):
            batch = items[start:start + 100]
            metadata = retry(lambda: api.get_paths_info(
                repo, [item["path"] for item in batch], repo_type="dataset", revision=revision))
            by_path = {entry.path: entry for entry in metadata}
            for item in batch:
                remote = by_path.get(item["path"])
                lfs = getattr(remote, "lfs", None)
                digest = lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)
                if digest and digest == rendered[item["key"]]["source_sha256"]:
                    verified[item["key"]] = digest
    return [{**item, "source_sha256": verified[item["key"]]} if item["key"] in verified else item
            for item in records]


def render_result_is_current(existing, book):
    """A ready render already owns this byte-identical source/profile.

    This is the publication-side guard for repository revision drift: an older
    artifact can still arrive for a book that is already published under the same
    source SHA and render profile. Keeping the existing render avoids replacing
    a valid immutable range with a revision-only duplicate.
    """
    return bool(isinstance(existing, dict)
                and existing.get("status") in {"ready", "skipped"}
                and same_source(existing, book)
                and existing.get("source_sha256") == book.get("source_sha256")
                and existing.get("render_profile") == book.get("render_profile")
                and existing.get("page_count") == book.get("page_count"))


def ocr_publication_is_current(previous, book):
    """Reject stale zero-shard republishes after an identical OCR publication."""
    return bool(previous and previous.get("status") in {"ready", "skipped"}
                and isinstance(book.get("source_sha256"), str) and len(book["source_sha256"]) == 64
                and previous.get("source_sha256") == book.get("source_sha256")
                and previous.get("source_revision") == book.get("source_revision")
                and previous.get("source_kind") == book.get("source_kind")
                and previous.get("profile") == book.get("profile")
                and previous.get("page_count") == book.get("page_count")
                and previous.get("render_manifest") == book.get("render_manifest")
                and previous.get("page_manifest") == book.get("page_manifest"))


def superseded_ocr_books(queue, rendered):
    """Old artifacts cannot overwrite a newer render/native-text generation."""
    superseded = set()
    for book in queue["books"]:
        latest = rendered.get(book["key"])
        if not isinstance(latest, dict):
            raise ValueError("OCR queue has no current render owner; retaining upload protection")
        if (latest.get("status") != book.get("status") or not same_source(latest, book)
                or any(latest.get(field) != book.get(field) for field in (
                    "source_sha256", "render_profile", "render_manifest", "page_count", "page_manifest"))):
            superseded.add(book["key"])
    return superseded


def filter_ocr_queue(queue, excluded):
    shards = []
    for shard in queue["shards"]:
        records = shard["records"] if isinstance(shard, dict) else shard
        remaining = [record for record in records if record["key"] not in excluded]
        if remaining:
            shards.append({**shard, "records": remaining} if isinstance(shard, dict) else remaining)
    return {**queue, "books": [book for book in queue["books"] if book["key"] not in excluded], "shards": shards}


def render_partition_matches(item, partition):
    if partition == "all":
        return True
    size = int(item.get("source_bytes") or 0)
    if not size:
        return partition == "small"
    is_small = size < SMALL_RENDER_MAX_SOURCE_BYTES
    return is_small if partition == "small" else not is_small


def pending_render(records, rendered, ocr, retry_failed=False, partition="all"):
    pending = []
    for item in records:
        if not render_partition_matches(item, partition):
            continue
        previous = rendered.get(item["key"])
        if djvu_fallback_current(previous, item):
            continue
        completed_ocr = ocr.get(item["key"], {})
        # A delivery-policy change must not automatically redo a completed or
        # explicitly failed recognition, nor discard its evidence or IDs.
        if (djvu_pages.is_native_item(item) and same_source(previous, item)
                and previous.get("status") in {"ready", "skipped"}
                and completed_ocr.get("status") in {"ready", "skipped", "failed"}
                and completed_ocr.get("source_sha256") == previous.get("source_sha256")):
            continue
        if same_source(previous, item) and previous.get("render_profile") == render_profile_for(item):
            if previous.get("status") in {"ready", "skipped"}:
                continue
            if previous.get("status") == "failed" and not retry_failed:
                continue
        pending.append(item)
    # Repeatedly failed PDFs must not monopolize the first checkpoint and
    # prevent untouched books from entering later batches. Native DjVu goes
    # first so the converted lane finishes the primary-document rollout before
    # spending capacity on other converted formats.
    return sorted(pending, key=lambda item: (
        rendered.get(item["key"], {}).get("status") == "failed",
        0 if djvu_pages.is_native_item(item) else 1,
        item["repo"], item["path"], item["source_kind"],
    ))


def pinned_keys(value: str) -> list[str]:
    """Exact ``repo/path`` identities for a pinned native-DjVu pilot run."""
    return [item.strip() for item in re.split(r"[|\n]", value) if item.strip()]


def pin_render_records(records: list[dict], wanted: list[str]) -> list[dict]:
    """Restrict a render plan to the requested native DjVu sources.

    A pinned key that is not an admitted native DjVu is an error: a pilot must
    never silently fall back to the derived-PDF path it exists to validate.
    """
    by_identity = {}
    for item in records:
        repo, path = str(item.get("repo") or ""), str(item.get("path") or "")
        by_identity[f"{repo}/{path}"] = item
        if item.get("key"):
            by_identity[str(item["key"])] = item
    missing = [value for value in wanted if value not in by_identity]
    if missing:
        raise ValueError("pinned source is not in this lane: " + ", ".join(missing))
    pinned = []
    for value in wanted:
        item = by_identity[value]
        if not djvu_pages.is_native_item(item) or not djvu_pages.native_enabled(item):
            raise ValueError("pinned source is not an admitted native DjVu: " + value)
        pinned.append(item)
    return pinned


def range_id(start, end):
    return f"{start:06d}-{end:06d}"


def render_ranges(book):
    count = book["page_count"]
    size = RENDER_RANGE_PAGES if count > RENDER_RANGE_THRESHOLD else count
    return [(start, min(start + size - 1, count)) for start in range(1, count + 1, size)]


def range_identity(book):
    return {field: book[field] for field in ("key", "source_sha256", "source_revision",
                                           "source_kind", "render_profile", "page_count")}


def valid_toc(toc, page_count):
    """Outline entries as written by pdf_assets.extract_pdf_outline, else []."""
    if not isinstance(toc, list) or len(toc) > pdf_assets.MAX_PDF_OUTLINE_ENTRIES:
        return []
    for entry in toc:
        if (not isinstance(entry, dict) or set(entry) != {"title", "page", "depth"}
                or not isinstance(entry["title"], str) or not entry["title"]
                or type(entry["page"]) is not int or not 1 <= entry["page"] <= page_count
                or type(entry["depth"]) is not int or not 0 <= entry["depth"] <= pdf_assets.MAX_PDF_OUTLINE_DEPTH):
            return []
    return toc


def validate_range(book, start, end, descriptor, *, with_toc=False):
    name = f"/render-range-{range_id(start, end)}.json"
    root = root_for(book["source_sha256"], book["key"], book["render_profile"])
    if descriptor.get("path") != (root / name.lstrip("/")).as_posix():
        raise ValueError("render range path mismatch")
    payload = json.loads(read_object(descriptor, name))
    if (payload.get("version") != 1 or payload.get("kind") != "pdf-render-range"
            or any(payload.get(field) != value for field, value in range_identity(book).items())
            or payload.get("start") != start or payload.get("end") != end):
        raise ValueError("render range identity mismatch")
    pages = payload.get("pages")
    if not isinstance(pages, list) or [p.get("p") for p in pages] != list(range(start, end + 1)):
        raise ValueError("incomplete render range")
    if (djvu_pages.is_native_item(book) and not book["render_profile"].endswith('-' + djvu_pages.DIRECT_READER_PROFILE)
            and book.get("force_image_render")
            and book["probe"]["classification"] == "native-text"
            and (not payload.get("image_rendered") or any(not ({'w', 'j', 'r'} & page.keys()) for page in pages))):
        raise ValueError("native render range lacks required page images")
    validate_render({**book, "page_manifest": None}, {**book, "version": 1, "kind": "pdf-render",
                                                    "complete": True, "page_manifest": None,
                                                    "image_rendered": payload.get("image_rendered", False), "pages": pages,
                                                    "classification": book["probe"]["classification"]},
                    page_numbers=range(start, end + 1))
    if with_toc:
        return pages, valid_toc(payload.get("toc", []), book["page_count"]) if start == 1 else []
    return pages


def require_current_render_policy(book):
    """Reject stale producer tasks without touching their existing artifacts."""
    # The identity contains independent encoder/recognizer options. Require
    # the delivery suffix here rather than making local publisher environment
    # defaults redefine otherwise valid immutable OCR render identities.
    suffix = ('-' + djvu_pages.DIRECT_READER_PROFILE if djvu_pages.is_native_item(book)
              else '-pdf-primary-only-v1')
    if not str(book.get("render_profile") or "").endswith(suffix):
        raise ValueError("stale document render policy; replan with primary-only worker")


def estimate_render_cost(item, source, progress):
    book = {**item, "render_profile": render_profile_for(item)}
    previous = progress.get(book["key"], {})
    image_rendered = False
    cost = pdf_render_schedule.history_cost(range_identity(book), previous, image_rendered)
    if cost is not None:
        return cost
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        queue = root / "book.json"
        output = root / "cost"
        pdf_ocr.write_json(queue, book)
        command = [sys.executable, str(Path(__file__).resolve()), "measure-render", "--queue", str(queue),
                   "--source", str(source.resolve()), "--output", str(output)]
        with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True) as process:
            try:
                process.wait(timeout=RENDER_SAMPLE_TIMEOUT)
            except subprocess.TimeoutExpired:
                # Terminate Poppler children too; sampling never owns a production worker.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        result = output / "render-cost.json"
        if result.is_file():
            return json.loads(result.read_text(encoding="utf-8"))
    return {"seconds_per_page": pdf_render_schedule.DEFAULT_PAGE_SECONDS,
            "setup_seconds": 0, "source": "fallback", "samples": 0}


def sample_render_cost(book, source, output=None):
    count = book["page_count"]
    samples = []
    for number in sorted({1, (count + 1) // 2, count}):
        try:
            with tempfile.TemporaryDirectory() as temp:
                result = render_book({**book, "start": number, "end": number}, source, Path(temp))
                timing = result["timing"]
                if (pdf_render_schedule.finite_seconds(timing.get("page_seconds"), positive=True) is not None
                        and pdf_render_schedule.finite_seconds(timing.get("setup_seconds")) is not None):
                    samples.append(timing)
        except Exception as exc:
            print(f"render timing sample unavailable: {type(exc).__name__}", flush=True)
        cost = ({"seconds_per_page": max(s["page_seconds"] for s in samples),
                 "setup_seconds": max(s["setup_seconds"] for s in samples),
                 "source": "sample", "samples": len(samples)} if samples else {
                     "seconds_per_page": pdf_render_schedule.DEFAULT_PAGE_SECONDS,
                     "setup_seconds": 0, "source": "fallback", "samples": 0})
        if output is not None:
            pending = output / "render-cost.pending.json"
            pdf_ocr.write_json(pending, cost)
            os.replace(pending, output / "render-cost.json")
    return cost


def plan_render_ranges(queue, progress, target_seconds=None):
    if target_seconds is not None and pdf_render_schedule.finite_seconds(target_seconds, positive=True) is None:
        raise ValueError("render target seconds must be finite and positive")
    books = [{**item, "render_profile": render_profile_for(item)}
              for shard in queue["shards"] for item in shard["records"]]
    tasks, saved = [], {}
    for book in books:
        prior = progress.get(book["key"], {})
        existing = prior.get("ranges", {}) if all(prior.get(k) == v for k, v in range_identity(book).items()) else {}
        completed = []
        for key, descriptor in existing.items():
            bounds = pdf_render_schedule.parse_range(key, book["page_count"])
            if bounds is None:
                continue
            start, end = bounds
            try:
                validate_range(book, start, end, descriptor)
                saved.setdefault(book["key"], {})[key] = descriptor
                completed.append(bounds)
            except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
                pass
        size = RENDER_RANGE_PAGES if book["page_count"] > RENDER_RANGE_THRESHOLD else book["page_count"]
        if target_seconds is not None:
            size = pdf_render_schedule.range_size(book.get("_render_cost", {}), target_seconds, size)
        for start, end in pdf_render_schedule.missing_ranges(book["page_count"], completed, size):
            tasks.append({**book, "start": start, "end": end})
    if target_seconds is None:
        count = min(256, len(tasks), max(1, math.ceil(sum(t["end"] - t["start"] + 1 for t in tasks) / 500)))
        shards = shared.weighted_shards(tasks, count, weight=lambda t: t["end"] - t["start"] + 1,
                                        order=lambda t: (-(t["end"] - t["start"] + 1), t["key"], t["start"])) if tasks else []
    else:
        shards = pdf_render_schedule.balance(tasks, target_seconds)
    return {**queue, "books": books, "saved_ranges": saved,
             **({"target_render_seconds": target_seconds} if target_seconds is not None else {}),
             "shard_count": len(shards), "shard_ids": list(range(len(shards))),
             "shards": [{"index": i, "page_count": sum(t["end"] - t["start"] + 1 for t in shard),
                         **({"estimated_seconds": round(sum(map(pdf_render_schedule.task_seconds, shard)), 2)}
                            if target_seconds is not None else {}),
                         "records": ([{key: task[key] for key in (*range_identity(task), "start", "end")}
                                      | {"_render_cost": task.get("_render_cost", {})} for task in shard]
                                     if target_seconds is not None else shard)} for i, shard in enumerate(shards)]}


def expand_render_tasks(queue, records):
    books = {book["key"]: book for book in queue.get("books", [])}
    tasks = []
    for record in records:
        book = books.get(record["key"])
        if book is None:
            raise ValueError("render task refers to an unknown book")
        if any(record.get(key) != value for key, value in range_identity(book).items()):
            raise ValueError("render task identity differs from book")
        tasks.append({**book, **record})
    return tasks


def render_book(item: dict, source: Path, bundle: Path, *, force_ocr_native=False) -> dict:
    started = time.perf_counter()
    source_sha, source_bytes = shared.hash_file(source)
    if item.get("source_sha256") and item["source_sha256"] != source_sha:
        raise ValueError("PDF source changed after planning")
    djvu = djvu_pages.is_native_item(item)
    if djvu:
        if djvu_pages.sniff(source) != "djvu":
            raise ValueError("native DjVu render requires DjVu source bytes")
        if force_ocr_native:
            raise ValueError("forced OCR is not defined for native DjVu sources")
    lin_native = not djvu and lin_pdf_text.applies(item)
    probe = item.get("probe") or (djvu_pages.probe(source) if djvu else
                                  lin_pdf_text.probe(source) if lin_native else pdf_ocr.probe_pdf(source))
    presentation = djvu_pages.reader_presentation(source) if djvu else {
        **(item.get("reader_presentation") or pdf_ocr.reader_presentation(source)),
        "strategy": "preserve-pdf", "policy": "djvu-only-page-stream-v1"}
    preserve_pdf = pdf_ocr.preserves_reader_pdf({"reader_presentation": presentation})
    if djvu and preserve_pdf:
        raise ValueError("a DjVu source cannot be preserved as a Reader PDF")
    djvu_infos = None
    if djvu:
        # The planner's probe is checked against the actual bytes; geometry is
        # always read from the source rather than trusted from the queue.
        djvu_infos = djvu_pages.inspect(source.read_bytes())
        if len(djvu_infos) != probe["page_count"]:
            raise ValueError("DjVu page count differs from planning probe")
    profile = render_profile_for(item) + ("-force-ocr-native-v1" if force_ocr_native else "")
    if "start" in item and not 1 <= item["start"] <= item["end"] <= probe["page_count"]:
        raise ValueError("invalid render page range")
    base = {**public_item(item), "source_sha256": source_sha, "source_bytes": source_bytes,
            "reader_presentation": presentation,
            "render_profile": profile, "profile": pdf_ocr.asset_profile(),
            "page_count": probe["page_count"],
            "classification": "scan" if force_ocr_native else probe["classification"],
            **({"force_ocr_native": True, "original_classification": probe["classification"]}
               if force_ocr_native else {})}
    root = root_for(source_sha, item["key"], profile)
    bundle.mkdir(parents=True, exist_ok=True)
    pages = []
    # Both document formats are read from their primary bytes. Stale plans and
    # force_image_render must not re-enable an image stream. OCR alone needs PNG.
    first, last = item.get("start", 1), item.get("end", probe["page_count"])
    # PDF outlines stay in the directly read primary. DjVu never passes through
    # PDF outline/image inspection, and OCR inputs keep their full render pixels.
    toc = []
    scan_images = {}
    native_text = {}
    if not lin_native and not force_ocr_native:
        native_numbers = [number for number in range(first, last + 1)
                          if probe["classification"] == "native-text"
                          or probe["page_chars"][number - 1] > 0]
        if djvu:
            texts = djvu_pages.text_pages(source, djvu_infos) if native_numbers else {}
            native_text = {number: texts[number] for number in native_numbers}
        else:
            native_text = pdf_ocr.native_pages(source, native_numbers)
    language, _ = pdf_ocr.book_ocr_config(item)
    explicit_language = pdf_ocr.language_source(item) in {'override', 'global', 'registry'}
    native_checks = {number: native_text_quality.assess(value, language, explicit_language)
                     for number, value in native_text.items()}
    page_started = time.perf_counter()
    with tempfile.TemporaryDirectory(dir=bundle) as temp, (
            lin_pdf_text.pymupdf.open(source) if lin_native else nullcontext(None)) as document:
        for number in range(item.get("start", 1), item.get("end", probe["page_count"]) + 1):
            raster = native_text.get(number, {}).get('raster_evidence')
            mixed_native = bool(native_checks.get(number, {}).get('usable') and raster and raster['regions'])
            native = not force_ocr_native and (lin_native or (
                native_checks.get(number, {}).get('usable', False)
                and not mixed_native
                and (probe['page_chars'][number - 1] >= pdf_ocr.MIN_NATIVE_PAGE_CHARS
                     or raster is not None and not raster['regions']
                     or native_text[number].get('native_evidence', {}).get('status') == 'decoded')))
            if native:
                text = lin_pdf_text.extract(document, number) if lin_native else native_text[number]
                payload = pdf_ocr.page_payload(number, text["width"], text["height"], text["blocks"], "native")
                payload.update(ocr_layout.arrange(payload["blocks"], payload["width"], payload["height"], {}))
                payload['extracted_text'] = text.get('extracted_text', text['text'])
                payload.update({k: text[k] for k in ('native_evidence', 'extracted_blocks') if k in text})
                payload['native_check'] = native_checks.get(number, native_text_quality.assess(text))
                out = bundle / root / "ocr" / f"page-{number:06d}.json.gz"
                pdf_ocr.write_gzip_json(out, payload)
                page = {"p": number, "source": "native", "width": text["width"], "height": text["height"],
                         'native_text_only': True,
                        "chars": len(payload["text"]), "text": payload["text"],
                        "text_spans": payload["text_spans"], "layout": payload["layout"]}
                set_page_meta(page, "o", metadata(out, bundle))
                pages.append(page)
                continue
            reader_pixels = scan_images.get(number) if not native and probe["page_chars"][number - 1] == 0 else None
            if djvu:
                png, width, height = djvu_pages.render_page(source, number, Path(temp),
                                                            info=djvu_infos[number - 1],
                                                            reader_jxl=False, reader_images=False)
            else:
                png, width, height = pdf_ocr.render_page(source, number, Path(temp), reader_pixels,
                                                          reader_jxl=False, reader_images=False)
            page = {"p": number, "source": "native" if native else "ocr", "width": width, "height": height}
            if mixed_native:
                page['native_components'] = {'blocks': native_text[number]['blocks'],
                    'text': native_text[number]['text'], 'check': native_checks[number], 'raster': raster}
            elif not native and number in native_text:
                page['native_rejection'] = {**native_checks[number], 'text': native_text[number]['text'],
                                             'blocks': native_text[number]['blocks'],
                    **{key: native_text[number][key] for key in ('native_evidence', 'extracted_text', 'extracted_blocks')
                       if key in native_text[number]}}
            destination = bundle / root / "ocr-input" / png.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(png), destination)
            set_page_meta(page, "i", metadata(destination, bundle))
            pages.append(page)
            print(f"rendered {number}/{probe['page_count']}: {item['key']}", flush=True)
    timing = {"page_count": len(pages), "image_rendered": any({'w', 'j', 'r'} & page.keys() for page in pages),
              "page_seconds": time.perf_counter() - page_started,
              "setup_seconds": page_started - started}
    if "start" in item:
        start, end = item["start"], item["end"]
        if not 1 <= start <= end <= probe["page_count"] or len(pages) != end - start + 1:
            raise ValueError("invalid render page range")
        descriptor = bundle / root / f"render-range-{range_id(start, end)}.json"
        pdf_ocr.write_json(descriptor, {**range_identity(base), "version": 1, "kind": "pdf-render-range",
                                        "image_rendered": any({'w', 'j', 'r'} & page.keys() for page in pages),
                                        "start": start, "end": end, "pages": pages,
                                        **({"toc": toc} if toc else {})})
        return {**range_identity(base), "status": "range", "start": start, "end": end,
                "timing": timing,
                "descriptor": metadata(descriptor, bundle)}
    image_pages = [{"page": p["p"], **reading_meta(p)} for p in pages if {'w', 'j', 'r'} & p.keys()]
    page_manifest_meta = None
    if image_pages:
        page_manifest = bundle / root / "page-manifest.json"
        pdf_ocr.write_json(page_manifest, pdf_assets.compact_page_manifest(
            source_sha, profile, image_pages, toc, manifest_dir=root,
            source_format='djvu' if djvu else None))
        page_manifest_meta = {**metadata(page_manifest, bundle), "version": pdf_assets.PAGE_MANIFEST_VERSION}
    manifest = {**base, "version": 1, "kind": "pdf-render", "complete": True,
                "image_rendered": bool(image_pages), "pages": pages, "page_manifest": page_manifest_meta}
    manifest_path = bundle / root / "render-manifest.json"
    pdf_ocr.write_json(manifest_path, manifest)
    return {**base, "status": "ready", "render_manifest": metadata(manifest_path, bundle),
            "page_manifest": page_manifest_meta, "image_rendered": bool(image_pages),
            "ocr_pages": sum(p["source"] == "ocr" for p in pages)}


def validate_render(item, manifest, page_numbers=None):
    if (manifest.get("version") != 1 or manifest.get("kind") != "pdf-render"
            or manifest.get("complete") is not True):
        raise ValueError("invalid render manifest")
    for field in ("key", "source_sha256", "source_revision", "render_profile", "page_count", "page_manifest"):
        if manifest.get(field) != item.get(field):
            raise ValueError(f"render identity mismatch: {field}")
    if (item.get('render_profile', '').endswith('-' + djvu_pages.DIRECT_READER_PROFILE)
            and (not djvu_pages.is_native_item(item) or not djvu_pages.is_native_item(manifest))):
        raise ValueError("direct DjVu render requires the native source format")
    pages = manifest.get("pages", [])
    expected = list(page_numbers) if page_numbers is not None else list(range(1, item["page_count"] + 1))
    if len(pages) != len(expected) or [p["p"] for p in pages] != expected:
        raise ValueError("incomplete render page sequence")
    direct_djvu = (djvu_pages.is_native_item(item)
                   and item.get('render_profile', '').endswith('-' + djvu_pages.DIRECT_READER_PROFILE))
    preserve_pdf = (direct_djvu or pdf_ocr.preserves_reader_pdf(item) or not djvu_pages.is_native_item(item)
                    and item.get('render_profile', '').endswith('-pdf-primary-only-v1'))
    if preserve_pdf and (manifest.get("image_rendered") or manifest.get("page_manifest")):
        raise ValueError("preserved PDF must not publish a reader image stream")
    if (item.get("force_image_render") and not preserve_pdf and manifest.get("classification") == "native-text"
            and not manifest.get("image_rendered")):
        raise ValueError("native render lacks required page images")
    root = root_for(item["source_sha256"], item["key"], item["render_profile"])
    for p in pages:
        if p.get("source") not in {"native", "ocr"} or p["width"] <= 0 or p["height"] <= 0:
            raise ValueError("invalid rendered page")
        if manifest.get('image_rendered') and not {'w', 'j', 'r'} & p.keys():
            raise ValueError('incomplete reader image stream')
        if 'r' in p:
            if (not djvu_pages.is_native_item(item) or not djvu_pages.is_native_item(manifest)
                    or 'w' in p or 'j' in p or preserve_pdf
                    or not manifest.get('image_rendered')
                    or not item['render_profile'].endswith('-' + djvu_pages.BITONAL_READER_PROFILE)
                    or p.get('rbucket') != shared.PDF_PAGES_BUCKET):
                raise ValueError('lossless reading PNG requires one qualified native DjVu page resource')
        elif any(field in p for field in ('rs', 'rb', 'rbucket')):
            raise ValueError('orphaned lossless reading PNG metadata')
        for field, suffix in (("i", ".png"), ("r", ".png"), ("w", ".webp"), ("j", ".jxl"), ("o", ".json.gz")):
            if p.get('native_text_only'):
                if p['source'] != 'native' or any(key in p for key in ('i', 'w', 'j', 'r')):
                    raise ValueError('invalid native text-only page')
                if field != 'o':
                    continue
            if (field == 'r' and 'r' not in p) or (field in {'w', 'j'} and 'r' in p):
                continue
            jxl_only = 'reader-jxl-v' in item['render_profile']
            if field == 'w' and jxl_only and not preserve_pdf:
                if 'w' in p:
                    raise ValueError('JXL reading profile contains a redundant WebP')
                continue
            if field in {"w", "j"} and preserve_pdf:
                if field in p:
                    raise ValueError("preserved PDF contains a recompressed reader image")
                continue
            if manifest["classification"] == "native-text" and not manifest.get("image_rendered"):
                if field in {'w', 'j'}:
                    if field in p:
                        raise ValueError('unadvertised reader image stream')
                    continue
                if field == 'i' and p['source'] == 'native':
                    continue
            if field == "j" and "-jxl-1-" not in item["render_profile"]:
                continue
            if field == "o" and p["source"] != "native":
                continue
            meta = page_meta(p, field)
            pdf_ocr.validate_ocr_object_path(meta["path"], f"page-{p['p']:06d}{suffix}")
            folder = {"i": "ocr-input", "w": "pages", "j": "pages", "r": "pages", "o": "ocr"}[field]
            if meta["path"] != (root / folder / f"page-{p['p']:06d}{suffix}").as_posix():
                raise ValueError("rendered page object outside book profile")
            if (type(meta["bytes"]) is not int or meta["bytes"] < 1
                    or not isinstance(meta["sha256"], str) or not re.fullmatch(r'[0-9a-f]{64}', meta["sha256"])):
                raise ValueError("invalid rendered object metadata")
    return manifest


def merge_render_ranges(old, new):
    identity = range_identity(new)
    ranges = dict(old.get("ranges", {})) if isinstance(old, dict) and all(
        old.get(key) == value for key, value in identity.items()) else {}
    ranges.update(new["ranges"])
    timings = dict(old.get("range_timings", {})) if isinstance(old, dict) and all(
        old.get(key) == value for key, value in identity.items()) and isinstance(old.get("range_timings", {}), dict) else {}
    timings.update(new.get("range_timings", {}))
    return {**identity, "ranges": ranges, **({"range_timings": timings} if timings else {})}


def assemble_render_book(book, descriptors, bundle):
    pages = []
    ranges = []
    toc = []
    for key, descriptor in descriptors.items():
        bounds = pdf_render_schedule.parse_range(key, book["page_count"])
        if bounds is None:
            raise ValueError("invalid render range key")
        ranges.append((*bounds, descriptor))
    for start, end, descriptor in sorted(ranges, key=lambda item: (item[0], item[1])):
        if start > len(pages) + 1:
            return None
        incoming, range_toc = validate_range(book, start, end, descriptor, with_toc=True)
        toc = toc or range_toc
        overlap = min(len(incoming), max(0, len(pages) - start + 1))
        if pages[start - 1:start - 1 + overlap] != incoming[:overlap]:
            raise ValueError("conflicting overlapping render pages")
        pages.extend(incoming[overlap:])
    if len(pages) != book["page_count"]:
        return None
    root = root_for(book["source_sha256"], book["key"], book["render_profile"])
    image_pages = [{"page": p["p"], **reading_meta(p)} for p in pages if {'w', 'j', 'r'} & p.keys()]
    page_manifest_meta = None
    if image_pages:
        page_manifest = bundle / root / "page-manifest.json"
        page_manifest.parent.mkdir(parents=True, exist_ok=True)
        pdf_ocr.write_json(page_manifest, pdf_assets.compact_page_manifest(
            book["source_sha256"], book["render_profile"], image_pages, toc, manifest_dir=root,
            source_format='djvu' if djvu_pages.is_native_item(book) else None))
        page_manifest_meta = {**metadata(page_manifest, bundle), "version": pdf_assets.PAGE_MANIFEST_VERSION}
    base = {**public_item(book), "classification": book["probe"]["classification"],
            "render_profile": book["render_profile"], "profile": book["profile"]}
    manifest = {**base, "version": 1, "kind": "pdf-render", "complete": True,
                "image_rendered": bool(image_pages), "pages": pages, "page_manifest": page_manifest_meta}
    output = bundle / root / "render-manifest.json"
    pdf_ocr.write_json(output, manifest)
    result = {**base, "status": "ready", "render_manifest": metadata(output, bundle),
              "page_manifest": page_manifest_meta, "image_rendered": bool(image_pages),
              "ocr_pages": sum(p["source"] == "ocr" for p in pages)}
    validate_render(result, manifest)
    return result


def recognition_identity(entry, options):
    recipe = {"options": normalized_layout(options), "quality": ocr_quality.POLICY,
                'quality_geometry': ocr_quality.ocr_geometry.PROFILE,
                'latin_spacing': ocr_latin_spacing.PROFILE,
                'source_reviews': ocr_source_review.for_source(entry.get('source_sha256')),
                'source_review_profile': ocr_source_review.PROFILE,
                'aligned_tables': ocr_aligned_table.PROFILE,
                'region_recognition':ocr_regions.PROFILE,
                'symbolic_tables':ocr_symbolic_table.PROFILE,
                'formula_tokens':ocr_formula_scripts.PROFILE,
                'formula_confirmation_rec':ocr_formula_scripts.CONFIRMATION_REC_SHA256,
                'binary_formula_sums':ocr_formula_sums.PROFILE,
                'numeric_fractions':ocr_fraction_layout.PROFILE,
                'grouped_tables':ocr_grouped_table.PROFILE,
                'flow_diagrams':ocr_flow_diagram.PROFILE,
                'chronology_tables':ocr_chronology_table.PROFILE,
               "japanese_models": ndl_ocr.PROFILE,
               'native_text': native_text_quality.PROFILE,
               'mixed_scripts': ocr_mixed_text.PROFILE,
               'script_evidence': ocr_script_evidence.PROFILE,
               'language_policy': ocr_quality.ROUTING_PROFILE,
               'mixed_native': 'native-raster-text-v3-rule-evidence',
               'tables': 'ruled-table-geometry-v3-sparse-rows-rotated-diagonal-header-crops-v1-sparse-cell-pixels-v2',
              'language_routing': entry.get('language_source') or pdf_ocr.language_source(entry)}
    digest = hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    # Recognition language/model belongs to the OCR worker. A language change
    # must reuse the published PNG render instead of forcing PDF rendering.
    language, backend = pdf_ocr.book_ocr_config(entry)
    base = pdf_ocr.asset_profile(language, backend, entry.get("ocr_revision"))
    return f"{base}-quality-v1-layout-{ocr_layout.VERSION}-{digest}"


LAYOUT_DEFAULTS = {"rotation": 0, "writing_mode": "auto", "join_soft_lines": True, "regions": []}


def normalized_layout(options):
    """Layout options without explicit defaults; `{"rotation": 0}` equals `{}`."""
    def strip(value):
        return {k: v for k, v in (value or {}).items() if LAYOUT_DEFAULTS.get(k, object()) != v}
    options = options or {}
    pages = {page: strip(value) for page, value in (options.get("pages") or {}).items()}
    return {"default": strip(options.get("default")), "pages": {k: v for k, v in pages.items() if v}}


def same_recognition_profile(old, entry):
    """A ready OCR entry is current when only default-valued layout keys differ."""
    return (old.get('profile') == entry.get('profile')
            and normalized_layout(old.get('layout_options')) == normalized_layout(entry.get('layout_options')))


def reusable_profile(old, new):
    a, sep_a, layout_a = str(old or "").partition("-layout-")
    b, sep_b, layout_b = str(new or "").partition("-layout-")
    return bool(sep_a and sep_b and layout_a == layout_b and "-quality-v1" in a and "-quality-v1" in b
                and re.sub(r'-jxl-[01]-(?:adaptive|[0-9.]+-[0-9]+)', '', a)
                == re.sub(r'-jxl-[01]-(?:adaptive|[0-9.]+-[0-9]+)', '', b))


def generation_for(book):
    return hashlib.sha256((book["render_manifest"]["sha256"] + book["profile"]).encode()).hexdigest()


def layout_options(overrides, key):
    config = overrides.get(key, {})
    default = ocr_layout.validate_options(config.get("default", {}))
    pages = config.get("pages", {})
    for page, value in pages.items():
        if not page.isdigit() or int(page) < 1:
            raise ValueError("layout page numbers must be positive integers")
        ocr_layout.validate_options({**default, **value})
    return {"default": default, "pages": pages}


def reuse_recognized_pages(old, entry, pages):
    """Reuse verified OCR when only Reader image objects changed."""
    same_recognition = reusable_profile(old.get("profile"), entry.get("profile"))
    if (old.get("status") != "ready" or not same_source(old, entry)
            or old.get("source_sha256") != entry.get("source_sha256")
            or not same_recognition):
        return {}
    try:
        metadata = {"path": old["ocr_manifest"], "sha256": old["ocr_manifest_sha256"],
                    "bytes": old["ocr_manifest_bytes"]}
        previous = json.loads(read_object(metadata, "/ocr-manifest.json"))
        if (previous.get("kind") != "pdf-ocr" or previous.get("complete") is not True
                or previous.get("source_sha256") != entry["source_sha256"]
                or previous.get("profile") != old["profile"]
                or previous.get("page_count") != len(pages)):
            return {}
        old_pages = previous.get("pages")
        if not isinstance(old_pages, list) or len(old_pages) != len(pages):
            return {}
        saved = {}
        for page, prior in zip(pages, old_pages):
            if prior.get("p") != page["p"] or prior.get("source") != page["source"]:
                return {}
            if page["source"] != "ocr":
                continue
            if (page_meta(prior, "i")["sha256"] != page_meta(page, "i")["sha256"]
                    or page_meta(prior, "i")["bytes"] != page_meta(page, "i")["bytes"]):
                return {}
            output = {**page, **{field: prior[field] for field in
                                 ("o", "os", "ob", "text", "text_spans", "layout", "chars", "recognition", "quality_check")}}
            pdf_ocr.validate_ocr_object_path(output["o"], f"page-{page['p']:06d}.json.gz")
            saved[str(page["p"])] = output
        return saved
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return {}


def reuse_progress_pages(previous, entry, pages):
    """Reuse unchanged inputs only when recognition, layout and policy also match."""
    if (previous.get("source_sha256") != entry.get("source_sha256")
            or not reusable_profile(previous.get("profile"), entry.get("profile"))):
        return {}
    saved_pages = previous.get("pages") if isinstance(previous, dict) else None
    if not isinstance(saved_pages, dict) or not saved_pages:
        return {}
    saved = {}
    for page in pages:
        if page.get("source") != "ocr":
            continue
        prior = saved_pages.get(str(page["p"]))
        if not isinstance(prior, dict) or not all(field in prior for field in ("i", "is", "ib")):
            # Old or partial progress entries are not evidence of a recognised
            # page; never trust them just because the book generation changed.
            continue
        if (page_meta(prior, "i").get("sha256") != page_meta(page, "i").get("sha256")
                or page_meta(prior, "i").get("bytes") != page_meta(page, "i").get("bytes")):
            continue
        output = {**page, **{field: prior[field] for field in
                             ("o", "os", "ob", "text", "text_spans", "layout", "chars", "recognition", "quality_check")
                             if field in prior}}
        try:
            pdf_ocr.validate_ocr_object_path(output["o"], f"page-{page['p']:06d}.json.gz")
        except (KeyError, TypeError, ValueError):
            continue
        saved[str(page["p"])] = output
    return saved


def plan_images(rendered, current, progress, limit=20, target=500, overrides=None,
                retry_failed_only=False):
    if limit < 1 or target < 1:
        raise ValueError("limit and target must be positive")
    books, tasks = [], []
    for key, entry in sorted(rendered.items(), key=lambda pair: (
            current.get(pair[0], {}).get("status") == "failed", pair[0])):
        if retry_failed_only and current.get(key, {}).get("status") != "failed":
            continue
        if entry.get("status") not in {"ready", "skipped"}:
            continue
        # Older page streams were classified as scans by Poppler even though
        # their repaired GBK source contains Unicode text. Backfill them from
        # the PDF; never send all of their pages to image recognition.
        if lin_pdf_text.applies(entry) and entry.get("ocr_pages", 0) > 0:
            continue
        old = current.get(key, {})
        options = layout_options(overrides or {}, key)
        # A delivery-only DjVu profile change is not authorization to rerun a
        # completed or rejected result, even if recognition settings drifted.
        if (not retry_failed_only and djvu_pages.is_native_item(entry)
                and entry.get('render_profile', '').endswith('-' + djvu_pages.DIRECT_READER_PROFILE)
                and old.get('status') in {'ready', 'skipped', 'failed'}
                and old.get('source_sha256') == entry.get('source_sha256')
                and old.get('page_count') == entry.get('page_count')
                and normalized_layout(old.get('layout_options')) == normalized_layout(options)):
            continue
        manifest = None
        if entry["status"] != "skipped":
            manifest = validate_render(entry, json.loads(read_object(entry["render_manifest"], "/render-manifest.json")))
        native_text = "\n".join(str(page.get("text") or "") for page in (manifest or {}).get("pages", []))
        if pdf_ocr.language_override(key):
            language, backend = pdf_ocr.book_ocr_config(entry)
            language_source = "override"
        elif pdf_ocr.OCR_LANG != "auto":
            language, _, backend = pdf_ocr.resolve_ocr_config(pdf_ocr.OCR_LANG, pdf_ocr.OCR_BACKEND)
            language_source = "global"
        elif native_text.strip():
            language = pdf_ocr.detect_text_language(native_text)
            backend = pdf_ocr.OCR_BACKEND
            language_source = "native-text"
        else:
            language, backend = pdf_ocr.book_ocr_config(entry)
            language_source = pdf_ocr.language_source(entry)
        override = pdf_ocr.language_override(key) or {}
        entry = {**entry, "ocr_language": language, "ocr_backend": backend,
                  "language_source": language_source,
                  **({"ocr_revision": override["revision"]} if override.get("revision") else {}),
                  "layout_options": options}
        entry['profile'] = recognition_identity(entry, options)
        if (not retry_failed_only and old.get("quality_status") in {"failed", "review"}
                and old.get("quality_check", {}).get("checked_pages") == entry.get("page_count")
                and old.get("source_sha256") == entry.get("source_sha256")
                and same_recognition_profile(old, entry)):
            # A complete rejected result needs a new model/policy or an explicit
            # retry; an automatic continuation must not cycle on the same book.
            continue
        if retry_failed_only and old.get("quality_status") in {"failed", "review"} and old.get("quality_check"):
            attempt = re.sub(r"[^a-z0-9._-]", "-", os.environ.get("GITHUB_RUN_ID", str(time.time_ns())).lower())
            entry["ocr_revision"] = str(entry.get("ocr_revision", "quality")) + "-retry-" + attempt
            entry["profile"] = recognition_identity(entry, options)
        if (old.get("status") in {"ready", "skipped"} and same_source(old, entry)
                and same_recognition_profile(old, entry)
                and old.get("source_sha256") == entry.get("source_sha256")
                and (old.get("page_manifest") == entry.get("page_manifest")
                     or djvu_pages.is_native_item(entry) and entry.get("render_profile", "").endswith(
                         '-' + djvu_pages.DIRECT_READER_PROFILE))):
            continue
        if len(books) >= limit:
            break
        if entry["status"] == "skipped":
            books.append(entry)
            continue
        generation = generation_for(entry)
        previous = progress.get(key, {})
        if previous.get("generation") == generation:
            saved = previous.get("pages", {})
        else:
            saved = (reuse_progress_pages(previous, entry, manifest["pages"])
                     or reuse_recognized_pages(old, entry, manifest["pages"]))
        book = {**entry, "pages": manifest["pages"], "saved": saved}
        books.append(book)
        pending = [p for p in manifest["pages"] if p["source"] == "ocr" and str(p["p"]) not in saved]
        # Fast ONNX recognition benefits from fewer model startups. Keep the
        # slower multilingual Paddle backend below a conservative task size so
        # one shard cannot approach the runner timeout.
        task_target = min(target, 1200) if entry["ocr_backend"] == "paddle_onnxruntime" else target
        for start in range(0, len(pending), task_target):
            tasks.append({"key": key, "generation": generation, "profile": entry["profile"],
                           "ocr_language": entry["ocr_language"], "ocr_backend": entry["ocr_backend"],
                           "language_source": language_source,
                           "routing_backend": backend if language_source in {"override", "global"} else pdf_ocr.OCR_BACKEND,
                           "source_sha256": entry["source_sha256"], "layout_options": options,
                           'source_reviews':ocr_source_review.for_source(entry['source_sha256']),
                          "pages": pending[start:start + task_target]})
    # Pack small books together, while large books can span several workers.
    count = min(256, len(tasks), max(1, math.ceil(sum(len(t["pages"]) for t in tasks) / target)))
    shards = shared.weighted_shards(tasks, count, weight=lambda t: len(t["pages"]),
                                   order=lambda t: (-len(t["pages"]), t["key"], t["pages"][0]["p"])) if tasks else []
    return {"version": 1, "kind": "pdf-image-ocr-queue", "language": pdf_ocr.OCR_LANG,
            "ocr_version": pdf_ocr.OCR_VERSION, "backend": pdf_ocr.OCR_BACKEND, "books": books,
            "target_pages_per_shard": target, "total_ocr_pages": sum(len(t["pages"]) for t in tasks),
            "shard_count": len(shards), "shard_ids": list(range(len(shards))), "shards": shards}


def adopt_recognized_page(page, path, raw, task=None):
    """Validate an existing page object at this generation's deterministic path."""
    payload = json.loads(gzip.decompress(raw))
    if (payload.get("kind") != "pdf-ocr-page" or payload.get("page") != page["p"] or payload.get("source") != "ocr"
            or payload.get("width") != page["width"] or payload.get("height") != page["height"]
            or not isinstance(payload.get("text"), str)
            or not isinstance(payload.get("text_spans", []), list)):
        raise ValueError(f"existing OCR object is not this page's result: {path}")
    if task is not None:
        check = ocr_quality.check_page(payload, source_sha256=task["source_sha256"],
                                       input_sha256=page["is"], profile=task["profile"])
        if payload.get("quality_check") != check:
            raise ValueError("existing OCR object lacks current quality evidence")
    return payload


def recognize_task(task, bundle, adopt_existing=False):
    root = root_for(task["source_sha256"], task["key"], task["generation"] + task["profile"])
    done, errors = [], []
    bundle.mkdir(parents=True, exist_ok=True)
    # An earlier attempt may have uploaded pages it never recorded. The root is
    # unique to this generation/profile/layout, so adopt those (validated)
    # objects instead of re-recognizing; uploads never overwrite them anyway.
    existing = (remote_sizes(shared.PDF_PAGES_BUCKET, (root / "ocr").as_posix() + "/", os.environ.get("HF_TOKEN"))
                if adopt_existing else {})
    with tempfile.TemporaryDirectory(dir=bundle) as temp:
        png = Path(temp) / "input.png"
        for page in task["pages"]:
            try:
                remote = (root / "ocr" / f"page-{page['p']:06d}.json.gz").as_posix()
                if remote in existing:
                    raw = read_existing_object(remote)
                    payload = adopt_recognized_page(page, remote, raw, task)
                    out = bundle / remote
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(raw)
                    result = {**page, "chars": len(payload["text"]), "text": payload["text"],
                              "text_spans": payload.get("text_spans", []), "layout": payload.get("layout", {}),
                              "recognition": payload["recognition"], "quality_check": payload["quality_check"]}
                    set_page_meta(result, "o", metadata(out, bundle))
                    done.append(result)
                    print(f"OCR page {page['p']}: adopted existing object ({task['key']})", flush=True)
                    continue
                png.write_bytes(read_object(page_meta(page, "i"), ".png"))
                from PIL import Image
                config = task.get("layout_options", {})
                options = {**config.get("default", {}), **config.get("pages", {}).get(str(page["p"]), {})}
                ocr_layout.validate_options(options)
                rotation = options.get("rotation", 0)
                with Image.open(png) as image:
                    if image.size != (page["width"], page["height"]):
                        raise ValueError("OCR input dimensions mismatch")
                    width, height = image.size
                    if rotation:
                        rotated = image.rotate(-rotation, expand=True)
                        width, height = rotated.size
                        rotated.save(png)
                        rotated.close()
                degenerate = min(width, height) < MIN_OCR_IMAGE_SIDE_PIXELS
                evidence = ocr_quality.select_language(task["ocr_language"], task.get("language_source", "filename"),
                                                       ocr_quality.probe_image(png))
                language = evidence["language"]
                backend = task.get("routing_backend", task["ocr_backend"])
                models = {}
                if degenerate:
                    # A thin scan strip such as 2200x3 pixels cannot contain
                    # recoverable text at the rendered resolution. Preserve it
                    # as an empty OCR page rather than letting RapidOCR reject
                    # the whole book with ResizeImgError.
                    blocks = []
                    print(f"OCR page {page['p']}: skipped degenerate image {width}x{height} "
                          f"({task['key']})", flush=True)
                else:
                    def recognize(selected):
                        if any(region.get('rotation') for region in options.get('regions',[])):
                            if page.get('native_components'):
                                raise ValueError('rotated region recognition cannot replace native components')
                            return ocr_regions.recognize(png,options['regions'],selected,backend,pdf_ocr)
                        if page.get('native_components'):
                            if rotation:
                                raise ValueError('mixed native/raster recognition requires original page orientation')
                            try:
                                from . import pdf_mixed_text
                            except ImportError:
                                import pdf_mixed_text
                            return pdf_mixed_text.recognize(png, page['native_components'], selected, backend, pdf_ocr)
                        if pdf_ocr.resolve_ocr_config(selected, backend) == pdf_ocr.resolve_ocr_config():
                            values = pdf_ocr.ocr_page(png, width, height)
                        else:
                            values = pdf_ocr.ocr_page(png, width, height, selected, backend)
                        return values, pdf_ocr.actual_ocr_models(selected, backend)
                    blocks, models = recognize(language)
                    evidence, blocks, models = ocr_script_evidence.recover_chinese_route(
                        png, evidence, blocks, models, recognize)
                    language = evidence['language']
                    evidence = ocr_script_evidence.corroborate(png, evidence, blocks)
                    if language=='en':
                        blocks,spacing=ocr_latin_spacing.recognize(png,blocks)
                        if spacing:models.setdefault('diagnostics',{})['latin_spacing']=spacing
                    if task.get('source_reviews'):
                        coverage=ocr_source_review.scientific_coverage(task['source_reviews'],page=page['p'],
                            input_sha256=page['is'],source_sha256=task['source_sha256'],language=language)
                        if coverage:evidence={**evidence,'scientific_symbol_coverage':coverage}
                        with Image.open(png) as original:
                            # Reviews refer to the exact unrotated input pixels.
                            if not rotation:
                                blocks,models=ocr_source_review.apply(original,blocks,models,task['source_reviews'],
                                    page=page['p'],input_sha256=page['is'])
                table = None
                aligned_tables=[]
                fractions=[]
                flow=None
                chronology=None
                if language=='en' and not degenerate and not page.get('native_components') and not any(
                        region.get('rotation') for region in options.get('regions',[])):
                    with Image.open(png) as image:
                        aligned_tables=ocr_aligned_table.prepare(image,blocks)
                        if aligned_tables:
                            blocks,aligned_tables,dashes=ocr_aligned_table.recover_dashes(image,blocks,aligned_tables)
                            aligned_tables=ocr_aligned_table.separate_category_rows(blocks,aligned_tables)
                            aligned_tables=ocr_aligned_table.align_wrapped_labels(blocks,aligned_tables)
                            if dashes:models.setdefault('diagnostics',{})['table_dashes']=dashes
                        elif not rotation:
                            chronology=ocr_chronology_table.detect(image,blocks)
                            if chronology is None:
                                flow=ocr_flow_diagram.detect(image,blocks)
                                if flow:blocks,models,flow=ocr_flow_diagram.filter_detections(image,blocks,models,flow)
                if language in {'ch', 'chinese_cht'} and not degenerate and not page.get('native_components'):
                    try:
                        from . import ocr_table_geometry, ocr_diagonal_header, ocr_sparse_cells
                    except ImportError:
                        import ocr_table_geometry, ocr_diagonal_header, ocr_sparse_cells
                    with Image.open(png) as image:
                        table = ocr_table_geometry.detect(image, blocks)
                        if table:
                            blocks, table, table_evidence = ocr_diagonal_header.recognize(image, table, blocks, pdf_ocr, language, backend)
                            if table_evidence:
                                models.setdefault('diagnostics', {})['table_headers'] = table_evidence
                            if table:
                                blocks,table,cell_evidence=ocr_sparse_cells.recognize(image,table,blocks,pdf_ocr,language,backend)
                                if cell_evidence:
                                    models.setdefault('diagnostics',{})['table_cells']=cell_evidence
                    if table is None and backend=='rapidocr_onnxruntime' and not any(
                            region.get('rotation') for region in options.get('regions',[])):
                        engine=pdf_ocr.get_ocr_engine(language,backend)
                        if hasattr(engine,'recognize_txt'):
                            def read_formula(pixels):
                                recognized=engine.recognize_txt([pixels[:,:,::-1]])
                                return recognized.txts[0],float(recognized.scores[0])
                            def read_secondary_formula(pixels):
                                # Do not mutate the worker's global model size:
                                # the primary model and checkpoint identity stay
                                # intact while a distinct small rec is loaded.
                                secondary=pdf_ocr.get_ocr_engine(language,backend,model_size='small')
                                recognized=secondary.recognize_txt([pixels[:,:,::-1]])
                                identity=pdf_ocr.actual_ocr_models(language,backend,model_size='small')['models']['rec']
                                if (identity.get('sha256')!=ocr_formula_scripts.CONFIRMATION_REC_SHA256
                                        or identity.get('sha256')==models.get('models',{}).get('rec',{}).get('sha256')):
                                    raise ValueError('formula confirmation must use a distinct recognizer')
                                models.setdefault('models',{})['formula_confirmation_rec']=identity
                                return recognized.txts[0],float(recognized.scores[0])
                            with Image.open(png) as image:
                                secondary_read=read_secondary_formula if language=='ch' and pdf_ocr.RAPID_MODEL_SIZE=='medium' else None
                                blocks,formula=ocr_formula_scripts.refine_tokens(image,blocks,read_formula,read_secondary=secondary_read)
                                if not rotation:
                                    blocks,formula_sums=ocr_formula_sums.refine(image,blocks,read_formula,read_secondary=secondary_read)
                                else:formula_sums=None
                            if formula:models.setdefault('diagnostics',{})['formula_scripts']=formula
                            if formula_sums:models.setdefault('diagnostics',{})['formula_sums']=formula_sums
                    if table is None and not any(region.get('rotation') for region in options.get('regions',[])):
                        with Image.open(png) as image:
                            fractions=ocr_fraction_layout.detect(image,blocks)
                payload = pdf_ocr.page_payload(page["p"], page["width"], page["height"], blocks, "ocr")
                if page.get('native_rejection'):
                    payload['native_rejection'] = page['native_rejection']
                if page.get('native_components'):
                    payload['native_components'] = page['native_components']
                if degenerate:
                    payload["ocr_warning"] = "degenerate-image-below-minimum-side"
                arranged = (ocr_regions.arrange(blocks,width,height,options,
                                models.get('diagnostics',{}).get('regional_recognition'))
                            if any(r.get('rotation') for r in options.get('regions',[])) else
                            ocr_aligned_table.arrange(blocks,width,height,aligned_tables,options) if aligned_tables else
                            ocr_chronology_table.arrange(blocks,width,height,chronology,options) if chronology else
                            ocr_flow_diagram.arrange(blocks,width,height,flow,options) if flow else
                            ocr_fraction_layout.arrange(blocks,width,height,fractions,options) if fractions else
                            ocr_table_geometry.arrange(blocks, width, height, table, options, include_writing_modes=True)
                            if table else ocr_layout.arrange(blocks, width, height, options))
                payload.update(ocr_layout.restore_coordinates(arranged, rotation))
                payload["recognition"] = {"policy": ocr_quality.POLICY, "profile": task["profile"],
                    "source_sha256": task["source_sha256"], "input_sha256": page["is"],
                    "language": language, "backend": models.get("backend", task["ocr_backend"]), "language_evidence": evidence,
                    "models": models, "degenerate": degenerate}
                payload["quality_check"] = ocr_quality.check_page(payload, source_sha256=task["source_sha256"],
                    input_sha256=page["is"], profile=task["profile"])
                out = bundle / root / "ocr" / f"page-{page['p']:06d}.json.gz"
                pdf_ocr.write_gzip_json(out, payload)
                result = {**page, "chars": len(payload["text"]), "text": payload["text"],
                          "text_spans": payload["text_spans"], "layout": payload["layout"],
                          "recognition": payload["recognition"], "quality_check": payload["quality_check"]}
                set_page_meta(result, "o", metadata(out, bundle))
                done.append(result)
                print(f"OCR page {page['p']}: {task['key']}", flush=True)
            except Exception as exc:
                errors.append({"page": page["p"], "code": "ocr-page-execution-failed", "stage": "recognition",
                               "input_sha256": page.get("is"), "error": f"{type(exc).__name__}: {exc}"[:1000]})
            finally:
                png.unlink(missing_ok=True)
    return {"key": task["key"], "generation": task["generation"], "pages": done, "errors": errors,
            "quality_status": "failed" if errors else ocr_quality.status(
                [i for p in done for i in p["quality_check"]["issues"]])}


def load_ocr_progress(api, repo, revision=None):
    """Per-book checkpoints after migration; the legacy registry only before it."""
    if type(api) is HfApi:
        store = pdf_ocr_progress.ProgressStore(os.environ.get("HF_TOKEN"))
        if store.exists():
            return store
    return load_registry(api, repo, PROGRESS_REGISTRY, revision)["files"]


def save_ocr_progress(api, repo, updates):
    if type(api) is not HfApi:
        save_registry(api, repo, PROGRESS_REGISTRY, updates, merge_progress)
        return
    token = os.environ.get("HF_TOKEN")
    store = pdf_ocr_progress.ProgressStore(token)
    # Runs under reader-sidecar: never load/rewrite the whole legacy registry.
    pdf_ocr_progress.ensure_migrated(store, token)
    store.save(updates, merge_progress)
    # GC parses every reader-index root; drop the multi-GB legacy copy once proven identical.
    pdf_ocr_progress.retire_legacy(store, token)


def merge_progress(old, new):
    pages = dict((old or {}).get("pages", {})) if (old or {}).get("generation") == new["generation"] else {}
    pages.update(new["pages"])
    same = (old or {}).get("generation") == new["generation"]
    errors = {str(e.get("page", "task")): e for e in (old or {}).get("errors", [])} if same else {}
    errors.update({str(e.get("page", "task")): e for e in new.get("errors", [])})
    for number in new["pages"]:
        errors.pop(number, None)
    identity = {k: v for k, v in (old or {}).items() if same and k in ("source_sha256", "profile")}
    return {**identity, **{k: new[k] for k in ("source_sha256", "profile") if k in new},
            "generation": new["generation"], "pages": pages, "errors": list(errors.values())}


def collect_progress(queue, results):
    books = {b["key"]: b for b in queue["books"] if b["status"] == "ready"}
    updates = {}
    for result in results:
        book = books.get(result["key"])
        if not book or result["generation"] != generation_for(book):
            raise ValueError("OCR result generation mismatch")
        source_pages = {p["p"]: p for p in book["pages"]}
        value = updates.setdefault(book["key"], {"generation": result["generation"], "pages": dict(book["saved"]),
            "source_sha256": book["source_sha256"], "profile": book["profile"], "errors": []})
        value["errors"].extend(result.get("errors", []))
        for page in result["pages"]:
            original = source_pages.get(page["p"], {})
            if original.get("source") != "ocr" or page.get("is") != original.get("is"):
                raise ValueError("OCR result input mismatch")
            pdf_ocr.validate_ocr_object_path(page["o"], f"page-{page['p']:06d}.json.gz")
            key = str(page["p"])
            if key in value["pages"] and value["pages"][key] != page:
                raise ValueError("conflicting OCR page results")
            value["pages"][key] = page
    return updates


def assemble_book(book, saved, bundle):
    base = {k: v for k, v in book.items() if k not in {"pages", "saved", "errors"}}
    if book["status"] == "skipped":
        return base
    pages = [p if p["source"] == "native" else saved.get(str(p["p"])) for p in book["pages"]]
    if any(p is None for p in pages):
        missing = [p["p"] for p, result in zip(book["pages"], pages) if result is None]
        return {**base, "status": "failed", "error": "OCR pages incomplete; uploaded pages retained for retry",
                "quality_status": "failed", "missing_pages": missing, "errors": book.get("errors", [])}
    texts, layers, checks = [], [], []
    language, backend = pdf_ocr.book_ocr_config(book)
    raw_pages = shared.ordered_reads(lambda page: read_object(page_meta(page, "o"), ".json.gz"), pages)
    with closing(raw_pages):
        for page, raw in zip(pages, raw_pages):
            payload = json.loads(gzip.decompress(raw))
            layer, page_text = assembled_page(book, page, payload, language)
            recognition = payload.get("recognition", {})
            compatible = (recognition.get("profile") == book["profile"]
                          or reusable_profile(recognition.get("profile"), book["profile"]))
            check = ocr_quality.check_page(payload, source_sha256=book["source_sha256"],
                input_sha256=page.get("is"), profile=recognition.get("profile") if compatible else book["profile"])
            checks.append(check)
            page_text["quality_check"] = check
            layers.append(layer)
            texts.append(page_text)
    quality = ocr_quality.book_report(checks, book["page_count"])
    pdf_ocr.write_json(bundle / "quality-report.json", quality)
    if quality["status"] != "passed":
        return {**base, "status": "failed", "quality_status": quality["status"], "quality_check": quality,
                "error": "OCR quality gate: " + ", ".join(sorted(quality["counts"])),
                "errors": book.get("errors", [])}
    # An explicit re-pin (repin_ocr_pages.py) assembles adopted page bytes into
    # a distinct root; it must never collide with the stale manifest's root.
    root = root_for(book["source_sha256"], book["key"],
                    book["render_manifest"]["sha256"] + book["profile"] + "|text-layer-v2-search-partitions"
                    + (f"|repin-{book['repin']}" if book.get("repin") else ""))
    text_layer_ref = pdf_reading_v3.build_text_bundle(layers, pages, root, bundle)
    text_path = bundle / root / "ocr" / "book-text.json.gz"
    pdf_ocr.write_gzip_json(text_path, {"version": 2, "kind": "pdf-book-text", "complete": True,
                                      "source_sha256": book["source_sha256"], "page_count": book["page_count"],
                                      "offset_unit": "unicode-codepoint", "profile": book["profile"],
                                      "revision": "raw", "quality": "unreviewed", "text_layer": text_layer_ref,
                                      "language": language, "ocr_version": pdf_ocr.resolve_ocr_config(language, backend)[1],
                                      "quality_check": quality, "pages": texts})
    manifest_path = bundle / root / "ocr-manifest.json"
    pdf_ocr.write_json(manifest_path, {
        "version": 1, "kind": "pdf-ocr", "complete": True, "profile": book["profile"],
        "engine": f"{backend} / {pdf_ocr.resolve_ocr_config(language, backend)[1]} / CPU / {language}",
        "language": language, "ocr_version": pdf_ocr.resolve_ocr_config(language, backend)[1],
        "backend": backend, "source_sha256": book["source_sha256"],
        **({'source_format': 'djvu'} if djvu_pages.is_native_item(book) else {}),
        "source_bytes": book["source_bytes"], "source_revision": book.get("source_revision", ""),
        "classification": book["classification"], "page_count": book["page_count"],
        "dpi": pdf_ocr.OCR_DPI, "pages": pages, "book_text": metadata(text_path, bundle),
        "text_layer": text_layer_ref, "text_revision": "raw", "text_quality": "unreviewed",
        "layout_options": book.get("layout_options", {}),
        "quality_check": quality,
        **({"page_manifest": book["page_manifest"]} if book.get("page_manifest") else {}),
    })
    meta = metadata(manifest_path, bundle)
    return {**base, "status": "ready", "language": language, "ocr_version": pdf_ocr.resolve_ocr_config(language, backend)[1],
            "backend": backend, "stream": bool(book.get("page_manifest")), "ocr_manifest": meta["path"],
            "ocr_manifest_sha256": meta["sha256"], "ocr_manifest_bytes": meta["bytes"],
            "text_layer": text_layer_ref, "text_revision": "raw", "text_quality": "unreviewed",
            "quality_status": "passed", "quality_check": quality}


def assembled_page(book, page, payload, language):
    if (payload.get("kind") != "pdf-ocr-page" or payload.get("page") != page["p"]
            or payload.get("source") != page["source"]):
        raise ValueError("OCR page payload mismatch")
    config = book.get("layout_options", {})
    options = {**config.get("default", {}), **config.get("pages", {}).get(str(page["p"]), {})}
    if page["source"] == "native":
        # Native extraction is already in original page coordinates.
        options.pop("rotation", None)
        if payload["blocks"]:
            payload.update(ocr_layout.arrange(payload.get("raw_blocks", payload["blocks"]), payload["width"], payload["height"], options,
                                              include_writing_modes=True))
        else:
            payload["text_spans"] = []
            payload["layout"] = {"version": ocr_layout.VERSION, "writing_mode": "auto",
                                  "review": ["native-text-without-positioned-blocks"] if payload["text"] else [],
                                  "mapping_precision": "block", "offset_unit": "unicode-codepoint"}
    raw_ref = {"bucket": shared.PDF_PAGES_BUCKET, **page_meta(page, "o"), "role": "provenance"}
    language = payload.get("recognition", {}).get("language", language)
    layer = pdf_text_layer.from_page(payload, book["source_sha256"], language, raw_ref, layout_options=options)
    return layer, {"page": page["p"], "text": payload["text"], "layout": payload["layout"],
                   "text_spans": payload["text_spans"], "text_generation": layer["generation"],
                   "quality": layer["quality"], "review_flags": layer["review_flags"]}


def publish_ocr_batches(books, updates, api, repo, output, batch_size=4):
    """Commit complete books incrementally; a later interruption keeps prior batches."""
    if type(batch_size) is not int or not 1 <= batch_size <= 20:
        raise ValueError("publication batch size must be between 1 and 20")
    report = {"version": 1, "kind": "pdf-ocr-publication-progress", "selected_books": len(books),
              "batch_size": batch_size, "batches": [], "books": [], "complete": False, "status": "running"}
    checkpoint = output / "publication.json"
    def save_report():
        pending = checkpoint.with_suffix(".pending.json")
        pdf_ocr.write_json(pending, report)
        os.replace(pending, checkpoint)
    save_report()
    for offset in range(0, len(books), batch_size):
        started = time.monotonic()
        selected = books[offset:offset + batch_size]
        completed = []
        with tempfile.TemporaryDirectory(dir=output) as directory:
            bundle = Path(directory)
            for book in selected:
                try:
                    saved = updates.get(book["key"], {}).get("pages", book.get("saved", {}))
                    book = {**book, "errors": updates.get(book["key"], {}).get("errors", [])}
                    # A failed assembly must not leave an unowned prefix in the batch.
                    with tempfile.TemporaryDirectory(dir=output) as temporary:
                        result = assemble_book(book, saved, Path(temporary))
                        if result["status"] == "ready":
                            shutil.copytree(Path(temporary) / "objects", bundle / "objects", dirs_exist_ok=True)
                    completed.append(result)
                except Exception as exc:
                    completed.append({**public_item({k: v for k, v in book.items() if k not in {"pages", "saved"}}),
                                      "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:1000]})
            assembly_seconds = time.monotonic() - started
            roots = sorted((bundle / "objects").glob("*/*/*"))
            paths = upload_objects(bundle)
            if len(roots) != len(paths):
                raise ValueError("assembled upload protection coverage mismatch")
            protected = {root.relative_to(bundle).as_posix(): path for root, path in zip(roots, paths)}
            for result in completed:
                if result.get("status") == "ready":
                    result["processing_roots"] = [protected[str(Path(result["ocr_manifest"]).parent)]]
            # The publisher checks every uploaded manifest, page and search
            # partition before registry/catalog promotion. Do not duplicate reads.
            if type(api) is HfApi:
                for index, result in enumerate(completed):
                    if result.get("status") != "ready":
                        continue
                    try:
                        publication.verify_ready_objects(result)
                    except Exception as exc:
                        completed[index] = {**{k: v for k, v in result.items() if k not in {
                            "ocr_manifest", "ocr_manifest_sha256", "ocr_manifest_bytes", "text_layer"}},
                            "status": "failed", "quality_status": "failed",
                            "error": f"publication verification: {type(exc).__name__}: {exc}"[:1000]}
            publication.publish(api, repo, completed, verified=type(api) is HfApi)
        report["books"].extend({k: r[k] for k in ("key", "status", "quality_status", "quality_check", "error",
                                                 "errors", "missing_pages") if k in r} for r in completed)
        batch = {"offset": offset, "books": len(selected),
                 "ready": sum(r.get("status") == "ready" for r in completed),
                 "failed": sum(r.get("status") == "failed" for r in completed),
                 "assembly_seconds": round(assembly_seconds, 3),
                 "total_seconds": round(time.monotonic() - started, 3)}
        report["batches"].append(batch)
        save_report()
        print("pdf_publication " + json.dumps(batch, sort_keys=True), flush=True)
    report["complete"] = True
    report["status"] = "failed" if any(r["status"] == "failed" for r in report["books"]) else "passed"
    save_report()
    write_execution_report(report, output / "quality-report.json")
    return report


def write_execution_report(report, path):
    """Durable JSON plus Actions summary/annotation; checkpoints survive a red job."""
    pdf_ocr.write_json(path, report)
    rows = report.get("books", report.get("results", []))
    problems = [r for r in rows if r.get("status") == "failed" or r.get("errors")
                or r.get("quality_status") in {"review", "failed"}]
    if not problems:
        return
    def escape(value):
        return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print("::error title=OCR quality gate::" + escape(
        f"{len(problems)} book/task results failed or require review. See {path.name}; retained pages remain resumable."), flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"\nOCR quality check: **{len(problems)} failed or require review**. Full report: `{path.name}`.\n\n")
            for row in problems[:100]:
                key = str(row.get("key", "")).replace("\0", "/").replace("\n", " ")
                check = row.get("quality_check", {})
                pages = check.get("affected_pages", row.get("missing_pages", []))
                codes = sorted(check.get("counts", {}))
                if not codes:
                    codes = sorted({i["code"] for p in row.get("pages", []) for i in p.get("quality_check", {}).get("issues", [])})
                    pages = [p["p"] for p in row.get("pages", []) if p.get("quality_check", {}).get("status") != "passed"]
                pages = sorted(set(pages + [e["page"] for e in row.get("errors", []) if type(e.get("page")) is int]))
                stream.write(f"- {key}: {row.get('error') or ', '.join(codes) or 'execution failed'}; pages {pages}\n")


def read_results(paths):
    results = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("results"), list):
            raise ValueError(f"invalid results: {path}")
        results.extend(data["results"])
    return results


def result_paths(paths, directory, expected=False):
    found = set(paths)
    if directory and directory.is_dir():
        found.update(directory.rglob("results*.json"))
    if expected and not found:
        raise ValueError("planned workers but no result artifacts found; refusing empty publication")
    return sorted(found)


def publish_legacy_render(queue, results, api, repo):
    planned = {item["key"]: item for shard in queue["shards"] for item in shard["records"]}
    for item in planned.values():
        require_current_render_policy(item)
    for result in results:
        item = planned.get(result["key"])
        if not item or result.get("source_sha256") != item.get("source_sha256"):
            raise ValueError("render result does not match source queue")
        if result.get("status") == "ready":
            manifest = validate_render(result, json.loads(read_object(result["render_manifest"], "/render-manifest.json")))
            if result.get("page_manifest"):
                images = json.loads(read_object(result["page_manifest"], "/page-manifest.json"))
                if images.get("page_count") != manifest["page_count"]:
                    raise ValueError("render page manifest count mismatch")
    results.extend({**public_item(x), "render_profile": render_profile_for(x)} for x in queue.get("failed", []))
    seen = {r["key"] for r in results}
    for item in planned.values():
        if item["key"] not in seen:
            results.append({**public_item(item), "render_profile": render_profile_for(item),
                            "status": "failed", "error": "render worker result missing"})
    save_registry(api, repo, RENDER_REGISTRY, {r["key"]: r for r in results}, publish_streams=True)
    print(f"render publication: {len(results)} results, "
          f"{sum(r.get('status') == 'ready' for r in results)} ready", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("plan-render", "measure-render", "render", "publish-render", "plan-ocr", "ocr", "publish-ocr"))
    parser.add_argument("--queue", type=Path, default=Path("output/pdf-ocr/queue.json"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/pdf-ocr/bundle"))
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--checkpoint", type=int, default=0)
    parser.add_argument("--keys", default="")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--partition", choices=("all", "small", "large"), default="all")
    parser.add_argument("--worker-lane", default="")
    parser.add_argument("--worker-owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    parser.add_argument("--native-text-stream", action="store_true")
    parser.add_argument("--target-render-seconds", type=int)
    parser.add_argument("--retry-failed-only", action="store_true")
    parser.add_argument("--results", type=Path, nargs="*", default=[])
    parser.add_argument("--results-dir", type=Path)
    parser.add_argument("--publish-batch-size", type=int, default=4)
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--layout-overrides", type=Path, default=Path("state/pdf_ocr_layout.json"))
    parser.add_argument("--assets-repo", default="vomebook/Reader-Assets")
    args = parser.parse_args()
    if not 1 <= args.publish_batch_size <= 20:
        parser.error("--publish-batch-size must be between 1 and 20")
    worker_config = None
    if args.worker_lane:
        if args.stage not in {"plan-render", "plan-ocr"} or args.partition != "all":
            parser.error("--worker-lane requires a planning stage and --partition all")
        worker_config = pdf_worker_lanes.load_config()
        if pdf_worker_lanes.lane_for_owner(worker_config, args.worker_owner) != args.worker_lane:
            parser.error("account cannot claim another PDF worker lane")
    if args.target_render_seconds is not None and (args.target_render_seconds < 1 or args.stage != "plan-render"):
        parser.error("--target-render-seconds requires plan-render and a positive value")
    if args.keys and args.stage != "plan-render":
        parser.error("--keys requires plan-render")
    if args.keys and args.checkpoint:
        parser.error("--keys cannot be combined with --checkpoint")
    if args.stage == "measure-render":
        if args.source is None:
            parser.error("measure-render requires --source")
        sample_render_cost(json.loads(args.queue.read_text(encoding="utf-8")), args.source, args.output)
        return 0
    api = HfApi(token=os.environ.get("HF_TOKEN"))
    repo = args.assets_repo
    args.output.mkdir(parents=True, exist_ok=True)
    if args.stage.startswith("plan-"):
        revision = "reader-assets-v2"
        rendered = load_registry(api, repo, RENDER_REGISTRY)["files"]
        current = load_registry(api, repo, publication.OCR_MANIFEST_NAME)["files"]
        if args.stage == "plan-render":
            assets = load_pdf_sources(api, repo)
            assets["revision"] = revision
            records = pdf_ocr.source_records(args.search_data, args.revisions, assets)
            if worker_config:
                records = pdf_worker_lanes.select_records(worker_config, records, args.worker_lane, args.worker_owner)
            wanted = pinned_keys(args.keys) if args.keys else []
            if wanted:
                if len(wanted) > args.limit:
                    raise ValueError("pinned key count exceeds --limit")
                records = pin_render_records(records, wanted)
            records = pin_unchanged_render_sources(api, records, rendered, current)
            records = pending_render(records, rendered, current, args.retry_failed, args.partition)
            if wanted:
                present = set()
                for item in records:
                    present.add(f"{item.get('repo')}/{item.get('path')}")
                    if item.get("key"):
                        present.add(str(item["key"]))
                absent = [value for value in wanted if value not in present]
                if absent:
                    raise ValueError("pinned source is not pending: " + ", ".join(absent))
            selected = pdf_ocr.queue(records, args.limit, args.checkpoint)
            render_progress = load_registry(api, repo, RENDER_PROGRESS_REGISTRY, revision)["files"]
            estimator = (lambda item, source: estimate_render_cost(item, source, render_progress)) \
                if args.target_render_seconds is not None else None
            queue = plan_pdf_ocr.plan(selected, native_text_stream=args.native_text_stream,
                                      render_estimator=estimator)
            queue["kind"] = "pdf-render-queue"
            queue = plan_render_ranges(queue, render_progress, args.target_render_seconds)
        else:
            if worker_config:
                rendered = {item["key"]: item for item in pdf_worker_lanes.select_records(
                    worker_config, [{**entry, "key": key} for key, entry in rendered.items()],
                    args.worker_lane, args.worker_owner)}
            progress = load_ocr_progress(api, repo, revision)
            overrides = json.loads(args.layout_overrides.read_text(encoding="utf-8")) if args.layout_overrides.is_file() else {}
            queue = plan_images(rendered, current, progress, args.limit,
                                plan_pdf_ocr.ocr_target_pages_per_shard(), overrides,
                                 retry_failed_only=args.retry_failed_only)
        if worker_config:
            queue.update(worker_lane=args.worker_lane, worker_owner=args.worker_owner.lower(),
                         worker_config=pdf_worker_lanes.config_identity(worker_config))
        args.queue.parent.mkdir(parents=True, exist_ok=True)
        pdf_ocr.write_json(args.queue, queue)
        print(f"{args.stage}: {queue['shard_count']} shards", flush=True)
        return 0
    if args.stage == "publish-render":
        queue = json.loads(args.queue.read_text(encoding="utf-8"))
        results = read_results(result_paths(args.results, args.results_dir, False))
        if "books" not in queue:
            if queue["shards"] and not results:
                raise ValueError("planned workers but no result artifacts found; refusing empty publication")
            publish_legacy_render(queue, results, api, repo)
            return 0
        for book in queue["books"]:
            require_current_render_policy(book)
        current = load_registry(api, repo, RENDER_REGISTRY)["files"]
        planned = {(item["key"], item["start"], item["end"]): item for item in expand_render_tasks(
            queue, [item for shard in queue["shards"] for item in shard["records"]])}
        books = {item["key"]: item for item in queue["books"]}
        completed_ranges = {key: dict(value) for key, value in queue.get("saved_ranges", {}).items()}
        range_timings = {}
        superseded_ranges = set()
        invalid_ranges = {}
        for result in results:
            item = planned.get((result["key"], result.get("start"), result.get("end")))
            if not item or any(result.get(field) != value for field, value in range_identity(item).items()):
                raise ValueError("render result does not match source queue")
            if result.get("status") == "range":
                if render_result_is_current(current.get(item["key"]), item):
                    superseded_ranges.add(item["key"])
                    continue
                start, end = item["start"], item["end"]
                try:
                    validate_range(item, start, end, result["descriptor"])
                except Exception as exc:
                    invalid_ranges[item["key"]] = f"{type(exc).__name__}: {exc}"[:1000]
                    print(f"render range rejected for {item['key']}: {invalid_ranges[item['key']]}", flush=True)
                    continue
                saved = completed_ranges.setdefault(item["key"], {})
                key = range_id(start, end)
                if key in saved and saved[key] != result["descriptor"]:
                    raise ValueError("conflicting render range results")
                saved[key] = result["descriptor"]
                timing = result.get("timing", {})
                if (isinstance(timing, dict) and timing.get("page_count") == end - start + 1
                        and pdf_render_schedule.finite_seconds(timing.get("page_seconds"), positive=True) is not None
                        and pdf_render_schedule.finite_seconds(timing.get("setup_seconds")) is not None):
                    range_timings.setdefault(item["key"], {})[key] = timing
        updates = [{**range_identity(book), "ranges": completed_ranges[key]}
                   for key, book in books.items()
                   if key in completed_ranges and key not in superseded_ranges]
        for update in updates:
            if update["key"] in range_timings:
                update["range_timings"] = range_timings[update["key"]]
        if updates:
            save_registry(api, repo, RENDER_PROGRESS_REGISTRY, {r["key"]: r for r in updates},
                          merge=merge_render_ranges)
        if superseded_ranges:
            print(f"render publication: kept {len(superseded_ranges)} already-ready source revision(s)", flush=True)
        if invalid_ranges:
            print(f"render publication: {len(invalid_ranges)} book(s) retained for retry", flush=True)
        published = [{**public_item(x), "render_profile": render_profile_for(x)} for x in queue.get("failed", [])]
        for book in books.values():
            key = book["key"]
            if key in superseded_ranges:
                continue
            if key in invalid_ranges:
                published.append({**public_item(book), "status": "failed", "error": invalid_ranges[key]})
                continue
            with tempfile.TemporaryDirectory(dir=args.output) as temp:
                try:
                    result = assemble_render_book(book, completed_ranges.get(book["key"], {}), Path(temp))
                    if result:
                        result["processing_roots"] = upload_objects(Path(temp))
                        validate_render(result, json.loads(read_object(result["render_manifest"], "/render-manifest.json")))
                        if result.get("page_manifest"):
                            images = json.loads(read_object(result["page_manifest"], "/page-manifest.json"))
                            if images.get("page_count") != result["page_count"]:
                                raise ValueError("render page manifest count mismatch")
                except Exception as exc:
                    result = {**public_item(book), "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:1000]}
                published.append(result or {**public_item(book), "status": "failed",
                                            "error": "render ranges incomplete; uploaded ranges retained for retry"})
        save_registry(api, repo, RENDER_REGISTRY, {r["key"]: r for r in published}, publish_streams=True)
        handoff_worker_results(api, repo, [r for r in results if r.get("status") == "range"
                                           and r.get("key") not in invalid_ranges])
        print(f"render publication: {len(published)} results, "
              f"{sum(r.get('status') == 'ready' for r in published)} ready", flush=True)
        return 0
    queue = json.loads(args.queue.read_text(encoding="utf-8"))
    expected = "pdf-render-queue" if args.stage == "render" else "pdf-image-ocr-queue"
    if queue.get("version") != 1 or queue.get("kind") != expected:
        raise ValueError("invalid stage queue")
    if args.stage == "publish-ocr":
        results = read_results(result_paths(args.results, args.results_dir, bool(queue["shards"])))
        superseded = superseded_ocr_books(queue, load_registry(api, repo, RENDER_REGISTRY)["files"])
        if superseded:
            queue = filter_ocr_queue(queue, superseded)
            results = [result for result in results if result.get("key") not in superseded]
            print(f"ocr publication: ignored {len(superseded)} superseded render queues; upload roots retained", flush=True)
            if not queue["books"]:
                pdf_ocr.write_json(args.output / "publication.json", {
                    "version": 1, "kind": "pdf-ocr-publication-progress", "selected_books": 0,
                    "superseded_books": len(superseded), "batches": [], "complete": True})
                return 0
        current = load_registry(api, repo, publication.OCR_MANIFEST_NAME)["files"]
        current_books = {book["key"] for book in queue["books"]
                         if ocr_publication_is_current(current.get(book["key"]), book)}
        if current_books:
            queue = filter_ocr_queue(queue, current_books)
            results = [result for result in results if result.get("key") not in current_books]
            if not queue["books"]:
                print(f"ocr publication: skipped {len(current_books)} already-ready books", flush=True)
                return 0
        updates = collect_progress(queue, results)
        save_ocr_progress(api, repo, updates)
        report = publish_ocr_batches(queue["books"], updates, api, repo, args.output, args.publish_batch_size)
        handoff_worker_results(api, repo, results)
        print(f"published {len(queue['books'])} book states; incomplete books retain page progress", flush=True)
        return 1 if report["status"] == "failed" else 0
    if not 0 <= args.shard < len(queue["shards"]):
        raise ValueError("invalid shard")
    tasks = queue["shards"][args.shard]
    if args.stage == "render":
        tasks = expand_render_tasks(queue, tasks["records"]) if "books" in queue else tasks["records"]
    else:
        # Same worker/model, small durable checkpoints. A timeout near page 500
        # must not discard all of that worker's completed recognition.
        tasks = [{**task, "pages": task["pages"][start:start + 25]}
                 for task in tasks for start in range(0, len(task["pages"]), 25)]
    results = []
    for task in tasks:
        try:
            with tempfile.TemporaryDirectory(dir=args.output) as temp:
                if args.stage == "render":
                    result = render_book(task, source_path(task), Path(temp))
                else:
                    result = recognize_task(task, Path(temp), adopt_existing=True)
                result["processing_roots"] = upload_objects(Path(temp))
            results.append(result)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:1000]
            if args.stage == "render":
                result = {**range_identity(task), "start": task["start"], "end": task["end"],
                          "status": "failed", "error": error}
            else:
                result = {"key": task["key"], "generation": task["generation"], "pages": [], "errors": [{"error": error}]}
            results.append(result)
        # Keep completed book/range metadata even if a later task times out.
        pdf_ocr.write_json(args.output / f"results-{args.shard}.json", {"version": 1, "results": results})
    # A single failed source is recorded in its result and retried explicitly;
    # failing the shard would discard every other book rendered by this job.
    print(f"{args.stage}: {len(results)} results, "
          f"{sum(1 for r in results if r.get('status') == 'failed' or r.get('errors'))} failed", flush=True)
    if args.stage == "ocr":
        write_execution_report({"version": 1, "results": results}, args.output / f"quality-{args.shard}.json")
        return int(any(r.get("errors") or r.get("quality_status") in {"review", "failed"} for r in results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
