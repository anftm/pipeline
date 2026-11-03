#!/usr/bin/env python3
"""Update generated directory files in the configured Hugging Face datasets."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta

REPOS = [
    "datasets/VoiceOfML/MLMRL-Hub",
    "datasets/VoiceOfML/Omnibus",
    "datasets/VoiceOfML/MLMRL-Library",
    "datasets/VoiceOfML/Teachers",
    "datasets/VoiceOfML/A-Historical-Learning-Data",
    "datasets/VoiceOfML/Japanese-Materials",
    "datasets/VoiceOfML/GPCREducation",
    "datasets/VoiceOfML/SovMaterials",
    "datasets/VoiceOfML/VOMEBOOK",
    "datasets/VoiceOfML/Marxists-Library",
]

HF_USERNAME = "VoiceOfML"
AUTO_COMMIT_PREFIX = "自动更新目录"
DIRECTORY_FILES = ("树形目录.txt", "直接目录.txt")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MUL_PY_PATH = os.path.join(SCRIPT_DIR, "mul.py")
HF_TOKEN = os.environ.get("HF_TOKEN", "")


def git_auth_env():
    encoded = base64.b64encode(f"{HF_USERNAME}:{HF_TOKEN}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://huggingface.co/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
    }


def run_cmd(cmd_list, cwd=None, extra_env=None):
    env = os.environ.copy()
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    if HF_TOKEN:
        env.update(git_auth_env())
    if extra_env:
        env.update(extra_env)
    try:
        return subprocess.run(cmd_list, cwd=cwd, env=env, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        return exc


def beijing_now_str():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def files_digest(repo_dir):
    result = {}
    for name in DIRECTORY_FILES:
        digest = hashlib.sha256()
        with open(os.path.join(repo_dir, name), "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result[name] = digest.hexdigest()
    return result


def process_repo(repo_path, clone_url=None):
    repo_name = repo_path.split("/")[-1]
    clone_url = clone_url or f"https://huggingface.co/{repo_path}"
    print(f"\n=== [{repo_name}] ===")

    result = run_cmd(["git", "ls-remote", clone_url, "HEAD"])
    if result.returncode != 0:
        print(f"  ls-remote 失败: {result.stderr.strip()}")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = os.path.join(tmpdir, "repo")
        result = run_cmd(["git", "clone", "--depth", "1", clone_url, repo_dir])
        if result.returncode != 0:
            print(f"  clone 失败: {result.stderr.strip()}")
            return None

        # Directory files are Git LFS objects. The clone deliberately skips
        # other large files, but these two files must be smudged before we can
        # compare generated content with the current repository version.
        result = run_cmd(
            ["git", "lfs", "pull", "--include", ",".join(DIRECTORY_FILES)],
            cwd=repo_dir,
            extra_env={"GIT_LFS_SKIP_SMUDGE": "0"},
        )
        if result.returncode != 0:
            print(f"  LFS 目录文件下载失败: {result.stderr.strip()[:300]}")
            return None
        try:
            original = files_digest(repo_dir)
        except FileNotFoundError:
            original = None

        # Execute the pipeline-owned generator without copying over, or ever
        # deleting, a source repository's own mul.py.
        result = subprocess.run([sys.executable, MUL_PY_PATH, repo_dir], cwd=repo_dir, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  mul.py 失败: {result.stderr.strip()[:300]}")
            return None

        # Check that generation produced both files. Untracked or previously
        # deleted files are then discovered by status and staged normally.
        missing = [name for name in DIRECTORY_FILES if not os.path.isfile(os.path.join(repo_dir, name))]
        if missing:
            print("  生成后目录文件缺失: " + ", ".join(missing))
            return None
        generated = files_digest(repo_dir)
        result = run_cmd(["git", "status", "--porcelain", "--", *DIRECTORY_FILES], cwd=repo_dir)
        if result.returncode != 0:
            print("  git status 失败")
            return None
        changed = bool(result.stdout.strip())
        if not changed:
            if original != generated:
                print("  Git 未报告生成结果变化，但目录文件哈希不一致")
                return None
            print("  目录文件无变化")
        else:
            result = run_cmd(["git", "add", *DIRECTORY_FILES], cwd=repo_dir)
            if result.returncode != 0:
                print(f"  git add 失败: {result.stderr.strip()[:300]}")
                return None
            result = run_cmd([
                "git", "-c", "user.name=github-actions[bot]",
                "-c", "user.email=github-actions[bot]@users.noreply.github.com",
                "commit", "-m", f"{AUTO_COMMIT_PREFIX} [{beijing_now_str()}] [auto-bot]",
            ], cwd=repo_dir)
            if result.returncode != 0:
                print(f"  git commit 失败: {result.stderr.strip()[:300]}")
                return None
            result = run_cmd(["git", "push"], cwd=repo_dir)
            if result.returncode != 0:
                print(f"  push 失败: {result.stderr.strip()[:300]}")
                return None
            print("  推送成功")

        # The caller may consume these catalogs only at the exact verified
        # revision.  A concurrent source update must not be labelled current.
        local = run_cmd(["git", "rev-parse", "HEAD"], cwd=repo_dir)
        remote = run_cmd(["git", "ls-remote", clone_url, "HEAD"])
        local_sha = local.stdout.strip() if local.returncode == 0 else ""
        remote_sha = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.split() else ""
        if not local_sha or local_sha != remote_sha:
            print("  目录版本回读失败或源仓库已变化")
            return None
        # For unchanged files the initial LFS pull already fetched the exact
        # remote bytes, and their hashes match the freshly generated output.
        # For a push, re-fetch the exact commit and verify its LFS payloads.
        if changed:
            with tempfile.TemporaryDirectory() as verify_dir:
                result = run_cmd(["git", "clone", "--depth", "1", clone_url, verify_dir])
                if result.returncode != 0:
                    print(f"  回读 clone 失败: {result.stderr.strip()[:300]}")
                    return None
                verified = run_cmd(["git", "rev-parse", "HEAD"], cwd=verify_dir)
                if verified.returncode != 0 or verified.stdout.strip() != local_sha:
                    print("  回读时源仓库已变化")
                    return None
                result = run_cmd(["git", "lfs", "pull", "--include", ",".join(DIRECTORY_FILES)],
                                 cwd=verify_dir, extra_env={"GIT_LFS_SKIP_SMUDGE": "0"})
                if result.returncode != 0:
                    print(f"  目录文件远端回读失败: {result.stderr.strip()[:300]}")
                    return None
                try:
                    if files_digest(verify_dir) != generated:
                        print("  目录文件远端回读哈希不匹配")
                        return None
                except OSError as error:
                    print(f"  目录文件远端回读失败: {error}")
                    return None
        print(f"  已确认目录版本: {local_sha}")
        return local_sha


def main():
    output = os.environ.get("DIRECTORY_REVISIONS_FILE", "")
    if output and os.path.exists(output):
        # Never leave a previous successful snapshot usable after a failure.
        os.remove(output)
    if not HF_TOKEN:
        print("HF_TOKEN 未设置")
        return 1
    failed = []
    revisions = {}
    for repo in REPOS:
        try:
            revision = process_repo(repo)
            if not revision:
                failed.append(repo)
            else:
                revisions[repo.removeprefix("datasets/")] = revision
        except Exception as exc:
            print(f"  未预期异常: {exc}")
            failed.append(repo)
    if failed:
        print("失败仓库: " + ", ".join(failed))
        return 1
    if output:
        # Only emit a usable snapshot when every source has succeeded.
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(revisions, handle, ensure_ascii=False, sort_keys=True)
        print(f"已记录 {len(revisions)} 个目录版本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
