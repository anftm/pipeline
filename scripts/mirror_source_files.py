#!/usr/bin/env python3
"""Mirror BHA publication source files into a Hugging Face dataset."""

import hashlib
import json
import os
import posixpath
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

UPSTREAM_OWNER = os.environ.get("BHA_ARCHIVE_OWNER", "banned-historical-archives")
REPO_START = int(os.environ.get("REPO_START", "0"))
REPO_END = int(os.environ.get("REPO_END", "31"))
HF_USERNAME = os.environ.get("HF_USERNAME", "vomebook")
HF_REPO = os.environ.get("HF_SOURCE_REPOSITORY", "vomebook/BHA-Source-Files")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
MANIFEST_NAME = "manifest.json"
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024


def api_json(url: str, token: str = "") -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def github_tree(repo: str) -> list[dict]:
    url = f"https://api.github.com/repos/{UPSTREAM_OWNER}/{repo}/git/trees/parsed?recursive=1"
    data = api_json(url, os.environ.get("GH_PAT", ""))
    if data.get("truncated"):
        raise RuntimeError(f"GitHub tree is truncated for {repo}")
    return [item for item in data.get("tree", []) if item.get("type") == "blob"]


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


def ensure_hf_repo(repo_dir: str) -> None:
    clone_url = f"https://huggingface.co/datasets/{HF_REPO}"
    askpass = Path(repo_dir).parent / "hf-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in *Username*) printf '%s\\n' \"$HF_USERNAME\" ;; *) printf '%s\\n' \"$HF_TOKEN\" ;; esac\n",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    auth_env = {"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "HF_USERNAME": HF_USERNAME}
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


def main() -> int:
    if not HF_TOKEN:
        raise RuntimeError("HF_TOKEN is required")
    with tempfile.TemporaryDirectory(prefix="bha-source-mirror-") as temp_dir:
        repo_dir = os.path.join(temp_dir, "source-files")
        ensure_hf_repo(repo_dir)
        manifest_path = Path(repo_dir) / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"version": 1, "files": {}}
        files = manifest.setdefault("files", {})
        changed = 0
        failures: list[str] = []
        for archive_id in range(REPO_START, REPO_END + 1):
            try:
                urls = source_urls(archive_id)
                print(f"archive{archive_id}: {len(urls)} source URLs")
                for url in urls:
                    old = files.get(url)
                    if old and (Path(repo_dir) / old["path"]).exists():
                        continue
                    with tempfile.NamedTemporaryFile(prefix="source-", dir=temp_dir, delete=False) as handle:
                        temporary = Path(handle.name)
                    try:
                        digest, size = download_to_path(url, temporary)
                    except Exception:
                        temporary.unlink(missing_ok=True)
                        raise
                    path = mirror_path(archive_id, digest, url)
                    target = Path(repo_dir) / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        shutil.move(str(temporary), str(target))
                    else:
                        temporary.unlink(missing_ok=True)
                    files[url] = {"archive_id": archive_id, "path": path, "sha256": digest, "bytes": size}
                    changed += 1
            except Exception as exc:
                failures.append(f"archive{archive_id}: {exc}")
                print(f"archive{archive_id} failed: {exc}")
        manifest["files"] = dict(sorted(files.items()))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if changed:
            auth = {"GIT_LFS_SKIP_SMUDGE": "1"}
            run(["git", "lfs", "install", "--local", "--skip-smudge"], cwd=repo_dir, env=auth)
            run(["git", "lfs", "track", "archives/**"], cwd=repo_dir, env=auth)
            run(["git", "add", MANIFEST_NAME, ".gitattributes", "archives"], cwd=repo_dir, env=auth)
            run(["git", "-c", "user.name=github-actions[bot]", "-c", "user.email=github-actions[bot]@users.noreply.github.com", "commit", "-m", "Update BHA source files"], cwd=repo_dir, env=auth)
            askpass = Path(temp_dir) / "hf-push-askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in *Username*) printf '%s\\n' \"$HF_USERNAME\" ;; *) printf '%s\\n' \"$HF_TOKEN\" ;; esac\n",
                encoding="utf-8",
            )
            askpass.chmod(stat.S_IRWXU)
            ret, _, err = run(["git", "push"], cwd=repo_dir, env={"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "HF_USERNAME": HF_USERNAME})
            if ret != 0:
                raise RuntimeError(f"HF push failed: {err}")
        if failures:
            print("\n".join(failures))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
