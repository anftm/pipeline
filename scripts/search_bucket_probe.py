"""Verify conditional S3 and Hub compatibility on a fresh isolated probe key.

This tool never changes search generations or publication pointers. By default
it only reports the exact planned namespace. `--apply` creates one random,
harmless probe object, verifies conditional writes and deletes it conditionally.
Any unknown write outcome stops execution; inspect the reported key before retry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets

try:
    from .reader_bucket_store import (
        HubBucketStore,
        S3BucketStore,
        S3NotFound,
        s3_error_code,
    )
    from .reader_page_cas import strong_etag
except ImportError:
    from reader_bucket_store import (
        HubBucketStore,
        S3BucketStore,
        S3NotFound,
        s3_error_code,
    )
    from reader_page_cas import strong_etag


BUCKET = "vomebook/search-data-v1"
PREFIX = "probes/s3-conditional-"


def _precondition_conflict(error: BaseException) -> bool:
    code, status = s3_error_code(error)
    return status in {409, 412} or str(code) in {"409", "412", "PreconditionFailed", "ConditionalRequestConflict"}


def probe(store, hub, probe_id: str) -> dict:
    """Verify a caller-supplied random probe ID; reject all non-probe paths."""
    if not isinstance(probe_id, str) or len(probe_id) != 32 or any(c not in "0123456789abcdef" for c in probe_id):
        raise ValueError("probe ID must be 32 lowercase hex characters")
    key = PREFIX + probe_id + ".json"
    namespace, name = store._location(BUCKET)
    if namespace != "vomebook" or name != "search-data-v1":
        raise ValueError("unexpected bucket mapping")
    client = store._client(namespace)
    initial = json.dumps({"kind": "search-bucket-conditional-probe", "id": probe_id, "step": 1},
                         sort_keys=True, separators=(",", ":")).encode()
    updated = json.dumps({"kind": "search-bucket-conditional-probe", "id": probe_id, "step": 2},
                         sort_keys=True, separators=(",", ":")).encode()
    print(json.dumps({"kind": "search-bucket-probe", "probe_key": key, "phase": "before-write"}), flush=True)
    try:
        store.read_bytes(BUCKET, key)
    except S3NotFound:
        pass
    else:
        raise RuntimeError("random probe key already exists; refusing to overwrite")
    # No retry: a lost response may hide a successful write. The key is emitted
    # before any mutation, and a later operator can inspect that precise key.
    client.put_object(Bucket=name, Key=key, Body=initial, ContentType="application/json", IfNoneMatch="*")
    raw = store.read_bytes(BUCKET, key)
    first = strong_etag(store.read_etags.get((BUCKET, key)))
    if raw != initial:
        raise ValueError("S3 create did not read back the expected bytes")
    try:
        client.put_object(Bucket=name, Key=key, Body=updated, ContentType="application/json", IfNoneMatch="*")
    except Exception as error:
        if not _precondition_conflict(error):
            raise
    else:
        raise ValueError("duplicate conditional create unexpectedly succeeded")
    if hub.read_bytes(BUCKET, key) != initial:
        raise ValueError("Hub and S3 reads disagree on the probe")
    client.put_object(Bucket=name, Key=key, Body=updated, ContentType="application/json", IfMatch=first)
    raw = store.read_bytes(BUCKET, key)
    latest = strong_etag(store.read_etags.get((BUCKET, key)))
    if raw != updated or hub.read_bytes(BUCKET, key) != updated:
        raise ValueError("updated probe bytes differ between Hub and S3")
    try:
        client.put_object(Bucket=name, Key=key, Body=initial, ContentType="application/json", IfMatch=first)
    except Exception as error:
        if not _precondition_conflict(error):
            raise
    else:
        raise ValueError("stale conditional update unexpectedly succeeded")
    # Exact key and freshly observed ETag only. A failure is unknown; never
    # convert it to success or retry deletion without reading exact state.
    client.delete_object(Bucket=name, Key=key, IfMatch=latest)
    try:
        store.read_bytes(BUCKET, key)
    except S3NotFound:
        deleted = True
    else:
        raise ValueError("probe still exists after conditional deletion")
    return {"version": 1, "kind": "search-bucket-compatibility-probe", "bucket": BUCKET,
            "probe_key": key, "if_none_match_verified": True, "if_match_verified": True,
            "stale_match_rejected": True, "hub_s3_reads_match": True, "conditional_delete_verified": deleted,
            "updated_sha256": hashlib.sha256(updated).hexdigest(), "production_pointer_written": False,
            "external_writers_isolated": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="create and conditionally delete exactly one random probe")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"bucket": BUCKET, "prefix": PREFIX, "write_enabled": False,
                          "production_pointer_written": False}))
        return 0
    from botocore.config import Config
    store = S3BucketStore()
    store._config = store._config.merge(Config(
        retries={"total_max_attempts": 1}, request_checksum_calculation="when_required",
        response_checksum_validation="when_required"))
    result = probe(store, HubBucketStore(), secrets.token_hex(16))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
