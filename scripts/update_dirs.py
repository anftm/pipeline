#!/usr/bin/env python3
"""Update generated directory files in the configured Hugging Face datasets."""

import os
import shutil
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
]

HF_USERNAME = "VoiceOfML"
BOT_AUTHOR = "github-actions[bot]"
AUTO_COMMIT_PREFIX = "自动更新目录"
DIRECTORY_FILES = ("树形目录.txt", "直接目录.txt")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MUL_PY_PATH = os.path.join(SCRIPT_DIR, "mul.py")
HF_TOKEN = os.environ.get("HF_TOKEN", "")


def run_cmd(cmd_list, cwd=None, extra_env=None):
    env = os.environ.copy()
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    if HF_TOKEN:
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {HF_TOKEN}",
        })
    if extra_env:
        env.update(extra_env)
    try:
        return subprocess.run(cmd_list, cwd=cwd, env=env, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        return exc


def beijing_now_str():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def process_repo(repo_path):
    repo_name = repo_path.split("/")[-1]
    clone_url = f"https://huggingface.co/{repo_path}"
    print(f"\n=== [{repo_name}] ===")

    result = run_cmd(["git", "ls-remote", clone_url, "HEAD"])
    if result.returncode != 0:
        print(f"  ls-remote 失败: {result.stderr.strip()}")
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = os.path.join(tmpdir, "repo")
        result = run_cmd(["git", "clone", "--depth", "1", clone_url, repo_dir])
        if result.returncode != 0:
            print(f"  clone 失败: {result.stderr.strip()}")
            return False

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
            return False

        result = run_cmd(["git", "log", "-1", "--format=%an|%s"], cwd=repo_dir)
        author, message = (result.stdout.strip().split("|", 1) + [""])[:2] if result.returncode == 0 else ("", "")
        if author == BOT_AUTHOR and message.startswith(AUTO_COMMIT_PREFIX):
            print("  最新提交已由目录 bot 生成，跳过")
            return True

        destination = os.path.join(repo_dir, "mul.py")
        shutil.copy(MUL_PY_PATH, destination)
        try:
            result = subprocess.run([sys.executable, "mul.py"], cwd=repo_dir, capture_output=True, text=True)
        finally:
            os.remove(destination)
        if result.returncode != 0:
            print(f"  mul.py 失败: {result.stderr.strip()[:300]}")
            return False

        result = run_cmd(["git", "status", "--porcelain", "--", *DIRECTORY_FILES], cwd=repo_dir)
        if result.returncode != 0:
            print("  git status 失败")
            return False
        if not result.stdout.strip():
            print("  目录文件无变化，跳过 push")
            return True

        run_cmd(["git", "add", *DIRECTORY_FILES], cwd=repo_dir)
        run_cmd([
            "git", "-c", "user.name=github-actions[bot]",
            "-c", "user.email=github-actions[bot]@users.noreply.github.com",
            "commit", "-m", f"{AUTO_COMMIT_PREFIX} [{beijing_now_str()}] [auto-bot]",
        ], cwd=repo_dir)
        push_command = ["git"]
        push_command.append("push")
        askpass = os.path.join(tmpdir, "git-askpass.sh")
        with open(askpass, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s\\n' \"$HF_USERNAME\" ;;\n"
                "  *) printf '%s\\n' \"$HF_TOKEN\" ;;\n"
                "esac\n"
            )
        os.chmod(askpass, 0o700)
        result = run_cmd(
            push_command,
            cwd=repo_dir,
            extra_env={
                "GIT_ASKPASS": askpass,
                "GIT_TERMINAL_PROMPT": "0",
                "HF_USERNAME": HF_USERNAME,
            },
        )
        if result.returncode != 0:
            print(f"  push 失败: {result.stderr.strip()[:300]}")
            return False
        print("  推送成功")
        return True


def main():
    if not HF_TOKEN:
        print("HF_TOKEN 未设置")
        return 1
    failed = []
    for repo in REPOS:
        try:
            if not process_repo(repo):
                failed.append(repo)
        except Exception as exc:
            print(f"  未预期异常: {exc}")
            failed.append(repo)
    if failed:
        print("失败仓库: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
