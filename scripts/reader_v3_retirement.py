"""Retire acknowledged v3 history roots without mutating immutable generations."""

from datetime import datetime, timedelta, timezone
import json
import hashlib
import re

try:
    from . import publish_reader_v3 as publication
except ImportError:
    import publish_reader_v3 as publication

STATE = "reader-index/v3/retirement.json"


def run(store, *, apply=False, now=None):
    pointer, catalog = publication.current(store)
    if not pointer:
        return {"applied": apply, "retired": []}
    history = json.loads(store.read_bytes(publication.ASSETS,
        f"reader-index/v3/promotions/{pointer['generation']}.json"))
    if history.get("acks") != {"hf": True, "pages": True}:
        return {"applied": apply, "retired": [], "blocked": "consumer-acknowledgments"}
    try:
        state = json.loads(store.read_bytes(publication.ASSETS, STATE))
    except FileNotFoundError:
        state = {"version": 1, "kind": "reader-v3-retirement", "retired_paths": [], "resources": []}
    if state.get("kind") != "reader-v3-retirement" or state.get("version") != 1:
        raise ValueError("invalid retirement state")
    retired = set(state["retired_paths"])
    inputs = dict(state.get("retired_inputs", {}))
    now = now or datetime.now(timezone.utc)
    for path in sorted(store.list_files(publication.ASSETS, ("reader-index/v3/promotions/",))):
        if path == f"reader-index/v3/promotions/{pointer['generation']}.json" or path in retired:
            continue
        record = json.loads(store.read_bytes(publication.ASSETS, path))
        if record.get("kind") != "reader-reading-promotion" or record.get("acks") != {"hf": True, "pages": True}:
            continue
        created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None or now - created < timedelta(days=record.get("retention_days", 30)):
            continue
        generation = record["generation"]
        catalog_path = f"reader-index/v3/generations/{generation}/catalog.json"
        if catalog_path == pointer["catalog"]["path"]:
            continue
        retired.update((path, catalog_path))
    released = []
    for path in sorted(store.list_files(publication.ASSETS, ("reader-index/processing/v3-",))):
        if path in retired:
            continue
        record = json.loads(store.read_bytes(publication.ASSETS, path))
        if record.get("kind") != "reader-processing-roots" or record.get("status") != "uploaded":
            continue
        candidate_ref = record.get("candidate")
        if not candidate_ref:
            continue
        candidate = publication.read_index(store, candidate_ref)
        active = catalog["files"].get(candidate.get("source_key"))
        if (not active or active["source_sha256"] != candidate.get("source_sha256")
                or active["resource"] != candidate.get("reading")
                or active["reading_generation"] != candidate.get("reading_generation")):
            continue
        released.append(path)
        retired.update((path, candidate_ref["path"]))
    # Lossless OCR inputs are released per book directory once that book's
    # complete OCR is the published v3 text. Open review tasks no longer hold
    # them: corrections render the page from the primary PDF, and their PNG
    # evidence is pinned metadata only. A mismatch skips that book (reported)
    # instead of aborting the whole journal.
    try:
        registry = json.loads(store.read_bytes(publication.ASSETS, "reader-index/pdf_ocr_manifest.json"))
    except FileNotFoundError:
        registry = {"files": {}}
    roots = dict(state.get("retired_input_roots", {}))
    done = {(item["source_sha256"], item["ocr_manifest_sha256"]) for item in roots.values()}
    skipped = {}
    for key, active in sorted(catalog["files"].items()):
        ocr = registry.get("files", {}).get(key, {})
        if ocr.get("status") != "ready" or ocr.get("source_sha256") != active["source_sha256"]:
            continue
        if (active["source_sha256"], ocr.get("ocr_manifest_sha256")) in done:
            continue
        reason = _releasable_roots(store, active, ocr, pointer["generation"])
        if isinstance(reason, str):
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        for root, item in reason.items():
            roots[root] = item
    state.update(retired_paths=sorted(retired), replacement_generation=pointer["generation"],
                 resources=[pointer["catalog"]], replacement_acks=history["acks"], retired_inputs=inputs,
                 retired_input_roots=roots)
    if apply:
        publication.assert_writer(store)
        if publication.current(store)[0] != pointer:
            raise ValueError("retirement pointer changed")
        raw = publication.encode(state)
        store.put_bytes(publication.ASSETS, STATE, raw)
        if store.read_bytes(publication.ASSETS, STATE) != raw:
            raise ValueError("retirement journal readback failed")
    return {"applied": apply, "retired": sorted(retired), "processing_handoffs": released,
            "released_input_objects": len(inputs), "released_input_roots": len(roots),
            "released_input_pages": sum(item["pages"] for item in roots.values()),
            "skipped_input_books": skipped}


INPUT_ROOT = re.compile(r"objects/[0-9a-f]{2}/([0-9a-f]{64})/[0-9a-f]{16}/ocr-input")


def _releasable_roots(store, active, ocr, generation):
    """Return {root: journal item} for one book, or a short skip reason."""
    def read(ref):
        return store.read_bytes(ref["bucket"], ref["path"])
    reading = publication.v3.decode(publication.v3.verified_read(active["resource"], read))
    if not reading.get("text_layer"):
        return "no-published-text"
    try:
        raw = store.read_bytes(publication.v3.shared.PDF_PAGES_BUCKET, ocr["ocr_manifest"])
    except (FileNotFoundError, KeyError):
        return "ocr-manifest-missing"
    if len(raw) != ocr.get("ocr_manifest_bytes") or hashlib.sha256(raw).hexdigest() != ocr.get("ocr_manifest_sha256"):
        return "ocr-manifest-checksum"
    manifest = json.loads(raw)
    source = active["source_sha256"]
    if (manifest.get("complete") is not True or manifest.get("page_count") != reading["page_count"]
            or manifest.get("source_sha256") != source
            or [page.get("p") for page in manifest.get("pages", [])] != list(range(1, reading["page_count"] + 1))):
        return "ocr-incomplete"
    roots = {}
    for page in manifest["pages"]:
        path = page.get("i")
        if not path or "/ocr-input/" not in path:
            continue
        if page.get("ibucket", publication.v3.shared.PDF_OCR_INPUT_BUCKET) != publication.v3.shared.PDF_OCR_INPUT_BUCKET:
            return "input-bucket"
        root = path.rsplit("/", 1)[0]
        match = INPUT_ROOT.fullmatch(root)
        if not match or match[1] != source:
            return "input-root-identity"
        item = roots.setdefault(root, {"bucket": publication.v3.shared.PDF_OCR_INPUT_BUCKET, "root": root,
                                       "source_sha256": source, "ocr_manifest_sha256": ocr["ocr_manifest_sha256"],
                                       "released_after_generation": generation, "pages": 0})
        item["pages"] += 1
    return roots or "no-ocr-inputs"
