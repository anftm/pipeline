#!/usr/bin/env python3
"""Publish explicitly pinned Lin PDF repairs through the central Reader writer."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re

from pypdf import PdfReader, PdfWriter

try:
    from . import publish_reader_v3 as publication, reader_assets, repair_gbk_pdf, reader_lifecycle, shared
    from .reader_bucket import publish_catalog
except ImportError:
    import publish_reader_v3 as publication, reader_assets, repair_gbk_pdf, reader_lifecycle, shared
    from reader_bucket import publish_catalog

DERIVED = "reader-index/derived_pdf_manifest.json"
SIDECAR = "reader-index/reader_assets.json.gz"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def source_spec(value):
    if not isinstance(value, dict):
        raise ValueError("invalid repair source spec")
    key = value.get("source_key")
    if not isinstance(key, str) or key.count("\0") != 1:
        raise ValueError("repair requires an exact source key")
    repo, path = key.split("\0")
    if (not reader_assets.known_gbk_pdf(repo, path) or any(p in {"", ".", ".."} for p in path.split("/"))
            or "\\" in path or any(ord(c) < 32 for c in path)
            or not re.fullmatch(r"[0-9a-f]{40}", str(value.get("source_revision", "")))
            or type(value.get("page_count")) is not int or value["page_count"] < 1):
        raise ValueError("repair source is not a pinned known Lin PDF")
    for field in ("source_sha256", "repaired_sha256"):
        publication.text.sha(value.get(field))
    previous = value.get("previous_pdf")
    publication.v3.resource(previous)
    if (previous["bucket"] != shared.PDF_PAGES_BUCKET or previous["role"] != "runtime"
            or not re.fullmatch(r"derived/[A-Za-z0-9._-]+/[a-z0-9]{32}/document\.pdf", previous["path"])
            or previous["bytes"] > 512 * 1024 * 1024):
        raise ValueError("repair requires an exact existing derived PDF")
    return value


def object_root(spec):
    recipe = digest(publication.encode({"source_key": spec["source_key"],
        "source_revision": spec["source_revision"], "profile": reader_assets.GBK_PDF_CONTRACT[0],
        "repaired_sha256": spec["repaired_sha256"]}))[:16]
    source = spec["source_sha256"]
    return f"objects/{source[:2]}/{source}/{recipe}"


def mapping(path):
    return {"s": 2, "m": "p", "p": path, "b": shared.PDF_PAGES_BUCKET}


def download_source(spec, workspace):
    from huggingface_hub import hf_hub_download
    repo, path = spec["source_key"].split("\0")
    cached = hf_hub_download(repo, path, repo_type="dataset", revision=spec["source_revision"],
                             token=os.environ.get("HF_TOKEN"))
    return Path(cached).read_bytes()


def build(spec, raw, workspace):
    if not 1 <= len(raw) <= 512 * 1024 * 1024:
        raise ValueError("repair source exceeds the document bound")
    if digest(raw) != spec["source_sha256"]:
        raise ValueError("pinned repair source checksum mismatch")
    workspace.mkdir(parents=True, exist_ok=True)
    source, target = workspace / "source.pdf", workspace / "repaired.pdf"
    source.write_bytes(raw)
    result = repair_gbk_pdf.repair_pdf(source, target)
    repaired = target.read_bytes()
    if digest(repaired) != spec["repaired_sha256"]:
        raise ValueError("rebuilt repair differs from the accepted candidate")
    before, after = PdfReader(source), PdfReader(target)
    if len(before.pages) != spec["page_count"] or len(after.pages) != spec["page_count"]:
        raise ValueError("repair changed page count")
    for original, candidate in zip(before.pages, after.pages):
        if (original.get_contents().get_data() != candidate.get_contents().get_data()
                or original.mediabox != candidate.mediabox or original.cropbox != candidate.cropbox
                or original.get("/Rotate", 0) != candidate.get("/Rotate", 0)):
            raise ValueError("repair changed page content or geometry")
    writer = PdfWriter(clone_from=after)
    for font in writer._objects:
        if (not isinstance(font, dict) or font.get("/Type") != "/Font"
                or font.get("/Subtype") != "/TrueType" or font.get("/Encoding") != "/WinAnsiEncoding"
                or repair_gbk_pdf.font_name(font.get("/BaseFont")) not in repair_gbk_pdf.FONT_NAMES):
            continue
        descriptor = font.get("/FontDescriptor")
        descriptor = descriptor.get_object() if descriptor else {}
        if not any(name in descriptor for name in ("/FontFile", "/FontFile2", "/FontFile3")):
            raise ValueError("repair retains an unembedded known WinAnsi font")
    # Use the same native Unicode engine as the Lin text pipeline. Pypdf is
    # the structural verifier, not the GBK text-decoding oracle.
    import pymupdf
    with pymupdf.open(target) as document:
        for probe in spec.get("text_probes", []):
            number, expected = probe.get("page"), probe.get("text")
            if type(number) is not int or not 1 <= number <= spec["page_count"] or not isinstance(expected, str) or not expected:
                raise ValueError("invalid repair text probe")
            actual = "".join(document[number - 1].get_text().split())
            if "".join(expected.split()) not in actual:
                raise ValueError("repair text probe failed")
    return repaired, result


def run(store, options, workspace, *, apply=False, download=None):
    sources = options.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        raise ValueError("explicit bounded repair sources required")
    specs = [source_spec(value) for value in sources]
    if len({spec["source_key"] for spec in specs}) != len(specs):
        raise ValueError("duplicate repair source")
    if apply:
        publication.assert_writer(store)
    pointer, catalog = publication.current(store)
    derived_raw = store.read_bytes(shared.PDF_PAGES_BUCKET, DERIVED)
    derived = json.loads(derived_raw)
    if (derived.get("version") != 1 or derived.get("kind") != "pdf-derived-source-index"
            or not isinstance(derived.get("files"), list)):
        raise ValueError("invalid current derived source index")
    entries = {entry["key"]: entry for entry in derived["files"]}
    if len(entries) != len(derived["files"]):
        raise ValueError("duplicate current derived source keys")
    sidecar_raw = store.read_bytes(publication.ASSETS, SIDECAR)
    sidecar = json.loads(gzip.decompress(sidecar_raw))
    if sidecar.get("v") != 1 or not isinstance(sidecar.get("f"), dict):
        raise ValueError("invalid current Reader sidecar")
    pinned = publication.pinned_base(store, catalog, sidecar)
    prepared = []
    for number, spec in enumerate(specs):
        key, previous = spec["source_key"], spec["previous_pdf"]
        if key in catalog["files"]:
            raise ValueError("repair must not replace a published v3 book or accepted text")
        entry = entries.get(key, {})
        root = object_root(spec)
        path, manifest_path = root + "/document.pdf", root + "/derived-manifest.json"
        old = entry.get("new_path") == previous["path"] and entry.get("derived_sha256") == previous["sha256"]
        already = (entry.get("new_path") == path and entry.get("derived_sha256") == spec["repaired_sha256"]
                   and entry.get("profile") == reader_assets.GBK_PDF_CONTRACT[0])
        if (not (old or already) or entry.get("source_revision") != spec["source_revision"]
                or entry.get("source_sha256") != spec["source_sha256"]
                or entry.get("source_extension") != "pdf"):
            raise ValueError("derived source changed since repair was planned")
        for base in (sidecar, pinned):
            if base["f"].get(key) not in (mapping(previous["path"]), mapping(path)):
                raise ValueError("repair source has changed routing or separately published text")
        repaired, verification = build(spec, (download or download_source)(spec, workspace / str(number)), workspace / str(number))
        resource = publication.metadata(shared.PDF_PAGES_BUCKET, path, repaired)
        old_pdf = publication.v3.verified_read(previous, lambda ref: store.read_bytes(ref["bucket"], ref["path"]))
        if len(PdfReader(io.BytesIO(old_pdf)).pages) != spec["page_count"]:
            raise ValueError("previous repair page count mismatch")
        updated = {**entry, "profile": reader_assets.GBK_PDF_CONTRACT[0], "derived_sha256": resource["sha256"],
            "derived_bytes": resource["bytes"], "path": path, "new_path": path, "manifest_path": manifest_path,
            "generation": reader_assets.GBK_PDF_CONTRACT[0], "bucket": shared.PDF_PAGES_BUCKET,
            "previous_pdf": {**previous, "role": "provenance"}, "verification": verification}
        manifest = publication.encode({**updated, "version": 1, "kind": "pdf-derived-source"})
        prepared.append({"key": key, "root": root, "resource": resource, "bytes": repaired,
            "manifest_path": manifest_path, "manifest_bytes": manifest, "entry": updated})
    report = {"applied": apply, "books": [{"source_key": book["key"], "pdf": book["resource"],
        "verification": book["entry"]["verification"]} for book in prepared]}
    if not apply:
        return report
    if (store.read_bytes(shared.PDF_PAGES_BUCKET, DERIVED) != derived_raw
            or store.read_bytes(publication.ASSETS, SIDECAR) != sidecar_raw or publication.current(store)[0] != pointer):
        raise ValueError("repair publication inputs changed during verification")
    identity = digest(publication.encode(specs))[:32]
    protection_path = f"reader-index/processing/gbk-{identity}.json"
    resources = [{"bucket": shared.PDF_PAGES_BUCKET, "root": book["root"]} for book in prepared]
    try:
        protection = json.loads(store.read_bytes(publication.ASSETS, protection_path))
        if protection.get("resources") != resources or protection.get("operation") != "gbk-font-repair":
            raise ValueError("repair protection root identity mismatch")
    except FileNotFoundError:
        protection = reader_lifecycle.processing_record(resources, "gbk-font-repair", {
            "repository": os.environ.get("GITHUB_REPOSITORY", ""), "run_id": os.environ.get("GITHUB_RUN_ID", "")})
        store.put_bytes(publication.ASSETS, protection_path, publication.encode(protection))
    for book in prepared:
        publication.immutable_put(store, shared.PDF_PAGES_BUCKET, book["resource"]["path"], book["bytes"])
        publication.immutable_put(store, shared.PDF_PAGES_BUCKET, book["manifest_path"], book["manifest_bytes"])
    updated_derived, updated_sidecar = copy.deepcopy(derived), copy.deepcopy(sidecar)
    replacements = {book["key"]: book["entry"] for book in prepared}
    updated_derived["files"] = [replacements.get(entry["key"], entry) for entry in derived["files"]]
    for book in prepared:
        updated_sidecar["f"][book["key"]] = mapping(book["resource"]["path"])
    new_derived = publication.encode(updated_derived)
    new_sidecar = gzip.compress(publication.encode(updated_sidecar), mtime=0)
    for bucket, path, raw, prior in ((shared.PDF_PAGES_BUCKET, DERIVED, new_derived, derived_raw),
            (publication.ASSETS, SIDECAR, new_sidecar, sidecar_raw)):
        if raw != prior:
            store.put_bytes(bucket, path, raw)
        if store.read_bytes(bucket, path) != raw:
            raise ValueError("repaired index readback mismatch")
    generation = publish_catalog(updated_sidecar, os.environ.get("HF_TOKEN"))
    unified = publication.unify(store, apply=True)
    if protection.get("status") != "released":
        store.put_bytes(publication.ASSETS, protection_path, publication.encode(
            reader_lifecycle.release_processing(protection, generation)))
    return {**report, "generation": unified["generation"], "v2_generation": generation,
            "processing_root": protection_path}
