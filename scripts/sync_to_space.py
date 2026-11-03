#!/usr/bin/env python3
"""
将 output/search_data.json 推送到 HF Space VoiceOfML/Search。

流程:
  1. 读取本地 output/search_data.json
  2. git clone VoiceOfML/Search（使用 HF_TOKEN 认证）
  3. 清除已退役的旧 OCR 文本标记，保留紧凑索引字段兼容
  4. 将 search_data.json 复制到 data/ 目录
  5. git commit & push（仅当有变化时）
  6. 清理临时目录

用法:
  python scripts/sync_to_space.py
  需要环境变量: HF_TOKEN
"""

import base64
import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import re
from pathlib import Path
from collections import defaultdict
import jieba

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

SOURCE_JSON = Path("output/search_data.json")
FOLDER_TREE_JSON = Path("output/folder_tree.json")
FOLDER_BROWSER_JSON = Path("output/folder_browser.json")
SPACE_REPO = "VoiceOfML/Search"
SEARCH_DATA_VERSION = 2
SEARCH_OUTPUT_PATHS = ("data/search_data.json.gz", "data/folder_tree.json.gz",
                       "data/folder_browser.json.gz", "data/search_words.bin.gz",
                       "data/search_ngrams_2.bin.gz", "data/search_ngrams_3.bin.gz",
                       "data/initial", "data/sidebar")
INITIAL_PAGE_SIZE = 100
NGRAM_MAGIC = b"VNG3"
WORD_INDEX_MAGIC = b"VWI2"
WORD_INDEX_TOKENIZER = "jieba-0.42.1-cjk-char-v1"


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


def encode_search_payload(records: list[dict]) -> dict:
    repo_ids = {}
    repos = []
    folder_ids = {}
    folders = []
    encoded_records = []

    for record in records:
        repo = record.get("Repo", "")
        if repo not in repo_ids:
            repo_ids[repo] = len(repos)
            repos.append(repo)

        folder_tuple = tuple(record.get("Folder", []) or [])
        if folder_tuple not in folder_ids:
            folder_ids[folder_tuple] = len(folders)
            folders.append(list(folder_tuple))

        encoded_records.append([
            repo_ids[repo],
            record.get("File", ""),
            record.get("Extension", ""),
            folder_ids[folder_tuple],
            record.get("Size", ""),
            1 if record.get("HasTxt", False) else 0,
        ])

    return {
        "v": SEARCH_DATA_VERSION,
        "rp": repos,
        "fd": folders,
        "rc": encoded_records,
    }


def encode_varint(value: int) -> bytes:
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def tokenize(text: str) -> set[str]:
    text_lower = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text_lower)
    chinese_text = re.sub(r"[a-z0-9\s]+", " ", text_lower)
    chinese_text = re.sub(r"[^\u4e00-\u9fff\u3400-\u4dbf\s]+", " ", chinese_text)
    chinese_tokens = jieba.lcut(chinese_text)
    tokens.extend(token.strip() for token in chinese_tokens if token.strip())
    chinese_token_set = set(chinese_tokens)
    tokens.extend(
        char for char in text_lower
        if ("\u4e00" <= char <= "\u9fff" or "\u3400" <= char <= "\u4dbf")
        and char not in chinese_token_set
    )
    return set(tokens)


def search_fields(records: list[dict]) -> list[tuple[str, str, str]]:
    return [
        (
            str(record.get("File") or "").lower(),
            str(record.get("Repo") or "").lower(),
            "/".join(str(item).lower() for item in (record.get("Folder") or [])),
        )
        for record in records
    ]


def search_fields_digest(fields: list[tuple[str, str, str]]) -> bytes:
    digest = hashlib.sha256()
    for values in fields:
        for value in values:
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
    return digest.digest()


def encode_postings(record_ids) -> bytes:
    output = bytearray()
    previous = 0
    for record_id in record_ids:
        output.extend(encode_varint(record_id - previous))
        previous = record_id
    return bytes(output)


def append_postings(output: bytearray, postings: dict[str, list[int]]) -> None:
    output.extend(encode_varint(len(postings)))
    for term in sorted(postings):
        key = term.encode("utf-8")
        encoded = encode_postings(postings[term])
        output.extend(encode_varint(len(key)))
        output.extend(key)
        output.extend(encode_varint(len(postings[term])))
        output.extend(encode_varint(len(encoded)))
        output.extend(encoded)


def write_ngram_index(path: Path, fields: list[tuple[str, str, str]], digest: bytes, width: int) -> None:
    postings: dict[str, list[int]] = defaultdict(list)
    for record_id, values in enumerate(fields):
        grams = set()
        for value in values:
            grams.update(value[index:index + width] for index in range(len(value) - width + 1))
        for gram in grams:
            postings[gram].append(record_id)

    output = bytearray(NGRAM_MAGIC)
    output.append(width)
    output.extend(digest)
    output.extend(encode_varint(len(fields)))
    append_postings(output, postings)
    import gzip
    path.write_bytes(gzip.compress(bytes(output), compresslevel=9, mtime=0))


def write_word_index(path: Path, records: list[dict], digest: bytes) -> None:
    postings: dict[str, list[int]] = defaultdict(list)
    for record_id, record in enumerate(records):
        text = " ".join([str(record.get("File") or "")] + [str(item) for item in (record.get("Folder") or [])])
        for token in tokenize(text):
            postings[token].append(record_id)

    output = bytearray(WORD_INDEX_MAGIC)
    tokenizer = WORD_INDEX_TOKENIZER.encode("utf-8")
    output.extend(encode_varint(len(tokenizer)))
    output.extend(tokenizer)
    output.extend(digest)
    output.extend(encode_varint(len(records)))
    append_postings(output, postings)
    import gzip
    path.write_bytes(gzip.compress(bytes(output), compresslevel=9, mtime=0))


def trim_initial_results(records: list[dict]) -> list[dict]:
    keys = ("Repo", "File", "Extension", "Folder", "Size", "HasTxt")
    return [
        {key: record.get(key, [] if key == "Folder" else "") for key in keys}
        for record in records
    ]


def build_initial_payload(records: list[dict], repo: str | None = None) -> dict:
    if repo:
        filtered = [record for record in records if record.get("Repo") == repo]
        mode = "repo"
    else:
        filtered = records
        mode = "global"
    return {
        "version": SEARCH_DATA_VERSION,
        "mode": mode,
        "repo": repo,
        "sort": "relevance",
        "page": 1,
        "page_size": INITIAL_PAGE_SIZE,
        "total": len(filtered),
        "results": trim_initial_results(filtered[:INITIAL_PAGE_SIZE]),
    }


def write_initial_payloads(data_dir: Path, records: list[dict]) -> list[str]:
    initial_dir = data_dir / "initial"
    repos_dir = initial_dir / "repos"
    if initial_dir.exists():
        shutil.rmtree(initial_dir)
    repos_dir.mkdir(parents=True, exist_ok=True)

    urls = ["/data/initial/global.json"]
    (initial_dir / "global.json").write_text(
        json.dumps(build_initial_payload(records), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    repos = sorted({record.get("Repo", "") for record in records if record.get("Repo")})
    for repo in repos:
        short = repo.split("/")[-1]
        safe_short = urllib.parse.quote(short, safe="")
        (repos_dir / f"{safe_short}.json").write_text(
            json.dumps(build_initial_payload(records, repo), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        urls.append(f"/data/initial/repos/{safe_short}.json")

    (initial_dir / "manifest.json").write_text(
        json.dumps({"version": SEARCH_DATA_VERSION, "urls": urls}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return urls


def normalize_browser_entry(entry: dict | None, repo: str, path: str = "") -> dict:
    entry = entry or {}
    folders = entry.get("folders") or entry.get("d") or []
    files = entry.get("files") or entry.get("f") or []
    return {
        "repo": repo,
        "path": path,
        "folders": folders,
        "files": files,
    }


def write_sidebar_payloads(data_dir: Path, records: list[dict], browser_data: dict) -> list[str]:
    sidebar_dir = data_dir / "sidebar"
    repos_dir = sidebar_dir / "repos"
    if sidebar_dir.exists():
        shutil.rmtree(sidebar_dir)
    repos_dir.mkdir(parents=True, exist_ok=True)
    repo_counts = {}
    for record in records:
        repo = record.get("Repo")
        if repo:
            repo_counts[repo] = repo_counts.get(repo, 0) + 1
    repos = [{"name": repo, "count": repo_counts[repo]} for repo in sorted(repo_counts)]
    urls = ["/data/sidebar/global.json"]
    (sidebar_dir / "global.json").write_text(
        json.dumps({"repos": repos}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    for repo in sorted(repo_counts):
        short = repo.split("/")[-1]
        safe_short = urllib.parse.quote(short, safe="")
        payload = normalize_browser_entry((browser_data.get(repo) or {}).get(""), repo, "")
        (repos_dir / f"{safe_short}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        urls.append(f"/data/sidebar/repos/{safe_short}.json")
    (sidebar_dir / "manifest.json").write_text(
        json.dumps({"version": SEARCH_DATA_VERSION, "urls": urls}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return urls


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def run(cmd: list[str], cwd: str = None, env: dict = None) -> tuple[int, str, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, env=merged_env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git_auth_env(host: str, username: str, token: str) -> dict[str, str]:
    credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
    }


def push_with_retry(cwd: str, auth_env: dict[str, str]) -> bool:
    for attempt in range(2):
        ret, _out, err = run(["git", "push"], cwd=cwd, env=auth_env)
        if ret == 0:
            print("   ✅ 推送成功")
            return True
        print(f"   ⚠ 推送失败 (第{attempt + 1}次): {err[:200]}")
        if attempt == 0:
            ret, _out, err = run(["git", "pull", "--rebase"], cwd=cwd, env=auth_env)
            if ret != 0:
                print(f"   ❌ 远端更新无法安全合并，停止推送: {err[:200]}")
                return False
            time.sleep(2)
    return False


def clone_space_repo(clone_url: str, target_dir: str, auth_env: dict) -> tuple[int, str, str]:
    env = {"GIT_LFS_SKIP_SMUDGE": "1", **auth_env}
    last = (1, "", "")
    for attempt in range(4):
        ret, out, err = run(["git", "clone", "--depth", "1", clone_url, target_dir], env=env)
        if ret == 0:
            return ret, out, err
        last = (ret, out, err)
        text = (out + "\n" + err).lower()
        if "429" in text or "rate limit" in text or "too many requests" in text:
            wait = 2 ** attempt
            print(f"   ⚠ HF 限流，等待 {wait}s 后重试克隆...")
            shutil.rmtree(target_dir, ignore_errors=True)
            time.sleep(wait)
            continue
        return ret, out, err
    return last


def clear_legacy_ocr_flags(records: list[dict], browser_data: dict) -> None:
    """Keep the v2 schema but never republish retired Space-txt OCR links."""
    for record in records:
        record["HasTxt"] = False
    for repo in browser_data.values():
        for entry in repo.values():
            for item in entry.get("f", []) or []:
                item["t"] = False


def create_space_if_missing(token: str, username: str) -> bool:
    create_url = "https://huggingface.co/api/repos/create"
    payload = json.dumps({
        "name": "Search",
        "type": "space",
        "sdk": "docker",
        "private": False,
        "namespace": username,
    }).encode("utf-8")
    req = urllib.request.Request(create_url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "VoiceOfML-Search-Pipeline/1.0")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                print(f"   ✅ Space 创建成功 (HTTP {resp.status})")
                return True
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")
            except Exception:
                pass
            if "already exists" in body.lower() or e.code == 409:
                print("   ⚠ Space 已存在，继续...")
                return True
            if e.code == 429:
                wait = 2 ** attempt
                print(f"   ⚠ 创建 Space 被限流，等待 {wait}s 后重试...")
                time.sleep(wait)
                continue
            print(f"   ❌ 创建 Space 失败: HTTP {e.code} {body}")
            return False
        except Exception as e:
            print(f"   ❌ 创建 Space 异常: {e}")
            return False
    return False


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

def main():
    token = os.environ.get("HF_TOKEN", "")
    username = os.environ.get("HF_USERNAME", "VoiceOfML")

    if not token:
        print("❌ 缺少 HF_TOKEN 环境变量")
        sys.exit(1)

    if not SOURCE_JSON.exists():
        print(f"❌ 源文件不存在: {SOURCE_JSON}")
        sys.exit(1)
    if not FOLDER_TREE_JSON.exists() or not FOLDER_BROWSER_JSON.exists():
        print("❌ 目录元数据不存在，请先运行 fetch_and_parse.py")
        sys.exit(1)

    print("=" * 60)
    print("📤 VoiceOfML Search Pipeline — sync_to_space")
    print("=" * 60)

    # ── 1. 读取本地 search_data.json ───────────────────
    print(f"\n📖 读取 {SOURCE_JSON} ...")
    records = decode_search_payload(json.loads(SOURCE_JSON.read_text(encoding="utf-8")))
    print(f"   共 {len(records)} 条记录")

    # ── 2. 克隆 Space 仓库 ─────────────────────────────
    print(f"\n📥 克隆 Space 仓库: {SPACE_REPO} ...")
    tmpdir = tempfile.mkdtemp(prefix="hf_space_sync_")
    clone_url = f"https://huggingface.co/spaces/{SPACE_REPO}"
    auth_env = git_auth_env("huggingface.co", username, token)

    ret, out, err = clone_space_repo(clone_url, tmpdir, auth_env)

    if ret != 0:
        print(f"   ⚠ 克隆失败: {err[:200]}")
        combined = (out + "\n" + err).lower()
        if "429" in combined or "rate limit" in combined or "too many requests" in combined:
            print("   ❌ Hugging Face 当前限流，稍后重试同步")
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(1)
        if "404" not in combined and "not found" not in combined and "repository not found" not in combined:
            print("   ❌ 克隆失败且不像仓库不存在，停止自动创建")
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(1)

        print("   尝试创建 Space（仓库不存在）...")
        ok = create_space_if_missing(token, username)
        if not ok:
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(1)

        time.sleep(2)
        ret, out, err = clone_space_repo(clone_url, tmpdir, auth_env)
        if ret != 0:
            print(f"   ❌ 重试克隆仍然失败: {err[:200]}")
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(1)

    print("   ✅ 克隆成功")

    # ── 3. 退役旧 Space-txt OCR 标记，保留 v2 字段布局 ──
    browser_data = json.loads(FOLDER_BROWSER_JSON.read_text(encoding="utf-8"))
    clear_legacy_ocr_flags(records, browser_data)

    # ── 4. 写入并压缩 data/search_data.json ────────────
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    json_text = json.dumps(encode_search_payload(records), ensure_ascii=False, separators=(",", ":"))

    # 生成压缩版（推送到 Space）
    import gzip
    dest_gz = data_dir / "search_data.json.gz"
    dest_gz.write_bytes(gzip.compress(json_text.encode("utf-8"), compresslevel=9, mtime=0))
    (data_dir / "folder_tree.json.gz").write_bytes(FOLDER_TREE_JSON.with_suffix(".json.gz").read_bytes())
    browser_text = json.dumps(browser_data, ensure_ascii=False, separators=(",", ":"))
    (data_dir / "folder_browser.json.gz").write_bytes(gzip.compress(browser_text.encode("utf-8"), compresslevel=9, mtime=0))
    fields = search_fields(records)
    digest = search_fields_digest(fields)
    write_word_index(data_dir / "search_words.bin.gz", records, digest)
    write_ngram_index(data_dir / "search_ngrams_2.bin.gz", fields, digest, 2)
    write_ngram_index(data_dir / "search_ngrams_3.bin.gz", fields, digest, 3)
    initial_urls = write_initial_payloads(data_dir, records)
    sidebar_urls = write_sidebar_payloads(data_dir, records, browser_data)

    file_size_mb = len(json_text.encode("utf-8")) / 1024 / 1024
    gz_size_mb = dest_gz.stat().st_size / 1024 / 1024
    print(f"\n💾 已写入:")
    print(f"   {dest_gz} ({gz_size_mb:.1f} MB)")
    print(f"   words: {((data_dir / 'search_words.bin.gz').stat().st_size / 1024 / 1024):.1f} MB")
    print(f"   ngrams: 2={((data_dir / 'search_ngrams_2.bin.gz').stat().st_size / 1024 / 1024):.1f} MB, 3={((data_dir / 'search_ngrams_3.bin.gz').stat().st_size / 1024 / 1024):.1f} MB")
    print(f"   initial: {len(initial_urls)} 个首屏文件")
    print(f"   sidebar: {len(sidebar_urls)} 个侧栏首屏文件")
    missing = [path for path in SEARCH_OUTPUT_PATHS if not (Path(tmpdir) / path).exists()]
    if missing:
        print("   ❌ 搜索输出不完整，停止发布: " + ", ".join(missing))
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 1
    # ── 5. Git commit & push ───────────────────────────
    print(f"\n📤 提交并推送...")
    run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=tmpdir)
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=tmpdir)
    ret, out, err = run(["git", "add", "-A", "--", *SEARCH_OUTPUT_PATHS], cwd=tmpdir)
    if ret != 0:
        print(f"   ❌ git add 失败: {err}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 1

    ret, out, err = run(["git", "status", "--porcelain", "--", *SEARCH_OUTPUT_PATHS], cwd=tmpdir)
    if ret != 0:
        print(f"   ❌ git status 失败: {err}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 1
    if not out:
        print("   ⚠ 文件无变化，跳过提交")
    else:
        ret, out, err = run(
            ["git", "commit", "--only", "-m", "chore: update search data [skip ci]",
             "--", *SEARCH_OUTPUT_PATHS],
            cwd=tmpdir,
        )
        if ret != 0:
            print(f"   ❌ git commit 失败: {err}")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return 1

        if not push_with_retry(tmpdir, auth_env):
            shutil.rmtree(tmpdir, ignore_errors=True)
            print("   ❌ 推送失败，未发布生成数据")
            return 1

    # ── 6. 清理临时目录 ────────────────────────────────
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n🧹 已清理临时目录")
    print("\n✅ sync_to_space 完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
