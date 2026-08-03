#!/usr/bin/env python3
"""Mirror BHA publication source files into a Hugging Face dataset."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UPSTREAM_OWNER = os.environ.get("BHA_ARCHIVE_OWNER", "banned-historical-archives")
REPO_START = int(os.environ.get("REPO_START", "0"))
REPO_END = int(os.environ.get("REPO_END", "31"))
HF_USERNAME = os.environ.get("HF_USERNAME", "vomebook")
HF_REPO = os.environ.get("HF_SOURCE_REPOSITORY", "vomebook/BHA-Source-Files")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
MANIFEST_NAME = "manifest.json"
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
PUSH_MAX_ATTEMPTS = int(os.environ.get("MIRROR_PUSH_MAX_ATTEMPTS", "10"))
PUSH_BACKOFF_SECONDS = int(os.environ.get("MIRROR_PUSH_BACKOFF_SECONDS", "300"))
PACE_OBJECTS_PER_MINUTE = float(os.environ.get("MIRROR_PACE_OBJECTS_PER_MINUTE", "120"))
PACE_MAX_SLEEP_SECONDS = int(os.environ.get("MIRROR_PACE_MAX_SLEEP_SECONDS", "600"))
LFS_MIN_BYTES = int(os.environ.get("MIRROR_LFS_MIN_BYTES", str(10 * 1024 * 1024)))
DOWNLOAD_CONCURRENCY = int(os.environ.get("MIRROR_DOWNLOAD_CONCURRENCY", "8"))
RATE_LIMIT_MARKERS = ("rate limit", "quota of", "429")


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


def disk_report(path: str) -> str:
    usage = shutil.disk_usage(path)
    used = usage.total - usage.free
    return f"disk used {used / 1024 ** 3:.2f} GiB / {usage.total / 1024 ** 3:.2f} GiB"


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


def is_rate_limited(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def pace_seconds(object_count: int) -> int:
    if object_count <= 0 or PACE_OBJECTS_PER_MINUTE <= 0:
        return 0
    expected = float(object_count) / (float(PACE_OBJECTS_PER_MINUTE) / 60.0)
    return min(int(expected), PACE_MAX_SLEEP_SECONDS)


def lfs_object_count(updates: dict[str, dict]) -> int:
    return sum(1 for value in updates.values() if value.get("bytes", 0) >= LFS_MIN_BYTES)


def write_conditional_lfs_clean(config_dir: Path, min_bytes: int) -> Path:
    script = config_dir / "lfs-clean-conditional.sh"
    script.write_text(
        "#!/bin/sh\n"
        "path=\"$1\"\n"
        "size=\"$(wc -c < \"$path\" 2>/dev/null || printf '0')\"\n"
        f"if [ \"$size\" -ge {min_bytes} ]; then\n"
        "    exec git lfs clean -- \"$path\"\n"
        "fi\n"
        "cat\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def write_lfs_filter_process(config_dir: Path, min_bytes: int) -> Path:
    script = config_dir / "lfs-filter-process.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        '"""Size-gated Git LFS filter process (filter.lfs.process)."""\n'
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import tempfile\n"
        "MAX_PACKET = 65516\n"
        f"MIN_BYTES = {min_bytes}\n"
        "def read_pkt():\n"
        "    header = sys.stdin.buffer.read(4)\n"
        "    if not header:\n"
        "        return None\n"
        "    length = int(header, 16)\n"
        "    if length == 0:\n"
        "        return b''\n"
        "    return sys.stdin.buffer.read(length - 4)\n"
        "def read_txt():\n"
        "    data = read_pkt()\n"
        "    if data is None:\n"
        "        return None\n"
        "    return data[:-1] if data.endswith(b'\\n') else data\n"
        "def write_pkt(data):\n"
        "    sys.stdout.buffer.write(('%04x' % (len(data) + 4)).encode() + data)\n"
        "    sys.stdout.buffer.flush()\n"
        "def write_flush():\n"
        "    sys.stdout.buffer.write(b'0000')\n"
        "    sys.stdout.buffer.flush()\n"
        "def main():\n"
        "    if read_txt() != b'git-filter-client':\n"
        "        return 1\n"
        "    if read_txt() != b'version=2':\n"
        "        return 1\n"
        "    if read_pkt() != b'':\n"
        "        return 1\n"
        "    write_pkt(b'git-filter-server\\n')\n"
        "    write_pkt(b'version=2\\n')\n"
        "    write_flush()\n"
        "    while True:\n"
        "        line = read_txt()\n"
        "        if line is None:\n"
        "            return 1\n"
        "        if line == b'':\n"
        "            break\n"
        "        if not line.startswith(b'capability='):\n"
        "            return 1\n"
        "    write_pkt(b'capability=clean\\n')\n"
        "    write_flush()\n"
        "    while True:\n"
        "        command = pathname = None\n"
        "        while True:\n"
        "            line = read_txt()\n"
        "            if line is None:\n"
        "                return 0\n"
        "            if line == b'':\n"
        "                break\n"
        "            if line.startswith(b'command='):\n"
        "                command = line[len(b'command='):]\n"
        "            elif line.startswith(b'pathname='):\n"
        "                pathname = line[len(b'pathname='):]\n"
        "        if command is None or pathname is None:\n"
        "            return 1\n"
        "        with tempfile.NamedTemporaryFile(prefix='lfs-filter-', delete=False) as tmp:\n"
        "            temp_path = tmp.name\n"
        "            while True:\n"
        "                chunk = read_pkt()\n"
        "                if chunk is None:\n"
        "                    return 0\n"
        "                if chunk == b'':\n"
        "                    break\n"
        "                tmp.write(chunk)\n"
        "        try:\n"
        "            size = os.path.getsize(temp_path)\n"
        "            with open(temp_path, 'rb') as source:\n"
        "                if command == b'clean' and size >= MIN_BYTES:\n"
        "                    proc = subprocess.run(\n"
        "                        ['git', 'lfs', 'clean', '--', pathname.decode('utf-8', 'replace')],\n"
        "                        stdin=source, stdout=subprocess.PIPE,\n"
        "                    )\n"
        "                    if proc.returncode != 0:\n"
        "                        write_pkt(b'status=error\\n')\n"
        "                        write_flush()\n"
        "                        continue\n"
        "                    output = proc.stdout\n"
        "                else:\n"
        "                    output = source.read()\n"
        "        finally:\n"
        "            os.unlink(temp_path)\n"
        "        write_pkt(b'status=success\\n')\n"
        "        write_flush()\n"
        "        while output:\n"
        "            write_pkt(output[:MAX_PACKET])\n"
        "            output = output[MAX_PACKET:]\n"
        "        write_flush()\n"
        "        write_flush()\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


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


def push_with_retry(repo_dir: str, askpass: Path) -> None:
    env = {"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "HF_USERNAME": HF_USERNAME}
    for attempt in range(1, PUSH_MAX_ATTEMPTS + 1):
        ret, out, err = run(["git", "push"], cwd=repo_dir, env=env)
        combined = f"{out}\n{err}"
        if ret == 0:
            return
        if not is_rate_limited(combined):
            raise RuntimeError(f"HF push failed: {combined.strip() or '(no output)'}")
        if attempt >= PUSH_MAX_ATTEMPTS:
            break
        print(
            f"HF push hit the request rate limit; waiting {PUSH_BACKOFF_SECONDS}s before retry "
            f"({attempt}/{PUSH_MAX_ATTEMPTS})",
            flush=True,
        )
        time.sleep(PUSH_BACKOFF_SECONDS)
    raise RuntimeError(
        f"HF push kept hitting the request rate limit after {PUSH_MAX_ATTEMPTS} attempts; "
        "wait 5 minutes and re-run (already-mirrored files are skipped)."
    )


def ensure_hf_repo(repo_dir: str) -> None:
    clone_url = f"https://huggingface.co/datasets/{HF_REPO}"
    askpass = Path(repo_dir).parent / "hf-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in *Username*) printf '%s\\n' \"$HF_USERNAME\" ;; *) printf '%s\\n' \"$HF_TOKEN\" ;; esac\n",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    auth_env = {
        "GIT_ASKPASS": str(askpass),
        "GIT_TERMINAL_PROMPT": "0",
        "HF_USERNAME": HF_USERNAME,
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    ret, _, err = run(["git", "clone", "--depth", "1", clone_url, repo_dir], env=auth_env)
    if ret == 0:
        return
    payload = json.dumps({"name": HF_REPO.split("/", 1)[1], "type": "dataset", "private": False, "organization": HF_REPO.split("/", 1)[0]}).encode()
    request = urllib.request.Request("https://huggingface.co/api/repos/create", data=payload, method="POST")
    request.add_header("Authorization", f"Bearer {HF_TOKEN}")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=60):
        pass
    ret, _, retry_err = run(["git", "clone", "--depth", "1", clone_url, repo_dir], env=auth_env)
    if ret != 0:
        raise RuntimeError(f"HF repository clone failed: {err or retry_err}")


def setup_lfs(repo_dir: str) -> None:
    auth = {"GIT_LFS_SKIP_SMUDGE": "1"}
    ret, out, err = run(["git", "lfs", "install", "--local", "--skip-smudge"], cwd=repo_dir, env=auth)
    if ret != 0:
        raise RuntimeError(f"git lfs install failed: {err or out}")
    ret, out, err = run(["git", "lfs", "track", "archives*/**"], cwd=repo_dir, env=auth)
    if ret != 0:
        raise RuntimeError(f"git lfs track failed: {err or out}")
    clean_script = write_conditional_lfs_clean(Path(repo_dir) / ".git", LFS_MIN_BYTES)
    ret, out, err = run(["git", "config", "filter.lfs.clean", f"{clean_script} %f"], cwd=repo_dir, env=auth)
    if ret != 0:
        raise RuntimeError(f"git config failed (filter.lfs.clean): {err or out}")
    process_script = write_lfs_filter_process(Path(repo_dir) / ".git", LFS_MIN_BYTES)
    ret, out, err = run(["git", "config", "filter.lfs.process", str(process_script)], cwd=repo_dir, env=auth)
    if ret != 0:
        raise RuntimeError(f"git config failed (filter.lfs.process): {err or out}")
    ret, out, err = run(["git", "config", "filter.lfs.required", "true"], cwd=repo_dir, env=auth)
    if ret != 0:
        raise RuntimeError(f"git config failed (filter.lfs.required): {err or out}")


def commit_and_push_archive(repo_dir: str, archive_id: int, manifest_path: Path, temp_dir: str, object_count: int = 0) -> None:
    auth = {"GIT_LFS_SKIP_SMUDGE": "1"}
    archive_dir = f"archives{archive_id}"
    ret, _, err = run(["git", "add", manifest_path.name, ".gitattributes", archive_dir], cwd=repo_dir, env=auth)
    if ret != 0:
        raise RuntimeError(f"git add failed: {err}")
    ret, _, err = run(
        [
            "git", "-c", "user.name=github-actions[bot]",
            "-c", "user.email=github-actions[bot]@users.noreply.github.com",
            "commit", "-m", f"Update BHA source files archive{archive_id}",
        ],
        cwd=repo_dir,
        env=auth,
    )
    if ret != 0:
        raise RuntimeError(f"git commit failed: {err}")
    askpass = Path(temp_dir) / "hf-push-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in *Username*) printf '%s\\n' \"$HF_USERNAME\" ;; *) printf '%s\\n' \"$HF_TOKEN\" ;; esac\n",
        encoding="utf-8",
    )
    askpass.chmod(stat.S_IRWXU)
    wait = pace_seconds(object_count)
    if wait:
        print(
            f"archive{archive_id}: pacing push by {wait}s for {object_count} new LFS object(s) "
            f"below the HF quota",
            flush=True,
        )
        time.sleep(wait)
    print(f"archive{archive_id}: pushing Git LFS objects", flush=True)
    push_with_retry(repo_dir, askpass)
    print(f"archive{archive_id}: push complete ({disk_report(repo_dir)})", flush=True)
    run(["git", "lfs", "prune"], cwd=repo_dir, env=auth)
    shutil.rmtree(Path(repo_dir) / archive_dir, ignore_errors=True)
    print(f"archive{archive_id}: pruned LFS objects ({disk_report(repo_dir)})", flush=True)


def mirror_archive(repo_dir: str, temp_dir: str, archive_id: int, files: dict[str, dict]) -> int:
    urls = source_urls(archive_id)
    print(f"archive{archive_id}: {len(urls)} source URLs ({disk_report(repo_dir)})", flush=True)
    pending = [
        url for url in urls
        if not (files.get(url) and (Path(repo_dir) / files[url]["path"]).exists())
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
        print(f"archive{archive_id}: all {len(pending)} source file(s) missing upstream (404), nothing to mirror", flush=True)
        return 0

    updates: dict[str, dict] = {}
    created_paths: list[Path] = []
    try:
        for url, digest, size, temporary in downloads:
            path = mirror_path(archive_id, digest, url)
            target = Path(repo_dir) / path
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
            while directory != Path(repo_dir) and directory != directory.parent:
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
    manifest_path = Path(repo_dir) / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps({"version": 1, "files": dict(sorted(files.items()))}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    commit_and_push_archive(repo_dir, archive_id, manifest_path, temp_dir, lfs_object_count(updates))
    return len(updates)


def main() -> int:
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is required")
    with tempfile.TemporaryDirectory(prefix="bha-source-mirror-") as temp_dir:
        repo_dir = os.path.join(temp_dir, "source-files")
        ensure_hf_repo(repo_dir)
        setup_lfs(repo_dir)
        manifest_path = Path(repo_dir) / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"version": 1, "files": {}}
        files = manifest.setdefault("files", {})
        failures: list[str] = []
        for archive_id in range(REPO_START, REPO_END + 1):
            try:
                mirrored = mirror_archive(repo_dir, temp_dir, archive_id, files)
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
