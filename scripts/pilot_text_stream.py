#!/usr/bin/env python3
"""Build and publish incremental native text Reader streams."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

try:
    from .reader_assets import decode_search_payload, relative_path, source_url
    from .shared import batch_bucket_files_with_retry
    from .reader_bucket import read_bytes, read_json
    from .reader_lifecycle import processing_record
    from .text_encoding import PROFILE, REVIEWED_SOURCE_REPAIRS, normalize_utf8
except ImportError:
    from reader_assets import decode_search_payload, relative_path, source_url
    from shared import batch_bucket_files_with_retry
    from reader_bucket import read_bytes, read_json
    from reader_lifecycle import processing_record
    from text_encoding import PROFILE, REVIEWED_SOURCE_REPAIRS, normalize_utf8


TARGET_BUCKET = "vomebook/reader-assets-v2"
ROOT = "documents/text"
SUPPORTED_EXTENSIONS = {"txt", "md", "markdown", "vcf", "ini"}
MAX_SOURCE_BYTES = 512 * 1024 * 1024
CHECKPOINT_BYTES = 64 * 1024 * 1024


class Utf8SourceUnchanged(Exception):
    """An already valid UTF-8 original does not need a new derivative."""
    def __init__(self, metadata: dict):
        super().__init__("source is already strict UTF-8; existing mapping retained")
        self.metadata = metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--extension", action="append", dest="extensions")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--bucket", default=TARGET_BUCKET)
    parser.add_argument("--checkpoint-size", type=int, default=64)
    parser.add_argument("--encoding-overrides", type=Path)
    parser.add_argument("--selection", type=Path,
                        help="optional exact source-key to expected SHA-256 map for a guarded rollout")
    parser.add_argument("--report", type=Path, default=Path("output/text-stream-report.json"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def download(url: str, target: Path, token: str | None) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "huggingface.co" or parsed.username or parsed.password:
        raise ValueError("text source must use the canonical HF HTTPS origin")
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            if urllib.parse.urlsplit(newurl).scheme != "https":
                raise ValueError("text source redirect must retain HTTPS")
            redirected = super().redirect_request(request, fp, code, message, headers, newurl)
            if redirected and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(request.full_url).netloc:
                redirected.remove_header("Authorization")
            return redirected
    request = urllib.request.Request(url)
    request.add_header("User-Agent", "VoiceOfML-Reader-Text/2")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(Redirect())
    for attempt in range(4):
        try:
            with opener.open(request, timeout=120) as response, target.open("wb") as output:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_SOURCE_BYTES:
                    raise ValueError("text source exceeds the Reader document limit")
                length = 0
                while chunk := response.read(1024 * 1024):
                    length += len(chunk)
                    if length > MAX_SOURCE_BYTES:
                        raise ValueError("text source exceeds the Reader document limit")
                    output.write(chunk)
            return
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 3:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)


def decode_text(raw: bytes) -> str:
    return normalize_utf8(raw)[0].decode("utf-8-sig")


def select_records(path: Path, revisions: dict, extension: str | set[str]) -> list[dict]:
    records = decode_search_payload(json.loads(path.read_text(encoding="utf-8")))
    selected = []
    wanted = SUPPORTED_EXTENSIONS if extension == "all" else ({extension} if isinstance(extension, str) else extension)
    seen = set()
    for record in records:
        ext = str(record.get("Extension") or "").lower().lstrip(".")
        if ext not in SUPPORTED_EXTENSIONS or ext not in wanted:
            continue
        repo = str(record.get("Repo") or "")
        revision = str(revisions.get(repo) or "")
        rel = relative_path(record)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("text source lacks a pinned repository revision")
        if (not rel or "\\" in rel or "\0" in rel or
                any(part in {"", ".", ".."} for part in rel.split("/"))):
            raise ValueError("invalid text source path")
        key = f"{repo}\0{rel}"
        if key not in seen:
            seen.add(key)
            selected.append({"repo": repo, "path": rel, "extension": ext, "revision": revision})
    selected.sort(key=lambda item: (item["extension"], item["repo"], item["path"]))
    return selected


def existing_index(bucket: str, token: str | None) -> dict:
    try:
        payload = read_json(f"{ROOT}/index.json", token, bucket=bucket)
    except FileNotFoundError:
        return {"version": 1, "kind": "text-stream-index", "files": []}
    if payload.get("version") != 1 or payload.get("kind") != "text-stream-index" or not isinstance(payload.get("files"), list):
        raise ValueError("invalid text stream index")
    entries = payload["files"]
    keys = [entry.get("key") for entry in entries if isinstance(entry, dict)]
    if len(keys) != len(entries) or any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("invalid or duplicate text stream keys")
    if not isinstance(payload.get("failures", []), list):
        raise ValueError("invalid text failure checkpoint")
    utf8 = payload.get("verified_utf8", [])
    if not isinstance(utf8, list) or any(not valid_utf8_checkpoint(entry) for entry in utf8):
        raise ValueError("invalid unchanged UTF-8 checkpoint")
    if len({entry["key"] for entry in utf8}) != len(utf8):
        raise ValueError("duplicate unchanged UTF-8 checkpoint")
    return payload


def valid_utf8_checkpoint(entry) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("key"), str) and entry["key"].count("\0") == 1
            and re.fullmatch(r"[0-9a-f]{40}", str(entry.get("source_revision", ""))) is not None
            and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("source_sha256", ""))) is not None
            and type(entry.get("source_bytes")) is int and 0 <= entry["source_bytes"] <= MAX_SOURCE_BYTES
            and entry.get("source_encoding") in {"utf-8", "utf-8-sig"}
            and entry.get("profile") == PROFILE)


def encoding_options(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, str) and value:
        return {"encoding": value}
    if (isinstance(value, dict) and set(value) <= {"encoding", "preserve_controls", "remove_prefix_byte", "reviewed_source_repair"}
            and isinstance(value.get("encoding"), str) and value["encoding"]
            and ("preserve_controls" not in value or
                 isinstance(value["preserve_controls"], dict) and value["preserve_controls"]
                 and all(key == "U+001A" and type(count) is int and count > 0
                         for key, count in value["preserve_controls"].items()))
            and ("remove_prefix_byte" not in value or
                 isinstance(value["remove_prefix_byte"], dict)
                 and set(value["remove_prefix_byte"]) == {"offset", "hex"}
                 and type(value["remove_prefix_byte"]["offset"]) is int
                 and 0 <= value["remove_prefix_byte"]["offset"] < 4096
                 and isinstance(value["remove_prefix_byte"]["hex"], str)
                  and re.fullmatch(r"[0-9a-f]{2}", value["remove_prefix_byte"]["hex"]))
            and ("reviewed_source_repair" not in value or
                 isinstance(value["reviewed_source_repair"], str)
                 and value["reviewed_source_repair"] in REVIEWED_SOURCE_REPAIRS
                 and not {"preserve_controls", "remove_prefix_byte"}.intersection(value))):
        return value
    raise ValueError("invalid exact-source encoding/control override")


def build_one(item: dict, work: Path, token: str | None, bucket: str,
              encoding: str | dict | None = None, *, skip_utf8: bool = False,
              expected_sha256: str | None = None) -> tuple[dict, dict[str, str]]:
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source"
    download(source_url(item["repo"], item["revision"], item["path"]), source, token)
    raw = source.read_bytes()
    source_digest = hashlib.sha256(raw).hexdigest()
    options = encoding_options(encoding)
    if "reviewed_source_repair" in options and expected_sha256 is None:
        raise ValueError("reviewed source repair requires an exact approved source checksum")
    if expected_sha256 is not None and source_digest != expected_sha256:
        raise ValueError("source checksum differs from the approved selection")
    normalized, metadata = normalize_utf8(raw, hint=item["path"].rsplit("/", 1)[-1], **options)
    if (skip_utf8 and metadata["source_encoding"] in {"utf-8", "utf-8-sig"}
            and not {"removed_prefix_byte", "reviewed_source_repair"}.intersection(metadata)):
        raise Utf8SourceUnchanged({"key": f"{item['repo']}\0{item['path']}",
            "source_revision": item["revision"], "source_sha256": source_digest,
            "source_bytes": len(raw), "source_encoding": metadata["source_encoding"]})
    if len(normalized) > MAX_SOURCE_BYTES:
        raise ValueError("UTF-8 output exceeds the Reader document limit")
    output_digest = hashlib.sha256(normalized).hexdigest()
    ext = item["extension"]
    output_ext = "md" if ext == "markdown" else ext
    root = f"{ROOT}/{ext}/{output_digest}"
    object_path = f"{root}/document.{output_ext}"
    # Version the manifest name too: an old producer may already have emitted
    # identical document bytes under a different metadata contract.
    manifest_name = f"{PROFILE}-document-manifest.json"
    manifest_path = f"{root}/{manifest_name}"
    output = work / f"document.{output_ext}"
    output.write_bytes(normalized)
    manifest = {
        "kind": "text-document-stream",
        "version": 1,
        "source_extension": ext,
        # Source identity belongs to the index entry. Identical normalized
        # content shares one immutable manifest, even across source encodings.
        "profile": PROFILE, "encoding": "utf-8", "bom": True,
        "bytes": len(normalized),
        "sha256": output_digest,
        "path": f"document.{output_ext}",
    }
    manifest_file = work / manifest_name
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    files = {object_path: str(output), manifest_path: str(manifest_file)}
    if "reviewed_source_repair" in metadata:
        # Retain the entire unmodified input alongside this protected bundle.
        # The qualified index reference keeps it reachable after handoff, too.
        original_path = f"{root}/original-{source_digest}.bin"
        files[original_path] = str(source)
        metadata["retained_source"] = {"bucket": bucket, "path": original_path,
                                       "sha256": source_digest, "bytes": len(raw), "role": "provenance"}
    return {
        "key": f"{item['repo']}\0{item['path']}", "repo": item["repo"], "path": item["path"],
        "extension": ext, "source_revision": item["revision"], "source_sha256": source_digest,
        "mode": "document-stream", "bucket": bucket, "object": object_path,
        "manifest": manifest_path, "bytes": len(normalized), "sha256": output_digest,
        **metadata,
    }, files


def pending_records(records: list[dict], previous: dict, *, force=False, retry_failures=False) -> list[dict]:
    completed = {entry["key"]: entry.get("source_revision") for entry in previous.get("files", [])
                 if entry.get("profile") == PROFILE}
    failed = {entry["key"]: entry.get("source_revision") for entry in previous.get("failures", [])
              if isinstance(entry, dict) and entry.get("profile") == PROFILE}
    completed.update({entry["key"]: entry["source_revision"] for entry in previous.get("verified_utf8", [])
                      if valid_utf8_checkpoint(entry)})
    return [item for item in records if force or
            completed.get(f"{item['repo']}\0{item['path']}") != item["revision"] and
            (retry_failures or failed.get(f"{item['repo']}\0{item['path']}") != item["revision"])]


def publish_checkpoint(previous: dict, entries: list[dict], failures: list[dict], uploads: dict,
                       root: Path, bucket: str, token: str | None, *, unchanged_utf8=()) -> dict:
    """Protect, upload and verify immutable bytes before advancing the index."""
    run = {key: os.environ.get(name, "") for key, name in
           (("repository", "GITHUB_REPOSITORY"), ("run_id", "GITHUB_RUN_ID"), ("attempt", "GITHUB_RUN_ATTEMPT"))}
    protections = []
    for number, entry in enumerate(entries):
        path = f"reader-index/processing/text-{uuid.uuid4().hex}.json"
        record = processing_record([{"bucket": bucket, "root": entry["object"].rsplit("/", 1)[0]}], "text-utf8-upload", run)
        local = root / f"protection-{number}.json"
        local.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        protections.append((str(local), path))
        entry["processing_roots"] = [path]
    if protections:
        batch_bucket_files_with_retry(bucket, protections, token)
    additions = []
    for remote, local in sorted(uploads.items()):
        expected = Path(local).read_bytes()
        try:
            current = read_bytes(remote, token, bucket=bucket)
        except FileNotFoundError:
            additions.append((local, remote))
        else:
            if current != expected:
                raise ValueError("immutable text object already has different bytes")
    if additions:
        batch_bucket_files_with_retry(bucket, additions, token)
    for remote, local in sorted(uploads.items()):
        if read_bytes(remote, token, bucket=bucket) != Path(local).read_bytes():
            raise ValueError("immutable text object readback failed")
    checked = [{**entry, "profile": PROFILE} for entry in unchanged_utf8]
    if any(not valid_utf8_checkpoint(entry) for entry in checked):
        raise ValueError("unchanged UTF-8 source lacks a complete verification identity")
    merged = {entry["key"]: entry for entry in previous["files"]}
    merged.update({entry["key"]: entry for entry in entries})
    failed = {entry["key"]: entry for entry in previous.get("failures", [])}
    for entry in [*entries, *checked]:
        failed.pop(entry["key"], None)
    failed.update({entry["key"]: entry for entry in failures})
    payload = {**previous, "files": [merged[key] for key in sorted(merged)],
               "failures": [failed[key] for key in sorted(failed)]}
    verified = {entry["key"]: entry for entry in previous.get("verified_utf8", [])}
    for entry in [*entries, *failures]:
        verified.pop(entry["key"], None)
    verified.update({entry["key"]: entry for entry in checked})
    if verified or "verified_utf8" in previous:
        payload["verified_utf8"] = [verified[key] for key in sorted(verified)]
    # The workflow's reader-sidecar lock serializes writers; this is an extra
    # expected-parent check, not a claim that the bucket supports compare/swap.
    if existing_index(bucket, token) != previous:
        raise ValueError("text index changed during conversion; refusing stale publication")
    local_index = root / "index.json"
    local_index.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    batch_bucket_files_with_retry(bucket, [(str(local_index), f"{ROOT}/index.json")], token)
    if existing_index(bucket, token) != payload:
        raise ValueError("text index readback failed")
    return payload


def main() -> int:
    args = parse_args()
    if args.limit < 0 or not 1 <= args.checkpoint_size <= 256:
        raise ValueError("limit must be non-negative; checkpoint-size must be 1..256")
    if args.apply and args.plan_only:
        raise ValueError("--apply and --plan-only are mutually exclusive")
    if args.bucket != TARGET_BUCKET:
        raise ValueError("text publication requires the current Reader bucket")
    token = os.environ.get("HF_TOKEN")
    if args.apply and not token:
        raise ValueError("HF_TOKEN is required for text publication")
    wanted = {ext.lower().lstrip(".") for ext in args.extensions or ["all"]}
    if "all" in wanted:
        wanted = set(SUPPORTED_EXTENSIONS)
    if wanted - SUPPORTED_EXTENSIONS:
        raise ValueError("unsupported text extension")
    overrides = json.loads(args.encoding_overrides.read_text(encoding="utf-8")) if args.encoding_overrides else {}
    if not isinstance(overrides, dict) or any(not isinstance(key, str) or key.count("\0") != 1 for key in overrides):
        raise ValueError("encoding overrides must map exact source keys to reviewed encodings/options")
    for value in overrides.values():
        if value is None:
            raise ValueError("an encoding override cannot be null")
        encoding_options(value)
    revisions = json.loads(args.revisions.read_text(encoding="utf-8"))
    previous = existing_index(args.bucket, token)
    records = select_records(args.search_data, revisions, wanted)
    selection_path = getattr(args, "selection", None)
    selection = json.loads(selection_path.read_text(encoding="utf-8")) if selection_path else {}
    if (not isinstance(selection, dict) or len(selection) > 256 or
            any(not isinstance(key, str) or key.count("\0") != 1 or
                not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for key, digest in selection.items())):
        raise ValueError("selection must map at most 256 exact source keys to expected SHA-256 digests")
    if selection:
        known = {f"{item['repo']}\0{item['path']}" for item in records}
        if set(selection) - known:
            raise ValueError("approved selection contains a source absent from the current metadata scope")
        records = [item for item in records if f"{item['repo']}\0{item['path']}" in selection]
    if args.apply and set(overrides) - set(selection):
        raise ValueError("published encoding/control overrides require an approved source-SHA selection")
    pending = pending_records(records, previous, force=args.force, retry_failures=args.retry_failures)
    selected = pending
    if args.limit:
        selected = selected[:args.limit]
    report = {"profile": PROFILE, "apply": args.apply, "plan_only": args.plan_only,
              "search_data_sha256": hashlib.sha256(args.search_data.read_bytes()).hexdigest(),
              "revisions_sha256": hashlib.sha256(args.revisions.read_bytes()).hexdigest(),
              "sources": len(records), "pending": len(pending), "selected": len(selected),
              "attempted": 0, "converted": 0, "published": 0,
              "unchanged_utf8": [], "converted_sources": [], "failures": [], "decisions": {}, "checkpoint_complete": False}
    if selection:
        report["selection_sha256"] = hashlib.sha256(selection_path.read_bytes()).hexdigest()
    if args.encoding_overrides:
        report["encoding_overrides_sha256"] = hashlib.sha256(args.encoding_overrides.read_bytes()).hexdigest()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    save()
    if args.plan_only:
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0
    cursor = 0
    while cursor < len(selected):
        uploads, entries, failures, checked_utf8 = {}, [], [], []
        with tempfile.TemporaryDirectory(prefix="reader-text-") as directory:
            root = Path(directory)
            batch_bytes = 0
            for number in range(args.checkpoint_size):
                if cursor >= len(selected):
                    break
                item = selected[cursor]
                cursor += 1
                key = f"{item['repo']}\0{item['path']}"
                try:
                    entry, files = build_one(item, root / str(number), token, args.bucket, overrides.get(key),
                                             skip_utf8=True, expected_sha256=selection.get(key))
                except Utf8SourceUnchanged as unchanged:
                    report["unchanged_utf8"].append(unchanged.metadata)
                    # The durable ledger changes no source byte or Reader mapping.
                    # It prevents a bounded next batch from selecting these files forever.
                    checked_utf8.append(unchanged.metadata)
                except (ValueError, UnicodeError) as error:
                    failures.append({"key": key, "source_revision": item["revision"], "profile": PROFILE,
                                      "error": f"{type(error).__name__}: {error}"})
                else:
                    entries.append(entry)
                    uploads.update(files)
                    decision = entry["source_encoding"] + ":" + entry["encoding_decision"]
                    report["decisions"][decision] = report["decisions"].get(decision, 0) + 1
                batch_bytes += sum(path.stat().st_size for path in (root / str(number)).rglob("*") if path.is_file())
                # This bounds staging, not document contents. A single complete
                # permitted document may exceed the soft checkpoint byte budget.
                if batch_bytes >= CHECKPOINT_BYTES:
                    break
            if args.apply and (entries or failures or checked_utf8):
                previous = publish_checkpoint(previous, entries, failures, uploads, root, args.bucket, token,
                                              unchanged_utf8=checked_utf8)
                report["published"] += len(entries)
            report["attempted"] = cursor
            report["converted"] += len(entries)
            report["converted_sources"].extend({key:entry[key] for key in
                ("key", "source_revision", "source_sha256", "source_encoding", "encoding_decision",
                 "object", "manifest", "sha256", "bytes", "characters", "preserved_controls", "source_roundtrip_verified",
                  "removed_prefix_byte", "reviewed_source_repair", "replaced_source_span", "retained_source", "normalization_recipe")
                 if key in entry} for entry in entries)
            report["failures"].extend(failures)
            save()
            print(f"verified checkpoint: converted={report['converted']} failures={len(report['failures'])}", flush=True)
    report["checkpoint_complete"] = True
    report["unattempted"] = len(pending) - len(selected)
    scope_keys = {f"{item['repo']}\0{item['path']}" for item in records}
    report["unresolved_failures"] = len([entry for entry in previous.get("failures", [])
        if entry.get("key") in scope_keys]) if args.apply else len(report["failures"])
    save()
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("checkpoint_complete=true\n")
            output.write(f"published_count={report['published']}\n")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return int(bool(report["failures"] or report["unresolved_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
