"""Plan and conditionally update the desired search data generation pointer.

A pointer is the desired generation, not a cross-site publication receipt.
Callers must independently verify immutable objects, both consumers, rollback
and the complete writer scope before applying a transition. One-object S3 CAS
is neither a distributed lock nor protection against ABA.
"""
from __future__ import annotations

import json
import re

try:
    from . import search_snapshot
    from .reader_bucket_store import S3BucketStore, S3NotFound
    from .reader_page_cas import strong_etag
except ImportError:
    import search_snapshot
    from reader_bucket_store import S3BucketStore, S3NotFound
    from reader_page_cas import strong_etag


BUCKET = "vomebook/search-data-v1"
KEY = "published/current.json"
GENERATION = re.compile(r"[0-9a-f]{64}\Z")


def _generation(value):
    if not isinstance(value, str) or not GENERATION.fullmatch(value):
        raise ValueError("invalid search generation")
    return value


def decode_pointer(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid search pointer JSON") from error
    if (not isinstance(value, dict) or set(value) != {"version", "kind", "desired", "previous", "transition"}
            or value["version"] != 1 or value["kind"] != "search-publication-pointer"):
        raise ValueError("invalid search pointer schema")
    _generation(value["desired"])
    if value["previous"] is not None:
        _generation(value["previous"])
    if not isinstance(value["transition"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["transition"]):
        raise ValueError("invalid transition identity")
    if search_snapshot.canonical(value) != raw:
        raise ValueError("search pointer must use canonical JSON")
    return value


def make_pointer(desired: str, previous: str | None, transition: str) -> bytes:
    _generation(desired)
    if previous is not None:
        _generation(previous)
    value = {"version": 1, "kind": "search-publication-pointer", "desired": desired,
             "previous": previous, "transition": transition}
    raw = search_snapshot.canonical(value)
    decode_pointer(raw)
    return raw


class S3SearchPointerStore:
    def __init__(self, store=None):
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            store._config = store._config.merge(Config(
                retries={"total_max_attempts": 1}, request_checksum_calculation="when_required",
                response_checksum_validation="when_required"))
        self.store = store

    def read_for_update(self) -> tuple[bytes | None, str | None]:
        try:
            raw = self.store.read_bytes(BUCKET, KEY)
        except S3NotFound:
            return None, None
        if len(raw) > 16 * 1024:
            raise ValueError("search publication pointer is oversized")
        decode_pointer(raw)
        return raw, strong_etag(self.store.read_etags.get((BUCKET, KEY)))

    def compare_and_swap(self, *, expected_etag: str | None, raw: bytes) -> None:
        decode_pointer(raw)
        namespace, name = self.store._location(BUCKET)
        if (namespace, name) != ("vomebook", "search-data-v1"):
            raise ValueError("unexpected search bucket mapping")
        condition = {"IfNoneMatch": "*"} if expected_etag is None else {"IfMatch": strong_etag(expected_etag)}
        # One attempt. If the response is lost, callers must read the exact
        # pointer and transition identity before deciding how to recover.
        self.store._client(namespace).put_object(Bucket=name, Key=KEY, Body=raw,
                                                  ContentType="application/json", **condition)


def apply_pointer(store, *, desired: str, expected: str | None, transition: str) -> dict:
    """CAS the requested change; caller must hold its independently verified writer coordination."""
    _generation(desired)
    if expected is not None:
        _generation(expected)
    raw, etag = store.read_for_update()
    if (raw is None) != (etag is None):
        raise ValueError("pointer identity and validator are inconsistent")
    current = decode_pointer(raw) if raw is not None else None
    actual = current["desired"] if current else None
    if actual != expected:
        raise ValueError("desired pointer parent changed; inspect before resuming")
    if desired == actual:
        raise ValueError("desired generation is already current")
    replacement = make_pointer(desired, actual, transition)
    try:
        store.compare_and_swap(expected_etag=etag, raw=replacement)
    except Exception:
        # The response could have been lost. Inspect, but never automatically
        # replay, even if the observed state looks unchanged.
        after, _ = store.read_for_update()
        if after == replacement:
            return {"status": "written-response-unknown", "desired": desired, "previous": actual,
                    "transition": transition, "consumers_verified": False}
        raise
    after, _ = store.read_for_update()
    if after != replacement:
        raise RuntimeError("pointer readback did not match; inspect exact state")
    return {"status": "written", "desired": desired, "previous": actual,
            "transition": transition, "consumers_verified": False}
