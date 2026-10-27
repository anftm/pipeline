#!/usr/bin/env python3
"""Read-only, bucket-qualified inventory and dependency graph for Reader GC."""

from __future__ import annotations

import argparse
import bisect
import functools
import gzip
import hashlib
import json
import posixpath
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from .reader_bucket_store import HubBucketStore, S3BucketStore
    from .shared import READER_ASSETS_BUCKET, PDF_PAGES_BUCKET, PDF_OCR_INPUT_BUCKET
    from . import reader_lifecycle, ccrd_lifecycle
    from .reader_gc_stream import catalog_json
except ImportError:
    from reader_bucket_store import HubBucketStore, S3BucketStore
    from shared import READER_ASSETS_BUCKET, PDF_PAGES_BUCKET, PDF_OCR_INPUT_BUCKET
    import reader_lifecycle
    import ccrd_lifecycle
    from reader_gc_stream import catalog_json


READER_BUCKETS = (READER_ASSETS_BUCKET, PDF_PAGES_BUCKET, PDF_OCR_INPUT_BUCKET)
BUCKETS = (*READER_BUCKETS, ccrd_lifecycle.BUCKET)
CATALOG_REF = (READER_ASSETS_BUCKET, "reader-index/reader_catalog.json")
RETIREMENT_REF = (READER_ASSETS_BUCKET, "reader-index/v3/retirement.json")
ACKED = {"hf": True, "pages": True}
MANAGED_PREFIXES = (
    "objects/", "ebook-chapters/", "staging/", "pages/", "documents/",
    "chapters/", "media/", "native/", "derived/",
    "reader-index/snapshots/",
)
# Run journals are the audit trail of the collector itself, mirrored as Actions
# artifacts. Control receipts are small point-in-time records. They are
# deliberately retained even though no root references them.
RETAINED_PREFIXES = ("reader-index/gc/", "reader-index/reader_gc_acknowledgments.json")
PATH_FIELDS = {
    "path", "paths", "object", "fallback", "manifest", "manifest_path", "root",
    "new_path", "reader_assets_path", "ocr_manifest", "p", "o", "i", "w", "j", "r",
    "c", "f", "pd", "document", "stream", "text", "resources", "inputs", "outputs",
}
BUCKET_FIELDS = {
    "p": "b", "o": "ob", "c": "cb", "f": "fb", "pd": "pdb",
    "i": "ibucket", "w": "wbucket", "j": "jbucket", "r": "rbucket",
    "reader_assets_path": "reader_assets_bucket",
}
MANIFEST_KINDS = {
    "pdf-pages", "image-page-stream", "image-pages", "pdf-render", "pdf-render-range",
    "pdf-ocr", "ebook-chapters", "pdf-derived-source", "office-document-stream",
    "spreadsheet-html-stream", "text-document-stream", "web-document-stream",
    "static-pdf-stream", "pdf-document-stream", "static-pdf-document-stream", "epub-chapters",
    "pdf-text-layer-index", "pdf-text-partition", "pdf-text-review",
    "pdf-reading", "pdf-preview-partition",
}


def managed_prefixes(bucket: str) -> tuple[str, ...]:
    return ccrd_lifecycle.MANAGED_PREFIXES if bucket == ccrd_lifecycle.BUCKET else MANAGED_PREFIXES


def is_root(path: str, bucket: str | None = None) -> bool:
    if bucket == ccrd_lifecycle.BUCKET:
        return ccrd_lifecycle.is_root(path)
    name = posixpath.basename(path)
    if path.startswith((
            "reader-index/generations/", "reader-index/snapshots/") + RETAINED_PREFIXES):
        return False
    return (path.startswith("reader-index/") and path.endswith((".json", ".json.gz"))) or (
        name == "index.json" or name.startswith("index-") and name.endswith(".json"))


def is_manifest(path: str) -> bool:
    name = posixpath.basename(path)
    return name.endswith("-manifest.json") or name.startswith("render-range-") and name.endswith(".json")


def decode(raw: bytes) -> dict:
    payload = json.loads(gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


class ReferenceGraph:
    def __init__(self, store, include_input: bool = True, generation_grace_days: int = 0,
                 progress=None, workers: int = 8, max_rounds: int = 20, full_passes: int = 3,
                 deadline: float | None = None, processing_supersessions=None):
        self.store = store
        self.include_input = include_input
        self.files = {}
        self.ordered = {}
        self.references = set()
        self.pending = set()
        self.seen = set()
        self.root_hashes = {}
        self.blockers = set()
        self.missing = set()
        self.ambiguous = set()
        self.trees = set()
        self.generation_grace_days = generation_grace_days
        self.catalog = {}
        self.retired_roots = set()
        self.retired_inputs = set()
        self.retired_input_roots = set()
        self.ignored_retired_pins = set()
        self.progress = progress or (lambda *args, **kwargs: None)
        self.workers = max(1, workers)
        self.max_rounds = max(1, max_rounds)
        self.full_passes = max(1, full_passes)
        self.deadline = deadline
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.origin = None
        # Per-origin facts are replaced whenever that origin is re-read, so a
        # root that changes during the scan is judged by its latest version.
        self.direct = {}
        self.handoffs = {}
        self.unreadable = {}
        self.failures = Counter()
        self.deferred = set()
        self.catalog_ids = set()
        self.retired_acked = set()
        self.fingerprints = {}
        self.root_names = {}
        self.expected_resources = {}
        self.loaded_digests = {}
        self.resource_warnings = []
        self.external_pins = set()
        self.externally_traversed = set()
        self.active_external_pin = False
        self.released_processing = {}
        self.processing_supersessions = processing_supersessions or {}

    def read_catalog(self, ref):
        parser = functools.partial(catalog_json, grace_days=self.generation_grace_days)
        if hasattr(self.store, "read_graph_json"):
            return self.store.read_graph_json(*ref, parser=parser)
        raw = self.store.read_bytes(*ref)
        return parser([raw], raw.startswith(b"\x1f\x8b"))

    def load(self, ref: tuple[str, str]) -> dict | None:
        try:
            if ref == CATALOG_REF:
                payload, digest = self.read_catalog(ref)
            elif hasattr(self.store, "read_graph_json"):
                payload, digest = self.store.read_graph_json(*ref)
            else:
                raw = self.store.read_bytes(*ref)
                payload, digest = decode(raw), hashlib.sha256(raw).hexdigest()
        except Exception as error:
            # A permissions/transport failure must never resemble an empty root.
            status = getattr(getattr(error, "response", None), "status_code", None)
            detail = f" HTTP {status}" if status else ""
            with self.lock:
                self.unreadable[ref] = f"unreadable JSON: {ref[0]}:{ref[1]} ({type(error).__name__}{detail})"
                self.failures[ref] += 1
            return None
        with self.lock:
            self.unreadable.pop(ref, None)
            self.loaded_digests[ref] = digest
            if is_root(ref[1], ref[0]):
                self.root_hashes[ref] = digest
                etag = getattr(self.store, "read_etags", {}).get(ref)
                self.fingerprints[ref] = ("etag", etag) if etag else ("sha256", digest)
        return payload

    def note_checksum(self, ref, expected) -> bool:
        """Record a retained-resource drift; only runtime roles block.

        Provenance bytes (a converted document manifest) are advisory: the
        export's text and document identity are verified independently, so a
        rewritten manifest must not mark the whole graph incomplete.
        """
        # Only Reader-sidecar provenance is advisory. Anything inside the CCRD
        # bucket is an authoritative lifecycle document: a checksum mismatch
        # there can hide a corrupted release and must fail closed.
        if expected.get("role") == "provenance" and ref[0] != ccrd_lifecycle.BUCKET:
            with self.lock:
                self.resource_warnings.append(f"provenance checksum drifted: {ref[0]}:{ref[1]}")
            return False
        self.blockers.add(f"retained resource checksum mismatch: {ref[0]}:{ref[1]}")
        return True

    def handoff_resolved(self, generation) -> bool:
        return isinstance(generation, str) and (
            generation in self.catalog_ids or generation in self.retired_acked)

    def merge_catalog(self, payload: dict):
        self.catalog = payload
        self.catalog_ids.update(name for name in payload.get("generations", {}) if isinstance(name, str))
        for name, entry in (payload.get("retired_generations") or {}).items():
            if (isinstance(entry, dict) and entry.get("replacement_generation")
                    and entry.get("replacement_acks") == ACKED):
                self.retired_acked.add(name)

    def merge_retirement(self, state: dict):
        for path, item in state.get("retired_inputs", {}).items():
            if (not re.fullmatch(r"objects/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{16}/ocr-input/page-[0-9]{6}\.png", path)
                    or not isinstance(item, dict) or item.get("path") != path
                    or item.get("bucket") != PDF_OCR_INPUT_BUCKET or item.get("source_sha256") != path.split("/")[2]):
                self.blockers.add("invalid retired OCR input identity")
            else:
                self.retired_inputs.add((PDF_OCR_INPUT_BUCKET, path))
        for root, item in (state.get("retired_input_roots") or {}).items():
            match = re.fullmatch(r"objects/[0-9a-f]{2}/([0-9a-f]{64})/[0-9a-f]{16}/ocr-input", root)
            if (not match or not isinstance(item, dict) or item.get("root") != root
                    or item.get("bucket") != PDF_OCR_INPUT_BUCKET or item.get("source_sha256") != match[1]):
                self.blockers.add("invalid retired OCR input root")
            else:
                released_at = item.get('released_at')
                if released_at:
                    try:
                        released = datetime.fromisoformat(str(released_at).replace('Z', '+00:00'))
                        if released.tzinfo is None:
                            raise ValueError('release time needs timezone')
                    except ValueError:
                        self.blockers.add('invalid retired OCR input release time')
                        continue
                    if datetime.now(timezone.utc) - released < timedelta(days=30):
                        continue
                self.retired_input_roots.add(root)
        for path in state["retired_paths"]:
            if not isinstance(path, str) or not re.fullmatch(
                    r"reader-index/(?:v3/(?:promotions/[0-9a-f]{64}\.json|generations/[0-9a-f]{64}/catalog\.json|candidates/[0-9a-f]{64}\.json)|processing/(?:v3|djvu|office-chapters)-[0-9a-f]{32}\.json)", path):
                self.blockers.add("invalid retired root path")
            else:
                self.retired_roots.add((READER_ASSETS_BUCKET, path))

    def process(self, ref, payload):
        self.origin = ref
        self.active_external_pin = ref in self.external_pins
        if self.active_external_pin:
            self.externally_traversed.add(ref)
        self.direct[ref] = set()
        self.handoffs.pop(ref, None)
        self.released_processing.pop(ref, None)
        try:
            if not self.validate(payload, *ref):
                return
            if ref == CATALOG_REF:
                self.merge_catalog(payload)
            elif ref == RETIREMENT_REF:
                self.merge_retirement(payload)
            self.walk(payload, *ref)
        finally:
            self.origin = None
            self.active_external_pin = False

    def input_retired(self, ref) -> bool:
        if ref in self.retired_inputs:
            return True
        bucket, path = ref
        return (bucket == PDF_OCR_INPUT_BUCKET and path.endswith(".png")
                and path.rsplit("/", 1)[0] in self.retired_input_roots)

    def mark(self, bucket: str, path: str, *, tree: bool = False, external_pin: bool = False):
        if bucket not in BUCKETS:
            self.blockers.add(f"unsupported reference bucket: {bucket}")
            return
        if (not path or path.startswith("/") or "\\" in path or "\0" in path
                or any(part in {".", "..", ""} for part in path.split("/"))):
            self.blockers.add(f"invalid object path: {bucket}:{path}")
            return
        ref = (bucket, path)
        if external_pin or self.active_external_pin:
            self.external_pins.add(ref)
        if self.input_retired(ref) and ref not in self.external_pins and not tree:
            return
        self.references.add(ref)
        if not tree and self.origin is not None:
            self.direct.setdefault(self.origin, set()).add(ref)
        if bucket not in self.files:
            self.blockers.add(f"referenced bucket was not inventoried: {bucket}")
            return
        if path not in self.files[bucket] and not tree:
            # Judged against the final inventory; may also appear later.
            self.deferred.add(ref)
            return
        graph_document = (ccrd_lifecycle.is_document(path) if bucket == ccrd_lifecycle.BUCKET else
                          is_manifest(path) or is_root(path) or path.startswith("reader-index/snapshots/"))
        if graph_document and ref not in self.seen:
            self.pending.add(ref)
        elif graph_document and ref in self.external_pins and ref not in self.externally_traversed:
            self.seen.discard(ref)
            self.pending.add(ref)
        if bucket == ccrd_lifecycle.BUCKET:
            return
        # HTML/CSS/XHTML, fonts, media companions and manifest siblings are an
        # immutable bundle. Keep the whole bundle instead of parsing markup.
        root = path if tree else posixpath.dirname(path)
        if root and not is_root(path):
            if (bucket, root) in self.trees:
                return
            self.trees.add((bucket, root))
            prefix = root + "/"
            ordered = self.ordered[bucket]
            start = bisect.bisect_left(ordered, prefix)
            end = bisect.bisect_left(ordered, prefix + "\U0010ffff")
            for child in ordered[start:end]:
                child_ref = (bucket, child)
                self.references.add(child_ref)
                if self.active_external_pin or external_pin:
                    self.external_pins.add(child_ref)
                if is_manifest(child) and child_ref not in self.seen:
                    self.pending.add(child_ref)

    def path(self, value: str, bucket: str, origin: str, explicit: bool, field: str):
        if is_manifest(origin) and field in PATH_FIELDS:
            relative = posixpath.join(posixpath.dirname(origin), value)
            if value.startswith(("pages/page-", "chapters/chapter-", "resources/")) or (
                    relative in self.files.get(bucket, set())):
                self.mark(bucket, relative, tree=field == "root")
                return
        if value.startswith(MANAGED_PREFIXES) or value.startswith("reader-index/"):
            if explicit:
                self.mark(bucket, value, tree=field == "root")
                return
            matches = [candidate for candidate, files in self.files.items()
                       if value in files or field == "root" and any(p.startswith(value + "/") for p in files)]
            if len(matches) > 1:
                self.ambiguous.add(value)
            if not matches:
                if "/ocr-input/" in value:
                    bucket = PDF_OCR_INPUT_BUCKET
                elif value.lower().endswith('.jxl'):
                    bucket = PDF_PAGES_BUCKET
                self.mark(bucket, value, tree=field == "root")
            for candidate in matches:
                self.mark(candidate, value, tree=field == "root")
        elif field in PATH_FIELDS and is_manifest(origin) and not value.startswith(("https:", "http:")):
            # Only manifests use bundle-relative paths. Index `path` fields
            # commonly describe upstream source files and are not object keys.
            self.mark(bucket, posixpath.join(posixpath.dirname(origin), value), tree=field == "root")

    def walk(self, value, bucket: str, origin: str, explicit: bool = False, field: str = ""):
        if bucket == ccrd_lifecycle.BUCKET and not field:
            for ref in ccrd_lifecycle.references(value, origin):
                key = ref['bucket'], ref['path']
                previous = self.expected_resources.get(key)
                if previous and (previous['sha256'], previous['bytes']) != (ref['sha256'], ref['bytes']):
                    self.blockers.add(f"conflicting retained resource identity: {key[0]}:{key[1]}")
                    continue
                self.expected_resources[key] = ref
                if key in self.loaded_digests and self.loaded_digests[key] != ref['sha256']:
                    self.note_checksum(key, ref)
                self.mark(*key, external_pin=True)
            return
        if isinstance(value, dict):
            if value.get("kind") == "reader-v3-retirement":
                self.walk(value.get("resources", []), bucket, origin, explicit, "resources")
                return
            if value.get("kind") == "reader-v3-automation":
                for task in value.get("tasks", {}).values():
                    candidate = task.get("candidate") or {}
                    if task.get("status") == "published" and (candidate.get("bucket"), candidate.get("path")) in self.retired_roots:
                        continue
                    task = dict(task)
                    spec = dict(task.get("spec") or {})
                    if spec.get("primary_source"):
                        planned = spec.pop("primary", None)
                        if isinstance(planned, dict):
                            target = (planned.get("bucket"), planned.get("path"))
                            if target[1] in self.files.get(target[0], set()):
                                self.walk(planned, bucket, origin, explicit, "primary")
                    task["spec"] = spec
                    self.walk(task, bucket, origin, explicit, "task")
                return
            if origin.endswith("reader_catalog.json") and value.get("kind") == "reader-catalog":
                current = (value.get("current") or {}).get("generation")
                for generation in (value.get("generations") or {}).values():
                    if isinstance(generation, dict) and reader_lifecycle.generation_live(
                            generation, current, grace_days=self.generation_grace_days):
                        self.walk(generation, bucket, origin, explicit, "generation")
                for entry in (value.get('retired_generations') or {}).values():
                    if (isinstance(entry, dict) and entry.get('withdrawal_reset_id')
                            and entry.get('replacement_acks') != ACKED and entry.get('snapshot')):
                        self.walk(entry['snapshot'], bucket, origin, explicit, 'snapshot')
                return
            if origin.startswith("reader-index/processing/") and value.get("status") == "released":
                generation = value.get("generation")
                supersession = self.processing_supersessions.get((bucket, origin))
                if supersession:
                    current = self.catalog.get('current', {}).get('generation')
                    accepted = self.catalog.get('generations', {}).get(current, {})
                    if (supersession.get('record_sha256') != self.loaded_digests.get((bucket, origin))
                            or supersession.get('original_generation') != generation
                            or supersession.get('replacement_generation') != current
                            or supersession.get('sidecar_sha256') != accepted.get('sidecar_sha256')):
                        self.blockers.add(f"processing supersession identity changed: {bucket}:{origin}")
                        supersession = None
                if self.handoff_resolved(generation) or supersession:
                    if value.get('kind') == 'reader-processing-roots':
                        self.released_processing[self.origin or (bucket, origin)] = {
                            'generation': generation, 'sha256': self.loaded_digests.get((bucket, origin)),
                            'operation': value.get('operation'), 'run': value.get('run'),
                            'released_at': value.get('released_at'),
                            **({'supersession': supersession} if supersession else {})}
                    return
                # Resolve against every catalog version read during the scan;
                # until then the root's resources stay protected.
                self.handoffs[self.origin or (bucket, origin)] = generation
            context = value.get("bucket")
            # Review evidence PNGs are pinned metadata only (corrections render
            # from the primary PDF), so a retired input no longer blocks here.
            if context is not None:
                if not isinstance(context, str) or context not in BUCKETS:
                    self.blockers.add(f"invalid bucket in {bucket}:{origin}")
                    return
                bucket, explicit = context, True
            for key, item in value.items():
                if key in {"orphans", "source_url", "source_path", "source", "text", "error",
                           "processing_roots", "observations", "components"}:
                    continue
                if key == "path" and (value.get("kind") in {"pdf-render", "pdf-render-range"} or (
                        value.get("repo") and "new_path" not in value and any(
                            name in value for name in ("object", "manifest", "reader_assets_path")))):
                    continue
                target = value.get(BUCKET_FIELDS.get(key, ""))
                # `ob` also means OCR object bytes in compact page entries.
                if not isinstance(target, str) and key == "o":
                    target = value.get("obucket")
                if key == "o" and not target and origin.endswith("reader_assets.json.gz"):
                    target = value.get("b")
                if key == "p" and isinstance(value.get("b"), str):
                    target = value["b"]
                if key == 'i' and isinstance(item, str) and not target:
                    target = PDF_OCR_INPUT_BUCKET
                if key == 'j' and isinstance(item, str) and not target:
                    matches = [b for b in (PDF_PAGES_BUCKET, PDF_OCR_INPUT_BUCKET) if item in self.files.get(b, set())]
                    target = matches[0] if len(matches) == 1 else PDF_PAGES_BUCKET
                if isinstance(target, str):
                    if target not in BUCKETS:
                        self.blockers.add(f"unsupported reference bucket: {target}")
                        continue
                    self.walk(item, target, origin, True, key)
                else:
                    self.walk(item, bucket, origin, explicit, key)
        elif isinstance(value, list):
            for item in value:
                self.walk(item, bucket, origin, explicit, field)
        elif isinstance(value, str) and (field in PATH_FIELDS or field == "references"):
            self.path(value, bucket, origin, explicit, field)

    def validate(self, payload: dict, bucket: str, path: str) -> bool:
        ref = (bucket, path)
        expected = self.expected_resources.get(ref)
        if expected and self.loaded_digests.get(ref) != expected['sha256']:
            if self.note_checksum(ref, expected):
                return False
        if bucket == ccrd_lifecycle.BUCKET:
            try:
                ccrd_lifecycle.references(payload, path)
            except (ValueError, KeyError, TypeError) as error:
                self.blockers.add(f"invalid CCRD lifecycle schema: {path} ({type(error).__name__})")
                return False
            return True
        if is_root(path, bucket):
            if payload.get("kind") == "reader-v3-retirement":
                if (payload.get("version") != 1 or payload.get("replacement_acks") != {"hf": True, "pages": True}
                        or not isinstance(payload.get("resources"), list)
                        or not isinstance(payload.get("retired_paths"), list)):
                    self.blockers.add(f"invalid retirement state: {bucket}:{path}")
                    return False
            elif payload.get("kind") == "reader-gc-acknowledgments":
                if (payload.get("version") != 1
                        or not isinstance(payload.get("current"), dict)
                        or not isinstance(payload["current"].get("generation"), str)
                        or not isinstance(payload["current"].get("catalog_sha256"), str)):
                    self.blockers.add(f"invalid GC acknowledgment: {bucket}:{path}")
                    return False
            elif payload.get("kind") == "pdf-ocr-progress-index":
                books = payload.get("books")
                migrated = payload.get("migrated_from")
                if (payload.get("version") != 1 or not isinstance(books, dict)
                        or any(not isinstance(key, str) or not isinstance(item, dict)
                               or not isinstance(item.get("generation"), str)
                               or not isinstance(item.get("pages"), list)
                               or any(type(page) is not int or page < 1 for page in item["pages"])
                               for key, item in books.items())
                        or (migrated is not None and (not isinstance(migrated, dict)
                            or migrated.get("path") != "reader-index/pdf_ocr_progress.json"
                            or type(migrated.get("bytes")) is not int or migrated["bytes"] <= 0
                            or not re.fullmatch(r"[0-9a-f]{64}", str(migrated.get("sha256", "")))))):
                    self.blockers.add(f"invalid PDF OCR progress index: {bucket}:{path}")
                    return False
            elif payload.get("kind") == "pdf-ocr-progress-book":
                files = payload.get("files")
                key = payload.get("key")
                entry = files.get(key) if isinstance(files, dict) and isinstance(key, str) else None
                if (payload.get("version") != 1 or not isinstance(key, str) or not key
                        or not isinstance(files, dict) or set(files) != {key}
                        or not isinstance(entry, dict) or not isinstance(entry.get("generation"), str)
                        or not isinstance(entry.get("pages"), dict)
                        or any(not isinstance(page, dict) or str(page.get("p")) != number
                               for number, page in entry["pages"].items())):
                    self.blockers.add(f"invalid PDF OCR progress shard: {bucket}:{path}")
                    return False
            elif payload.get("kind") in {"reader-v3-automation", "reader-v3-correction-state"}:
                if (payload.get("version") != 1 or not isinstance(payload.get("tasks"), dict)
                        or not isinstance(payload.get("days"), dict)
                        or any(not isinstance(task, dict) for task in payload["tasks"].values())):
                    self.blockers.add(f"invalid v3 task state: {bucket}:{path}")
                    return False
            elif payload.get("kind") == "pdf-text-correction":
                try:
                    try:
                        from .pdf_text_layer import sha
                    except ImportError:
                        from pdf_text_layer import sha
                    for field in ("base_generation", "raw_sha256", "page_identity"):
                        sha(payload.get(field))
                    if (payload.get("version") != 1 or type(payload.get("page")) is not int
                            or payload["page"] < 1 or not isinstance(payload.get("replacements"), list)
                            or not isinstance(payload.get("evidence"), dict)):
                        raise ValueError("invalid proposal")
                except (ValueError, TypeError):
                    self.blockers.add(f"invalid v3 correction proposal: {bucket}:{path}")
                    return False
            elif "lifecycle" in posixpath.basename(path):
                if not isinstance(payload.get("orphans", {}), dict):
                    self.blockers.add(f"invalid lifecycle: {bucket}:{path}")
                    return False
            elif path.endswith("reader_assets.json.gz"):
                if payload.get("v") != 1 or not isinstance(payload.get("f"), dict):
                    self.blockers.add(f"invalid sidecar: {bucket}:{path}")
                    return False
            elif path.endswith("reader_catalog.json"):
                if payload.get("version") != 1 or payload.get("kind") != "reader-catalog" \
                        or not isinstance(payload.get("generations"), dict):
                    self.blockers.add(f"invalid catalog: {bucket}:{path}")
                    return False
                current = payload.get("current")
                if not isinstance(current, dict) or current.get("generation") not in payload["generations"]:
                    self.blockers.add(f"missing current catalog generation: {bucket}:{path}")
                    return False
            elif path.endswith("reader_gc_state.json"):
                if payload.get("version") != 1 or not isinstance(payload.get("orphans"), dict):
                    self.blockers.add(f"invalid GC state: {bucket}:{path}")
                    return False
            elif not any(isinstance(payload.get(key), (dict, list)) for key in (
                    "files", "resources", "leases", "generations")):
                self.blockers.add(f"unknown root schema: {bucket}:{path}")
                return False
            for key in ("files", "f", "resources", "leases", "generations"):
                entries = payload.get(key)
                if entries is None:
                    continue
                if not isinstance(entries, (dict, list)):
                    self.blockers.add(f"invalid root entries: {bucket}:{path}")
                    return False
                entries = entries.values() if isinstance(entries, dict) else entries
                if any(not isinstance(entry, dict) for entry in entries):
                    self.blockers.add(f"invalid root entry: {bucket}:{path}")
                    return False
        elif is_manifest(path):
            if payload.get("kind") not in MANIFEST_KINDS:
                self.blockers.add(f"unknown manifest kind: {bucket}:{path}")
                return False
            if payload.get("kind") in {"pdf-text-layer-index", "pdf-reading"}:
                try:
                    try:
                        from .pdf_reading_v3 import validate_text_manifest, validate_reading_manifest
                    except ImportError:
                        from pdf_reading_v3 import validate_text_manifest, validate_reading_manifest
                    validator = validate_text_manifest if payload["kind"] == "pdf-text-layer-index" else validate_reading_manifest
                    validator(payload)
                except (ValueError, KeyError, TypeError):
                    self.blockers.add(f"invalid v3 manifest: {bucket}:{path}")
                    return False
            if payload.get("kind") in {"pdf-pages", "image-page-stream", "image-pages"}:
                count = payload.get("page_count")
                if type(count) is not int or count < 1:
                    self.blockers.add(f"invalid page count: {bucket}:{path}")
                    return False
                if payload["kind"] == "pdf-pages":
                    root = posixpath.dirname(path)
                    codec = payload.get('codec', 'webp')
                    if codec not in {'webp', 'jxl', 'png', 'mixed'}:
                        self.blockers.add(f"unknown page codec: {bucket}:{path}")
                        return False
                    if codec in {'png', 'mixed'}:
                        images = payload.get('images')
                        if (payload.get('source_format') != 'djvu' or not isinstance(images, list)
                                or len(images) != count or any(not isinstance(entry, dict)
                                or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256', '')))
                                or type(entry.get('bytes')) is not int or entry['bytes'] < 1
                                or codec == 'mixed' and entry.get('codec') not in {'png', 'webp', 'jxl'}
                                for entry in images)):
                            self.blockers.add(f'invalid mixed DjVu page resources: {bucket}:{path}')
                            return False
                    for number in range(1, count + 1):
                        page_codec = payload['images'][number - 1]['codec'] if codec == 'mixed' else codec
                        self.mark(bucket, f"{root}/pages/page-{number:06d}.{page_codec}")
        return True

    def inventory(self, bucket):
        self.progress("inventory", bucket=bucket)
        files = self.store.list_files(bucket, ("",))
        self.progress("inventory-complete", bucket=bucket, objects=len(files))
        return files

    def set_inventory(self, bucket, files):
        self.files[bucket] = files
        self.ordered[bucket] = sorted(files)
        self.root_names[bucket] = {path for path in files if is_root(path, bucket)}

    def drain(self, pool):
        # Keep every reader busy: one slow object must not stall a whole batch.
        inflight, checked = {}, 0
        while True:
            while self.pending and len(inflight) < self.workers * 2:
                ref = self.pending.pop()
                if ref in self.seen:
                    continue
                self.seen.add(ref)
                inflight[pool.submit(self.load, ref)] = ref
            if not inflight:
                return
            if self.deadline is not None and time.monotonic() > self.deadline:
                for future in inflight:
                    future.cancel()
                self.pending.update(inflight.values())
                raise TimeoutError("GC scan deadline reached")
            done, _ = wait(inflight, timeout=30, return_when=FIRST_COMPLETED)
            for future in done:
                ref = inflight.pop(future)
                payload = future.result()
                if payload is not None:
                    self.process(ref, payload)
                checked += 1
                if checked % 1024 == 0:
                    print(f"GC graph: checked {len(self.seen)} roots/manifests; {len(self.pending)} pending; "
                          f"{time.monotonic() - self.started:.0f}s", flush=True)
                    self.progress("reference-traversal", checked=len(self.seen), pending=len(self.pending))

    def changed(self, ref) -> bool:
        recorded = self.fingerprints.get(ref)
        if recorded is None:
            return True
        listed = getattr(self.store, "root_etags", {}).get(ref[0], {})
        if recorded[0] == "etag" and ref[1] in listed:
            return listed[ref[1]] != recorded[1]
        try:
            actual = self.store.digest(*ref) if hasattr(self.store, "digest") else (
                hashlib.sha256(self.store.read_bytes(*ref)).hexdigest())
        except FileNotFoundError:
            return True
        except Exception as error:
            self.unreadable[ref] = f"root recheck failed: {ref[0]}:{ref[1]} ({type(error).__name__})"
            self.failures[ref] += 1
            return False
        return actual != self.root_hashes.get(ref)

    def protect_additions(self, bucket, added):
        for path in added:
            parent = posixpath.dirname(path)
            while parent:
                if (bucket, parent) in self.trees:
                    ref = (bucket, path)
                    self.references.add(ref)
                    if is_manifest(path) and ref not in self.seen:
                        self.pending.add(ref)
                    break
                parent = posixpath.dirname(parent)

    def list_roots(self, bucket):
        if bucket == ccrd_lifecycle.BUCKET:
            return {path for path in self.store.list_files(bucket, ('control/', 'current.json'))
                    if is_root(path, bucket)}
        if hasattr(self.store, "list_roots"):
            parents = {posixpath.dirname(path) + "/" for path in self.root_names.get(bucket, set())
                       if not path.startswith("reader-index/") and "/" in path}
            return set(self.store.list_roots(bucket, parents))
        return {path for path in self.store.list_files(bucket, ("",)) if is_root(path, bucket)}

    def root_round(self, pool, round_number) -> bool:
        """Read every listed root at its current version; True when nothing changed."""
        self.drain(pool)
        work = set()
        for bucket in tuple(self.files):
            self.progress("root-inventory-recheck", bucket=bucket, round=round_number)
            roots = self.list_roots(bucket)
            self.root_names[bucket] = roots
            for path in sorted(roots):
                ref = (bucket, path)
                if ref in self.retired_roots:
                    continue
                if ref not in self.seen:
                    work.add(ref)
                    self.references.add(ref)
                elif ref not in self.unreadable and self.changed(ref):
                    work.add(ref)
        work.update(ref for ref in self.unreadable if self.failures[ref] < 3
                    and (ref[1] in self.root_names.get(ref[0], set())
                         or ref[1] in self.files.get(ref[0], set())))
        self.progress("root-content-recheck", roots=len(self.root_hashes), changed=len(work),
                      round=round_number)
        if not work and not self.pending:
            return True
        print(f"GC graph: round {round_number} re-reading {len(work)} new or changed roots; "
              f"{time.monotonic() - self.started:.0f}s", flush=True)
        self.seen.difference_update(work)
        self.pending.update(work)
        return False

    def full_pass(self) -> bool:
        """Refresh object existence; True unless it revealed roots in new locations."""
        new_roots = False
        for bucket in tuple(self.files):
            self.progress("inventory-refresh", bucket=bucket)
            latest = self.store.list_files(bucket, ("",))
            previous, known = self.files[bucket], self.root_names.get(bucket, set())
            self.files[bucket] = latest
            self.ordered[bucket] = sorted(latest)
            self.protect_additions(bucket, latest - previous)
            roots = {path for path in latest if is_root(path, bucket)}
            new_roots |= bool(roots - known)
            self.root_names[bucket] = known | roots
        for ref in list(self.deferred):
            if ref[1] in self.files.get(ref[0], set()):
                self.deferred.discard(ref)
                previous, self.origin = self.origin, None
                self.mark(*ref)
                self.origin = previous
        return not new_roots and not self.pending

    def converge(self, pool) -> bool:
        """Converge on roots with cheap root-only listings, then confirm with a full inventory."""
        rounds = 0
        for _ in range(self.full_passes):
            while True:
                rounds += 1
                if rounds > self.max_rounds:
                    return False
                try:
                    if self.root_round(pool, rounds):
                        break
                except TimeoutError:
                    raise
                except Exception as error:
                    self.blockers.add(f"root inventory recheck failed ({type(error).__name__})")
                    return True
            try:
                if self.full_pass():
                    return True
            except Exception as error:
                self.blockers.add(f"root inventory recheck failed ({type(error).__name__})")
                return True
        return False

    def finalize(self):
        # A completed handoff lets the accepted catalog own the resources. Keep
        # the small audit in the collection plan; retire old processing records
        # across every format instead of repeatedly scanning them forever.
        explicit = set().union(*self.direct.values()) if self.direct else set()
        for ref in self.released_processing:
            if ref not in self.external_pins and ref not in explicit:
                self.retired_roots.add(ref)
                self.references.discard(ref)
        def alive(ref):
            if ref in self.retired_roots and ref not in self.external_pins:
                return False
            if is_root(ref[1], ref[0]):
                return ref[1] in self.root_names.get(ref[0], set())
            return ref[1] in self.files.get(ref[0], set())
        # A journaled OCR input root is a deliberate deletion, not a missing
        # reference. Older catalogs and CCRD releases may still pin those PNGs;
        # the journal supersedes them, so a stale pin must never block the graph.
        self.ignored_retired_pins = {ref for origin, refs in self.direct.items()
                                     for ref in refs if self.input_retired(ref)}
        self.missing = {ref for origin, refs in self.direct.items() if alive(origin)
                         for ref in refs if (not self.input_retired(ref) or ref in self.external_pins)
                        and ref[0] in self.files and ref[1] not in self.files[ref[0]]}
        for ref, message in self.unreadable.items():
            if alive(ref):
                self.blockers.add(message)
        for ref, generation in self.handoffs.items():
            if alive(ref) and not self.handoff_resolved(generation):
                self.blockers.add(f"released root lacks catalog handoff: {ref[0]}:{ref[1]}")

    def build(self) -> dict:
        if not self.include_input:
            self.blockers.add("input bucket inventory was explicitly skipped")
        try:
            self.store.root_predicate = is_root
        except AttributeError:
            pass
        for bucket in BUCKETS:
            if bucket == PDF_OCR_INPUT_BUCKET and not self.include_input:
                continue
            try:
                self.set_inventory(bucket, self.inventory(bucket))
                print(f"GC graph: inventory {bucket} objects={len(self.files[bucket])}; "
                      f"{time.monotonic() - self.started:.0f}s", flush=True)
            except Exception as error:
                self.blockers.add(f"inventory unavailable: {bucket} ({type(error).__name__})")
        required = (READER_ASSETS_BUCKET, "reader-index/reader_assets.json.gz")
        if required[1] not in self.files.get(required[0], set()):
            self.blockers.add("canonical Reader sidecar is missing")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            # The catalog and retirement journal decide handoffs and retired
            # roots, so read them before the processing roots.
            for ref in (CATALOG_REF, RETIREMENT_REF):
                if ref[1] in self.files.get(ref[0], set()):
                    self.references.add(ref)
                    self.seen.add(ref)
                    payload = self.load(ref)
                    if payload is not None:
                        self.process(ref, payload)
            for bucket, roots in self.root_names.items():
                for path in sorted(roots):
                    ref = (bucket, path)
                    if ref not in self.retired_roots and ref not in self.seen:
                        self.references.add(ref)
                        self.pending.add(ref)
            try:
                if not self.converge(pool):
                    self.blockers.add(f"root inventory did not stabilize after {self.max_rounds} rounds")
            except TimeoutError:
                self.blockers.add(f"GC scan deadline reached with {len(self.pending)} unread roots/manifests")
                pool.shutdown(wait=False, cancel_futures=True)
        self.finalize()
        if self.missing:
            self.blockers.add("referenced objects are missing")
        buckets = {}
        complete = not self.blockers
        for bucket, files in self.files.items():
            retired = sorted(path for path in files if (bucket, path) in self.retired_roots
                             and (bucket, path) not in self.references)
            unreferenced = sorted(path for path in files if (bucket, path) not in self.references
                                  and path.startswith(managed_prefixes(bucket)))
            # Run journals are deliberate telemetry: the same bytes are uploaded as
            # an Actions artifact, and the active run keeps writing them. They are
            # neither a content candidate nor an unknown format.
            retained = sorted(path for path in files if path.startswith(RETAINED_PREFIXES))
            # A root the retirement journal explicitly released is not managed by
            # content prefix, but it is still a deliberate, bounded retirement.
            # Without this tier those paths stayed "unmanaged" forever and the GC
            # criterion could not reclaim its own superseded roots.
            reclaimed = sorted(set(unreferenced) | set(retired))
            unmanaged = sorted(path for path in files if (bucket, path) not in self.references
                                and not path.startswith(managed_prefixes(bucket))
                                and (bucket, path) not in self.retired_roots
                                and not path.startswith(RETAINED_PREFIXES))
            buckets[bucket] = {
                "objects": len(files),
                "referenced": sum((bucket, path) in self.references for path in files),
                "formats": dict(sorted(Counter(
                    "".join(Path(path).suffixes[-2:]).lower() if path.endswith(".gz")
                    else Path(path).suffix.lower() or "(none)" for path in files).items())),
                "unreferenced": unreferenced,
                "retired": retired,
                "retained": retained,
                "candidates": reclaimed if complete else [],
                "unmanaged": unmanaged,
            }
        return {
            "version": 1, "mode": "report-only", "graph_complete": complete,
            "consistency": "converged: every current root read at its latest listed version; "
                           "references from earlier root versions unioned conservatively",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "deletion_enabled": False,
            "warnings": sorted(set(self.resource_warnings)),
            "deletion_blockers": ["remaining producers lack shared mutation coordination"],
            "blockers": sorted(self.blockers), "buckets": buckets,
            "missing": [f"{b}:{p}" for b, p in sorted(self.missing)],
            "ambiguous_paths": sorted(self.ambiguous),
            "processing_retirements": [{"bucket": bucket, "path": path, **evidence}
                for (bucket, path), evidence in sorted(self.released_processing.items())
                if (bucket, path) in self.retired_roots and (bucket, path) not in self.references],
            "roots": [f"{b}:{p}" for b, p in sorted(self.root_hashes)],
        }


def read_gc_state(store) -> dict:
    try:
        payload = decode(store.read_bytes(READER_ASSETS_BUCKET, "reader-index/reader_gc_state.json"))
    except FileNotFoundError:
        return {"version": 1, "kind": "reader-gc-state", "orphans": {}}
    if payload.get("version") != 1 or payload.get("kind") != "reader-gc-state" \
            or not isinstance(payload.get("orphans"), dict):
        raise RuntimeError("invalid Reader GC state")
    return payload


def plan_retention(state: dict, report: dict, grace_days: int, limit: int, today=None) -> tuple[dict, dict]:
    if not report.get("graph_complete"):
        raise RuntimeError("reference graph is incomplete; refusing orphan observations")
    if grace_days < 0 or limit < 0:
        raise ValueError("orphan grace and limit must be non-negative")
    today = today or date.today()
    current = {f"{bucket}:{path}" for bucket, data in report["buckets"].items()
               for path in data.get("candidates", [])}
    orphans = {key: value for key, value in state.get("orphans", {}).items()
               if key in current and isinstance(value, dict)}
    for key in sorted(current):
        orphans.setdefault(key, {"first_seen": today.isoformat()})
    updated = {"version": 1, "kind": "reader-gc-state", "orphans": dict(sorted(orphans.items()))}
    cutoff = today - timedelta(days=grace_days)
    eligible = []
    for key, value in updated["orphans"].items():
        try:
            if date.fromisoformat(value["first_seen"]) <= cutoff:
                eligible.append(key)
        except (KeyError, TypeError, ValueError):
            continue
    eligible = sorted(eligible)[:limit] if limit else sorted(eligible)
    return updated, {"marked": len(current), "past_grace": len(eligible),
                     "past_grace_paths": eligible, "deletion_authorized": False}


def report_exit_code(report: dict, allow_incomplete: bool = False) -> int:
    """A report-only scan may race with writers and intentionally stay incomplete.

    Retention and deletion never use this escape hatch: they require a complete
    graph and pause every producer for the authoritative scan.
    """
    return 0 if report.get("graph_complete") or allow_incomplete else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-input-bucket", action="store_true")
    parser.add_argument("--record-observations", action="store_true")
    parser.add_argument("--storage", choices=("hub", "s3"), default="hub")
    parser.add_argument("--grace-days", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--generation-grace-days", type=int, default=0)
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel root/manifest readers")
    parser.add_argument("--max-rounds", type=int, default=20,
                        help="root-only convergence rounds before reporting an unstable snapshot")
    parser.add_argument("--deadline-minutes", type=int, default=0,
                        help="write an incomplete report instead of exceeding this wall-clock budget")
    parser.add_argument("--snapshot-retries", type=int, default=2,
                        help="retry transient root/object races before returning an incomplete graph")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="exit zero when a report-only scan cannot converge; never authorizes retention")
    args = parser.parse_args()
    if (args.grace_days < 0 or args.limit < 0 or args.generation_grace_days < 0
            or args.snapshot_retries < 0 or args.snapshot_retries > 5
            or not 1 <= args.workers <= 128 or not 1 <= args.max_rounds <= 60):
        parser.error("grace periods and limit must be non-negative")
    store = HubBucketStore() if args.storage == "hub" else S3BucketStore()
    transient_markers = ("root inventory did not stabilize", "unreadable JSON", "root recheck failed",
                         "root inventory recheck failed")
    report = None
    deadline = time.monotonic() + args.deadline_minutes * 60 if args.deadline_minutes > 0 else None
    for attempt in range(args.snapshot_retries + 1):
        report = ReferenceGraph(store, not args.skip_input_bucket, args.generation_grace_days,
                                workers=args.workers, max_rounds=args.max_rounds, deadline=deadline).build()
        if any("deadline reached" in blocker for blocker in report.get("blockers", [])):
            break
        if report["graph_complete"] or not any(
                any(marker in blocker for marker in transient_markers)
                for blocker in report.get("blockers", [])) or attempt == args.snapshot_retries:
            break
        delay = min(30, 2 ** attempt)
        print(f"GC snapshot changed during read; retrying in {delay}s "
              f"({attempt + 2}/{args.snapshot_retries + 1})", flush=True)
        time.sleep(delay)
    assert report is not None
    if report["graph_complete"]:
        state, report["retention"] = plan_retention(read_gc_state(store), report, args.grace_days, args.limit)
        if args.record_observations:
            store.put_json(READER_ASSETS_BUCKET, "reader-index/reader_gc_state.json", state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                           encoding="utf-8")
    for bucket, counts in report["buckets"].items():
        print(f"{bucket}: objects={counts['objects']} referenced={counts['referenced']} "
              f"unreferenced={len(counts['unreferenced'])} unmanaged={len(counts['unmanaged'])}")
    print(f"graph_complete={report['graph_complete']} deletion_enabled=False report={args.output}")
    if not report["graph_complete"]:
        print("::warning::Reader GC report did not converge; deletion_enabled=False")
    return report_exit_code(report, args.allow_incomplete)


if __name__ == "__main__":
    raise SystemExit(main())
