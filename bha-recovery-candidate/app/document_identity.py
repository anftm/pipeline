import hashlib
import json
from collections.abc import Mapping


def content_hash(source: Mapping[str, object]) -> str:
    """Versioned preview baseline binding editable content to its identity.

    This replaces the prior content-only hash in the *offline candidate*.
    It is a point-in-time comparison, not a lock, immutable revision or proof
    that a different writer cannot change the source afterwards.
    """
    value: dict[str, object] = {
        "baseline_version": 2,
        "doc_id": source.get("doc_id"),
        "archive_id": source.get("archive_id"),
        "article_id": source.get("article_id"),
        "publication_id": source.get("publication_id"),
        "article": source.get("article") or {},
        "title": source.get("title"),
        "publication_name": source.get("publication_name"),
        "publication_author": source.get("publication_author"),
        "publication_type": source.get("publication_type"),
        "source_files": source.get("source_files") or [],
    }
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(b"bha-preview-baseline-v2\0" + serialized).hexdigest()
