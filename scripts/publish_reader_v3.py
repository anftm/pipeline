#!/usr/bin/env python3
"""Stage verified immutable v3 candidates, then promote one serialized pointer."""

from __future__ import annotations

import argparse
import copy
import hashlib
import gzip
import io
import json
import os
import re
import tempfile
import time
from pathlib import Path
import uuid
from collections import OrderedDict
import threading
import shutil
import weakref

try:
    from . import pdf_reading_v3 as v3, pdf_text_layer as text, reader_lifecycle, shared
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import pdf_reading_v3 as v3
    import pdf_text_layer as text
    import reader_lifecycle
    import shared
    from reader_bucket_store import HubBucketStore

ASSETS = shared.READER_ASSETS_BUCKET
POINTER = "reader-index/v3/current.json"
PROTOCOL = "central-reader-sidecar-v3-v1"


PUBLIC_OBJECT = re.compile(
    r"objects/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{16}/(?:document\.pdf|document\.djvu|"
    r"reading-manifest\.json|page-map\.json\.gz|text-layer-manifest\.json|"
    r"pages/page-[0-9]{6}\.(?:jxl|png)|"
    r"text/(?:page-[0-9]{6}\.json\.gz|book-text\.json\.gz|search-[0-9]{6}-[0-9]{6}\.json\.gz|partition-[0-9]{6}-[0-9]{6}-manifest\.json)|"
    r"preview/(?:page-[0-9]{6}\.(?:png|webp|jpeg|svg|jxl)|partition-[0-9]{6}-[0-9]{6}-manifest\.json))")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def metadata(bucket, path, raw, role="runtime"):
    return {"bucket": bucket, "path": path, "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "role": role}


def index_resource(ref):
    if (not isinstance(ref, dict) or ref.get("bucket") != ASSETS
            or not isinstance(ref.get("path"), str)
            or not ref["path"].startswith("reader-index/v3/")
            or any(part in {"", ".", ".."} for part in ref["path"].split("/"))
            or "\\" in ref["path"] or any(ord(c) < 32 for c in ref["path"])
            or "?" in ref["path"] or "#" in ref["path"]
            or type(ref.get("bytes")) is not int or ref["bytes"] < 1):
        raise ValueError("invalid v3 catalog resource")
    text.sha(ref.get("sha256"))
    return ref


def validate_reading_ref(ref):
    v3.resource(ref)
    if (ref["bucket"] != shared.PDF_PAGES_BUCKET or ref["role"] != "runtime"
            or not re.fullmatch(r"objects/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{16}/reading-manifest\.json", ref["path"])):
        raise ValueError("candidate does not name a public reading manifest")
    return ref


def read_index(store, ref):
    index_resource(ref)
    raw = store.read_bytes(ref["bucket"], ref["path"])
    if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("v3 catalog checksum mismatch")
    return json.loads(raw)


def validate_catalog(catalog):
    if (catalog.get("version") != 3 or catalog.get("kind") != "reader-reading-catalog"
            or not isinstance(catalog.get("files"), dict)):
        raise ValueError("invalid reading catalog")
    body = {k: v for k, v in catalog.items() if k != "generation"}
    if text.digest(body) != catalog.get("generation"):
        raise ValueError("reading catalog generation mismatch")
    if catalog.get("base_sidecar"):
        base_sidecar_ref(catalog["base_sidecar"])
    if 'derived_pdf_redirects' in catalog:
        try:
            from .reader_page_redirects import validated as validate_redirects
        except ImportError:
            from reader_page_redirects import validated as validate_redirects
        if validate_redirects(catalog) and not catalog.get('base_sidecar'):
            raise ValueError('derived PDF redirects require a pinned base')
    if "suppressed_sources" in catalog:
        try:
            from .reader_route_control import validated_sources
        except ImportError:
            from reader_route_control import validated_sources
        suppressed = validated_sources(catalog)
        if suppressed and not catalog.get('base_sidecar'):
            raise ValueError('Reader route suppressions require a pinned base')
    for key, entry in catalog["files"].items():
        if not isinstance(key, str) or not key or not isinstance(entry, dict):
            raise ValueError("invalid reading catalog entry")
        validate_reading_ref(entry["resource"])
        text.sha(entry["source_sha256"])
        text.sha(entry["reading_generation"])
        if (entry["resource"]["bucket"] != shared.PDF_PAGES_BUCKET
                or not entry["resource"]["path"].startswith(
                    f"objects/{entry['source_sha256'][:2]}/{entry['source_sha256']}/")
                or not entry["resource"]["path"].endswith("/reading-manifest.json")):
            raise ValueError("catalog reading resource identity mismatch")
    return catalog


def base_sidecar_ref(ref):
    if (ref.get("bucket") != ASSETS or ref.get("role") != "runtime"
            or not re.fullmatch(r"reader-index/snapshots/[0-9a-f]{64}/reader_assets\.json\.gz", ref.get("path", ""))
            or type(ref.get("bytes")) is not int or not 1 <= ref["bytes"] <= 8 * 1024 * 1024):
        raise ValueError("invalid unified sidecar resource")
    text.sha(ref.get("sha256"))
    if ref["path"].split("/")[2] != ref["sha256"]:
        raise ValueError("sidecar snapshot identity mismatch")
    return ref


def pinned_base(store, catalog, fallback):
    ref = catalog.get("base_sidecar")
    if not ref:
        return fallback
    base_sidecar_ref(ref)
    raw = store.read_bytes(ref["bucket"], ref["path"])
    if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("sidecar snapshot checksum mismatch")
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        decoded = stream.read(50 * 1024 * 1024 + 1)
    if len(decoded) > 50 * 1024 * 1024:
        raise ValueError("sidecar snapshot expands beyond limit")
    base = json.loads(decoded)
    if base.get("v") != 1 or not isinstance(base.get("f"), dict):
        raise ValueError("invalid sidecar snapshot")
    if set(catalog.get('suppressed_sources', [])) & base['f'].keys():
        raise ValueError('pinned sidecar exposes suppressed Reader route')
    return base


def unify(store, *, apply=False, conditional_pointer=False, expected_canonical=None, expected_pointer=None):
    try:
        from .reader_route_control import prepare_base
    except ImportError:
        from reader_route_control import prepare_base
    pointer, catalog = current(store)
    if catalog.get('derived_pdf_redirects'):
        raise ValueError('active derived PDF redirects require an audited base refresh')
    canonical = store.read_bytes(ASSETS, "reader-index/reader_assets.json.gz")
    if expected_pointer is not None and pointer != expected_pointer:
        raise ValueError('reading pointer changed before unification')
    if expected_canonical is not None and hashlib.sha256(canonical).hexdigest() != text.sha(expected_canonical):
        raise ValueError('canonical sidecar changed before unification')
    raw, ref = prepare_base(catalog, canonical)
    if catalog.get("base_sidecar") == ref:
        return {"applied": apply, "unchanged": True, "generation": pointer["generation"]}
    if apply:
        assert_writer(store)
        if current(store)[0] != pointer or store.read_bytes(ASSETS, "reader-index/reader_assets.json.gz") != canonical:
            raise ValueError("Reader roots changed during unification")
        immutable_put(store, ASSETS, ref["path"], raw)
    result = {"version": 3, "kind": "reader-reading-catalog", "files": catalog["files"],
              **({"suppressed_sources": catalog["suppressed_sources"]} if "suppressed_sources" in catalog else {}),
              **({"derived_pdf_redirects": catalog["derived_pdf_redirects"]} if "derived_pdf_redirects" in catalog else {}),
              "base_sidecar": ref, "parent_generation": pointer["generation"] if pointer else None}
    return commit_catalog(store, result, pointer, apply=apply, conditional_pointer=conditional_pointer)


def current(store):
    try:
        pointer = json.loads(store.read_bytes(ASSETS, POINTER))
    except FileNotFoundError:
        return None, {"files": {}}
    if pointer.get("version") != 1 or pointer.get("kind") != "reader-reading-pointer":
        raise ValueError("invalid reading pointer")
    generation = text.sha(pointer.get("generation"))
    ref = index_resource(pointer["catalog"])
    if ref["path"] != f"reader-index/v3/generations/{generation}/catalog.json":
        raise ValueError("pointer catalog path mismatch")
    catalog = validate_catalog(read_index(store, ref))
    if catalog["generation"] != generation:
        raise ValueError("pointer/catalog generation mismatch")
    return pointer, catalog


def immutable_put(store, bucket, path, raw):
    try:
        prior = store.read_bytes(bucket, path)
    except FileNotFoundError:
        store.put_bytes(bucket, path, raw)
    else:
        if prior != raw:
            raise ValueError(f"immutable object conflict: {bucket}:{path}")
    actual = store.read_bytes(bucket, path)
    if actual != raw:
        raise ValueError("uploaded immutable object differs from candidate")


def assert_writer(store):
    store.assert_serialized_writer(PROTOCOL)


def disposable_evidence(ref):
    return (ref.get("role") == "review" and ref.get("bucket") == shared.PDF_OCR_INPUT_BUCKET
            and "/ocr-input/" in ref.get("path", "") and ref["path"].endswith(".png"))


def read_dataset_primary(ref):
    """Read a fixed dataset revision for verification, never materialize it in a bucket."""
    if not isinstance(ref, dict) or not isinstance(ref.get('repo'), str) or not isinstance(ref.get('path'), str):
        raise ValueError('invalid dataset primary identity')
    v3.validate_dataset_primary(ref, ref['repo'] + '\0' + ref['path'], ref.get('sha256'))
    from huggingface_hub import HfApi, hf_hub_download
    entries = HfApi().get_paths_info(ref['repo'], [ref['path']], repo_type='dataset', revision=ref['revision'])
    if len(entries) != 1 or entries[0].size != ref['bytes']:
        raise ValueError('pinned dataset primary size mismatch')
    cached = hf_hub_download(ref['repo'], ref['path'], repo_type='dataset', revision=ref['revision'])
    raw = Path(cached).read_bytes()
    if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('pinned dataset primary checksum mismatch')
    return raw


class CandidateReader:
    def __init__(self, store, bundle=None, *, cache_bytes=128 * 1024 * 1024):
        self.store, self.bundle = store, bundle.resolve() if bundle else None
        self.refs, self.local, self.dataset_sources = {}, {}, {}
        self.verified = set()
        self.cache_bytes, self.cached_bytes = cache_bytes, 0
        self.cache = OrderedDict()
        self.cache_directory = Path(tempfile.mkdtemp(prefix="v3-verified-read-"))
        self.cleanup_cache = weakref.finalize(self, shutil.rmtree, self.cache_directory, ignore_errors=True)
        self.lock = threading.RLock()
        self.read_stats = {"remote_reads": 0, "cache_hits": 0, "remote_bytes": 0}

    def pin(self, ref):
        """Record an exact reference without reading its bytes."""
        v3.resource(ref)
        key = (ref["bucket"], ref["path"])
        with self.lock:
            if key in self.refs and any(self.refs[key][field] != ref[field] for field in ("sha256", "bytes")):
                raise ValueError("conflicting candidate reference digests")
            self.refs.setdefault(key, dict(ref))

    def close(self):
        self.cleanup_cache()
        self.cache.clear()
        self.cached_bytes = 0

    def __call__(self, ref):
        if isinstance(ref, dict) and ref.get('kind') == 'dataset-source':
            repo, source_path = ref.get('repo'), ref.get('path')
            if not isinstance(repo, str) or not isinstance(source_path, str):
                raise ValueError('invalid dataset primary identity')
            v3.validate_dataset_primary(ref, repo + '\0' + source_path, ref.get('sha256'))
            key = (ref['repo'], ref['path'], ref['revision'])
            cache_key = ('dataset', *key, ref['sha256'])
            with self.lock:
                prior = self.dataset_sources.get(key)
                if prior is not None and prior != ref:
                    raise ValueError('conflicting pinned dataset source')
                self.dataset_sources[key] = dict(ref)
                if cache_key in self.cache:
                    self.read_stats['cache_hits'] += 1
                    cached, size = self.cache.pop(cache_key)
                    self.cache[cache_key] = (cached, size)
                    return cached.read_bytes()
            raw = read_dataset_primary(ref)
            if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
                raise ValueError('pinned dataset primary checksum mismatch')
            with self.lock:
                prior = self.dataset_sources.get(key)
                if prior is not None and prior != ref:
                    raise ValueError('conflicting pinned dataset source')
                self.read_stats['remote_reads'] += 1
                self.read_stats['remote_bytes'] += len(raw)
                if len(raw) <= self.cache_bytes and cache_key not in self.cache:
                    while self.cached_bytes + len(raw) > self.cache_bytes:
                        _, (old, size) = self.cache.popitem(last=False)
                        old.unlink()
                        self.cached_bytes -= size
                    target = self.cache_directory / text.digest(cache_key)
                    target.write_bytes(raw)
                    self.cache[cache_key] = (target, len(raw))
                    self.cached_bytes += len(raw)
            return raw
        v3.resource(ref)
        if ref["role"] == "runtime" and not (
                ref["bucket"] == shared.PDF_PAGES_BUCKET and PUBLIC_OBJECT.fullmatch(ref["path"]) or
                ref["bucket"] == shared.PDF_PAGES_BUCKET and re.fullmatch(
                    r"derived/[A-Za-z0-9._-]+/[a-z0-9]{32}/document\.pdf", ref["path"]) or
                ref["bucket"] == shared.READER_ASSETS_BUCKET and re.fullmatch(
                    r"documents/pdf/[a-z0-9_-]+/[0-9a-f]{64}/document\.pdf", ref["path"])):
            raise ValueError("runtime candidate resource lacks a public Reader path")
        key = (ref["bucket"], ref["path"])
        with self.lock:
            if key in self.refs and any(self.refs[key][field] != ref[field] for field in ("sha256", "bytes")):
                raise ValueError("conflicting candidate reference digests")
            self.refs[key] = dict(ref)
        if self.bundle and ref["bucket"] == shared.PDF_PAGES_BUCKET:
            target = (self.bundle / ref["path"]).resolve()
            if not target.is_relative_to(self.bundle):
                raise ValueError("candidate path escapes bundle")
            if target.is_file():
                raw = target.read_bytes()
                if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                    raise ValueError("candidate resource checksum mismatch")
                with self.lock:
                    self.local[key] = target
                    self.verified.add(key)
                return raw
        with self.lock:
            if key in self.cache:
                self.read_stats["cache_hits"] += 1
                cached, size = self.cache.pop(key)
                self.cache[key] = (cached, size)
                return cached.read_bytes()
        raw = self.store.read_bytes(*key)
        if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("candidate resource checksum mismatch")
        with self.lock:
            self.verified.add(key)
            self.read_stats["remote_reads"] += 1
            self.read_stats["remote_bytes"] += len(raw)
            if len(raw) <= self.cache_bytes and key not in self.cache:
                while self.cached_bytes + len(raw) > self.cache_bytes:
                    _, (old, size) = self.cache.popitem(last=False)
                    old.unlink()
                    self.cached_bytes -= size
                target = self.cache_directory / text.digest(key)
                target.write_bytes(raw)
                self.cache[key] = (target, len(raw))
                self.cached_bytes += len(raw)
        return raw

    def closure(self):
        """Verify provenance/review dependencies too, preserving all named evidence."""
        seen = set()
        while set(self.refs) - seen:
            key = min(set(self.refs) - seen)
            seen.add(key)
            ref = self.refs[key]
            if disposable_evidence(ref):
                # Lossless OCR-input PNGs named as review evidence stay pinned in
                # the candidate, but are never read: corrections render the page
                # from the primary PDF, and retired inputs may be collected.
                continue
            if key in self.verified and not ref["path"].endswith((".json", ".json.gz")):
                # Its complete exact bytes were already checked by this reader.
                # Binary media have no nested dependencies; retain the pin but
                # do not download/decode the same page again for graph traversal.
                continue
            raw = v3.verified_read(ref, self)
            if not ref["path"].endswith((".json", ".json.gz")):
                continue
            payload = v3.decode(raw)
            def walk(value):
                if isinstance(value, dict):
                    if value.get('kind') == 'dataset-source':
                        self(value)
                    elif {"bucket", "path", "sha256", "bytes", "role"} <= value.keys():
                        if value["path"].startswith("reader-index/v3/"):
                            index_resource(value)
                            read_index(self.store, value)
                        elif disposable_evidence(value):
                            self.pin(value)
                        else:
                            self(value)
                    else:
                        for child in value.values():
                            walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
            walk(payload)


def commit_catalog(store, catalog, pointer, *, apply=False, restored_source=None, conditional_pointer=False):
    parent = pointer["generation"] if pointer else None
    if catalog.get("parent_generation") != parent:
        raise ValueError("catalog parent mismatch")
    generation = text.digest(catalog)
    catalog["generation"] = generation
    validate_catalog(catalog)
    raw = encode(catalog)
    catalog_ref = metadata(ASSETS, f"reader-index/v3/generations/{generation}/catalog.json", raw)
    new_pointer = {"version": 1, "kind": "reader-reading-pointer", "generation": generation,
                   "catalog": catalog_ref, "resources": [catalog_ref]}
    report = {"generation": generation, "parent_generation": parent, "applied": apply,
              "pointer": new_pointer, "files": len(catalog["files"])}
    if not apply:
        return report
    assert_writer(store)
    # Central Actions owns the serialized writer. Most Hub writes do not have
    # CAS; page migration separately opts into an S3 conditional pointer put.
    latest, prior_catalog = current(store)
    if latest != pointer:
        raise ValueError("reading pointer changed during verification")
    try:
        from .reader_page_redirects import preserve as preserve_redirects
    except ImportError:
        from reader_page_redirects import preserve as preserve_redirects
    preserve_redirects(prior_catalog, catalog)
    before = set(prior_catalog.get('suppressed_sources', []))
    after = set(catalog.get('suppressed_sources', []))
    if before - after and before - after != {restored_source}:
        raise ValueError('new catalog must preserve Reader route suppressions')
    if catalog.get('suppressed_sources'):
        pinned_base(store, catalog, {})
    immutable_put(store, ASSETS, catalog_ref["path"], raw)
    history = {"version": 1, "kind": "reader-reading-promotion", "generation": generation,
               "parent_generation": parent, "resources": [catalog_ref],
               "acks": {"hf": False, "pages": False}, "retention_days": 30,
               "created_at": reader_lifecycle.now_iso()}
    history_path = f"reader-index/v3/promotions/{generation}.json"
    try:
        prior_history = json.loads(store.read_bytes(ASSETS, history_path))
        if prior_history.get("generation") != generation:
            raise ValueError("promotion record identity mismatch")
    except FileNotFoundError:
        store.put_bytes(ASSETS, history_path, encode(history))
    if current(store)[0] != pointer:
        raise ValueError("reading pointer changed before promotion")
    if pointer:
        try:
            from . import reader_gc_retirement as retirement
        except ImportError:
            import reader_gc_retirement as retirement
        _, prior = current(store)
        hint = retirement.record([entry['resource'] for entry in prior['files'].values()],
                                 [entry['resource'] for entry in catalog['files'].values()],
                                 generation, 'v3-replacement')
        if hint:
            store.put_bytes(ASSETS, hint[0], retirement.encode(hint[1]))
    if conditional_pointer:
        # Only page migration opts in: its old pointer must exist and match the
        # exact bytes read with this ETag, not just a previous Hub GET.
        if pointer is None:
            raise ValueError('conditional page promotion requires an existing pointer')
        observed, etag = store.read_for_update(ASSETS, POINTER)
        if json.loads(observed) != pointer:
            raise ValueError('reading pointer changed before conditional promotion')
        store.put_if_match(ASSETS, POINTER, encode(new_pointer), etag)
    else:
        store.put_bytes(ASSETS, POINTER, encode(new_pointer))
    if current(store)[0] != new_pointer:
        raise ValueError("reading pointer verification failed")
    return report


def withdraw(store, options, expected_parent, *, apply=False):
    """Remove only one defective v3 override; retain the source and all history."""
    key = options.get("source_key")
    generation = text.sha(options.get("reading_generation"))
    if not isinstance(key, str) or not key:
        raise ValueError("withdrawal requires an exact source key")
    pointer, catalog = current(store)
    active = catalog["files"].get(key)
    if active is None:
        report = {"applied": apply, "unchanged": True}
    else:
        if not pointer or pointer["generation"] != expected_parent or active["reading_generation"] != generation:
            raise ValueError("stale withdrawal generation")
        actor = os.environ.get("GITHUB_ACTOR", "").strip()
        if not actor:
            raise ValueError("authenticated withdrawal actor required")
        files = {k: entry for k, entry in catalog["files"].items() if k != key}
        report = commit_catalog(store, {"version": 3, "kind": "reader-reading-catalog", "files": files,
            **({"suppressed_sources": catalog["suppressed_sources"]} if "suppressed_sources" in catalog else {}),
            **({"derived_pdf_redirects": catalog["derived_pdf_redirects"]} if "derived_pdf_redirects" in catalog else {}),
            **({"base_sidecar": catalog["base_sidecar"]} if catalog.get("base_sidecar") else {}),
            "parent_generation": expected_parent, "withdrawal": {"source_key": key,
            "reading_generation": generation, "resource": active["resource"], "actor": actor,
            "reason": "source-conversion-needs-review"}}, pointer, apply=apply)
    return report


def acknowledge(store, surface, receipt, *, apply=False):
    if surface not in {"hf", "pages"}:
        raise ValueError("invalid consumer surface")
    pointer, catalog = current(store)
    if (not pointer or receipt.get("version") != 1 or receipt.get("surface") != surface
            or receipt.get("active") is not True or receipt.get("generation") != pointer["generation"]
            or receipt.get("catalog_sha256") != pointer["catalog"]["sha256"]
            or receipt.get("files") != len(catalog["files"])):
        raise ValueError("consumer did not observe current reading generation")
    if not apply:
        return {"generation": pointer["generation"], "surface": surface, "applied": False}
    path = f"reader-index/v3/promotions/{pointer['generation']}.json"
    history = json.loads(store.read_bytes(ASSETS, path))
    if history.get("generation") != pointer["generation"]:
        raise ValueError("consumer acknowledgment promotion mismatch")
    updated = copy.deepcopy(history)
    updated["acks"][surface] = True
    updated.setdefault("receipts", {})[surface] = copy.deepcopy(receipt)
    if apply:
        assert_writer(store)
        if receipt.get("projection_verified") is not True or not receipt.get("checked_url"):
            raise ValueError("consumer acknowledgment requires read-only projection acceptance")
        if current(store)[0] != pointer:
            raise ValueError("consumer receipt became stale")
        store.put_bytes(ASSETS, path, encode(updated))
    return {"generation": pointer["generation"], "surface": surface, "applied": apply,
            "acks": updated["acks"]}


def observe_consumer(store, surface, client, *, absent_source=None):
    """Read deployed status AND projection; a successful upload is not a receipt."""
    import gzip
    pointer, catalog = current(store)
    if not pointer:
        raise ValueError("no v3 generation to accept")
    if absent_source is not None and not catalog.get('base_sidecar'):
        raise ValueError('route verification requires a pinned base')
    endpoints = {
        "hf": ("https://voiceofml-search.hf.space/api/reader-v3-status",
               "https://voiceofml-search.hf.space/api/reader-assets"),
        "pages": ("https://vomebook.github.io/search/data/reader_v3_receipt.json",
                  "https://vomebook.github.io/search/data/reader_assets.json.gz"),
    }
    if surface not in endpoints:
        raise ValueError("invalid consumer surface")
    status_url, sidecar_url = endpoints[surface]
    def fetch(url, limit):
        with client.stream("GET", url, headers={"Cache-Control": "no-cache"}) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise ValueError("consumer acceptance response too large")
                chunks.append(chunk)
            return b"".join(chunks)
    receipt = json.loads(fetch(status_url, 1024 * 1024))
    raw = fetch(sidecar_url, 64 * 1024 * 1024)
    if surface == "pages":
        if (receipt.get("reader_contract") != "pdf-reading-v3-v1"
                or hashlib.sha256(raw).hexdigest() != receipt.get("sidecar_sha256")):
            raise ValueError("Pages deployment receipt/projection mismatch")
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            expanded = stream.read(64 * 1024 * 1024 + 1)
        if len(expanded) > 64 * 1024 * 1024:
            raise ValueError("consumer sidecar expands beyond limit")
        sidecar = json.loads(expanded)
    else:
        sidecar = json.loads(raw)
    if sidecar.get("v") != 1 or not isinstance(sidecar.get("f"), dict):
        raise ValueError("invalid deployed consumer sidecar")
    if absent_source is not None and absent_source in sidecar['f']:
        raise ValueError("deployed consumer still exposes withdrawn route")
    if catalog.get("base_sidecar"):
        projected = project_sidecar(pinned_base(store, catalog, {}), catalog)
        routing = ("s", "m", "p", "b", "pd", "pdb", "c", "cb", "f", "o", "ob", "om")
        def normalized(entry):
            # HF's decoder reduces failed entries to {s: 4}; diagnostics are
            # not routes and must not block an otherwise identical projection.
            if entry.get('s') == 4:
                return {'s': 4}
            return {field: entry[field] for field in routing if field in entry}
        expected = {key: normalized(entry) for key, entry in projected["f"].items()}
        actual = {key: normalized(entry) for key, entry in sidecar["f"].items()}
        if expected != actual:
            raise ValueError("deployed full-format sidecar differs from pinned generation")
    for key, entry in catalog["files"].items():
        expected = {"s": 2, "m": "p", "p": entry["resource"]["path"], "b": shared.PDF_PAGES_BUCKET}
        if sidecar["f"].get(key) != expected:
            raise ValueError("deployed consumer resolves a different reading manifest")
    acknowledge(store, surface, receipt)
    return {**receipt, "checked_url": status_url, "checked_at": reader_lifecycle.now_iso(),
            "projection_verified": True}


ACKNOWLEDGMENTS = "reader-index/reader_gc_acknowledgments.json"


def record_legacy_acknowledgments(store, *, apply=False):
    """Record that both deployed consumers serve the current base generation.

    The canonical Reader catalog can exceed 2 GB, and nothing in production ever
    wrote ``acks`` into it, so ``compact_catalog`` could never drop history. This
    writes one small receipt instead: it names the exact current generation, the
    sidecar digest it binds, and the pinned projection the deployments serve,
    which is what the GC collector needs before it may compact. It never claims a
    generation the deployments do not serve.
    """
    try:
        from .reader_gc_stream import catalog_identity
    except ImportError:
        from reader_gc_stream import catalog_identity
    identity, digest = store.read_graph_json(ASSETS, "reader-index/reader_catalog.json",
                                             parser=catalog_identity)
    if identity.get('redirect_sidecar_pending'):
        return {'applied': False, 'reason': 'sidecar-reconciliation-pending', 'generation': identity['current']}
    if identity["bound_sidecar_sha256"] is None:
        raise ValueError("current catalog generation binds no canonical sidecar")
    base_raw = store.read_bytes(ASSETS, "reader-index/reader_assets.json.gz")
    projection_sha256 = hashlib.sha256(base_raw).hexdigest()
    base = json.loads(gzip.decompress(base_raw))
    canonical_sha256 = hashlib.sha256(json.dumps(
        base, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if canonical_sha256 != identity["bound_sidecar_sha256"]:
        # The catalog's current generation binds an older sidecar than the one
        # deployed consumers would receive. Nothing may be retired against it.
        return {"applied": False, "reason": "canonical-sidecar-newer-than-catalog",
                "generation": identity["current"]}
    pointer, v3_catalog = current(store)
    if not pointer:
        raise ValueError("no deployed v3 projection to acknowledge")
    pinned = v3_catalog.get("base_sidecar") or {}
    if pinned.get("sha256") != projection_sha256:
        # A stale pinned base means the deployments may still serve an older
        # sidecar; acknowledging the newest catalog generation would authorize
        # dropping references those deployments still use.
        return {"applied": False, "reason": "pinned-sidecar-does-not-match-canonical",
                "generation": identity["current"]}
    entry = {"generation": identity["current"],
             "sidecar_sha256": identity["bound_sidecar_sha256"],
             "catalog_sha256": digest, "projection_sha256": projection_sha256,
             "acknowledged_at": reader_lifecycle.now_iso()}
    payload = {"version": 1, "kind": "reader-gc-acknowledgments", "current": entry}
    if apply:
        assert_writer(store)
        if store.digest(ASSETS, "reader-index/reader_catalog.json") != digest:
            raise ValueError("catalog changed during acknowledgment")
        store.put_bytes(ASSETS, ACKNOWLEDGMENTS, encode(payload))
        if json.loads(store.read_bytes(ASSETS, ACKNOWLEDGMENTS)) != payload:
            raise ValueError("acknowledgment readback failed")
    return {**payload, "applied": apply}


def compact_legacy_catalog(store, *, apply=False, grace_days=0):
    """Shrink the base catalog once both deployments are proven on its current generation.

    This runs in the ordinary publish path so the 2 GB object does not need a
    drained GC window to stay bounded. It only rewrites catalog metadata and
    retired tombstones; content objects are still collected exclusively by the
    drained collector. Refuses when the acknowledgment receipt does not match the
    exact current generation and catalog digest.
    """
    try:
        from .reader_gc_stream import catalog_identity, compact_catalog_stream
    except ImportError:
        from reader_gc_stream import catalog_identity, compact_catalog_stream
    path = "reader-index/reader_catalog.json"
    identity, digest = store.read_graph_json(ASSETS, path, parser=catalog_identity)
    if identity.get('redirect_sidecar_pending'):
        return {'applied': False, 'reason': 'sidecar-reconciliation-pending'}
    try:
        receipt = json.loads(store.read_bytes(ASSETS, ACKNOWLEDGMENTS))
    except FileNotFoundError:
        return {"applied": False, "reason": "no-acknowledgment-receipt"}
    current = receipt.get("current") or {}
    if (receipt.get("version") != 1 or receipt.get("kind") != "reader-gc-acknowledgments"
            or current.get("generation") != identity["current"]
            or current.get("sidecar_sha256") != identity["bound_sidecar_sha256"]
            or current.get("catalog_sha256") != digest):
        return {"applied": False, "reason": "stale-acknowledgment-receipt"}
    if hashlib.sha256(store.read_bytes(
            ASSETS, "reader-index/reader_assets.json.gz")).hexdigest() != current.get("projection_sha256"):
        # A newer sidecar may reference objects that only older generations
        # list; the receipt no longer describes what is deployed.
        return {"applied": False, "reason": "stale-acknowledgment-receipt"}

    def build(chunks, compressed):
        return compact_catalog_stream(chunks, compressed, current=identity["current"],
                                      acknowledged=True, grace_days=grace_days)

    encoded, _source_digest, stats = store.read_graph_json(ASSETS, path, parser=build)
    wire = hashlib.sha256(encoded).hexdigest()
    report = {"applied": apply, "before_digest": digest,
              "after_digest": wire, "after_bytes": len(encoded),
              "generations": stats["kept"], "retired": stats["dropped"]}
    if not stats["dropped"]:
        report["unchanged"] = True
        return report
    if apply:
        assert_writer(store)
        if store.digest(ASSETS, path) != digest:
            raise ValueError("catalog changed during compaction")
        if hashlib.sha256(store.read_bytes(
                ASSETS, "reader-index/reader_assets.json.gz")).hexdigest() != current.get("projection_sha256"):
            # A newer sidecar may already reference objects that only older
            # generations list; never retire those references mid-publication.
            raise ValueError("canonical sidecar changed during compaction")
        store.put_bytes(ASSETS, path, encoded)
        if store.digest(ASSETS, path) != wire:
            raise ValueError("compacted catalog readback mismatch")
    return report


def acknowledge_all(store, client, *, apply=False, timeout_seconds=1200,
                    sleep_fn=time.sleep, clock=time.monotonic):
    """Acknowledge both consumers without turning propagation lag into a publish failure."""
    import httpx

    pending = {"hf", "pages"}
    report = {"applied": apply, "complete": False, "consumers": {},
              "pending": sorted(pending), "timeout_seconds": timeout_seconds}
    if current(store)[0] is None:
        report["reason"] = "no-current-generation"
        return report

    errors = {}
    deadline = clock() + timeout_seconds
    while pending:
        for surface in sorted(pending):
            try:
                receipt = observe_consumer(store, surface, client)
                report["consumers"][surface] = acknowledge(store, surface, receipt, apply=apply)
                pending.remove(surface)
                errors.pop(surface, None)
            except (ValueError, httpx.HTTPError) as error:
                errors[surface] = type(error).__name__
        if not pending:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep_fn(min(20, remaining))

    report["pending"] = sorted(pending)
    report["complete"] = not pending
    if pending:
        report["reason"] = "consumer-projection-timeout"
    if errors:
        report["errors"] = errors
    return report


def verify_generation(store, client, expected, *, timeout_seconds=0, sleep_fn=time.sleep, clock=time.monotonic):
    """Observe both deployed projections without writing GC acknowledgments."""
    import httpx
    pointer, _ = current(store)
    if not pointer or pointer['generation'] != text.sha(expected):
        raise ValueError('v3 generation changed before verification')
    pending, receipts, errors = {'hf', 'pages'}, {}, {}
    deadline = clock() + max(0, timeout_seconds)
    while pending:
        for surface in sorted(pending):
            try:
                receipt = observe_consumer(store, surface, client)
                if receipt.get('generation') != expected or receipt.get('projection_verified') is not True:
                    raise ValueError('unexpected consumer generation')
                receipts[surface] = receipt
                pending.remove(surface)
                errors.pop(surface, None)
            except (ValueError, httpx.HTTPError, OSError) as error:
                errors[surface] = type(error).__name__
        if current(store)[0] != pointer:
            raise ValueError('v3 generation changed during verification')
        if pending:
            remaining = deadline - clock()
            if remaining <= 0:
                raise RuntimeError(f'consumer verification incomplete: {errors}')
            sleep_fn(min(20, remaining))
    return {'generation': expected, 'complete': True, 'consumers': receipts, 'objects_deleted': False}


def project_sidecar(sidecar, catalog):
    validate_catalog(catalog)
    if sidecar.get("v") != 1 or not isinstance(sidecar.get("f"), dict):
        raise ValueError("invalid legacy sidecar")
    result = copy.deepcopy(sidecar)
    for key, entry in catalog["files"].items():
        result["f"][key] = {"s": 2, "m": "p", "p": entry["resource"]["path"],
                            "b": shared.PDF_PAGES_BUCKET}
    raw = encode(catalog)
    result["v3"] = {"generation": catalog["generation"], "catalog_sha256": hashlib.sha256(raw).hexdigest(),
                    "files": len(catalog["files"])}
    return result


class CentralHubStore(HubBucketStore):
    def put_files(self, bucket, additions):
        shared.batch_bucket_files_with_retry(bucket, additions, self.token(bucket))

    def assert_serialized_writer(self, protocol):
        try:
            from .reader_producer_scope import load_config
        except ImportError:
            from reader_producer_scope import load_config
        expected = load_config()["publisher_repository"]
        if (protocol != PROTOCOL or os.environ.get("GITHUB_ACTIONS") != "true"
                or os.environ.get("GITHUB_REPOSITORY") != expected
                or not os.environ.get("GITHUB_RUN_ID")
                or os.environ.get("GITHUB_WORKFLOW") != "Reader/桶 · Publish v3 Reading Generation"
                or os.environ.get("GITHUB_WORKFLOW_REF") != expected + "/.github/workflows/reader-v3-publish.yml@refs/heads/main"
                or os.environ.get("READER_V3_WRITE_PROTOCOL") != PROTOCOL):
            raise RuntimeError("v3 mutation requires the central serialized workflow")



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("withdraw", "ack", "ack-all", "acknowledge-legacy",
                                            "compact-legacy", "inspect", "project", "retire",
                                            "unify", "reset-documents", "remove-route", "restore-route", "verify-route",
                                            "migrate-derived", "resume-derived", "redirect-derived", "reconcile-derived-index", "reconcile-sidecar", "compact-legacy-history", "publish-generation", "verify-generation", "check-page-window"))
    parser.add_argument("--resource-json")
    parser.add_argument("--expected-parent", default="none")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--surface", choices=("hf", "pages"))
    parser.add_argument("--route-action", choices=("remove-route", "restore-route"), default="remove-route")
    parser.add_argument("--wait-seconds", type=int, default=0, help="maximum time to wait for route propagation")
    args = parser.parse_args()
    store = CentralHubStore()
    if args.command == 'publish-generation':
        if not args.apply or args.expected_parent == 'none':
            parser.error('publish-generation requires --apply and --expected-parent')
        assert_writer(store)
        pointer, _ = current(store)
        if not pointer or pointer['generation'] != text.sha(args.expected_parent):
            raise ValueError('v3 generation changed before republication')
        report = {'applied': True, 'generation': pointer['generation'], 'objects_deleted': False}
    elif args.command == 'verify-generation':
        import httpx
        if args.expected_parent == 'none':
            parser.error('verify-generation requires --expected-parent')
        expected = text.sha(args.expected_parent)
        if not 0 <= args.wait_seconds <= 1200:
            parser.error('--wait-seconds must be between 0 and 1200')
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            report = verify_generation(store, client, expected, timeout_seconds=args.wait_seconds)
    elif args.command == 'check-page-window':
        try:
            from . import reader_page_migrate as migrate
        except ImportError:
            import reader_page_migrate as migrate
        options = json.loads(args.resource_json or '{}')
        if not isinstance(options, dict) or options.get('external_writers_stopped') is not True:
            raise ValueError('page publication requires external-writer confirmation')
        os.environ['READER_PAGE_EXTERNAL_WRITERS_STOPPED'] = 'true'
        migrate.require_writer_window(store)
        report = {'github_producers_drained': True, 'external_bucket_credentials_verified': False}
    elif args.command == 'resume-derived':
        try:
            from . import reader_page_migrate as migrate
        except ImportError:
            import reader_page_migrate as migrate
        from huggingface_hub import HfApi
        options = json.loads(args.resource_json or '{}')
        if (not args.apply or not isinstance(options, dict)
                or set(options) != {'transaction_id', 'external_writers_stopped'}
                or options['external_writers_stopped'] is not True):
            raise ValueError('resume-derived requires an exact transaction and external-writer confirmation')
        os.environ['READER_PAGE_EXTERNAL_WRITERS_STOPPED'] = 'true'
        migrate.require_writer_window(store)
        try:
            from .reader_page_cas import ConditionalPageStore
        except ImportError:
            from reader_page_cas import ConditionalPageStore
        store = ConditionalPageStore(store)
        store.preflight()
        report = migrate.resume(store, HfApi(token=os.environ.get('HF_TOKEN')), options['transaction_id'])
    elif args.command == 'redirect-derived':
        try:
            from . import reader_page_atomic as redirect
            from .reader_page_cas import ConditionalPageStore
        except ImportError:
            import reader_page_atomic as redirect
            from reader_page_cas import ConditionalPageStore
        from huggingface_hub import HfApi
        options = json.loads(args.resource_json or '{}')
        expected = {'index_sha256', 'sidecar_sha256', 'v3_generation'}
        if not isinstance(options, dict) or set(options) != expected:
            raise ValueError('redirect-derived requires exact index, sidecar and v3 identities')
        if args.apply:
            assert_writer(store)
            store = ConditionalPageStore(store)
            store.preflight()
        api = HfApi(token=os.environ.get('HF_TOKEN'))
        plan = redirect.prepare(store, api, expected_index=options['index_sha256'],
                                expected_sidecar=options['sidecar_sha256'], expected_v3=options['v3_generation'])
        report = redirect.apply(store, api, plan) if args.apply else redirect.report(plan, applied=False)
    elif args.command == 'reconcile-derived-index':
        try:
            from . import reader_page_reconcile as reconcile
            from .reader_page_cas import ConditionalPageStore
        except ImportError:
            import reader_page_reconcile as reconcile
            from reader_page_cas import ConditionalPageStore
        from huggingface_hub import HfApi
        options = json.loads(args.resource_json or '{}')
        if not isinstance(options, dict) or set(options) != {'index_sha256', 'v3_generation'}:
            raise ValueError('reconcile-derived-index requires exact PDF index and v3 identities')
        if args.apply:
            assert_writer(store)
            store = ConditionalPageStore(store)
            store.preflight()
        api = HfApi(token=os.environ.get('HF_TOKEN'))
        plan = reconcile.prepare(store, api, expected_index=options['index_sha256'],
                                 expected_generation=options['v3_generation'])
        report = reconcile.apply(store, api, plan) if args.apply else reconcile.result(plan, applied=False)
    elif args.command == 'reconcile-sidecar':
        try:
            from . import reader_sidecar_reconcile as sidecar
            from .reader_page_cas import ConditionalPageStore
        except ImportError:
            import reader_sidecar_reconcile as sidecar
            from reader_page_cas import ConditionalPageStore
        from huggingface_hub import HfApi
        options = json.loads(args.resource_json or '{}')
        identities = {'index_sha256', 'sidecar_sha256', 'catalog_sha256', 'v3_generation'}
        declared = 'owner_declared_single_writer'
        keys = set(options) if isinstance(options, dict) else set()
        resuming = keys == {'transaction_id', declared}
        if not (keys == identities or keys == identities | {declared} or resuming):
            raise ValueError('reconcile-sidecar requires exact root identities, or a transaction_id to resume')
        if (args.apply or resuming) and options.get(declared) is not True:
            raise ValueError('writing requires the owner declaration owner_declared_single_writer=true')
        if resuming and not args.apply:
            raise ValueError('resuming a sidecar transaction requires --apply')
        if args.apply:
            os.environ['READER_OWNER_DECLARED_SINGLE_WRITER'] = 'true'
            assert_writer(store)
            store = ConditionalPageStore(store)
            store.preflight()
        api = HfApi(token=os.environ.get('HF_TOKEN'))
        if resuming:
            report = sidecar.resume(store, api, options['transaction_id'])
        else:
            value = sidecar.prepare(store, api, expected_index=options['index_sha256'],
                                    expected_sidecar=options['sidecar_sha256'],
                                    expected_catalog=options['catalog_sha256'],
                                    expected_generation=options['v3_generation'])
            report = sidecar.apply(store, api, value) if args.apply else sidecar.preview(value)
        if args.apply:
            report = {**report, 'owner_declared_single_writer': True, 'external_credentials_verified': False}
    elif args.command == 'compact-legacy-history':
        try:
            from . import reader_legacy_compact as legacy_compact
            from .reader_page_cas import ConditionalPageStore
        except ImportError:
            import reader_legacy_compact as legacy_compact
            from reader_page_cas import ConditionalPageStore
        options = json.loads(args.resource_json or '{}')
        identities = {'v3_generation', 'catalog_sha256'}
        declared = 'owner_declared_single_writer'
        keys = set(options) if isinstance(options, dict) else set()
        resuming = keys == {'transaction_id', declared}
        if not (keys == identities or keys == identities | {declared} or resuming):
            raise ValueError('compact-legacy-history requires exact v3 and catalog identities, or a transaction_id to resume')
        if (args.apply or resuming) and options.get(declared) is not True:
            raise ValueError('writing requires the owner declaration owner_declared_single_writer=true')
        if resuming and not args.apply:
            raise ValueError('resuming a legacy compaction requires --apply')
        if args.apply:
            os.environ['READER_OWNER_DECLARED_SINGLE_WRITER'] = 'true'
            assert_writer(store)
            store = ConditionalPageStore(store)
            store.preflight()
        if resuming:
            report = legacy_compact.resume(store, options['transaction_id'])
        else:
            value = legacy_compact.prepare(store, expected_generation=options['v3_generation'],
                                           expected_catalog=options['catalog_sha256'])
            report = legacy_compact.apply(store, value) if args.apply else legacy_compact.preview(value)
        if args.apply:
            report = {**report, 'owner_declared_single_writer': True, 'external_credentials_verified': False}
    elif args.command == 'migrate-derived':
        try:
            from . import reader_page_migrate as migrate
        except ImportError:
            import reader_page_migrate as migrate
        from huggingface_hub import HfApi
        options = json.loads(args.resource_json or '{}')
        expected = {'index_sha256', 'sidecar_sha256', 'v3_generation'}
        if args.apply:
            expected.add('external_writers_stopped')
        if (not isinstance(options, dict)
                or (set(options) != expected and (args.apply or set(options) != expected | {'check_s3'}))
                or args.apply and options['external_writers_stopped'] is not True
                or not args.apply and 'check_s3' in options and options['check_s3'] is not True):
            raise ValueError('migrate-derived requires exact root identities, optional check_s3=true for preview, and external-writer confirmation for apply')
        if args.apply:
            os.environ['READER_PAGE_EXTERNAL_WRITERS_STOPPED'] = 'true'
            migrate.require_writer_window(store)
        if args.apply or options.get('check_s3') is True:
            try:
                from .reader_page_cas import ConditionalPageStore
            except ImportError:
                from reader_page_cas import ConditionalPageStore
            store = ConditionalPageStore(store)
            store.preflight()
        api = HfApi(token=os.environ.get('HF_TOKEN'))
        plan = migrate.prepare(store, api, expected_index=options['index_sha256'],
                               expected_sidecar=options['sidecar_sha256'], expected_v3=options['v3_generation'])
        if args.apply:
            report = migrate.apply(store, api, plan)
        else:
            report = {'applied': False, 'migrated_entries': len(plan['audit']['planned']),
                      'changed_routes': plan['changed_routes'], 'base_generation': plan['generation'],
                      'previous_v3_generation': plan['pointer']['generation'], 'objects_deleted': False,
                      'unindexed_legacy_pdfs': len(plan['audit']['unindexed_legacy_pdfs']),
                      'unclassified_object_pdfs': len(plan['audit']['unclassified_object_pdfs']),
                      **({'s3_roots_verified': True} if options.get('check_s3') is True else {})}
    elif args.command in {"remove-route", "restore-route", "verify-route"}:
        try:
            from .reader_route_control import change, verify_consumers
        except ImportError:
            from reader_route_control import change, verify_consumers
        options = json.loads(args.resource_json or '{}')
        if args.expected_parent == 'none':
            parser.error('route operation requires --expected-parent')
        expected = text.sha(args.expected_parent)
        if args.command == 'verify-route':
            if not 0 <= args.wait_seconds <= 1200:
                parser.error('--wait-seconds must be between 0 and 1200')
            import httpx
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                report = verify_consumers(store, client, options, expected, timeout_seconds=args.wait_seconds,
                                          action='remove' if args.route_action == 'remove-route' else 'restore')
        else:
            _, catalog = current(store)
            action = 'remove' if args.command == 'remove-route' else 'restore'
            if catalog.get('derived_pdf_redirects'):
                try:
                    from . import reader_page_refresh as refresh
                    from .reader_page_cas import ConditionalPageStore
                except ImportError:
                    import reader_page_refresh as refresh
                    from reader_page_cas import ConditionalPageStore
                from huggingface_hub import HfApi
                if args.apply:
                    assert_writer(store)
                    store = ConditionalPageStore(store)
                    store.preflight()
                report = refresh.run(store, HfApi(token=os.environ.get('HF_TOKEN')),
                                     expected_parent=expected, action=action, options=options, apply=args.apply)
            else:
                report = change(store, options, expected, action=action, apply=args.apply)
    elif args.command == "reset-documents":
        try:
            from .reader_document_reset import withdraw as reset_documents, DocumentProducerFence
        except ImportError:
            from reader_document_reset import withdraw as reset_documents, DocumentProducerFence
        if args.apply:
            store.assert_serialized_writer(PROTOCOL)
            DocumentProducerFence().check()
        report = reset_documents(store, json.loads(args.resource_json or '{}'), apply=args.apply)
    elif args.command == "unify":
        _, current_catalog = current(store)
        if current_catalog.get('derived_pdf_redirects'):
            try:
                from . import reader_page_refresh as refresh
                from .reader_page_cas import ConditionalPageStore
            except ImportError:
                import reader_page_refresh as refresh
                from reader_page_cas import ConditionalPageStore
            from huggingface_hub import HfApi
            if args.apply:
                assert_writer(store)
                store = ConditionalPageStore(store)
                store.preflight()
            report = refresh.run(store, HfApi(token=os.environ.get('HF_TOKEN')), apply=args.apply)
        else:
            report = unify(store, apply=args.apply)
    elif args.command == "withdraw":
        report = withdraw(store, json.loads(args.resource_json or "{}"),
                          None if args.expected_parent == "none" else text.sha(args.expected_parent), apply=args.apply)
    elif args.command == "retire":
        try:
            from .reader_v3_retirement import run
        except ImportError:
            from reader_v3_retirement import run
        report = run(store, apply=args.apply)
    elif args.command == "ack-all":
        import httpx
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            report = acknowledge_all(store, client, apply=args.apply)
    elif args.command == "acknowledge-legacy":
        report = record_legacy_acknowledgments(store, apply=args.apply)
    elif args.command == "compact-legacy":
        report = compact_legacy_catalog(store, apply=args.apply)
    elif args.command == "ack":
        if not args.surface:
            parser.error("ack requires --surface")
        import httpx
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            receipt = observe_consumer(store, args.surface, client)
        report = acknowledge(store, args.surface, receipt, apply=args.apply)
    elif args.command == "inspect":
        pointer, catalog = current(store)
        report = {"pointer": pointer, "files": len(catalog["files"])}
    else:
        try:
            from .reader_bucket import read_bytes
        except ImportError:
            from reader_bucket import read_bytes
        import gzip
        pointer, catalog = current(store)
        base = json.loads(gzip.decompress(read_bytes("reader-index/reader_assets.json.gz", os.environ.get("HF_TOKEN"))))
        report = project_sidecar(base, catalog) if pointer else base
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(encode(report))
    else:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
