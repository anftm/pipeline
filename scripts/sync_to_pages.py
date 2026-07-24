#!/usr/bin/env python3
"""
将 search_data.json.gz 推送到 GitHub Pages 仓库（仅数据文件，不含前端代码）。

用法:
  python scripts/sync_to_pages.py
  需要环境变量: PAGES_TOKEN, PAGES_REPO (如 anftm/search)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import posixpath
import urllib.parse
from pathlib import Path

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

SOURCE_JSON = Path("output/search_data.json")
FOLDER_TREE_JSON = Path("output/folder_tree.json")
FOLDER_BROWSER_JSON = Path("output/folder_browser.json")
HF_SPACE_REPO = "VoiceOfML/Search"
TXT_BASE_URL = f"https://huggingface.co/spaces/{HF_SPACE_REPO}/resolve/main/txt"
SEARCH_DATA_VERSION = 2
INITIAL_PAGE_SIZE = 100


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

    urls = ["/search/data/initial/global.json"]
    (initial_dir / "global.json").write_text(
        json.dumps(build_initial_payload(records), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    repos = sorted({record.get("Repo", "") for record in records if record.get("Repo")})
    for repo in repos:
        short = repo.split("/")[-1]
        safe_short = urllib.parse.quote(short, safe="")
        (repos_dir / f"{short}.json").write_text(
            json.dumps(build_initial_payload(records, repo), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        urls.append(f"/search/data/initial/repos/{safe_short}.json")

    (initial_dir / "manifest.json").write_text(
        json.dumps({"version": SEARCH_DATA_VERSION, "urls": urls}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return urls


def build_relative_path(record: dict) -> str:
    filename = record.get("File", "")
    extension = record.get("Extension", "")
    full_name = f"{filename}.{extension}" if extension else filename
    folders = record.get("Folder", []) or []
    return posixpath.join(*folders, full_name) if folders else full_name


def get_stem_from_relative_path(raw_path: str) -> str:
    if "." not in raw_path:
        return raw_path
    base, ext = posixpath.splitext(raw_path)
    return base if ext else raw_path


def run(cmd: str, cwd: str = None, env: dict = None) -> tuple[int, str, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=merged_env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main():
    token = os.environ.get("PAGES_TOKEN", "")
    pages_repo = os.environ.get("PAGES_REPO", "")
    username = "x-access-token"

    if not token:
        print("❌ 缺少 GITHUB_TOKEN")
        sys.exit(1)
    if not pages_repo:
        print("❌ 缺少 PAGES_REPO（如 anftm/search）")
        sys.exit(1)

    if not SOURCE_JSON.exists():
        print(f"❌ 源文件不存在: {SOURCE_JSON}")
        sys.exit(1)
    if not FOLDER_TREE_JSON.exists() or not FOLDER_BROWSER_JSON.exists():
        print("❌ 目录元数据不存在，请先运行 fetch_and_parse.py")
        sys.exit(1)

    print("=" * 60)
    print("📤 VoiceOfML Search Pipeline — sync_to_pages")
    print("=" * 60)

    # ── 1. 读取本地 JSON ──────────────────────────────
    print(f"\n📖 读取 {SOURCE_JSON} ...")
    records = decode_search_payload(json.loads(SOURCE_JSON.read_text(encoding="utf-8")))
    print(f"   共 {len(records)} 条记录")

    # ── 2. 从 HF Space 获取 txt 列表 ───────────────────
    print(f"\n📂 从 HF Space 获取 txt 文件列表...")
    txt_set = set()
    try:
        clone_url = f"https://huggingface.co/spaces/{HF_SPACE_REPO}"
        tmp_hf = tempfile.mkdtemp(prefix="hf_txt_scan_")
        ret, out, err = run(f"git clone --depth 1 {clone_url} {tmp_hf}")
        if ret == 0:
            txt_dir = Path(tmp_hf) / "txt"
            if txt_dir.exists():
                for f in txt_dir.rglob("*.txt"):
                    if f.is_file():
                        rel = str(f.relative_to(txt_dir))
                        if rel.endswith(".txt"):
                            txt_set.add(rel[:-4])
            shutil.rmtree(tmp_hf, ignore_errors=True)
            print(f"   找到 {len(txt_set)} 个 txt 文件")
        else:
            print(f"   ⚠ 无法克隆 HF Space（可能首次运行），HasTxt 将全部为 false")
    except Exception as e:
        print(f"   ⚠ 扫描 txt 失败: {e}")

    # ── 3. 设置 HasTxt + 替换在线阅读链接 ─────────────
    has_txt_count = 0
    for rec in records:
        stem = get_stem_from_relative_path(build_relative_path(rec))
        if stem in txt_set:
            rec["HasTxt"] = True
            has_txt_count += 1

    print(f"   设置了 {has_txt_count} 个 HasTxt = True")

    browser_data = json.loads(FOLDER_BROWSER_JSON.read_text(encoding="utf-8"))
    for record in records:
        repo_browser = browser_data.get(record.get("Repo", ""), {})
        folder_path = "/".join(record.get("Folder", []) or [])
        entry = repo_browser.get(folder_path, {})
        for item in entry.get("f", []) or []:
            if item.get("n", "") == record.get("File", "") and item.get("e", "") == record.get("Extension", ""):
                item["t"] = bool(record.get("HasTxt", False))
                break

    # ── 4. 克隆 Pages 仓库 ────────────────────────────
    print(f"\n📥 克隆 GitHub Pages 仓库: {pages_repo} ...")
    tmpdir = tempfile.mkdtemp(prefix="pages_sync_")
    clone_url = f"https://{username}:{token}@github.com/{pages_repo}.git"

    ret, out, err = run(f"git clone --depth 1 {clone_url} {tmpdir}")
    if ret != 0:
        print(f"   ⚠ 克隆失败: {err[:200]}")
        # 尝试创建
        import urllib.request
        import urllib.error
        create_url = f"https://api.github.com/repos/{pages_repo}"
        req = urllib.request.Request(create_url)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "VoiceOfML-Pipeline/1.0")
        req.add_header("Accept", "application/vnd.github+json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"   仓库已存在，重试克隆...")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"   仓库不存在，请先在 GitHub 创建 {pages_repo}")
                print(f"   并启用 GitHub Pages（Settings → Pages → Source: Deploy from a branch）")
                shutil.rmtree(tmpdir, ignore_errors=True)
                sys.exit(1)

        time.sleep(2)
        ret, out, err = run(f"git clone {clone_url} {tmpdir}")
        if ret != 0:
            print(f"   ❌ 重试克隆失败: {err[:200]}")
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(1)

    print("   ✅ 克隆成功")

    # ── 5. 生成数据文件 ────────────────────────────────
    import gzip
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    json_text = json.dumps(encode_search_payload(records), ensure_ascii=False, separators=(",", ":"))
    gz_path = data_dir / "search_data.json.gz"
    gz_path.write_bytes(gzip.compress(json_text.encode("utf-8"), compresslevel=9, mtime=0))
    (data_dir / "folder_tree.json.gz").write_bytes(FOLDER_TREE_JSON.with_suffix(".json.gz").read_bytes())
    browser_text = json.dumps(browser_data, ensure_ascii=False, separators=(",", ":"))
    (data_dir / "folder_browser.json.gz").write_bytes(gzip.compress(browser_text.encode("utf-8"), compresslevel=9, mtime=0))
    initial_urls = write_initial_payloads(data_dir, records)

    file_size_mb = len(json_text.encode("utf-8")) / 1024 / 1024
    gz_size_mb = gz_path.stat().st_size / 1024 / 1024
    print(f"\n💾 已写入:")
    print(f"   原始: {file_size_mb:.1f} MB")
    print(f"   gzip: {gz_size_mb:.1f} MB")
    print(f"   initial: {len(initial_urls)} 个首屏文件")

    # ── 6. Git commit & push ───────────────────────────
    print(f"\n📤 提交并推送...")

    run('git config user.email "github-actions[bot]@users.noreply.github.com"', cwd=tmpdir)
    run('git config user.name "github-actions[bot]"', cwd=tmpdir)

    run("git add -A", cwd=tmpdir)
    ret, out, err = run("git status --porcelain", cwd=tmpdir)

    if not out:
        print("   ⚠ 无变化，跳过提交")
    else:
        ret, out, err = run(
            'git commit -m "chore: update search data [skip ci]"',
            cwd=tmpdir,
        )
        if ret != 0 and "nothing to commit" not in err:
            print(f"   ⚠ git commit 失败: {err}")

        for attempt in range(2):
            ret, out, err = run("git push", cwd=tmpdir)
            if ret == 0:
                print("   ✅ 推送成功")
                break
            print(f"   ⚠ 推送失败 (第{attempt + 1}次): {err[:200]}")
            if attempt == 0:
                run("git pull --rebase", cwd=tmpdir)
                time.sleep(2)

    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n🧹 已清理临时目录")
    print("\n✅ sync_to_pages 完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
