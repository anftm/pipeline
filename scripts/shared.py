#!/usr/bin/env python3
"""Shared file hashing, hash sharding, and Hugging Face retry helpers."""

import hashlib
import math
import os
import random
import re
import time
from email.utils import parsedate_to_datetime
from collections.abc import Callable
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

CHUNK_BYTES = 1024 * 1024
PDF_PAGES_BUCKET = "vomebook/pdf-pages-v2"
PDF_OCR_INPUT_BUCKET = "melsm/pdf-archive-v2"
READER_ASSETS_BUCKET = "vomebook/reader-assets-v2"

T = TypeVar("T")


def ordered_reads(operation, items, workers: int = 4):
    """Read in source order, retaining/submitting at most ``workers`` objects."""
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("read workers must be between 1 and 8")
    iterator = iter(items)
    exhausted = object()
    if workers == 1:
        for item in iterator:
            yield operation(item)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        try:
            for _ in range(workers):
                item = next(iterator, exhausted)
                if item is exhausted:
                    break
                pending.append(pool.submit(operation, item))
            while pending:
                yield pending.popleft().result()
                item = next(iterator, exhausted)
                if item is not exhausted:
                    pending.append(pool.submit(operation, item))
        finally:
            for future in pending:
                future.cancel()


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


def bucket_upload_retry_delay(error: BaseException, attempt: int) -> float:
    """Respect a supplied reset countdown, or cool down an opaque Xet 429."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            seconds = float(retry_after)
        except (TypeError, ValueError, OverflowError):
            try:
                seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError, AttributeError):
                seconds = None
        if seconds is not None and math.isfinite(seconds):
            return max(0, min(300, math.ceil(seconds)))
    details = f"{error} {getattr(response, 'text', '')}"
    match = re.search(r"\bretry\s+after\s+(\d+(?:\.\d+)?)\s+seconds\b", details, re.IGNORECASE)
    if match:
        return math.ceil(min(300, float(match.group(1))))
    if hf_status_code(error) == 429 or re.search(r"\b429\b|too many requests", details, re.IGNORECASE):
        # Native Xet errors can omit both headers and body. Seconds-scale retries
        # then exhaust the whole budget inside a single rate-limit window.
        return min(300, 60 * 2 ** min(attempt, 3))
    return hf_retry_delay(attempt, cap=120, max_shift=7)


def batch_bucket_files_with_retry(bucket: str, additions: list[tuple[str | Path | bytes, str]], token: str | None,
                                  *, max_attempts: int = 12, reset_xet_on_retry: bool = False,
                                  deletions: list[str] | None = None) -> None:
    """Upload the same bucket batch with bounded retry for HF/Xet throttling.

    Session reset is opt-in for dedicated serial publishers only: aborting the
    global Xet runtime would otherwise cancel unrelated concurrent operations.
    """
    from huggingface_hub import batch_bucket_files

    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    mutation = {'add': additions} if deletions is None else {'delete': list(deletions)}
    if deletions is not None and additions:
        raise ValueError('a retried bucket batch must upload or delete, not both')
    for attempt in range(max_attempts):
        try:
            batch_bucket_files(bucket, **mutation, token=token)
            return
        except Exception as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            text = str(error).lower()
            if status is None:
                match = re.search(r"http status (?:client|server) error\s*\((\d{3})\b", text)
                status = int(match.group(1)) if match else None
            retryable = (
                status in {408, 425, 429, 500, 502, 503, 504}
                or (status is None and (
                    "too many requests" in text or "xet-write-token" in text
                    or "timeout" in text or isinstance(error, (ConnectionError, TimeoutError))))
            )
            if not retryable or attempt + 1 == max_attempts:
                raise
            if reset_xet_on_retry and ("xet-write-token" in text or "previous task error" in text):
                from huggingface_hub.utils._xet import abort_xet_session
                # The failed batch has returned; this caller explicitly owns
                # all Xet operations in the process. A fresh session avoids
                # replaying a poisoned runtime's "Previous task error" forever.
                abort_xet_session()
            delay = bucket_upload_retry_delay(error, attempt)
            delay = min(300, delay + random.uniform(0, 3))
            print(f"transient HF bucket {'delete' if deletions is not None else 'upload'} error ({status or type(error).__name__}); "
                  f"retrying in {delay:.1f}s (attempt {attempt + 1}/{max_attempts})", flush=True)
            time.sleep(delay)


def pdf_pages_sidecar_entry(path: str, result: dict | None = None, current: dict | None = None) -> dict:
    """Compact search-sidecar entry for a published PDF page stream."""
    entry = {"s": 2, "m": "p", "p": path, "b": PDF_PAGES_BUCKET}
    if (result or {}).get("source_format") == "djvu":
        # A native DjVu stream has no browser-renderable primary; never keep a
        # derived PDF companion from an earlier generation as the hybrid source.
        return entry
    document = (result or {}).get("reader_assets_path", "")
    bucket = (result or {}).get("reader_assets_bucket", "")
    if not document and current:
        if str(current.get("p", "")).endswith("/document.pdf"):
            document, bucket = current["p"], current.get("b", "")
        else:
            document, bucket = current.get("pd", ""), current.get("pdb", "")
    if document.endswith("/document.pdf") and bucket in {PDF_PAGES_BUCKET, READER_ASSETS_BUCKET}:
        entry.update(pd=document, pdb=bucket)
    return entry


def apply_pdf_pages_sidecar_entry(entry: dict, path: str, result: dict) -> dict:
    """Update ``entry`` in place with a page-stream route for ``result``."""
    entry.update(pdf_pages_sidecar_entry(path, result, entry))
    if result.get("source_format") == "djvu":
        entry.pop("pd", None)
        entry.pop("pdb", None)
    return entry


def preserve_pdf_sidecar_entry(current: dict | None, result: dict) -> dict:
    entry = dict(current or {})
    if (result.get("status") != "ready"
            or result.get('source_format') == 'djvu'
            or not str(entry.get("p", "")).endswith(("/page-manifest.json", "/reading-manifest.json"))):
        return entry
    document = result.get('reader_assets_path') or entry.get('pd')
    bucket = result.get('reader_assets_bucket') or entry.get('pdb') or PDF_PAGES_BUCKET
    if document and str(document).endswith('/document.pdf'):
        entry.update({"s": 2, "m": "p", "p": document, "b": bucket})
        entry.pop('pd', None)
        entry.pop('pdb', None)
        if entry.get("o"):
            entry["ob"] = PDF_PAGES_BUCKET
        return entry
    if entry.get("o"):
        return {"s": 3, "m": "p", "o": entry["o"], "b": PDF_PAGES_BUCKET,
                **({"om": entry["om"]} if entry.get("om") else {})}
    return {"s": 4}


def merge_pdf_ocr_sidecar_entry(current: dict | None, result: dict) -> dict | None:
    """Merge OCR metadata without losing an existing Reader asset mapping."""
    entry = preserve_pdf_sidecar_entry(current, result)
    if result.get("status") == "failed":
        if result.get("quality_status") in {"failed", "review"}:
            for field in ("o", "om", "op", "ob"):
                entry.pop(field, None)
            entry.update(om="needs-review" if result["quality_status"] == "review" else "failed",
                         oe=result.get("error", "OCR quality check failed"))
            if not entry.get("p"):
                entry["s"] = 4
        return entry or {"s": 4, "om": "failed", "oe": result.get("error", "OCR failed")}
    if result.get("status") == "ready":
        page_manifest = result.get("page_manifest")
        if result.get('source_format') != 'djvu':
            page_manifest = None
        # A completed page stream must take precedence over an older optimized
        # PDF route.  OCR recognition can publish after rendering, so keeping
        # the PDF in `p` would make Reader ignore the already available pages.
        if (isinstance(page_manifest, dict) and isinstance(page_manifest.get("path"), str)):
            apply_pdf_pages_sidecar_entry(entry, page_manifest["path"], result)
        entry.update({
            "o": result["ocr_manifest"],
            "om": result.get("classification", ""),
        })
        if entry.get("p") and str(entry["p"]).endswith("/page-manifest.json"):
            entry["b"] = PDF_PAGES_BUCKET
        elif entry.get("p"):
            entry["ob"] = PDF_PAGES_BUCKET
        else:
            entry.update({"s": 3, "m": "p", "b": PDF_PAGES_BUCKET})
        return entry
    if entry.get("s") == 3:
        return None
    for field in ("o", "om", "op", "ob"):
        entry.pop(field, None)
    return entry or None
