"""Construct the existing Pages and Space search outputs without publishing them.

This is an offline preparation step. It accepts a captured pipeline output and
fixed source revisions, creates both site surfaces in a new directory and
produces the existing local snapshot report. It does not access the network,
write a bucket pointer, advance pipeline state, or modify either site checkout.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path

try:
    from . import search_snapshot, sync_to_pages, sync_to_space
except ImportError:  # Direct execution from scripts/.
    import search_snapshot
    import sync_to_pages
    import sync_to_space


MAX_INPUT_BYTES = 256 * 1024 * 1024


def _read_json_file(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"input must be a bounded ordinary file: {path}")
    return json.loads(path.read_bytes())


def _validate_corpus(value, revisions):
    if (not isinstance(value, dict) or value.get("v") != 2
            or not isinstance(value.get("rp"), list) or not isinstance(value.get("fd"), list)
            or not isinstance(value.get("rc"), list) or not value["rc"]):
        raise ValueError("expected a nonempty v2 compact search corpus")
    repos, folders, rows = value["rp"], value["fd"], value["rc"]
    if (not repos or any(not isinstance(repo, str)
                         or not re.fullmatch(r"VoiceOfML/[A-Za-z0-9._-]+", repo) for repo in repos)
            or len(set(repos)) != len(repos) or not isinstance(revisions, dict)
            or set(revisions) != set(repos)
            or any(not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
                   for revision in revisions.values())):
        raise ValueError("source revisions must exactly cover unique corpus repositories")
    if any(not isinstance(folder, list) or any(not isinstance(part, str) for part in folder)
           for folder in folders):
        raise ValueError("invalid corpus folder dictionary")
    for row in rows:
        if (not isinstance(row, list) or len(row) != 6 or type(row[0]) is not int
                or not 0 <= row[0] < len(repos) or not isinstance(row[1], str)
                or not isinstance(row[2], str) or type(row[3]) is not int
                or not 0 <= row[3] < len(folders) or type(row[5]) is not int
                or row[5] not in (0, 1)):
            raise ValueError("invalid compact search record")
    return rows


def build_generation(source: Path, tree: Path, browser: Path, revisions: Path, output: Path) -> dict:
    """Prepare both surfaces in a fresh directory; return a non-publishable report."""
    inputs = [source, tree, browser, revisions]
    if output.exists() or output.is_symlink():
        raise ValueError("output must not already exist")
    resolved_output = output.resolve()
    if any(resolved_output == p.resolve() or resolved_output in p.resolve().parents for p in inputs):
        raise ValueError("output must not contain or overwrite an input")
    payload = _read_json_file(source)
    source_revisions = _read_json_file(revisions)
    rows = _validate_corpus(payload, source_revisions)
    tree_value, browser_value = _read_json_file(tree), _read_json_file(browser)
    if not isinstance(tree_value, dict) or not isinstance(browser_value, dict):
        raise TypeError("folder metadata must be JSON objects")
    repos = set(payload["rp"])
    if set(tree_value) != repos or set(browser_value) != repos:
        raise ValueError("folder metadata must exactly cover corpus repositories")

    records = sync_to_space.decode_search_payload(payload)
    if len(records) != len(rows):
        raise ValueError("corpus decoder lost records")
    sync_to_space.clear_legacy_ocr_flags(records, browser_value)
    compact = sync_to_space.encode_search_payload(records)
    if sync_to_pages.encode_search_payload(records) != compact:
        raise ValueError("Pages and Space corpus encoders disagree")
    compact_bytes = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tree_gz = gzip.compress(json.dumps(tree_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                            compresslevel=9, mtime=0)
    browser_gz = gzip.compress(json.dumps(browser_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                               compresslevel=9, mtime=0)
    corpus_gz = gzip.compress(compact_bytes, compresslevel=9, mtime=0)

    output.mkdir(parents=True, exist_ok=False)
    pages, space = output / "pages", output / "space"
    pages.mkdir()
    space.mkdir()
    for directory, adapter in ((pages, sync_to_pages), (space, sync_to_space)):
        (directory / "search_data.json.gz").write_bytes(corpus_gz)
        (directory / "folder_tree.json.gz").write_bytes(tree_gz)
        (directory / "folder_browser.json.gz").write_bytes(browser_gz)
        adapter.write_initial_payloads(directory, records)
        adapter.write_sidebar_payloads(directory, records, browser_value)
    fields = sync_to_space.search_fields(records)
    digest = sync_to_space.search_fields_digest(fields)
    sync_to_space.write_word_index(space / "search_words.bin.gz", records, digest)
    for width in (2, 3):
        sync_to_space.write_ngram_index(space / f"search_ngrams_{width}.bin.gz", fields, digest, width)

    # The v2 report intentionally binds the normalized corpus, not the input
    # byte representation, because the existing producer clears retired OCR flags.
    normalized_source = output / "normalized-source.json"
    normalized_source.write_bytes(compact_bytes)
    report = search_snapshot.build_manifest(pages, space, source_revisions, normalized_source)
    (output / "snapshot-report.json").write_bytes(search_snapshot.canonical(report))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="captured output/search_data.json")
    parser.add_argument("--tree", type=Path, required=True, help="captured output/folder_tree.json")
    parser.add_argument("--browser", type=Path, required=True, help="captured output/folder_browser.json")
    parser.add_argument("--revisions", type=Path, required=True, help="captured full source revision map")
    parser.add_argument("--output", type=Path, required=True, help="new local output directory")
    args = parser.parse_args(argv)
    report = build_generation(args.source, args.tree, args.browser, args.revisions, args.output)
    print(json.dumps({"generation": report["generation"], "records": report["record_count"],
                      "publication_ready": report["publication_ready"],
                      "remote_writes": report["remote_writes"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
