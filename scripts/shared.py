#!/usr/bin/env python3
"""Shared file hashing, hash sharding, and Hugging Face retry helpers."""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

CHUNK_BYTES = 1024 * 1024
PDF_PAGES_BUCKET = "vomebook/pdf-pages"

T = TypeVar("T")


def hash_file(path: Path) -> tuple[str, int]:
    """Return the (sha256 hex digest, byte size) of a file."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def hash_for_key(key: str, shard_count: int) -> int:
    """Stable hash shard assignment shared by asset queue planners."""
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big") % shard_count


def weighted_shards(records: list[T], shard_count: int, *, weight: Callable[[T], int],
                     order: Callable[[T], tuple]) -> list[list[T]]:
    """Assign records to the least-loaded shard using caller weight/order functions."""
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    shards: list[list[T]] = [[] for _ in range(shard_count)]
    loads = [0] * shard_count
    for item in sorted(records, key=order):
        index = min(range(shard_count), key=lambda value: (loads[value], value))
        shards[index].append(item)
        loads[index] += weight(item)
    return shards


def hf_status_code(exc: BaseException) -> int | None:
    """Extract the HTTP status from a Hugging Face hub error, if present."""
    return getattr(getattr(exc, "response", None), "status_code", None)


def is_retryable_hf_status(status: int | None, extra: frozenset = frozenset({409, 412})) -> bool:
    """Whether an HF API failure is worth retrying (parent race, rate limit, 5xx)."""
    if status is None:
        return False
    return status in extra or status == 429 or 500 <= status < 600


def hf_retry_delay(attempt: int, cap: int = 60, max_shift: int = 5) -> int:
    """Bounded exponential backoff shared by HF publication retries."""
    return min(cap, 2 ** min(attempt, max_shift))


def pdf_pages_sidecar_entry(path: str) -> dict:
    """Compact search-sidecar entry for a published PDF page stream."""
    return {"s": 2, "m": "p", "p": path, "b": PDF_PAGES_BUCKET}
