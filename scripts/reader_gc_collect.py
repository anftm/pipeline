#!/usr/bin/env python3
"""Collect unreferenced current-bucket objects during a drained maintenance window."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
import re
import time
from pathlib import Path

try:
    from . import reader_gc_graph as graph, reader_lifecycle as lifecycle
    from .reader_bucket_store import HubBucketStore
    from .reader_gc_fence import GitHubProducerFence, assert_collector_context
    from .publish_reader_v3 import current, project_sidecar, observe_consumer
except ImportError:
    import reader_gc_graph as graph
    import reader_lifecycle as lifecycle
    from reader_bucket_store import HubBucketStore
    from reader_gc_fence import GitHubProducerFence, assert_collector_context
    from publish_reader_v3 import current, project_sidecar, observe_consumer


ASSETS = graph.READER_ASSETS_BUCKET
CATALOG = "reader-index/reader_catalog.json"
SIDECAR = "reader-index/reader_assets.json.gz"
CONSUMERS = "reader-index/reader_gc_consumers.json"


def encode(payload):
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def snapshot_digest(store, bucket, path):
    if hasattr(store, "digest"):
        return store.digest(bucket, path)
    return hashlib.sha256(store.read_bytes(bucket, path)).hexdigest()


def observe_projections(store, client):
    base_raw = store.read_bytes(ASSETS, SIDECAR)
    base = graph.decode(base_raw)
    catalog_raw = store.read_bytes(ASSETS, CATALOG)
    catalog = graph.decode(catalog_raw)
    pointer, v3_catalog = current(store)
    expected = project_sidecar(base, v3_catalog) if pointer else base
    receipts, refs = {}, {}
    for surface, url in (("hf", "https://voiceofml-search.hf.space/api/reader-assets"),
                         ("pages", "https://vomebook.github.io/search/data/reader_assets.json.gz")):
        response = client.get(url, headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        projection = graph.decode(response.content)
        if projection.get("v") != 1 or projection.get("f") != expected.get("f"):
            raise ValueError(f"{surface} has not deployed the current complete sidecar")
        if pointer:
            receipt = observe_consumer(store, surface, client)
        else:
            receipt = {"checked_url": url, "checked_at": lifecycle.now_iso(), "projection_verified": True}
        receipts[surface] = {**receipt, "base_sidecar_sha256": hashlib.sha256(
            json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "deployed_projection_sha256": hashlib.sha256(response.content).hexdigest()}
        for ref in lifecycle.sidecar_references(projection):
            refs[(ref["bucket"], ref["path"])] = ref
    if store.read_bytes(ASSETS, SIDECAR) != base_raw or store.read_bytes(ASSETS, CATALOG) != catalog_raw:
        raise RuntimeError("canonical indexes changed during consumer acceptance")
    if current(store)[0] != pointer:
        raise RuntimeError("v3 pointer changed during consumer acceptance")
    current_id = catalog["current"]["generation"]
    if catalog["generations"][current_id]["sidecar_sha256"] != receipts["hf"]["base_sidecar_sha256"]:
        raise ValueError("catalog does not bind the current canonical sidecar")
    return catalog_raw, {"version": 1, "kind": "reader-gc-consumers", "receipts": receipts,
                         "resources": list(refs.values())}


def retire_catalog(store, raw, consumers, grace_days, *, apply):
    catalog = graph.decode(raw)
    current_id = catalog["current"]["generation"]
    acknowledged = copy.deepcopy(catalog)
    acknowledged["generations"][current_id]["acks"] = {"hf": True, "pages": True}
    for entry in acknowledged["generations"].values():
        if entry.get("status") == "superseded":
            entry["replacement_acks"] = {"hf": True, "pages": True}
    compacted = lifecycle.compact_catalog(acknowledged, current_id, grace_days=grace_days)
    compacted["retirement_receipts"] = consumers["receipts"]
    encoded = encode(compacted)
    result = {"before_bytes": len(raw), "after_bytes": len(encoded),
              "before_generations": len(catalog["generations"]),
              "after_generations": len(compacted["generations"]), "current": current_id}
    if apply:
        if store.read_bytes(ASSETS, CATALOG) != raw:
            raise RuntimeError("catalog changed before retirement")
        store.put_bytes(ASSETS, CONSUMERS, encode(consumers))
        store.put_bytes(ASSETS, CATALOG, encoded)
        if store.read_bytes(ASSETS, CATALOG) != encoded:
            raise RuntimeError("retired catalog readback mismatch")
    return compacted, result


def retired_snapshots(catalog):
    result = set()
    for generation, entry in catalog.get("retired_generations", {}).items():
        snapshot = entry.get("snapshot")
        if not snapshot:
            continue
        path = f"reader-index/generations/{generation}/reader_assets.json.gz"
        if (not re.fullmatch(r"[0-9a-f]{32}", generation)
                or snapshot.get("bucket") != ASSETS or snapshot.get("path") != path
                or entry.get("replacement_acks") != {"hf": True, "pages": True}):
            raise ValueError("invalid retired catalog snapshot")
        result.add(path)
    return result


def verify_roots(store, reference_graph):
    for bucket, files in reference_graph.files.items():
        roots = {p for p in store.list_files(bucket, ("",)) if graph.is_root(p)}
        if roots != {p for p in files if graph.is_root(p)}:
            raise RuntimeError("root inventory changed before deletion")
    for ref, expected in reference_graph.root_hashes.items():
        if snapshot_digest(store, *ref) != expected:
            raise RuntimeError("root changed before deletion")


def collect(store, fence, reference_graph, report, catalog, *, grace_days=0, limit=0,
            batch_size=500, apply=False, checkpoint=None):
    if not report["graph_complete"]:
        raise RuntimeError("reference graph is incomplete; no object deletion is authorized")
    state, retention = graph.plan_retention(graph.read_gc_state(store), report, grace_days, limit)
    eligible = {tuple(key.split(":", 1)) for key in retention["past_grace_paths"]}
    snapshots = retired_snapshots(catalog)
    eligible.update((ASSETS, path) for path in snapshots if path in reference_graph.files.get(ASSETS, set())
                    and (ASSETS, path) not in reference_graph.references)
    selected = sorted(eligible)[:limit] if limit else sorted(eligible)
    result = {"planned_objects": len(selected), "confirmed_deleted_objects": 0,
              "confirmed_deleted_bytes": 0, "deleted_bytes_complete": True,
              "batches": [], "applied": apply}
    if not apply:
        return result
    fence.check()
    verify_roots(store, reference_graph)
    for bucket in graph.BUCKETS:
        paths = [path for b, path in selected if b == bucket]
        for offset in range(0, len(paths), batch_size):
            fence.check()
            batch = paths[offset:offset + batch_size]
            if any((bucket, p) in reference_graph.references for p in batch):
                raise RuntimeError("referenced object entered deletion batch")
            if any(not p.startswith(graph.MANAGED_PREFIXES) and not (
                    bucket == ASSETS and p in snapshots) for p in batch):
                raise RuntimeError("unmanaged object entered deletion batch")
            record = {"bucket": bucket, "paths": batch, "status": "submitted"}
            result["batches"].append(record)
            if checkpoint:
                checkpoint(result)
            store.delete(bucket, batch)
            record["status"] = "accepted"
            if checkpoint:
                checkpoint(result)
        if not paths:
            continue
        fence.check()
        remaining = store.list_files(bucket, ("",))
        retained = {p for b, p in reference_graph.references if b == bucket
                    and p in reference_graph.files[bucket]}
        if not retained.issubset(remaining):
            raise RuntimeError("a retained object disappeared during collection")
        if set(paths) & remaining:
            raise RuntimeError("bucket deletion readback still contains planned objects")
        result["confirmed_deleted_objects"] += len(paths)
        sizes = getattr(store, "object_sizes", {})
        result["confirmed_deleted_bytes"] += sum(sizes.get((bucket, p), 0) for p in paths)
        result["deleted_bytes_complete"] &= all((bucket, p) in sizes for p in paths)
        for record in result["batches"]:
            if record["bucket"] == bucket:
                record["status"] = "verified-deleted"
        for path in paths:
            state["orphans"].pop(f"{bucket}:{path}", None)
        if checkpoint:
            checkpoint(result)
        print(f"{bucket}: verified deletion of {len(paths)} objects", flush=True)
    store.put_json(ASSETS, "reader-index/reader_gc_state.json", state)
    return result


def main():
    import httpx
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--grace-days", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.grace_days < 0 or args.limit < 0:
        parser.error("grace and limit must be non-negative")
    if args.apply:
        assert_collector_context()
    store, fence = HubBucketStore(), GitHubProducerFence()
    report = {"version": 1, "mode": "collect" if args.apply else "plan", "grace_days": args.grace_days,
              "started_at": lifecycle.now_iso(), "complete": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_bytes(encode(report))
    run = os.environ.get("GITHUB_RUN_ID", "local")
    def checkpoint(result):
        report["collection"] = result
        save()
        store.put_bytes(ASSETS, f"reader-index/gc/{run}/collection.json", encode(result))
    try:
        report["producer_fence"] = fence.check()
        deadline = time.monotonic() + 1200
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            while True:
                try:
                    raw, consumers = observe_projections(store, client)
                    break
                except (ValueError, httpx.HTTPError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("current consumer projection acceptance timed out") from None
                    time.sleep(20)
        fence.check()
        catalog, report["catalog"] = retire_catalog(store, raw, consumers, args.grace_days, apply=args.apply)
        save()
        reference_graph = graph.ReferenceGraph(store, generation_grace_days=args.grace_days)
        scanned = reference_graph.build()
        report["graph"] = scanned
        save()
        report["collection"] = collect(store, fence, reference_graph, scanned, catalog,
            grace_days=args.grace_days, limit=args.limit, apply=args.apply,
            checkpoint=checkpoint if args.apply else None)
        report["complete"] = True
        print(json.dumps({"catalog": report["catalog"], "collection": {
            k: v for k, v in report["collection"].items() if k != "batches"}}), flush=True)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["finished_at"] = lifecycle.now_iso()
        save()


if __name__ == "__main__":
    main()
