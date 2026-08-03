#!/usr/bin/env python3
"""Mirror BHA publication source files into a Hugging Face dataset."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi

UPSTREAM_OWNER = os.environ.get("BHA_ARCHIVE_OWNER", "banned-historical-archives")
REPO_START = int(os.environ.get("REPO_START", "0"))
REPO_END = int(os.environ.get("REPO_END", "31"))
HF_REPO = os.environ.get("HF_SOURCE_REPOSITORY", "vomebook/BHA-Source-Files")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
MANIFEST_NAME = "manifest.json"
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
PACE_OBJECTS_PER_MINUTE = float(os.environ.get("MIRROR_PACE_OBJECTS_PER_MINUTE", "120"))
PACE_MAX_SLEEP_SECONDS = int(os.environ.get("MIRROR_PACE_MAX_SLEEP_SECONDS", "600"))
DOWNLOAD_CONCURRENCY = int(os.environ.get("MIRROR_DOWNLOAD_CONCURRENCY", "8"))


def api_json(url: str, token: str = "") -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def github_tree(repo: str) -> list[dict]:
    url = f"https://api.github.com/repos/{UPSTREAM_OWNER}/{repo}/git/trees/parsed?recursive=1"
    data = api_json(url, os.environ.get("GH_PAT", ""))
    if not data.get("truncated"):
        return [item for item in data.get("tree", []) if item.get("type") == "blob"]
    print(f"GitHub tree is truncated for {repo}; falling back to a shallow clone", flush=True)
    return cloned_parsed_blobs(repo)


def cloned_parsed_blobs(repo: str) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix=f"{repo}-parsed-") as clone_dir:
        ret, out, err = run(
            [
                "git", "clone", "--depth", "1", "--single-branch", "--branch", "parsed",
                "--filter=blob:none", "--no-checkout",
                f"https://github.com/{UPSTREAM_OWNER}/{repo}", clone_dir,
            ],
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        if ret != 0:
            raise RuntimeError(f"parsed branch clone failed for {repo}: {err or out}")
        ret, out, err = run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=clone_dir)
        if ret != 0:
            raise RuntimeError(f"parsed branch listing failed for {repo}: {err or out}")
        return [{"path": line, "type": "blob"} for line in out.splitlines() if line]


def read_metadata(repo: str, path: str) -> dict:
    url = f"https://raw.githubusercontent.com/{UPSTREAM_OWNER}/{repo}/parsed/{urllib.parse.quote(path, safe='/')}"
    return api_json(url, os.environ.get("GH_PAT", "")) if path.endswith(".json") else json.loads(download_bytes(url))


def download_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "anftm-source-mirror/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read(MAX_FILE_BYTES + 1)


def download_to_path(url: str, target: Path) -> tuple[str, int]:
    request = urllib.request.Request(url, headers={"User-Agent": "anftm-source-mirror/1.0"})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise RuntimeError(f"file exceeds {MAX_FILE_BYTES} bytes")
            digest.update(chunk)
            output.write(chunk)
    return digest.hexdigest(), size


def source_urls(archive_id: int) -> list[str]:
    repo = f"banned-historical-archives{archive_id}"
    urls: set[str] = set()
    for item in github_tree(repo):
        path = str(item.get("path") or "")
        if not path.endswith(".metadata"):
            continue
        url = f"https://raw.githubusercontent.com/{UPSTREAM_OWNER}/{repo}/parsed/{urllib.parse.quote(path, safe='/')}"
        publication = json.loads(download_bytes(url))
        for value in publication.get("files") or []:
            value = str(value).strip()
            if value.startswith(("http://", "https://")):
                urls.add(value)
    return sorted(urls)


def mirror_path(archive_id: int, digest: str, url: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower() or ".bin"
    return f"archives{archive_id}/{digest[:2]}/{digest}{suffix}"


def run(args: list[str], cwd: str | None = None, env: dict | None = None) -> tuple[int, str, str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    result = subprocess.run(args, cwd=cwd, env=merged, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def pace_seconds(object_count: int) -> int:
    if object_count <= 0 or PACE_OBJECTS_PER_MINUTE <= 0:
        return 0
    expected = float(object_count) / (float(PACE_OBJECTS_PER_MINUTE) / 60.0)
    return min(int(expected), PACE_MAX_SLEEP_SECONDS)


def _download_one(url: str, temp_dir: str) -> tuple[str, str, int, Path] | None:
    with tempfile.NamedTemporaryFile(prefix="source-", dir=temp_dir, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        digest, size = download_to_path(url, temporary)
        return url, digest, size, temporary
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            temporary.unlink(missing_ok=True)
            raise
        try:
            recovered = historical_url(url)
        except Exception as recovery_exc:
            print(f"history recovery failed for {url}: {recovery_exc}", flush=True)
            recovered = None
        if recovered is not None:
            try:
                digest, size = download_to_path(recovered, temporary)
                print(f"recovered {url} from commit history", flush=True)
                return url, digest, size, temporary
            except urllib.error.HTTPError as retry_exc:
                if retry_exc.code != 404:
                    temporary.unlink(missing_ok=True)
                    raise
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        temporary.unlink(missing_ok=True)
        print(f"missing upstream (404), skipped: {url}", flush=True)
        return None
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def historical_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    parts = parsed.path.split("/")
    if parsed.netloc != "raw.githubusercontent.com" or len(parts) < 5:
        return None
    owner, repo, ref = parts[1], parts[2], parts[3]
    relpath = "/".join(parts[4:])
    token = os.environ.get("GH_PAT", "")
    commits = api_json(
        f"https://api.github.com/repos/{owner}/{repo}/commits"
        f"?path={urllib.parse.quote(relpath, safe='/')}&sha={urllib.parse.quote(ref)}&per_page=5",
        token,
    )
    if not isinstance(commits, list) or not commits:
        return None
    newest = commits[0].get("sha")
    if not newest:
        return None
    detail = api_json(f"https://api.github.com/repos/{owner}/{repo}/commits/{newest}", token)
    parents = detail.get("parents") or []
    if not parents or not parents[0].get("sha"):
        return None
    parent = parents[0]["sha"]
    entry = api_json(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{urllib.parse.quote(relpath, safe='/')}?ref={parent}",
        token,
    )
    if not isinstance(entry, dict) or entry.get("type") != "file":
        return None
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{parent}/{relpath}"


def remote_manifest(api: HfApi) -> dict:
    if not api.file_exists(repo_id=HF_REPO, repo_type="dataset", filename=MANIFEST_NAME):
        return {"version": 1, "files": {}}
    local_path = api.hf_hub_download(repo_id=HF_REPO, repo_type="dataset", filename=MANIFEST_NAME)
    return json.loads(Path(local_path).read_text(encoding="utf-8"))


def upload_archive(api: HfApi, archive_id: int, files: dict[str, dict], updates: dict[str, dict], temp_dir: str) -> None:
    operations = [
        CommitOperationAdd(path_in_repo=meta["path"], path_or_fileobj=str(Path(temp_dir) / meta["path"]))
        for meta in updates.values()
    ]
    manifest_bytes = (
        json.dumps({"version": 1, "files": dict(sorted(files.items()))}, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    operations.append(CommitOperationAdd(path_in_repo=MANIFEST_NAME, path_or_fileobj=manifest_bytes))
    wait = pace_seconds(len(operations))
    if wait:
        print(
            f"archive{archive_id}: pacing upload by {wait}s for {len(operations)} new file(s) "
            "below the HF rate limit",
            flush=True,
        )
        time.sleep(wait)
    print(f"archive{archive_id}: committing {len(operations)} file(s) to {HF_REPO} via Hub API", flush=True)
    api.create_commit(
        repo_id=HF_REPO,
        repo_type="dataset",
        operations=operations,
        commit_message=f"Update BHA source files archive{archive_id}",
    )
    print(f"archive{archive_id}: commit complete", flush=True)


def mirror_archive(api: HfApi, temp_dir: str, archive_id: int, files: dict[str, dict]) -> int:
    urls = source_urls(archive_id)
    print(f"archive{archive_id}: {len(urls)} source URLs", flush=True)
    pending = [
        url for url in urls
        if not (
            files.get(url)
            and api.file_exists(repo_id=HF_REPO, repo_type="dataset", filename=files[url]["path"])
        )
    ]
    if not pending:
        print(f"archive{archive_id}: no new files", flush=True)
        return 0

    print(f"archive{archive_id}: {len(pending)} new file(s) to download", flush=True)
    downloads: list[tuple[str, str, int, Path]] = []
    if DOWNLOAD_CONCURRENCY > 1:
        print(f"archive{archive_id}: downloading with {DOWNLOAD_CONCURRENCY} concurrent workers", flush=True)
        with ThreadPoolExecutor(max_workers=DOWNLOAD_CONCURRENCY) as pool:
            futures = {pool.submit(_download_one, url, temp_dir): url for url in pending}
            completed = 0
            for future in as_completed(futures):
                url = futures[future]
                try:
                    downloads.append(future.result())
                except Exception as exc:
                    pool.shutdown(cancel_futures=True)
                    raise RuntimeError(f"archive{archive_id}: download failed for {url}: {exc}") from exc
                completed += 1
                if completed % 50 == 0 or completed == len(pending):
                    print(f"archive{archive_id}: [{completed}/{len(pending)}] files downloaded", flush=True)
    else:
        for url in pending:
            downloads.append(_download_one(url, temp_dir))

    downloads = [item for item in downloads if item is not None]
    if not downloads:
        print(
            f"archive{archive_id}: all {len(pending)} source file(s) missing upstream (404), "
            "nothing to mirror",
            flush=True,
        )
        return 0

    updates: dict[str, dict] = {}
    created_paths: list[Path] = []
    try:
        for url, digest, size, temporary in downloads:
            path = mirror_path(archive_id, digest, url)
            target = Path(temp_dir) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.move(str(temporary), str(target))
                created_paths.append(target)
            else:
                temporary.unlink(missing_ok=True)
            updates[url] = {"archive_id": archive_id, "path": path, "sha256": digest, "bytes": size}
        print(
            f"archive{archive_id}: downloaded {sum(v['bytes'] for v in updates.values()) / 1024 ** 3:.2f} GiB",
            flush=True,
        )
    except Exception:
        for target in created_paths:
            target.unlink(missing_ok=True)
        directories: set[Path] = set()
        for target in created_paths:
            directory = target.parent
            while directory != Path(temp_dir) and directory != directory.parent:
                directories.add(directory)
                directory = directory.parent
        directories = sorted(directories, key=lambda item: len(item.parts), reverse=True)
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise

    if not updates:
        print(f"archive{archive_id}: no new files", flush=True)
        return 0
    files.update(updates)
    upload_archive(api, archive_id, files, updates, temp_dir)
    return len(updates)


def main() -> int:
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is required")
    api = HfApi(token=HF_TOKEN)
    with tempfile.TemporaryDirectory(prefix="bha-source-mirror-") as temp_dir:
        manifest = remote_manifest(api)
        files = manifest.setdefault("files", {})
        failures: list[str] = []
        for archive_id in range(REPO_START, REPO_END + 1):
            try:
                mirrored = mirror_archive(api, temp_dir, archive_id, files)
                print(f"archive{archive_id}: mirrored {mirrored} new files", flush=True)
            except Exception as exc:
                failures.append(f"archive{archive_id}: {exc}")
                print(f"archive{archive_id} failed: {exc}", flush=True)
        if failures:
            print("\n".join(failures))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
