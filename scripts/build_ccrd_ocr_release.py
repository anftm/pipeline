#!/usr/bin/env python3
"""Build an additive OCR index with the pinned CCRD builder; no uploads/deletes."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3


def normalize_database(path: Path) -> None:
    """Make the pinned builder's SQLite bytes reproducible.

    The builder inserts ``postings`` in Python set/dict order, so identical
    exports produce different bytes under different hash seeds. That breaks
    resuming an interrupted upload of the same release generation. Rewrite the
    table in token order and compact it; the indexed content is unchanged.
    """
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute(
            "CREATE TABLE postings_normalized (token_hash BLOB PRIMARY KEY, "
            "docs BLOB NOT NULL, doc_count INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO postings_normalized (token_hash, docs, doc_count) "
            "SELECT token_hash, docs, doc_count FROM postings ORDER BY token_hash"
        )
        connection.execute("DROP TABLE postings")
        connection.execute("ALTER TABLE postings_normalized RENAME TO postings")
        connection.commit()
        connection.execute("PRAGMA page_size = 4096")
        connection.execute("VACUUM")


def module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def build_release(root: Path, export_refs: list[dict], consumer_root: Path,
                  builder_sha256: str, base_metadata_sha256: str) -> dict:
    consumer = module(consumer_root / "ocr_release.py", "ccrd_ocr_release_contract")
    consumer.sha(builder_sha256); consumer.sha(base_metadata_sha256)
    builder_path = consumer_root / "build_fulltext_db.py"
    if hashlib.sha256(builder_path.read_bytes()).hexdigest() != builder_sha256:
        raise ValueError("CCRD builder differs from approved pinned source")
    refs = sorted(export_refs, key=lambda ref: (ref.get("bucket", ""), ref.get("path", "")))
    records = []
    ids, paths = set(), set()
    for ref in refs:
        record, _export = consumer.export_record(root, ref)
        if record["doc_id"] in ids or record["display_rel_path"] in paths:
            raise ValueError("OCR document ID/path collision; never overwrite a book")
        ids.add(record["doc_id"]); paths.add(record["display_rel_path"])
        records.append(record)
    if not records:
        raise ValueError("cannot build an empty OCR release")
    identity = {"version": 1, "kind": "ccrd-ocr-search-release",
                "base_metadata_sha256": base_metadata_sha256, "builder_sha256": builder_sha256,
                "tokenizer_version": consumer.TOKENIZER, "exports": refs}
    generation = consumer.digest(consumer.canonical(identity))
    relative_db = f"indexes/ocr/{generation}/OCR.sqlite3"
    target = root / relative_db
    manifest_path = f"releases/ocr-{generation}/manifest.json"
    if target.exists() or (root / manifest_path).exists():
        raise ValueError("release generation already exists; use fresh output")
    builder = module(builder_path, "pinned_ccrd_fulltext_builder")
    if builder.TOKENIZER_VERSION != consumer.TOKENIZER:
        raise ValueError("CCRD runtime/builder tokenizer mismatch")
    builder.BASE_DIR = root
    builder.FULLTEXT_DIR = target.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    builder.build_source_db("OCR", records)
    normalize_database(target)
    consumer.validate_database(target, records, root)
    raw = target.read_bytes()
    manifest = {**identity, "generation": generation, "documents": len(records),
                "database": {"bucket": consumer.BUCKET, "path": relative_db,
                             "sha256": consumer.digest(raw), "bytes": len(raw), "role": "runtime"}}
    manifest_raw = consumer.canonical(manifest)
    destination = root / manifest_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(manifest_raw)
    consumer.load_release(root, manifest_path, consumer.digest(manifest_raw), base_metadata_sha256)
    return {"manifest": manifest, "path": manifest_path, "sha256": consumer.digest(manifest_raw),
            "bytes": len(manifest_raw), "remote_writes": 0, "model_calls": 0,
            "publication_gate": "four-bucket reference closure plus serialized promotion required"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Local exported text/objects and text/exports")
    parser.add_argument("--exports", type=Path, required=True, help="JSON array of exact export references")
    parser.add_argument("--consumer-root", type=Path, required=True)
    parser.add_argument("--builder-sha256", required=True)
    parser.add_argument("--base-metadata-sha256", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = build_release(args.root, json.loads(args.exports.read_text()), args.consumer_root,
                           args.builder_sha256, args.base_metadata_sha256)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"path": result["path"], "sha256": result["sha256"], "remote_writes": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
