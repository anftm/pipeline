"""Strict, startup-pinned OCR text/index releases. No network, pointers or deletion."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import unicodedata


BUCKET = "vomebook/ccrd-index"
TOKENIZER = "cjk-bigram-boundary-fts5-v6-snippet-anchors"
SOURCE = "OCR"
SOURCE_NAME = "VoiceOfML"


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(value) -> str:
    return hashlib.sha256(value).hexdigest()


def sha(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid release SHA-256")
    return value


def safe_path(value: str) -> str:
    if (not isinstance(value, str) or not value or value.startswith("/") or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(char) < 32 for char in value)):
        raise ValueError("invalid release relative path")
    return value


def read_resource(root: Path, ref: dict) -> bytes:
    if not isinstance(ref, dict) or ref.get("bucket") != BUCKET:
        raise ValueError("wrong OCR release bucket")
    path = safe_path(ref.get("path"))
    if not path.startswith(("text/objects/", "text/exports/", "indexes/ocr/")):
        raise ValueError("unmanaged OCR resource path")
    expected = sha(ref.get("sha256"))
    size = ref.get("bytes")
    if type(size) is not int or size < 1:
        raise ValueError("invalid OCR resource size")
    root = root.resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise ValueError("missing OCR release resource or path escape")
    raw = target.read_bytes()
    if len(raw) != size or digest(raw) != expected:
        raise ValueError("OCR release resource checksum mismatch")
    return raw


def export_record(root: Path, ref: dict) -> tuple[dict, dict]:
    export = json.loads(read_resource(root, ref))
    if (export.get("kind") != "ccrd-ocr-text-export" or export.get("version") != 1
            or export.get("complete") is not True or export.get("recipe") != "ocr-plain-utf8-pages-v1"):
        raise ValueError("unsupported OCR export schema")
    if not re.fullmatch(r"OCR:[1-9][0-9]*", str(export.get("doc_id", ""))) or int(export["doc_id"].split(":")[1]) >= 1 << 63:
        raise ValueError("invalid OCR document identity")
    repo, original = export.get("origin_repo"), safe_path(export.get("origin_rel_path"))
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
        raise ValueError("invalid OCR origin repository")
    if not re.fullmatch(r"[0-9a-f]{40}", str(export.get("origin_revision", ""))):
        raise ValueError("OCR origin must be commit-pinned")
    readable = safe_path(export.get("readable_rel_path"))
    expected_path = repo + "/" + str(PurePosixPath(original).with_suffix(".txt"))
    if readable != expected_path:
        raise ValueError("OCR readable path does not preserve its origin")
    raw = read_resource(root, export.get("text"))
    body = raw.decode("utf-8", errors="strict")
    if body.startswith("\ufeff"):
        raise ValueError("plain OCR export must not hide a BOM in its indexed text")
    identity = {key: export[key] for key in ("recipe", "origin_repo", "origin_rel_path", "origin_revision", "origin_sha256",
                "source_sha256", "doc_id", "text_generation", "txt_sha256")}
    if 'body_admission' in export or 'body_admission_sha256' in export:
        admission = export.get('body_admission', {})
        summary = admission.get('quality_check', {})
        if (admission.get('policy') != 'complete-body-quality-v1'
                or admission.get('source_sha256') != export.get('source_sha256')
                or admission.get('text_generation') != export.get('text_generation')
                or summary.get('status') != 'passed' or summary.get('issues') != []
                or summary.get('checked_pages') != export.get('page_count')
                or summary.get('page_count') != export.get('page_count')
                or [p.get('page') for p in admission.get('pages', [])] != list(range(1, export.get('page_count', 0) + 1))
                or any(p.get('quality_check', {}).get('status') != 'passed'
                       or p.get('quality_check', {}).get('issues') != []
                       or p.get('quality_check', {}).get('page') != p.get('page')
                       for p in admission.get('pages', []))
                or digest(canonical(admission).rstrip(b'\n')) != export.get('body_admission_sha256')):
            raise ValueError('invalid complete-body quality admission')
        identity['body_admission_sha256'] = sha(export['body_admission_sha256'])
    if export.get("source_sha256_role") != "reader-primary":
        raise ValueError("OCR source/derived digest roles must be explicit")
    for field in ("origin_sha256", "source_sha256", "text_generation", "txt_sha256", "generation", "book_id"):
        sha(export.get(field))
    generation = digest(canonical(identity).rstrip(b"\n"))
    if (generation != export["generation"] or digest(raw) != export["txt_sha256"]
            or export["book_id"] != digest((repo + "\0" + original).encode())):
        raise ValueError("OCR export generation mismatch")
    pages, count = export.get("pages"), export.get("page_count")
    if type(count) is not int or count < 1 or not isinstance(pages, list) or len(pages) != count:
        raise ValueError("OCR export is missing pages")
    if export.get("offset_unit") != "unicode-codepoint" or export.get("page_separator") != "\n\n":
        raise ValueError("unsupported page offset recipe")
    cursor, reviewed = 0, 0
    for number, page in enumerate(pages, 1):
        start, end = page.get("start"), page.get("end")
        if (page.get("page") != number or type(start) is not int or type(end) is not int
                or start != cursor or not start <= end <= len(body)):
            raise ValueError("OCR page order/offset mismatch")
        sha(page.get("text_generation"))
        if digest(body[start:end].encode()) != sha(page.get("text_sha256")):
            raise ValueError("OCR page text digest mismatch")
        if page.get("quality") not in {"unreviewed", "partially-reviewed", "accepted"}:
            raise ValueError("invalid OCR page quality")
        reviewed += page["quality"] != "unreviewed"
        cursor = end
        if number < count:
            if body[cursor:cursor + 2] != "\n\n":
                raise ValueError("OCR page separator mismatch")
            cursor += 2
    if cursor != len(body) or export.get("accepted_pages") != reviewed or export.get("quality") != ("partially-reviewed" if reviewed else "unreviewed"):
        raise ValueError("OCR whole-book coverage/quality mismatch")
    resources = export.get("retained_reader_resources")
    if not isinstance(resources, list) or not resources:
        raise ValueError("OCR provenance retention references are missing")
    for resource in resources:
        if resource.get("bucket") not in {"vomebook/reader-assets-v2", "vomebook/pdf-pages-v2", "melsm/pdf-archive-v2"}:
            raise ValueError("unknown retained Reader bucket")
        safe_path(resource.get("path")); sha(resource.get("sha256"))
        if type(resource.get("bytes")) is not int or resource["bytes"] < 1:
            raise ValueError("invalid retained Reader resource")
    path = PurePosixPath(readable)
    record = {"doc_id": export["doc_id"], "source": SOURCE, "source_name": SOURCE_NAME,
              "display_name": path.stem, "display_dirs": list(path.parts[:-1]), "display_rel_path": readable,
              "extension": "txt", "size": len(raw), "has_preview": True, "has_fulltext": True,
              "storage_root": ".", "storage_rel_path": export["text"]["path"],
              "text_generation": export["text_generation"], "text_quality": export["quality"]}
    return record, export


def directory_payloads(records: list[dict]) -> tuple[list[dict], dict]:
    browser = defaultdict(lambda: {"folders": [], "files": []})
    counts, direct = defaultdict(int), defaultdict(list)
    children = defaultdict(set)
    for record in records:
        parts = record["display_dirs"]
        for depth in range(len(parts) + 1):
            path = "/".join(parts[:depth]); counts[path] += 1
            if depth < len(parts): children[path].add(parts[depth])
        direct["/".join(parts)].append(record)
    for path in counts:
        browser[path]["folders"] = [{"name": name, "path": (path + "/" if path else "") + name,
                                      "count": counts[(path + "/" if path else "") + name]} for name in sorted(children[path])]
        browser[path]["files"] = [{"name": item["display_name"], "ext": "txt", "size": item["size"],
                                    "doc_id": item["doc_id"]} for item in sorted(direct[path], key=lambda item: (item["display_name"], item["doc_id"]))]
    def node(name, path):
        return {"name": name, "path": path, "count": counts[path], "hasDirectFiles": bool(direct[path]),
                "hasChildren": bool(children[path]), "showSelfToggle": bool(path and direct[path] and children[path]),
                "children": [node(child, (path + "/" if path else "") + child) for child in sorted(children[path])]}
    # The source slug identifies this collection. Do not add a synthetic
    # `OCR成果/` directory above the preserved origin hierarchy; VoiceOfML,
    # or any other source owner, is the actual top-level folder.
    return [node(child, child) for child in sorted(children[""])], dict(browser)


def zip_member(record: dict) -> str:
    """Keep legal origin hierarchy; bound non-portable components deterministically."""
    replacements = {":": "：", "*": "＊", "?": "？", '"': "＂", "<": "＜", ">": "＞", "|": "｜", "\\": "＼"}
    def component(original):
        value = re.sub(r'[:*?"<>|\\\x00-\x1f]', lambda match: replacements.get(match[0], "_"), original).rstrip(" .") or "_"
        if value != original or len(value.encode()) > 180:
            suffix = "-" + digest(original.encode())[:12]
            raw = value.encode()[:180 - len(suffix)]
            value = raw.decode("utf-8", errors="ignore").rstrip(" .") + suffix
        return value
    parts = safe_path(record["display_rel_path"]).split("/")
    parts[-1] = component(PurePosixPath(parts[-1]).stem) + ".txt"
    return "/".join([component(part) for part in parts])


def validate_database(path: Path, records: list[dict], text_root: Path) -> None:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("invalid OCR SQLite integrity")
        version = db.execute("SELECT value FROM metadata WHERE key='tokenizer_version'").fetchone()
        if version != (TOKENIZER,):
            raise ValueError("OCR tokenizer mismatch")
        ids = {row[0] for row in db.execute("SELECT doc_id FROM documents")}
        if ids != {record["doc_id"] for record in records}:
            raise ValueError("OCR metadata/database ID set mismatch")
        if db.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0] != len(records):
            raise ValueError("OCR content rows mismatch")
        for record in records:
            body = (text_root / record["storage_rel_path"]).read_text(encoding="utf-8", errors="strict")
            normalized = unicodedata.normalize("NFKC", body).casefold().replace("\0", " ")
            row = db.execute("SELECT content FROM content_fts WHERE rowid=?", (int(record["doc_id"].split(":")[1]),)).fetchone()
            if row != (normalized,):
                raise ValueError("OCR indexed content differs from readable TXT")
        columns = {row[1] for row in db.execute("PRAGMA table_info(snippet_anchors)")}
        if not {"token_hash", "doc_number", "char_offset"} <= columns:
            raise ValueError("OCR snippet anchor schema missing")


def load_release(bucket: Path, manifest_path: str, expected_sha256: str, base_metadata_sha256: str) -> dict:
    safe_path(manifest_path); sha(expected_sha256); sha(base_metadata_sha256)
    bucket = bucket.resolve()
    target = (bucket / manifest_path).resolve()
    if bucket not in target.parents or not manifest_path.startswith("releases/ocr-"):
        raise ValueError("OCR release path outside immutable release namespace")
    raw = target.read_bytes()
    if digest(raw) != expected_sha256:
        raise ValueError("OCR release manifest checksum mismatch")
    manifest = json.loads(raw)
    if (manifest.get("kind") != "ccrd-ocr-search-release" or manifest.get("version") != 1
            or manifest.get("tokenizer_version") != TOKENIZER or manifest.get("base_metadata_sha256") != base_metadata_sha256):
        raise ValueError("incompatible OCR release/base snapshot")
    sha(manifest.get("generation")); sha(manifest.get("builder_sha256"))
    identity = {key: manifest[key] for key in ("version", "kind", "base_metadata_sha256", "builder_sha256", "tokenizer_version", "exports")}
    if (manifest["generation"] != digest(canonical(identity))
            or manifest_path != f'releases/ocr-{manifest["generation"]}/manifest.json'):
        raise ValueError("OCR release identity/path mismatch")
    refs = manifest.get("exports")
    if not isinstance(refs, list) or not refs:
        raise ValueError("empty OCR release")
    records, exports = [], []
    ids, paths = set(), set()
    for ref in refs:
        record, export = export_record(bucket, ref)
        if record["doc_id"] in ids or record["display_rel_path"] in paths:
            raise ValueError("OCR ID/readable-path collision; no silent overwrite")
        ids.add(record["doc_id"]); paths.add(record["display_rel_path"])
        records.append(record); exports.append(export)
    if manifest.get("documents") != len(records):
        raise ValueError("OCR release count mismatch")
    database = manifest.get("database")
    read_resource(bucket, database)
    db_path = (bucket / database["path"]).resolve()
    if database["path"] != f'indexes/ocr/{manifest["generation"]}/OCR.sqlite3':
        raise ValueError("OCR database must retain its source namespace")
    validate_database(db_path, records, bucket)
    tree, browser = directory_payloads(records)
    return {"manifest": manifest, "records": records, "exports": exports, "database": db_path,
            "text_paths": {record["doc_id"]: bucket / record["storage_rel_path"] for record in records},
            "tree": tree, "browser": browser}


def pin_release(bucket: Path, manifest_path: str, expected_sha256: str,
                base_metadata_sha256: str, cache: Path) -> dict:
    """Verify the whole candidate before pinning a process-local immutable copy."""
    candidate = load_release(bucket, manifest_path, expected_sha256, base_metadata_sha256)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / candidate["manifest"]["generation"]
    if destination.exists():
        return load_release(destination, manifest_path, expected_sha256, base_metadata_sha256)
    stage = Path(tempfile.mkdtemp(prefix="ocr-stage-", dir=cache))
    try:
        refs = [*candidate["manifest"]["exports"], candidate["manifest"]["database"],
                *[export["text"] for export in candidate["exports"]]]
        for ref in refs:
            raw = read_resource(bucket, ref)
            target = stage / ref["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output:
                output.write(raw); output.flush(); os.fsync(output.fileno())
        target = stage / manifest_path
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest_raw = (bucket / manifest_path).read_bytes()
        if digest(manifest_raw) != expected_sha256:
            raise ValueError("OCR candidate changed while being copied")
        target.write_bytes(manifest_raw)
        load_release(stage, manifest_path, expected_sha256, base_metadata_sha256)
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return load_release(destination, manifest_path, expected_sha256, base_metadata_sha256)
