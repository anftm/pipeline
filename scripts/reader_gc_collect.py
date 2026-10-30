#!/usr/bin/env python3
"""Collect unreferenced current-bucket objects during a drained maintenance window."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from . import reader_gc_graph as graph, reader_lifecycle as lifecycle, reader_gc_retirement as retirement
    from .reader_bucket_store import HubBucketStore
    from .reader_gc_fence import GitHubProducerFence, assert_collector_context
    from .publish_reader_v3 import current, project_sidecar, observe_consumer
except ImportError:
    import reader_gc_graph as graph
    import reader_lifecycle as lifecycle
    import reader_gc_retirement as retirement
    from reader_bucket_store import HubBucketStore
    from reader_gc_fence import GitHubProducerFence, assert_collector_context
    from publish_reader_v3 import current, project_sidecar, observe_consumer


ASSETS = graph.READER_ASSETS_BUCKET
CATALOG = "reader-index/reader_catalog.json"
SIDECAR = "reader-index/reader_assets.json.gz"
CONSUMERS = "reader-index/reader_gc_consumers.json"


ACKNOWLEDGMENTS = "reader-index/reader_gc_acknowledgments.json"


def catalog_acknowledged(store, identity):
    """Whether both deployments were proven against this exact base generation.

    The publishing workflow writes one receipt after it verifies both deployed
    projections, because the 2 GB canonical catalog never had working ``acks``.
    A missing or stale receipt keeps every generation.
    """
    try:
        receipt = json.loads(store.read_bytes(ASSETS, ACKNOWLEDGMENTS))
    except FileNotFoundError:
        return False, "no-acknowledgment-receipt"
    current = receipt.get("current") or {}
    if (receipt.get("version") != 1 or receipt.get("kind") != "reader-gc-acknowledgments"
            or current.get("generation") != identity["current"]
            or current.get("sidecar_sha256") != identity.get("bound_sidecar_sha256")
            or current.get("catalog_sha256") != identity["digest"]):
        return False, "stale-acknowledgment-receipt"
    return True, "workflow-receipt"


def reject_unaccepted_ccrd_collection(store) -> None:
    """New cross-domain publication is report-only until its deletion tier is accepted."""
    paths = store.list_files(graph.ccrd_lifecycle.BUCKET, ('control/', 'text/exports/', 'releases/ocr-'))
    if paths:
        raise RuntimeError(
            'four-bucket OCR-library collection is report-only; '
            'use reader-gc-ccrd.yml for the separate CCRD OCR deletion tier')


def observe_ccrd_projection(store, client):
    """Reader-only deletion still requires the actual fourth-domain consumer.

    CCRD text exports retain Reader resources, so the deployed CCRD index must
    resolve the exact OCR release the bucket pointer names before any Reader
    object is deleted. A missing pointer means the domain is not in use.
    """
    bucket = graph.ccrd_lifecycle.BUCKET
    try:
        raw = store.read_bytes(bucket, 'control/ocr-current.json')
    except FileNotFoundError:
        return None
    pointer = json.loads(raw)
    graph.ccrd_lifecycle.references(pointer, 'control/ocr-current.json')
    ref = graph.ccrd_lifecycle.resource(pointer['release'])
    release_raw = store.read_bytes(ref['bucket'], ref['path'])
    if len(release_raw) != ref['bytes'] or hashlib.sha256(release_raw).hexdigest() != ref['sha256']:
        raise ValueError('CCRD consumer release checksum mismatch')
    release = json.loads(release_raw)
    graph.ccrd_lifecycle.references(release, ref['path'])
    response = client.get('https://vomebook-search.hf.space/api/index-status',
                          headers={'Cache-Control': 'no-cache'})
    response.raise_for_status()
    status = response.json()
    if release['generation'] != pointer['generation']:
        raise ValueError('CCRD current release differs from its pointer')
    if status.get('ready') is not True or not {'CCRD', 'CW'} <= set(status.get('loaded_sources', [])):
        raise ValueError('CCRD consumer is not ready with its base sources')
    withdrawn = status.get('ocr_release') is None
    receipt = None
    if withdrawn:
        try:
            receipt_raw = store.read_bytes(bucket, 'control/ocr-consumer.json')
        except FileNotFoundError as error:
            raise ValueError('CCRD OCR withdrawal receipt is missing') from error
        receipt = json.loads(receipt_raw)
        graph.ccrd_lifecycle.references(receipt, 'control/ocr-consumer.json')
        if (receipt.get('status') != 'consumer-withdrawn'
                or receipt.get('previous_generation') != pointer['generation']
                or receipt.get('pointer_sha256') != hashlib.sha256(raw).hexdigest()
                or receipt.get('resources') != []
                or not {'CCRD', 'CW', 'RMRB', 'PLA'} <= set(status.get('loaded_sources', []))
                or (status.get('bucket') or {}).get('generation') != receipt.get('base_generation')):
            raise ValueError('CCRD OCR withdrawal receipt is stale or incomplete')
        if store.read_bytes(bucket, 'control/ocr-consumer.json') != receipt_raw:
            raise RuntimeError('CCRD OCR consumer receipt changed during acceptance')
    elif status.get('ocr_release') != pointer['generation']:
        # `ocr_release` is set only after the Space pinned the exact OCR
        # activation; the source list names logical OCR repositories, not a
        # literal "OCR" database.
        raise ValueError('CCRD has not loaded the exact current OCR release')
    if store.read_bytes(bucket, 'control/ocr-current.json') != raw:
        raise RuntimeError('CCRD pointer changed during consumer acceptance')
    return {'checked_url': 'https://vomebook-search.hf.space/api/index-status',
            'checked_at': lifecycle.now_iso(), 'generation': pointer['generation'], 'release': ref,
            'projection_verified': True, 'consumer_withdrawn': withdrawn}


def encode(payload):
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def snapshot_digest(store, bucket, path):
    if hasattr(store, "digest"):
        return store.digest(bucket, path)
    return hashlib.sha256(store.read_bytes(bucket, path)).hexdigest()


def read_catalog_stream(store, parser):
    """Run a streaming parser over the catalog without materializing it."""
    if hasattr(store, "read_graph_json"):
        return store.read_graph_json(ASSETS, CATALOG, parser=parser)
    raw = store.read_bytes(ASSETS, CATALOG)
    return parser([raw], raw.startswith(b"\x1f\x8b"))


def catalog_identity(store):
    """Digest plus the current generation identity, streamed.

    The canonical catalog can exceed 2 GB, so it is never decoded whole.
    """
    try:
        from .reader_gc_stream import catalog_identity as parser
    except ImportError:
        from reader_gc_stream import catalog_identity as parser
    payload, digest = read_catalog_stream(store, parser)
    return {**payload, "digest": digest}


def observe_projections(store, client):
    base_raw = store.read_bytes(ASSETS, SIDECAR)
    base = graph.decode(base_raw)
    identity = catalog_identity(store)
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
    ccrd = observe_ccrd_projection(store, client)
    if ccrd:
        receipts['ccrd'] = ccrd
        ref = ccrd['release']
        refs[(ref['bucket'], ref['path'])] = ref
    if store.read_bytes(ASSETS, SIDECAR) != base_raw:
        raise RuntimeError("canonical sidecar changed during consumer acceptance")
    if snapshot_digest(store, ASSETS, CATALOG) != identity["digest"]:
        raise RuntimeError("canonical catalog changed during consumer acceptance")
    if current(store)[0] != pointer:
        raise RuntimeError("v3 pointer changed during consumer acceptance")
    if identity["bound_sidecar_sha256"] != receipts["hf"]["base_sidecar_sha256"]:
        raise ValueError("catalog does not bind the current canonical sidecar")
    return identity, {"version": 1, "kind": "reader-gc-consumers", "receipts": receipts,
                      "resources": list(refs.values())}


def retire_catalog(store, identity, consumers, grace_days, *, apply):
    """Stream the canonical catalog into a compact one; never decode it whole."""
    try:
        from .reader_gc_stream import compact_catalog_stream
    except ImportError:
        from reader_gc_stream import compact_catalog_stream
    current_id = identity["current"]
    acknowledged, reason = catalog_acknowledged(store, identity)

    def build(chunks, compressed):
        return compact_catalog_stream(chunks, compressed, current=current_id,
                                      acknowledged=acknowledged, grace_days=grace_days)

    encoded, _source_digest, stats = read_catalog_stream(store, build)
    result = {"after_bytes": len(encoded),
              "after_generations": stats["kept"],
              "retired_generations": stats["retired_generations"],
              "current": current_id,
              "acknowledged": acknowledged,
              "acknowledgment_source": reason,
              "bound_sidecar_sha256": identity.get("bound_sidecar_sha256"),
              "catalog_sha256": hashlib.sha256(encoded).hexdigest(),
              "source_catalog_sha256": identity["digest"]}
    if apply and acknowledged:
        if snapshot_digest(store, ASSETS, CATALOG) != identity["digest"]:
            raise RuntimeError("catalog changed before retirement")
        store.put_bytes(ASSETS, CONSUMERS, encode(consumers))
        store.put_bytes(ASSETS, CATALOG, encoded)
        if store.read_bytes(ASSETS, CATALOG) != encoded:
            raise RuntimeError("retired catalog readback mismatch")
    return encoded, result


def retired_snapshots(catalog):
    result = set()
    for generation, entry in catalog.get("retired_generations", {}).items():
        if entry.get('withdrawal_reset_id') and entry.get('replacement_acks') != {'hf': True, 'pages': True}:
            continue
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


def collect_retired_snapshots(store, fence, catalog, *, apply=False, checkpoint=None):
    """Retire catalog-owned snapshots; this never authorizes content-bundle deletion."""
    expected_catalog = store.read_bytes(ASSETS, CATALOG)
    if apply and graph.decode(expected_catalog) != catalog:
        raise RuntimeError("catalog changed before snapshot collection")
    targets = retired_snapshots(catalog)
    reference_graph = graph.ReferenceGraph(store)
    scanned = reference_graph.build()
    if not scanned['graph_complete']:
        raise RuntimeError('reference graph is incomplete; snapshot deletion is blocked')
    targets = {path for path in targets if (ASSETS, path) not in reference_graph.references}
    current_id = catalog["current"]["generation"]
    current_snapshot = catalog["generations"][current_id].get("snapshot")
    if not current_snapshot or current_snapshot["path"] in targets:
        raise ValueError("current catalog snapshot is not protected")
    refs = {entry["snapshot"]["path"]: entry["snapshot"]
            for entry in catalog.get("retired_generations", {}).values() if entry.get("snapshot")}
    present, sizes = [], {}
    for path in sorted(targets):
        try:
            raw = store.read_bytes(ASSETS, path)
        except FileNotFoundError:
            continue
        ref = refs[path]
        if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("retired snapshot checksum mismatch")
        present.append(path)
        sizes[path] = len(raw)
    current_raw = store.read_bytes(ASSETS, current_snapshot["path"])
    if (len(current_raw) != current_snapshot["bytes"]
            or hashlib.sha256(current_raw).hexdigest() != current_snapshot["sha256"]):
        raise ValueError("current snapshot checksum mismatch")
    result = {"scope": "retired-catalog-snapshots", "planned_objects": len(present),
               "confirmed_deleted_objects": 0, "confirmed_deleted_bytes": 0,
              "deleted_bytes_complete": True, "applied": apply, "batches": [], 'complete': False}
    if not apply or not present:
        result['complete'] = bool(apply)
        return result
    fence.check()
    verify_roots(store, reference_graph)
    if store.read_bytes(ASSETS, CATALOG) != expected_catalog:
        raise RuntimeError("catalog changed before snapshot deletion")
    batch = {"bucket": ASSETS, "paths": present, "status": "submitted"}
    result["batches"].append(batch)
    if checkpoint:
        checkpoint(result)
    store.delete(ASSETS, present)
    batch["status"] = "accepted"
    if checkpoint:
        checkpoint(result)
    fence.check()
    verify_roots(store, reference_graph)
    for path in present:
        try:
            store.read_bytes(ASSETS, path)
        except FileNotFoundError:
            continue
        raise RuntimeError("retired snapshot deletion readback still contains object")
    if store.read_bytes(ASSETS, current_snapshot["path"]) != current_raw:
        raise RuntimeError("current snapshot changed during retirement")
    if store.read_bytes(ASSETS, CATALOG) != expected_catalog:
        raise RuntimeError("catalog changed during snapshot retirement")
    batch["status"] = "verified-deleted"
    result.update(confirmed_deleted_objects=len(present), confirmed_deleted_bytes=sum(sizes.values()), complete=True)
    if checkpoint:
        checkpoint(result)
    return result


def verify_roots(store, reference_graph, collected_roots=frozenset()):
    present_roots = set()
    for bucket, files in reference_graph.files.items():
        roots = (reference_graph.list_roots(bucket) if hasattr(reference_graph, "list_roots") else
                 {p for p in store.list_files(bucket, ("",)) if graph.is_root(p, bucket)})
        present_roots.update((bucket, path) for path in roots)
        expected = {p for p in files if graph.is_root(p, bucket)}
        retired = {p for b, p in collected_roots if b == bucket}
        if roots - retired != expected - retired:
            raise RuntimeError("root inventory changed before deletion")

    def recheck_root(item):
        ref, expected = item
        if ref in collected_roots and ref not in present_roots:
            return
        if hasattr(reference_graph, "changed"):
            changed = reference_graph.changed(ref)
            if ref in reference_graph.unreadable:
                raise RuntimeError("root unreadable before deletion")
        else:
            changed = snapshot_digest(store, *ref) != expected
        if changed:
            raise RuntimeError("root changed before deletion")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(recheck_root, sorted(reference_graph.root_hashes.items())))


def collect(store, fence, reference_graph, report, catalog, *, grace_days=0, limit=0,
            batch_size=500, apply=False, checkpoint=None, reader_only=False,
            workers=1, resume=None, retirement_roots=None):
    if not report["graph_complete"]:
        raise RuntimeError("reference graph is incomplete; no object deletion is authorized")
    if reader_only and set(reference_graph.files) != set(graph.BUCKETS):
        raise RuntimeError("reader-only deletion requires the complete four-bucket reference closure")
    if apply and not reader_only:
        reject_unaccepted_ccrd_collection(store)
    # Persist observations for every candidate even when this window has a small
    # deletion budget. Applying a limit here used to reset/lose later candidates.
    state, retention = graph.plan_retention(graph.read_gc_state(store), report, grace_days, 0)
    eligible = {tuple(key.split(":", 1)) for key in retention["past_grace_paths"]}
    snapshots = retired_snapshots(catalog)
    eligible.update((ASSETS, path) for path in snapshots if path in reference_graph.files.get(ASSETS, set())
                    and (ASSETS, path) not in reference_graph.references)
    excluded = {ref for ref in eligible if reader_only and ref[0] not in graph.READER_BUCKETS}
    eligible -= excluded
    if retirement_roots is not None:
        eligible = {ref for ref in eligible if retirement.within(*ref, retirement_roots)
                    or reference_graph.input_retired(ref) or ref in reference_graph.retired_roots
                    or ref[0] == ASSETS and ref[1] in snapshots}
    selected = sorted(eligible)[:limit] if limit else sorted(eligible)
    if resume:
        previous = sorted((entry['bucket'], entry['path']) for entry in resume['targets'])
        if (resume.get('plan_sha256') != hashlib.sha256(encode(previous)).hexdigest()
                or resume.get('reader_only') != reader_only):
            raise ValueError('saved collection plan identity mismatch')
        if any(ref[1] in reference_graph.files.get(ref[0], set()) and ref not in eligible for ref in previous):
            raise RuntimeError('a saved deletion target is no longer eligible')
        selected = previous
    if not 1 <= workers <= 8 or not 1 <= batch_size <= 1000:
        raise ValueError('invalid deletion worker or batch limit')
    sizes = getattr(store, 'object_sizes', {})
    target_sizes = {f'{bucket}:{path}': sizes.get((bucket, path)) for bucket, path in selected}
    if resume:
        target_sizes = {**resume.get('target_sizes', {}),
                        **{key: value for key, value in target_sizes.items() if value is not None}}
    selected_set = set(selected)
    result = {"planned_objects": len(selected), "confirmed_deleted_objects": 0,
              "confirmed_deleted_bytes": 0, "deleted_bytes_complete": True,
              "batches": [], "applied": apply, "reader_only": reader_only,
              "excluded_other_bucket_candidates": len(excluded),
               "targets": [{"bucket": bucket, "path": path} for bucket, path in selected],
               "target_sizes": target_sizes,
               "processing_retirements": [entry for entry in report.get('processing_retirements', [])
                    if (entry['bucket'], entry['path']) in selected_set],
                "plan_sha256": hashlib.sha256(encode(selected)).hexdigest(),
                "scope": 'retired-assets' if retirement_roots is not None else 'unreferenced',
                "complete": False}
    if resume:
        result['batches'] = [dict(entry) for entry in resume.get('batches', [])]
    if not apply:
        return result
    fence.check()
    verify_roots(store, reference_graph)
    store.put_json(ASSETS, 'reader-index/reader_gc_state.json', state)
    # This collector owns the state mutation; include its new digest in the
    # root barrier instead of rejecting our own retention checkpoint.
    state_ref = (ASSETS, 'reader-index/reader_gc_state.json')
    reference_graph.files.setdefault(ASSETS, set()).add(state_ref[1])
    reference_graph.root_names.setdefault(ASSETS, set()).add(state_ref[1])
    reference_graph.references.add(state_ref)
    reference_graph.root_hashes[state_ref] = snapshot_digest(store, *state_ref)
    reference_graph.fingerprints[state_ref] = ('sha256', reference_graph.root_hashes[state_ref])
    if checkpoint:
        # Persist the exact planned targets before the first delete request.
        checkpoint(result)
    lock = threading.Lock()
    last_barrier = [time.monotonic()]
    def barrier():
        fence.check()
        verify_roots(store, reference_graph, frozenset(selected))
        last_barrier[0] = time.monotonic()
    def save():
        if checkpoint:
            checkpoint(result)
    for bucket in graph.BUCKETS:
        paths = [path for b, path in selected if b == bucket]
        if not paths:
            continue
        barrier()
        existing_records = {(entry['bucket'], tuple(entry['paths'])): entry for entry in result['batches']}
        def delete_batch(batch):
            if any((bucket, p) in reference_graph.references for p in batch):
                raise RuntimeError("referenced object entered deletion batch")
            if any(not p.startswith(graph.managed_prefixes(bucket)) and not (
                    bucket == ASSETS and (p in snapshots
                                          or (bucket, p) in reference_graph.retired_roots))
                    for p in batch):
                raise RuntimeError("unmanaged object entered deletion batch")
            with lock:
                if time.monotonic() - last_barrier[0] >= 60:
                    barrier()
                record = existing_records.get((bucket, tuple(batch)))
                # Re-listing and a fresh complete graph prove which objects
                # survived a partial delete. No historical success hides a
                # recreated object or bypasses a new reference.
                present = [path for path in batch if path in reference_graph.files[bucket]]
                if record is None:
                    record = {"bucket": bucket, "paths": batch, "status": "submitted"}
                    result['batches'].append(record)
                else:
                    record['status'] = 'submitted'
                save()
            if present:
                store.delete(bucket, present)
            with lock:
                record['status'] = 'accepted'
                save()
        batches = [paths[offset:offset + batch_size] for offset in range(0, len(paths), batch_size)]
        for _ in lifecycle.ordered_reads(delete_batch, batches, workers=workers):
            pass
        barrier()
        remaining = store.list_files(bucket, ("",))
        retained = {p for b, p in reference_graph.references if b == bucket
                    and p in reference_graph.files[bucket]}
        if not retained.issubset(remaining):
            raise RuntimeError("a retained object disappeared during collection")
        if set(paths) & remaining:
            raise RuntimeError("bucket deletion readback still contains planned objects")
        result["confirmed_deleted_objects"] += len(paths)
        result["confirmed_deleted_bytes"] += sum(target_sizes.get(f'{bucket}:{p}') or 0 for p in paths)
        result["deleted_bytes_complete"] &= all(target_sizes.get(f'{bucket}:{p}') is not None for p in paths)
        for record in result["batches"]:
            if record["bucket"] == bucket:
                record["status"] = "verified-deleted"
        for path in paths:
            state["orphans"].pop(f"{bucket}:{path}", None)
        if checkpoint:
            checkpoint(result)
        print(f"{bucket}: verified deletion of {len(paths)} objects", flush=True)
    store.put_json(ASSETS, "reader-index/reader_gc_state.json", state)
    result['complete'] = True
    if checkpoint:
        checkpoint(result)
    return result


def main():
    import httpx
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--grace-days", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retired-snapshots-only", action="store_true")
    parser.add_argument("--reader-only", action="store_true",
                        help="Delete only the original Reader buckets; keep every four-domain reference")
    parser.add_argument("--storage", choices=("hub", "s3"), default="hub")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--resume-run', default='')
    parser.add_argument('--retired-assets-only', action='store_true')
    parser.add_argument('--auto-resume', action='store_true')
    args = parser.parse_args()
    if (args.grace_days < 0 or args.limit < 0 or not 1 <= args.workers <= 8
            or args.resume_run and not re.fullmatch(r'[1-9][0-9]*', args.resume_run)):
        parser.error("grace and limit must be non-negative")
    if args.apply:
        assert_collector_context()
    if args.storage == "s3":
        try:
            from .reader_gc_store import CollectionStore
        except ImportError:
            from reader_gc_store import CollectionStore
        store = CollectionStore()
    else:
        store = HubBucketStore()
    fence = GitHubProducerFence()
    if args.apply and not args.reader_only:
        reject_unaccepted_ccrd_collection(store)
    report = {"version": 1, "mode": "collect" if args.apply else "plan", "grace_days": args.grace_days,
              "started_at": lifecycle.now_iso(), "complete": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_bytes(encode(report))
    run = os.environ.get("GITHUB_RUN_ID", "local")
    scope = 'retired-assets' if args.retired_assets_only else 'unreferenced'
    last_checkpoint = [0.0]
    def checkpoint(result, force=False):
        report["collection"] = result
        if force or result.get('complete') or time.monotonic() - last_checkpoint[0] >= 30:
            save()
            store.put_bytes(ASSETS, f"reader-index/gc/{run}/collection.json", encode(result))
            if args.auto_resume:
                retirement.save_cursor(store, run, scope, result, result.get('complete', False))
            last_checkpoint[0] = time.monotonic()
    last_progress = 0
    def progress(stage, **counts):
        nonlocal last_progress
        value = {"stage": stage, "updated_at": lifecycle.now_iso(), **counts}
        print(json.dumps(value), flush=True)
        if args.apply and time.monotonic() - last_progress >= 30:
            store.put_bytes(ASSETS, f"reader-index/gc/{run}/progress.json", encode(value))
            last_progress = time.monotonic()
    store.inventory_progress = lambda bucket, count: progress("inventory-progress", bucket=bucket, objects=count)
    try:
        report["producer_fence"] = fence.check()
        deadline = time.monotonic() + 1200
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            while True:
                try:
                    identity, consumers = observe_projections(store, client)
                    break
                except (ValueError, httpx.HTTPError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("current consumer projection acceptance timed out") from None
                    time.sleep(20)
        fence.check()
        catalog_raw, report["catalog"] = retire_catalog(
            store, identity, consumers, args.grace_days, apply=args.apply)
        catalog = graph.decode(catalog_raw)
        save()
        if args.retired_snapshots_only:
            report["collection"] = collect_retired_snapshots(store, fence, catalog,
                apply=args.apply, checkpoint=checkpoint if args.apply else None)
            report["complete"] = True
            print(json.dumps({k: v for k, v in report["collection"].items() if k != "batches"}), flush=True)
            return
        try:
            from . import reader_processing_history
        except ImportError:
            import reader_processing_history
        fence.check()
        supersessions = reader_processing_history.reconcile(store, catalog, consumers)
        report['processing_supersessions'] = [{'bucket': bucket, 'path': path, **value}
            for (bucket, path), value in sorted(supersessions.items())]
        save()
        if args.apply and supersessions:
            # Save original generation, exact record bytes and current consumer
            # proof before collecting any historical upload record or content.
            audit = encode({'version': 1, 'kind': 'reader-processing-supersessions',
                            'records': report['processing_supersessions']})
            audit_path = f'reader-index/gc/{run}/processing-supersessions-{hashlib.sha256(audit).hexdigest()}.json'
            store.put_bytes(ASSETS, audit_path, audit)
            if store.read_bytes(ASSETS, audit_path) != audit:
                raise RuntimeError('processing supersession audit readback mismatch')
        reference_graph = graph.ReferenceGraph(store, generation_grace_days=args.grace_days, progress=progress,
                                              processing_supersessions=supersessions)
        scanned = reference_graph.build()
        report["graph"] = scanned
        save()
        progress("deletion-preflight", graph_complete=scanned["graph_complete"],
                 blockers=len(scanned["blockers"]))
        resume = None
        if args.auto_resume and not args.resume_run:
            saved, resume = retirement.pending_plan(store)
            if saved:
                args.resume_run = saved['run_id']
                scope = saved['scope']
                args.retired_assets_only = scope == 'retired-assets'
        if args.resume_run and resume is None:
            resume = json.loads(store.read_bytes(ASSETS, f'reader-index/gc/{args.resume_run}/collection.json'))
            if args.auto_resume and resume.get('complete'):
                resume = None
        hints = retirement.load(store) if args.retired_assets_only else None
        report['retirement_receipts'] = len(hints or {})
        if resume and args.auto_resume:
            changed = [r for r in resume['targets'] if (r['bucket'], r['path']) in reference_graph.references]
            if changed:
                # Current ownership wins. Keep the old audit, invalidate its
                # deletion authority and plan the remaining eligible objects.
                report['superseded_plan'] = {'run_id': args.resume_run, 'plan_sha256': resume['plan_sha256'],
                                             'reason': 'targets-referenced-again', 'retained': changed}
                resume = None
        report["collection"] = collect(store, fence, reference_graph, scanned, catalog,
            grace_days=args.grace_days, limit=args.limit, apply=args.apply,
            checkpoint=checkpoint if args.apply else None, reader_only=args.reader_only,
            workers=args.workers, resume=resume,
            retirement_roots=retirement.targets(hints) if hints is not None else None)
        report["complete"] = True
        print(json.dumps({"catalog": report["catalog"], "collection": {
            k: v for k, v in report["collection"].items() if k != "batches"}}), flush=True)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["finished_at"] = lifecycle.now_iso()
        save()
        if args.apply and report.get('collection'):
            checkpoint(report['collection'], force=True)


if __name__ == "__main__":
    main()
