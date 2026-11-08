"""Immutable search-generation staging and verified reads, without a publisher.

The caller supplies the storage adapter; this module never advances a live
pointer, overwrites an existing object, deletes data or infers source provenance.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol

try:
    from . import search_snapshot
except ImportError:
    import search_snapshot


class ObjectStore(Protocol):
    def read_optional(self, key: str) -> bytes | None:
        """Return exact stored bytes, or None only for a verified missing key."""

    def create_immutable(self, key: str, data: bytes) -> None:
        """Conditionally create a missing key; a conflict must not overwrite."""


def object_key(digest: str) -> str:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid SHA-256 object identity")
    return f"objects/sha256/{digest[:2]}/{digest}"


def make_manifest(report: dict) -> dict:
    if not isinstance(report, dict) or report.get("remote_writes") != 0 or report.get("publication_ready") is not False:
        raise ValueError("expected an offline search snapshot report")
    generation = report.get("generation")
    identity = {key: value for key, value in report.items()
                if key not in {"generation", "remote_writes", "publication_ready"}}
    if not isinstance(generation, str) or hashlib.sha256(search_snapshot.canonical(identity)).hexdigest() != generation:
        raise ValueError("search snapshot identity mismatch")
    objects = {}
    files = report.get("files")
    if not isinstance(files, dict) or set(files) != {"pages", "space"}:
        raise ValueError("invalid search snapshot surfaces")
    for surface in ("pages", "space"):
        entries = files[surface]
        if not isinstance(entries, dict) or not entries:
            raise ValueError("empty search snapshot surface")
        objects[surface] = {}
        for name, metadata in entries.items():
            search_snapshot.safe_relative(name)
            if not isinstance(metadata, dict) or set(metadata) != {"sha256", "bytes"}:
                raise ValueError("invalid search member metadata")
            digest, size = metadata["sha256"], metadata["bytes"]
            key = object_key(digest)
            if type(size) is not int or not 1 <= size <= search_snapshot.MAX_DATA_GZIP:
                raise ValueError("invalid search member size")
            objects[surface][name] = {"sha256": digest, "bytes": size, "object": key}
    return {**report, "remote_writes": "candidate objects uploaded; this manifest is not a publication pointer",
            "publication_ready": False, "object_layout": "objects/sha256/<first-two>/<sha256>", "objects": objects}


def _verify_local_report(directory: Path) -> dict:
    report_file = directory / "snapshot-report.json"
    if report_file.is_symlink() or not report_file.is_file():
        raise ValueError("missing local search snapshot report")
    report = json.loads(report_file.read_bytes())
    search_snapshot.verify_manifest(report, directory / "pages", directory / "space",
                                    directory / "normalized-source.json")
    return report


def _create_or_verify(store: ObjectStore, key: str, expected: bytes) -> None:
    observed = store.read_optional(key)
    if observed is None:
        try:
            store.create_immutable(key, expected)
        except Exception:
            # A conflict or lost response has an unknown outcome. Inspect the
            # exact key once; never blindly replay a write.
            observed = store.read_optional(key)
            if observed != expected:
                raise
        else:
            observed = store.read_optional(key)
    if observed != expected:
        raise ValueError(f"immutable search object differs or is unavailable: {key}")


def stage_generation(directory: Path, store: ObjectStore) -> dict:
    """Stage a fully rechecked local generation; no mutable root is touched."""
    report = _verify_local_report(directory)
    manifest = make_manifest(report)
    manifest_key = f"generations/{report['generation']}/manifest.json"
    previous = store.read_optional(manifest_key)
    if previous is not None:
        try:
            previous_manifest = json.loads(previous)
            checks = previous_manifest.get("additional_checks")
            base = {k: v for k, v in previous_manifest.items() if k != "additional_checks"}
            if (base != manifest or (checks is not None and
                    (not isinstance(checks, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                         for k, v in checks.items())))
                    or search_snapshot.canonical(previous_manifest) != previous):
                raise ValueError("existing manifest does not match the local immutable identity")
        except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid existing immutable search manifest") from error
        # Auxiliary observations are not proof of provenance or publishability.
        # Keep the existing canonical bytes rather than overwriting them.
        manifest_bytes = previous
    else:
        manifest_bytes = search_snapshot.canonical(manifest)
    objects = {}
    for surface, entries in manifest["objects"].items():
        for name, metadata in entries.items():
            source = directory / surface / name
            raw = source.read_bytes()
            if len(raw) != metadata["bytes"] or hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
                raise ValueError(f"local search member changed before staging: {surface}/{name}")
            existing = objects.setdefault(metadata["object"], raw)
            if existing != raw:
                raise ValueError("content-address collision")
    for key, raw in sorted(objects.items()):
        _create_or_verify(store, key, raw)
    _create_or_verify(store, manifest_key, manifest_bytes)
    return {"generation": report["generation"], "objects": len(objects),
            "logical_files": sum(len(entries) for entries in manifest["objects"].values()),
            "publication_ready": False, "pointer_written": False}
