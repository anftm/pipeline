#!/usr/bin/env python3
"""Read-only PDF stage counts with source/page deduplication and snapshot identity."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

try:
    from . import publish_reader_v3 as publication
except ImportError:
    import publish_reader_v3 as publication


REGISTRIES = ("pdf_render_manifest.json", "pdf_ocr_manifest.json",
              "pdf_render_progress.json", "pdf_ocr_progress.json")
HISTORY = "reader-index/progress/history.json"


def identity(entry):
    value = entry.get("source_sha256")
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def covered_ranges(ranges, page_count):
    intervals = []
    for label in ranges:
        if not re.fullmatch(r"[0-9]+-[0-9]+", label):
            raise ValueError("invalid progress range")
        start, end = map(int, label.split("-"))
        if not 1 <= start <= end <= page_count:
            raise ValueError("progress range outside book")
        intervals.append((start, end))
    total, last = 0, 0
    for start, end in sorted(intervals):
        total += max(0, end - max(last, start - 1))
        last = max(last, end)
    return total


def summarize(render, ocr, render_progress, ocr_progress, catalog, automation, correction):
    books, unknown = {}, []
    def book(key, entry):
        source = identity(entry)
        if source is None:
            unknown.append(key)
            return None
        count = entry.get("page_count")
        if type(count) is not int or count < 1:
            raise ValueError("invalid registry page count")
        item = books.setdefault(source, {"source_sha256": source, "keys": set(), "page_count": count,
            "render_ready": False, "ocr_ready": False, "v3_promoted": False,
            "render_partial_pages": 0, "ocr_pages": set(), "errors": set()})
        if item["page_count"] != count:
            raise ValueError("one source digest has conflicting page counts")
        item["keys"].add(key)
        return item
    for stage, registry in (("render", render), ("ocr", ocr)):
        for key, entry in registry.items():
            item = book(key, entry)
            if item is None:
                continue
            item[stage + "_ready"] |= entry.get("status") == "ready"
            if entry.get("status") == "failed":
                item["errors"].add(stage)
    for key, entry in render_progress.items():
        item = book(key, entry)
        if item:
            item["render_partial_pages"] = max(item["render_partial_pages"],
                covered_ranges(entry.get("ranges", {}), item["page_count"]))
    unbound_progress = []
    for key, entry in ocr_progress.items():
        current = ocr.get(key, {})
        descriptor = current.get("render_manifest", {})
        if not current.get("profile") or not descriptor.get("sha256"):
            unbound_progress.append(key)
            continue
        generation = hashlib.sha256((descriptor["sha256"] + current["profile"]).encode()).hexdigest()
        if entry.get("generation") != generation:
            unbound_progress.append(key)
            continue
        item = book(key, current)
        if item:
            for label, page in entry.get("pages", {}).items():
                number = int(label)
                if not 1 <= number <= item["page_count"] or page.get("p") != number:
                    raise ValueError("invalid recognized page identity")
                item["ocr_pages"].add(number)
    for key, entry in catalog.get("files", {}).items():
        # V3 catalog entries reference a manifest, not a page count: correlate only
        # matching source digests, never infer a count from a different revision.
        source = identity(entry)
        if source in books:
            books[source]["v3_promoted"] = True
            books[source]["keys"].add(key)
    values = []
    for item in books.values():
        item["ocr_recognized_pages"] = len(item.pop("ocr_pages"))
        item["keys"], item["errors"] = sorted(item["keys"]), sorted(item["errors"])
        values.append(item)
    values.sort(key=lambda item: item["source_sha256"])
    return {"version": 1, "kind": "pdf-production-progress", "scope": "observed-registries",
        "source_books": len(values), "books": values,
        "render_ready_books": sum(p["render_ready"] for p in values),
        "render_ready_pages": sum(p["page_count"] for p in values if p["render_ready"]),
        "render_partial_pages": sum(p["render_partial_pages"] for p in values if not p["render_ready"]),
        "ocr_ready_books": sum(p["ocr_ready"] for p in values),
        "ocr_ready_pages": sum(p["page_count"] for p in values if p["ocr_ready"]),
        "ocr_recognized_pages": sum(p["ocr_recognized_pages"] for p in values),
        "ocr_recognized_scope": "generation-matched-retained-checkpoints",
        "v3_promoted_books": sum(p["v3_promoted"] for p in values),
        "v3_catalog_entries": len(catalog.get("files", {})),
        "unknown_source_keys": sorted(set(unknown)), "unbound_ocr_progress_keys": sorted(unbound_progress),
        "v3_automation": dict(Counter(t.get("status", "unknown") for t in automation.get("tasks", {}).values())),
        "correction": dict(Counter(t.get("status", "unknown") for t in correction.get("tasks", {}).values())),
        "correction_days": correction.get("days", {})}


def build_report(store=None):
    store = store or publication.HubBucketStore()
    snapshots = {}
    def registry(name):
        path = "reader-index/" + name
        try:
            data, digest = store.read_graph_json(publication.ASSETS, path)
        except FileNotFoundError:
            snapshots[path] = {"absent": True}
            return {"files": {}, "tasks": {}, "days": {}}
        snapshots[path] = {"sha256": digest}
        return data
    def ocr_progress():
        # The per-book index replaces the multi-GB legacy registry once migrated.
        index = registry("pdf_ocr_progress/index.json")
        if index.get("kind") != "pdf-ocr-progress-index":
            snapshots.pop("reader-index/pdf_ocr_progress/index.json", None)
            return registry("pdf_ocr_progress.json")
        return {"version": 1, "files": {
            key: {"generation": item["generation"],
                  "pages": {str(number): {"p": number} for number in item["pages"]}}
            for key, item in index["books"].items()}}
    registries = [registry(name) if name != "pdf_ocr_progress.json" else ocr_progress()
                  for name in REGISTRIES]
    for data in registries:
        if data.get("version") != 1 or not isinstance(data.get("files"), dict):
            if data.get("files") != {}:
                raise ValueError("invalid progress registry")
    pointer, catalog = publication.current(store)
    report = summarize(*(data["files"] for data in registries), catalog,
                       registry("v3/automation.json"), registry("v3/corrections/state.json"))
    report.update(observed_at=datetime.now(timezone.utc).isoformat(), snapshots=snapshots,
                  v3_generation=pointer["generation"] if pointer else None)
    if publication.current(store)[0] != pointer:
        raise RuntimeError("v3 pointer changed while reporting")
    return report


def record_progress(store, report):
    try:
        from .pdf_worker_lanes import load_config
    except ImportError:
        from pdf_worker_lanes import load_config
    central = load_config()["publisher_repository"]
    ref = os.environ.get("GITHUB_WORKFLOW_REF")
    if (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != central
            or ref not in {central + "/.github/workflows/pdf-account-publish.yml@refs/heads/main",
                           central + "/.github/workflows/reader-v3-publish.yml@refs/heads/main"}):
        raise RuntimeError("progress history requires the serialized central publisher")
    try:
        history = json.loads(store.read_bytes(publication.ASSETS, HISTORY))
    except FileNotFoundError:
        history = {"version": 1, "kind": "pdf-progress-history", "resources": [], "observations": []}
    if history.get("kind") != "pdf-progress-history" or not isinstance(history.get("observations"), list):
        raise ValueError("invalid progress history")
    observation = {key: report[key] for key in ("observed_at", "source_books", "render_ready_books",
        "render_ready_pages", "ocr_ready_books", "ocr_ready_pages", "ocr_recognized_pages", "v3_promoted_books")}
    if history["observations"]:
        previous = history["observations"][-1]
        elapsed = (datetime.fromisoformat(observation["observed_at"]) -
                   datetime.fromisoformat(previous["observed_at"])).total_seconds()
        deltas = {key: observation[key] - previous[key] for key in observation if key != "observed_at"}
        report["since_previous_observation"] = {"elapsed_seconds": elapsed, "deltas": deltas,
            "scope": "net-registry-growth-not-model-execution-rate",
            "hourly_net_growth": {key: value * 3600 / elapsed for key, value in deltas.items()} if elapsed > 0 else {}}
    history["observations"] = (history["observations"] + [observation])[-1024:]
    raw = publication.encode(history)
    store.put_bytes(publication.ASSETS, HISTORY, raw)
    if store.read_bytes(publication.ASSETS, HISTORY) != raw:
        raise ValueError("progress history readback mismatch")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    report = build_report()
    if args.record:
        record_progress(publication.HubBucketStore(), report)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
