"""Retire acknowledged v3 history roots without mutating immutable generations."""

from datetime import datetime, timedelta, timezone
import json
import hashlib

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
    # Input release is eligible only after complete text publication with no open
    # text-review tasks. Old audit resources remain immutable and recoverable.
    try:
        registry = json.loads(store.read_bytes(publication.ASSETS, "reader-index/pdf_ocr_manifest.json"))
    except FileNotFoundError:
        registry = {"files": {}}
    for key, active in catalog["files"].items():
        reading = publication.v3.decode(publication.v3.verified_read(active["resource"],
            lambda ref: store.read_bytes(ref["bucket"], ref["path"])))
        text_ref = reading.get("text_layer")
        if not text_ref:
            continue
        index = publication.v3.decode(publication.v3.verified_read(text_ref,
            lambda ref: store.read_bytes(ref["bucket"], ref["path"])))
        review = publication.v3.decode(publication.v3.verified_read(index["review"],
            lambda ref: store.read_bytes(ref["bucket"], ref["path"])))
        if review.get("tasks") or "tasks" not in review:
            continue
        publication.v3.verify_text_bundle(text_ref,
            lambda ref: store.read_bytes(ref["bucket"], ref["path"]), active["source_sha256"], reading["page_count"])
        ocr = registry.get("files", {}).get(key, {})
        if ocr.get("status") != "ready" or ocr.get("source_sha256") != active["source_sha256"]:
            continue
        raw = store.read_bytes(publication.v3.shared.PDF_PAGES_BUCKET, ocr["ocr_manifest"])
        if len(raw) != ocr["ocr_manifest_bytes"] or hashlib.sha256(raw).hexdigest() != ocr["ocr_manifest_sha256"]:
            raise ValueError("input retirement OCR manifest checksum mismatch")
        manifest = json.loads(raw)
        if (manifest.get("complete") is not True or manifest.get("page_count") != reading["page_count"]
                or manifest.get("source_sha256") != active["source_sha256"]
                or [page.get("p") for page in manifest.get("pages", [])] != list(range(1, reading["page_count"] + 1))):
            raise ValueError("input retirement requires complete OCR")
        for page in manifest["pages"]:
            if page.get("i") and "/ocr-input/" in page["i"]:
                item = {"bucket": publication.v3.shared.PDF_OCR_INPUT_BUCKET, "path": page["i"],
                        "source_sha256": active["source_sha256"], "sha256": page["is"], "bytes": page["ib"],
                        "released_after_generation": pointer["generation"]}
                inputs[item["path"]] = item
    state.update(retired_paths=sorted(retired), replacement_generation=pointer["generation"],
                 resources=[pointer["catalog"]], replacement_acks=history["acks"], retired_inputs=inputs)
    if apply:
        publication.assert_writer(store)
        if publication.current(store)[0] != pointer:
            raise ValueError("retirement pointer changed")
        raw = publication.encode(state)
        store.put_bytes(publication.ASSETS, STATE, raw)
        if store.read_bytes(publication.ASSETS, STATE) != raw:
            raise ValueError("retirement journal readback failed")
    return {"applied": apply, "retired": sorted(retired), "processing_handoffs": released,
            "released_input_objects": len(inputs)}
