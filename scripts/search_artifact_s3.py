"""S3 conditional-create adapter for immutable search-generation staging.

Requires existing HF S3 credentials. This module cannot update live pointers or
remove files; accepted writes are subsequently read and verified by the caller.
"""
from __future__ import annotations

import hashlib
import re

try:
    from .reader_bucket_store import S3BucketStore, S3NotFound, s3_error_code
except ImportError:
    from reader_bucket_store import S3BucketStore, S3NotFound, s3_error_code


BUCKET = "vomebook/search-data-v1"
KEY_RE = re.compile(r"(?:objects/sha256/[0-9a-f]{2}/[0-9a-f]{64}|generations/[0-9a-f]{64}/manifest\.json)\Z")
MAX_BYTES = 32 * 1024 * 1024


class S3SearchArtifactStore:
    """Read exact keys and create a missing immutable key once via If-None-Match."""

    def __init__(self, store=None):
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            # A lost response has an unknown result. Disable automatic write replay.
            store._config = store._config.merge(Config(
                retries={"total_max_attempts": 1}, request_checksum_calculation="when_required",
                response_checksum_validation="when_required"))
        self.store = store

    @staticmethod
    def _check(key: str) -> None:
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise ValueError("search staging key is outside the immutable namespace")
        if key.startswith("objects/"):
            parts = key.split("/")
            if parts[2] != parts[3][:2]:
                raise ValueError("search object shard does not match its digest")

    def read_optional(self, key: str) -> bytes | None:
        self._check(key)
        try:
            raw = self.store.read_bytes(BUCKET, key)
        except S3NotFound:
            return None
        except Exception as error:
            code, status = s3_error_code(error)
            if status == 404 or str(code) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            raise ValueError("search object is too large or not bytes")
        return raw

    def create_immutable(self, key: str, data: bytes) -> None:
        self._check(key)
        if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_BYTES:
            raise ValueError("invalid immutable search object bytes")
        if key.startswith("objects/") and hashlib.sha256(data).hexdigest() != key.rsplit("/", 1)[-1]:
            raise ValueError("search object bytes do not match their content address")
        namespace, bucket = self.store._location(BUCKET)
        content_type = "application/json" if key.endswith(".json") else "application/octet-stream"
        self.store._client(namespace).put_object(Bucket=bucket, Key=key, Body=data,
                                                  ContentType=content_type, IfNoneMatch="*")
