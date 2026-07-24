#!/usr/bin/env python3
"""One-off publisher for source files that are already txt.

This script is intentionally separate from sync_to_space.py. The normal data
sync should only scan existing Space txt files and set HasTxt; this script is
for the one-time bootstrap upload of txt files.
"""

import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SOURCE_JSON = Path("output/search_data.json")
SPACE_REPO = "VoiceOfML/Search"
MAX_GIT_TXT_BYTES = 10 * 1024 * 1024


def decode_search_payload(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    repos = data.get("rp", []) or []
    folders = data.get("fd", []) or []
    records = []
    for item in data.get("rc", []) or []:
        if not isinstance(item, list) or len(item) < 6:
            continue
        repo = repos[item[0]] if isinstance(item[0], int) and 0 <= item[0] < len(repos) else ""
        folder = folders[item[3]] if isinstance(item[3], int) and 0 <= item[3] < len(folders) else []
        records.append({
            "Repo": repo,
            "File": item[1],
            "Extension": item[2],
            "Folder": folder,
            "Size": item[4],
            "HasTxt": bool(item[5]),
        })
    return records


def run(cmd: str, cwd: str | None = None, env: dict | None = None) -> tuple[int, str, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=merged_env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def clone_space_repo(clone_url: str, target_dir: str) -> tuple[int, str, str]:
    env = {"GIT_LFS_SKIP_SMUDGE": "1"}
    ret, out, err = run(f"git clone --depth 1 {clone_url} {target_dir}", env=env)
    if ret == 0:
        run("git lfs install --local --skip-smudge", cwd=target_dir)
    return ret, out, err


def build_relative_path(record: dict) -> str:
    filename = record.get("File", "")
    extension = record.get("Extension", "")
    full_name = f"{filename}.{extension}" if extension else filename
    folders = record.get("Folder", []) or []
    return posixpath.join(*folders, full_name) if folders else full_name


def stem_from_relative_path(raw_path: str) -> str:
    base, ext = posixpath.splitext(raw_path)
    return base if ext else raw_path


def source_file_url(record: dict) -> str:
    repo = record.get("Repo", "")
    rel_path = build_relative_path(record)
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{urllib.parse.quote(rel_path, safe='/')}"


def decode_text_bytes(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith(b"\xff\xfe\x00\x00") or raw.startswith(b"\x00\x00\xfe\xff"):
        return raw.decode("utf-32"), "utf-32"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    for encoding in ("utf-8", "gb18030", "big5", "cp932", "shift_jis", "euc_jp", "cp1251", "koi8_r", "cp1252"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def download_text_file(url: str, token: str) -> str | None:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "VoiceOfML-Search-Pipeline/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"   ⚠ 下载失败: {url} ({e})")
        return None
    if len(raw) > MAX_GIT_TXT_BYTES:
        print(f"   ⚠ 跳过超过 10 MiB 的 txt: {url} ({len(raw)} bytes)")
        return None
    text, encoding = decode_text_bytes(raw)
    if encoding == "utf-8-replace":
        print(f"   ⚠ 编码无法可靠识别，已 replacement 兜底: {url}")
    return text


def publish_source_txt_files(records: list[dict], repo_dir: Path, token: str) -> tuple[int, int, int]:
    txt_dir = repo_dir / "txt"
    written = 0
    existing = 0
    skipped_large = 0
    for record in records:
        if str(record.get("Extension", "")).lower() != "txt":
            continue
        size = record.get("Size")
        if isinstance(size, (int, float)) and size > MAX_GIT_TXT_BYTES:
            skipped_large += 1
            print(f"   ⚠ 跳过超过 10 MiB 的 txt: {build_relative_path(record)} ({int(size)} bytes)")
            continue
        rel_path = build_relative_path(record)
        stem = stem_from_relative_path(rel_path)
        if not stem:
            continue
        dest = txt_dir / f"{stem}.txt"
        if dest.exists():
            existing += 1
            continue
        text = download_text_file(source_file_url(record), token)
        if text is None:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        written += 1
    return written, existing, skipped_large


def main() -> int:
    token = os.environ.get("HF_TOKEN", "")
    username = os.environ.get("HF_USERNAME", "VoiceOfML")
    if not token:
        print("❌ 缺少 HF_TOKEN 环境变量")
        return 1
    if not SOURCE_JSON.exists():
        print(f"❌ 源文件不存在: {SOURCE_JSON}")
        return 1

    records = decode_search_payload(json.loads(SOURCE_JSON.read_text(encoding="utf-8")))
    txt_records = [record for record in records if str(record.get("Extension", "")).lower() == "txt"]
    print(f"📖 共 {len(records)} 条记录，其中源 txt {len(txt_records)} 条")

    tmpdir = tempfile.mkdtemp(prefix="hf_txt_publish_")
    clone_url = f"https://{username}:{token}@huggingface.co/spaces/{SPACE_REPO}"
    try:
        ret, out, err = clone_space_repo(clone_url, tmpdir)
        if ret != 0:
            print(f"❌ 克隆 Space 失败: {err[:300]}")
            return 1
        written, existing, skipped_large = publish_source_txt_files(records, Path(tmpdir), token)
        print(f"💾 新写入 {written} 个 txt，已存在 {existing} 个 txt，跳过大文件 {skipped_large} 个")
        run('git config user.email "github-actions[bot]@users.noreply.github.com"', cwd=tmpdir)
        run('git config user.name "github-actions[bot]"', cwd=tmpdir)
        ret, out, err = run("git add txt", cwd=tmpdir)
        if ret != 0:
            print(f"❌ git add txt 失败: {err}")
            return 1
        ret, out, err = run("git status --porcelain", cwd=tmpdir)
        if not out:
            print("⚠ txt 无变化，跳过提交")
            return 0
        ret, out, err = run('git commit -m "chore: publish source txt files [skip ci]"', cwd=tmpdir)
        if ret != 0:
            print(f"❌ git commit 失败: {err}")
            return 1
        for attempt in range(2):
            ret, out, err = run("git push", cwd=tmpdir)
            if ret == 0:
                print("✅ txt 推送成功")
                return 0
            print(f"⚠ 推送失败 (第{attempt + 1}次): {err[:200]}")
            if attempt == 0:
                run("git pull --rebase", cwd=tmpdir)
                time.sleep(2)
        return 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
