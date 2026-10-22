#!/usr/bin/env python3
"""Plan only Reader stream workers with pending source records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from huggingface_hub import HfFileSystem

try:
    from .reader_assets import bucket_conversion_contract, decode_search_payload, relative_path
    from .reader_stream_index import merge_shards, reusable_entry, unchanged_sources
    from .reader_bucket_store import retry_read
except ImportError:
    from reader_assets import bucket_conversion_contract, decode_search_payload, relative_path
    from reader_stream_index import merge_shards, reusable_entry, unchanged_sources
    from reader_bucket_store import retry_read


KINDS = {
    "image": {"extensions": ("jpg", "jpeg", "png", "bmp", "webp", "gif"), "sharded": True},
    "media": {"extensions": ("audio", "video", "swf"), "sharded": True},
    "ebook": {"extensions": ("epub", "mobi", "azw3", "fb2", "chm"), "sharded": True},
    "office-chapters": {"extensions": ("doc", "docx", "odt", "rtf"), "sharded": False},
    "converted-ebook": {"extensions": ("mobi", "azw3", "fb2"), "sharded": False},
    "text": {"extensions": ("txt", "md", "markdown", "vcf", "ini"), "sharded": False},
    "office": {"extensions": ("doc", "docx", "odt", "rtf"), "sharded": False},
    "spreadsheet": {"extensions": ("xls", "xlsx", "csv", "ods"), "sharded": False},
    "web": {"extensions": ("html", "htm", "mht", "mhtml"), "sharded": False},
    "static-pdf": {"extensions": ("ppt", "pptx", "pps", "wps", "ps", "tif", "tiff"), "sharded": False},
}


def args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--extension", action="append", dest="extensions")
    p.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    p.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    p.add_argument("--bucket", default="vomebook/reader-assets-v2")
    p.add_argument("--shard-count", type=int, default=8)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--retry-failures", action="store_true")
    p.add_argument("--verify-source-sha", action="store_true")
    p.add_argument("--report", type=Path)
    return p.parse_args()


def read_json(fs: HfFileSystem, bucket: str, path: str) -> dict:
    def load():
        with fs.open(f"hf://buckets/{bucket}/{path}", "rb") as stream:
            value = json.loads(stream.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"invalid Reader stream index: {bucket}/{path}")
        return value
    try:
        return retry_read(load)
    except FileNotFoundError:
        return {}


def source_extension(record: dict) -> str:
    return str(record.get("Extension") or "").lower().lstrip(".")


def selected_records(path: Path, revisions: dict, kind: str, wanted: set[str]) -> list[dict]:
    records = decode_search_payload(json.loads(path.read_text(encoding="utf-8")))
    result = []
    for record in records:
        ext = source_extension(record)
        repo = str(record.get("Repo") or "")
        rel = relative_path(record)
        revision = str(revisions.get(repo) or "")
        if not repo or not revision:
            continue
        if kind == "media":
            contract = bucket_conversion_contract(repo, rel, ext, int(record.get("Size") or 0))
            media_kind = contract[1] if contract else ("swf" if ext == "swf" else "")
            # Media worker names are audio/video/swf, while source extensions vary.
            if media_kind not in wanted:
                continue
            ext = media_kind
        elif ext not in wanted:
            continue
        result.append({"key": f"{repo}\0{rel}", "extension": ext, "revision": revision})
    return result


def root_for(kind: str, extension: str) -> str:
    if kind == "image":
        return "pages/image"
    if kind == "media":
        return f"media/{extension}"
    if kind in {"ebook", "converted-ebook"}:
        return f"chapters/ebook/{extension}"
    if kind == "office-chapters":
        # Office sources are normalized to EPUB chapters, so all four source
        # extensions share one canonical index and one GC-visible namespace.
        return "chapters/ebook/epub"
    return {
        "text": "documents/text",
        "office": f"documents/office/{extension}",
        "spreadsheet": f"documents/spreadsheet/{extension}",
        "web": f"documents/web/{extension}",
        "static-pdf": f"documents/pdf/{extension}",
    }[kind]


def completed_entries(index: dict, retry_failures: bool = False) -> dict[str, str]:
    completed = {}
    fields = ("files",) if retry_failures else ("files", "failures")
    for field in fields:
        for entry in index.get(field, []):
            if isinstance(entry, dict) and entry.get("key"):
                if field == "files" and not reusable_entry(entry):
                    continue
                completed[entry["key"]] = entry.get("source_revision")
    return completed


def main() -> int:
    a = args()
    if a.shard_count < 1:
        raise ValueError("shard-count must be positive")
    config = KINDS[a.kind]
    requested = set(a.extensions or config["extensions"])
    wanted = set(config["extensions"]) if "all" in requested else requested
    invalid = wanted - set(config["extensions"])
    if invalid:
        raise ValueError(f"unsupported extensions: {sorted(invalid)}")
    revisions = json.loads(a.revisions.read_text(encoding="utf-8"))
    records = selected_records(a.search_data, revisions, a.kind, wanted)
    token = os.environ.get("HF_TOKEN")
    fs = HfFileSystem(token=token)
    include = []
    summaries = []
    for extension in sorted(wanted):
        candidates = [item for item in records if item["extension"] == extension]
        if config["sharded"]:
            canonical = read_json(fs, a.bucket, f"{root_for(a.kind, extension)}/index.json")
            indexes = [read_json(fs, a.bucket, f"{root_for(a.kind, extension)}/index-{shard:02d}.json")
                       for shard in range(a.shard_count)]
            source_extensions = {extension}
            if a.kind == "media":
                source_extensions = {str(x["key"].rsplit(".", 1)[-1]).lower() for x in candidates}
            combined = merge_shards(canonical, [(number, index) for number, index in enumerate(indexes) if index],
                                    "stream-index", source_extensions, a.shard_count)
            unchanged = unchanged_sources(candidates, combined, token) if a.verify_source_sha else set()
            pending_count = 0
            for shard in range(a.shard_count):
                index = indexes[shard]
                completed = completed_entries(index, a.retry_failures)
                completed.update(completed_entries(combined, a.retry_failures))
                pending = [x for x in candidates if completed.get(x["key"]) != x["revision"] and
                            x["key"] not in unchanged and
                            int.from_bytes(hashlib.sha256(x["key"].encode()).digest()[:8], "big") % a.shard_count == shard]
                pending_count += len(pending)
                if pending:
                    include.append({"extension": extension, "shard": shard})
            summaries.append({"extension": extension, "sources": len(candidates), "pending": pending_count,
                              "unchanged_sha": len(unchanged), "checkpoint_failures": len(combined.get("failures", []))})
        else:
            index = read_json(fs, a.bucket, f"{root_for(a.kind, extension)}/index.json")
            completed = completed_entries(index, a.retry_failures)
            if any(completed.get(x["key"]) != x["revision"] for x in candidates):
                include.append({"extension": extension})
            summaries.append({"extension": extension, "sources": len(candidates),
                              "pending": sum(completed.get(x["key"]) != x["revision"] for x in candidates),
                              "checkpoint_failures": len(index.get("failures", []))})
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps({"include": include}, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"include": include}, ensure_ascii=False))
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps({"kind": a.kind, "extensions": summaries}, ensure_ascii=False,
                                      indent=2) + "\n", encoding="utf-8")
        for summary in summaries:
            print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
