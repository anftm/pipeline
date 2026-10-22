"""Durable per-format Reader checkpoints and shard-safe canonical merges."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from huggingface_hub import HfApi, HfFileSystem

try:
    from . import reader_lifecycle
    from .shared import batch_bucket_files_with_retry, hash_file
    from .reader_bucket_store import retry_s3
except ImportError:
    import reader_lifecycle
    from shared import batch_bucket_files_with_retry, hash_file
    from reader_bucket_store import retry_s3


def validate_index(value: dict, path: str) -> dict:
    if (not isinstance(value, dict) or not isinstance(value.get("files"), list)
            or not isinstance(value.get("failures", []), list)):
        raise ValueError(f"invalid Reader stream index: {path}")
    for field in ("files", "failures"):
        if any(not isinstance(entry, dict) or not isinstance(entry.get("key"), str)
               or not entry["key"] for entry in value.get(field, [])):
            raise ValueError(f"invalid Reader stream {field}: {path}")
    return value


def read_index(bucket: str, path: str, token: str | None, kind: str) -> dict:
    try:
        fs = HfFileSystem(token=token)
        uri = f"hf://buckets/{bucket}/{path}"
        fs.invalidate_cache(uri)
        with fs.open(uri, "rb") as stream:
            value = json.loads(stream.read().decode("utf-8"))
    except FileNotFoundError:
        return {"version": 1, "kind": kind, "files": [], "failures": []}
    return validate_index(value, f"{bucket}/{path}")


def shard_for(key: str, count: int) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % count


def reusable_entry(entry: dict) -> bool:
    expected = {"ffmpeg-audio-mp3-v1": "audio.mp3",
                "ffmpeg-video-mp4-h264-aac-v1": "video.mp4"}.get(entry.get("profile"))
    return expected is None or str(entry.get("object") or "").endswith("/" + expected)


def merge_indexes(previous: dict, entries: list[dict], failures: list[dict], kind: str) -> dict:
    files = {entry["key"]: entry for entry in previous.get("files", [])}
    errors = {entry["key"]: entry for entry in previous.get("failures", [])}
    for entry in entries:
        old = files.get(entry["key"], {})
        if old.get("checkpointed_at", "") > entry.get("checkpointed_at", ""):
            continue
        files[entry["key"]] = entry
        # A successful old revision must not erase a newer revision's failure.
        failure = errors.get(entry["key"], {})
        if (reusable_entry(entry) and (failure.get("source_revision") == entry.get("source_revision")
                or (entry.get("checkpointed_at") and failure.get("checkpointed_at", "") <= entry["checkpointed_at"]))
                and (not failure.get("profile") or failure.get("profile") == entry.get("profile"))):
            errors.pop(entry["key"], None)
    for failure in failures:
        key = failure["key"]
        current = files.get(key, {})
        if (reusable_entry(current) and current.get("source_revision") == failure.get("source_revision")
                and (not failure.get("profile") or current.get("profile") == failure["profile"])):
            continue
        if errors.get(key, {}).get("checkpointed_at", "") <= failure.get("checkpointed_at", ""):
            errors[key] = failure
    return {"version": 1, "kind": kind,
            "files": [files[key] for key in sorted(files)],
            "failures": [errors[key] for key in sorted(errors)]}


def completed_revisions(index: dict, retry_failures: bool = False) -> dict[str, str]:
    result = {entry["key"]: entry.get("source_revision") for entry in index.get("files", []) if reusable_entry(entry)}
    if not retry_failures:
        result.update({entry["key"]: entry.get("source_revision") for entry in index.get("failures", [])})
    return result


def unchanged_sources(records: list[dict], index: dict, token: str | None) -> set[str]:
    """A repository commit bump alone must not re-download unchanged LFS media/books."""
    files = {entry["key"]: entry for entry in index.get("files", [])}
    groups = {}
    for item in records:
        key = item.get("key") or f"{item['repo']}\0{item['path']}"
        entry = files.get(key, {})
        if (not reusable_entry(entry) or not entry.get("source_sha256")
                or entry.get("source_revision") == item["revision"]):
            continue
        repo, path = key.split("\0", 1)
        groups.setdefault((repo, item["revision"]), []).append((path, key))
    unchanged = set()
    api = HfApi(token=token)
    for (repo, revision), pairs in groups.items():
        for start in range(0, len(pairs), 100):
            batch = pairs[start:start + 100]
            metadata = api.get_paths_info(repo, [path for path, _ in batch], revision=revision,
                                          repo_type="dataset")
            by_path = {entry.path: entry for entry in metadata}
            for path, key in batch:
                remote = by_path.get(path)
                lfs = getattr(remote, "lfs", None)
                digest = lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)
                if digest and digest == files[key]["source_sha256"]:
                    unchanged.add(key)
    return unchanged


def source_failure(item: dict, error: Exception) -> dict:
    return {"key": f"{item['repo']}\0{item['path']}", "repo": item["repo"], "path": item["path"],
            "extension": item.get("extension", ""), "source_revision": item["revision"],
            "checkpointed_at": datetime.now(timezone.utc).isoformat(),
            "error": f"{type(error).__name__}: {error}"[:2000]}


@lru_cache(maxsize=1)
def s3_client():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url="https://s3.hf.co/vomebook",
        aws_access_key_id=os.environ["HF_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["HF_S3_SECRET_ACCESS_KEY"],
        config=Config(region_name="us-east-1", s3={"addressing_style": "path"},
                      connect_timeout=10, read_timeout=60, retries={"mode": "adaptive", "max_attempts": 8}))


def remote_identity(fs: HfFileSystem, bucket: str, path: str) -> tuple[str, int] | None:
    if bucket == "vomebook/reader-assets-v2" and os.environ.get("HF_S3_ACCESS_KEY_ID") and os.environ.get("HF_S3_SECRET_ACCESS_KEY"):
        def read():
            response = s3_client().get_object(Bucket="reader-assets-v2", Key=path)
            body = response["Body"]
            digest, size = hashlib.sha256(), 0
            try:
                while chunk := body.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            finally:
                body.close()
            return digest.hexdigest(), size
        try:
            return retry_s3(read)
        except FileNotFoundError:
            return None
    try:
        with fs.open(f"hf://buckets/{bucket}/{path}", "rb") as stream:
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            return digest.hexdigest(), size
    except FileNotFoundError:
        return None


def publish_checkpoint(bucket: str, path: str, previous: dict, entries: list[dict],
                       failures: list[dict], uploads: dict[str, str], work: Path,
                       token: str | None, *, apply: bool, kind: str) -> dict:
    """Protect/upload content before the index; even all-failed batches persist."""
    roots = []
    for entry in entries:
        entry["checkpointed_at"] = datetime.now(timezone.utc).isoformat()
        resources = {entry["root"]} if entry.get("root") else set()
        for field in ("object", "manifest", "fallback"):
            if entry.get(field):
                resources.add(str(Path(entry[field]).parent))
        resources = {root for root in resources if not any(root.startswith(other + "/")
                                                         for other in resources if other != root)}
        identity = hashlib.sha256((entry["key"] + "\0" + "\0".join(sorted(resources))).encode()).hexdigest()[:32]
        record_path = f"reader-index/processing/stream-upload-{identity}.json"
        entry["processing_roots"] = [record_path]
        record = reader_lifecycle.processing_record(
            [{"bucket": bucket, "root": root} for root in sorted(resources)],
            "reader-stream-upload", {"repository": os.environ.get("GITHUB_REPOSITORY", ""),
                                     "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                                     "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")})
        local = work / "processing" / Path(record_path).name
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        roots.append((str(local), record_path))
    payload = merge_indexes(previous, entries, failures, kind)
    checkpoint = work / "checkpoint.json"
    checkpoint.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if apply:
        if roots:
            batch_bucket_files_with_retry(bucket, roots, token)
        fs = HfFileSystem(token=token)
        identities = {remote: hash_file(Path(local)) for remote, local in uploads.items()}
        additions = []
        for remote, local in sorted(uploads.items()):
            current = remote_identity(fs, bucket, remote)
            if current is None:
                additions.append((local, remote))
            elif current != identities[remote]:
                raise ValueError(f"immutable Reader stream object has different bytes: {remote}")
        if additions:
            batch_bucket_files_with_retry(bucket, additions, token)
        if uploads:
            # Reopen after upload, never infer completeness from upload success.
            fs = HfFileSystem(token=token)
            for remote in uploads:
                if remote_identity(fs, bucket, remote) != identities[remote]:
                    raise ValueError(f"Reader stream object readback failed: {remote}")
        batch_bucket_files_with_retry(bucket, [(str(checkpoint), path)], token)
        if read_index(bucket, path, token, kind) != payload:
            raise ValueError(f"Reader stream checkpoint readback failed: {path}")
    return payload


def merge_shards(canonical: dict, shards: list[tuple[int, dict]], kind: str,
                 source_extensions: set[str], shard_count: int = 8) -> dict:
    """Ignore stale full-index copies belonging to other extensions/shards."""
    result = canonical
    for shard, payload in shards:
        validate_index(payload, f"shard {shard}")

        def owned(entry):
            extension = str(entry.get("source_extension") or
                            Path(entry["key"].split("\0")[-1]).suffix.lstrip(".")).lower()
            return extension in source_extensions and shard_for(entry["key"], shard_count) == shard

        result = merge_indexes(result, [e for e in payload.get("files", []) if owned(e)],
                               [e for e in payload.get("failures", []) if owned(e)], kind)
    return result


def run_documents(selected: list[dict], *, bucket: str, token: str | None, limit: int,
                  retry_failures: bool, strict: bool, apply: bool, prefix: str,
                  kind: str, build, work: Path, checkpoint_every: int = 10) -> int:
    attempts, converted, failed = 0, 0, 0
    categories = sorted({f"{prefix}/{item['extension']}" for item in selected})
    for category in categories:
        path = f"{category}/index.json"
        previous = read_index(bucket, path, token, kind)
        completed = completed_revisions(previous, retry_failures)
        pending = [item for item in selected if f"{prefix}/{item['extension']}" == category
                   and completed.get(f"{item['repo']}\0{item['path']}") != item["revision"]]
        if limit:
            pending = pending[:max(0, limit - attempts)]
        for start in range(0, len(pending), checkpoint_every):
            entries, failures, uploads = [], [], {}
            batch_work = work / category.replace("/", "_") / str(start)
            batch_work.mkdir(parents=True, exist_ok=True)
            for number, item in enumerate(pending[start:start + checkpoint_every]):
                attempts += 1
                try:
                    entry, files = build(item, batch_work / str(number), token, bucket)
                    entries.append(entry)
                    uploads.update(files)
                    converted += 1
                except Exception as error:
                    failures.append(source_failure(item, error))
                    failed += 1
                    print(f"failed: {item['repo']}/{item['path']}: {type(error).__name__}: {error}", flush=True)
            previous = publish_checkpoint(bucket, path, previous, entries, failures, uploads,
                                          batch_work, token, apply=apply, kind=kind)
            print(f"checkpoint={path} converted={len(entries)} failures={len(failures)} apply={apply}", flush=True)
            shutil.rmtree(batch_work)
    print(f"attempted={attempts} converted={converted} failures={failed}", flush=True)
    return 1 if strict and failed else 0
