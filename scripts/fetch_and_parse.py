#!/usr/bin/env python3
"""
每天拉取 9 个 HF 仓库的 直接目录.txt，生成 search_data.json。
仅当有仓库 commit SHA 变化时才重新生成。

用法:
  python scripts/fetch_and_parse.py
  可选环境变量: HF_TOKEN（提升 API 频率限制）
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

REPOS = [
    "VoiceOfML/VOMEBOOK",
    "VoiceOfML/SovMaterials",
    "VoiceOfML/GPCREducation",
    "VoiceOfML/Teachers",
    "VoiceOfML/MLMRL-Library",
    "VoiceOfML/Japanese-Materials",
    "VoiceOfML/A-Historical-Learning-Data",
    "VoiceOfML/MLMRL-Hub",
    "VoiceOfML/Omnibus",
]

STATE_FILE = Path("state/commits.json")
OUTPUT_DIR = Path("output")
OUTPUT_FILE = OUTPUT_DIR / "search_data.json"
FOLDER_TREE_FILE = OUTPUT_DIR / "folder_tree.json"
FOLDER_BROWSER_FILE = OUTPUT_DIR / "folder_browser.json"
META_FILE = OUTPUT_DIR / "meta.json"
SEARCH_DIR = OUTPUT_DIR / "search"
REPOS_DIR = OUTPUT_DIR / "repos"
LEGACY_DIR = OUTPUT_DIR / "legacy"

API_DATASETS = "https://huggingface.co/api/datasets"
RAW_BASE = "https://huggingface.co/datasets"


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def _make_request(url: str, token: str) -> urllib.request.Request:
    """构造带 User-Agent 和可选 Bearer token 的 HTTP 请求。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "VoiceOfML-Search-Pipeline/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def http_get_json(url: str, token: str = "") -> dict | list:
    """GET JSON 端点，遇 429 自动重试，带指数退避。"""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(_make_request(url, token), timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt
                print(f"  ⏳ 频率限制 (429)，等待 {wait}s 后重试...")
                time.sleep(wait)
                continue
            print(f"  ⚠ HTTP {e.code}: {url}")
            return {}
        except Exception as e:
            print(f"  ⚠ HTTP GET JSON 失败 [{url}]: {e}")
            return {}
    return {}


def http_get_text(url: str, token: str = "") -> str:
    """GET 文本端点，成功返回字符串，失败返回空字符串。"""
    try:
        with urllib.request.urlopen(_make_request(url, token), timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"  ⚠ HTTP GET TEXT 失败 [{url}]: {e}")
        return ""


def encode_url_path(raw_path: str) -> str:
    """
    对路径进行 URL 百分号编码 (UTF-8)，保留 '/' 不编码。

    示例:
      •重要资料/《国际歌》.pdf
      → %E2%80%A2%E9%87%8D%E8%A6%81%E8%B5%84%E6%96%99/%E3%80%8A%E5%9B%BD%E9%99%85%E6%AD%8C%E3%80%8B.pdf
    """
    return urllib.parse.quote(raw_path, safe="/")


def repo_to_id(repo: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")
    return slug or "repo"


def normalize_size(size) -> int:
    if isinstance(size, (int, float)):
        return max(0, int(size))
    try:
        return max(0, int(size or 0))
    except Exception:
        return 0

def batch_get_sizes(repo: str, paths: list[str], token: str, max_bytes: int = 80000) -> dict:
    url = f"https://huggingface.co/api/datasets/{repo}/paths-info/main"
    size_map = {}
    total = len(paths)
    idx = 0
    batch_num = 0

    while idx < total:
        # 动态取一批，控制 payload 不超过 max_bytes
        batch = []
        payload_size = 0
        overhead = 30  # {"paths":[]} 的固定开销

        while idx < total and payload_size + overhead + len(paths[idx].encode('utf-8')) + 5 < max_bytes:
            batch.append(paths[idx])
            payload_size += len(paths[idx].encode('utf-8')) + 3  # 每条加引号和逗号
            idx += 1

        if not batch:
            # 单条路径就超长（罕见），强制单独发送
            batch = [paths[idx]]
            idx += 1

        batch_num += 1
        payload = json.dumps({"paths": batch}).encode("utf-8")

        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("User-Agent", "VoiceOfML-Search-Pipeline/1.0")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                results = json.loads(resp.read().decode("utf-8"))
                for item in results:
                    p = item.get("path", "")
                    s = item.get("size", 0)
                    if p and s:
                        size_map[p] = s
        except Exception as e:
            print(f"  ⚠ paths-info 失败 (batch {batch_num}, {len(batch)}条): {e}")

        if idx < total:
            time.sleep(0.3)

    return size_map

def parse_one_line(line: str, repo: str, size_map: dict) -> dict | None:
    """
    解析 txt 中的一行路径，返回一条 JSON 记录。不合法则返回 None。

    输入示例:
      ./•重要资料/《国际歌》在中国——国际歌的译本、底本与传播.pdf

    返回:
      {
        "Repo": "VoiceOfML/MLMRL-Hub",
        "File": "《国际歌》在中国——国际歌的译本、底本与传播",
        "Extension": "pdf",
        "Link": "https://huggingface.co/datasets/VoiceOfML/MLMRL-Hub/resolve/main/%E2%80%A2...",
        "Path": "https://huggingface.co/datasets/VoiceOfML/MLMRL-Hub/blob/main/%E2%80%A2...",
        "Folder": ["•重要资料"],
        "Size": 12345,
        "HasTxt": false
      }
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # 过滤 Git LFS 指针行
    if line.startswith("version https://git-lfs") or line.startswith("oid sha256:") or line.startswith("size "):
        return None
    # 去掉开头的 ./
    if line.startswith("./"):
        rel_path = line[2:]
    elif line.startswith("/"):
        rel_path = line[1:]
    else:
        rel_path = line

    if not rel_path:
        return None

    # 拆分文件夹部分和文件名部分
    parts = rel_path.split("/")
    filename_part = parts[-1] if parts else ""
    folder_parts = parts[:-1] if len(parts) > 1 else []

    # 提取文件名和扩展名
    if "." in filename_part and not filename_part.startswith("."):
        # 普通文件: name.ext
        last_dot = filename_part.rfind(".")
        file_name = filename_part[:last_dot]
        extension = filename_part[last_dot + 1:]
    elif filename_part.startswith(".") and filename_part.count(".") >= 2:
        # 隐藏文件带扩展名: .gitignore
        last_dot = filename_part.rfind(".")
        file_name = filename_part[:last_dot]
        extension = filename_part[last_dot + 1:]
    else:
        # 无后缀文件（纯文件名 或 只以点开头的隐藏文件如 .hidden）
        file_name = filename_part
        extension = ""

    # 构造两个链接（路径做 URL 编码）
    encoded_rel = encode_url_path(rel_path)

    link = f"https://huggingface.co/datasets/{repo}/resolve/main/{encoded_rel}"
    path_url = f"https://huggingface.co/datasets/{repo}/blob/main/{encoded_rel}"

    # 文件大小（可能为空字符串）
    size = normalize_size(size_map.get(rel_path, 0))

    return {
        "Repo": repo,
        "RepoId": repo_to_id(repo),
        "File": file_name,
        "Extension": extension,
        "Link": link,
        "Path": path_url,
        "Folder": folder_parts,
        "Size": size,
        "HasTxt": False,
    }


def get_repo_sha(repo: str, token: str) -> str:
    """
    通过 HF API 获取仓库最新 commit SHA。
    返回空字符串表示获取失败。
    """
    url = f"{API_DATASETS}/{repo}"
    data = http_get_json(url, token)
    if isinstance(data, dict):
        return data.get("sha", "")
    return ""


def build_legacy_folder_tree(records: list[dict]) -> dict:
    tree_by_repo = {}
    browser_by_repo = {}

    for rec in records:
        repo = rec.get("Repo", "")
        folders = rec.get("Folder", []) or []
        dir_path = "/".join(folders)

        if repo not in tree_by_repo:
            short = repo.split("/")[-1] if repo else ""
            tree_by_repo[repo] = [{
                "name": short,
                "path": "",
                "count": 0,
                "hasDirectFiles": False,
                "hasChildren": False,
                "showSelfToggle": False,
                "children": [],
            }]
            browser_by_repo[repo] = {}

        root = tree_by_repo[repo][0]
        root["count"] += 1

        if dir_path not in browser_by_repo[repo]:
            browser_by_repo[repo][dir_path] = {"folders": [], "files": [], "current_path": dir_path}

        browser_by_repo[repo][dir_path]["files"].append({
            "name": rec.get("File", ""),
            "ext": rec.get("Extension", ""),
            "link": rec.get("Link", ""),
            "path": rec.get("Path", ""),
            "hasTxt": rec.get("HasTxt", False),
            "size": normalize_size(rec.get("Size", 0)),
        })

        node = root
        parent_path = ""
        for depth, part in enumerate(folders, start=1):
            path = "/".join(folders[:depth])
            child = next((c for c in node["children"] if c["path"] == path), None)
            if child is None:
                child = {
                    "name": part,
                    "path": path,
                    "count": 0,
                    "hasDirectFiles": False,
                    "hasChildren": False,
                    "showSelfToggle": False,
                    "children": [],
                }
                node["children"].append(child)
                parent_entry = browser_by_repo[repo].setdefault(parent_path, {"folders": [], "files": [], "current_path": parent_path})
                parent_entry["folders"].append({"name": part, "path": path, "count": 0})
            child["count"] += 1
            node = child
            parent_path = path

        if folders:
            node["hasDirectFiles"] = True
        else:
            root["hasDirectFiles"] = True

    for repo, roots in tree_by_repo.items():
        def finalize(node: dict) -> None:
            node["children"].sort(key=lambda x: x["name"])
            node["hasChildren"] = len(node["children"]) > 0
            node["showSelfToggle"] = bool(node["hasDirectFiles"] and node["hasChildren"] and node["path"] != "")
            browser_entry = browser_by_repo[repo].setdefault(node["path"], {"folders": [], "files": [], "current_path": node["path"]})
            browser_entry["folders"].sort(key=lambda x: x["name"])
            browser_entry["files"].sort(key=lambda x: x["name"])
            for folder_item in browser_entry["folders"]:
                child_node = next((c for c in node["children"] if c["path"] == folder_item["path"]), None)
                if child_node:
                    folder_item["count"] = child_node["count"]
            for child in node["children"]:
                finalize(child)

        finalize(roots[0])

    return {"tree": tree_by_repo, "browser": browser_by_repo}


def build_repo_tree_browser(repo: str, repo_records: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    short = repo.split("/")[-1] if repo else ""
    root = {
        "name": short,
        "path": "",
        "count": 0,
        "hasDirectFiles": False,
        "hasChildren": False,
        "showSelfToggle": False,
        "children": [],
    }
    browser: dict[str, dict] = {}

    for rec in repo_records:
        folders = rec.get("Folder", []) or []
        dir_path = "/".join(folders)
        root["count"] += 1

        entry = browser.setdefault(dir_path, {"currentPath": dir_path, "folders": [], "files": []})
        entry["files"].append({
            "id": rec["Id"],
            "name": rec.get("File", ""),
            "ext": rec.get("Extension", ""),
            "size": normalize_size(rec.get("Size", 0)),
            "hasTxt": bool(rec.get("HasTxt", False)),
        })

        node = root
        parent_path = ""
        for depth, part in enumerate(folders, start=1):
            path = "/".join(folders[:depth])
            child = next((item for item in node["children"] if item["path"] == path), None)
            if child is None:
                child = {
                    "name": part,
                    "path": path,
                    "count": 0,
                    "hasDirectFiles": False,
                    "hasChildren": False,
                    "showSelfToggle": False,
                    "children": [],
                }
                node["children"].append(child)
                parent_entry = browser.setdefault(parent_path, {"currentPath": parent_path, "folders": [], "files": []})
                parent_entry["folders"].append({"name": part, "path": path, "count": 0})
            child["count"] += 1
            node = child
            parent_path = path

        if folders:
            node["hasDirectFiles"] = True
        else:
            root["hasDirectFiles"] = True

    def finalize(node: dict) -> None:
        node["children"].sort(key=lambda item: item["name"])
        node["hasChildren"] = len(node["children"]) > 0
        node["showSelfToggle"] = bool(node["hasDirectFiles"] and node["hasChildren"] and node["path"] != "")
        entry = browser.setdefault(node["path"], {"currentPath": node["path"], "folders": [], "files": []})
        entry["folders"].sort(key=lambda item: item["name"])
        entry["files"].sort(key=lambda item: item["name"])
        for folder_item in entry["folders"]:
            child_node = next((child for child in node["children"] if child["path"] == folder_item["path"]), None)
            if child_node:
                folder_item["count"] = child_node["count"]
        for child in node["children"]:
            finalize(child)

    finalize(root)
    return [root], browser


def build_search_record(rec: dict) -> dict:
    return {
        "id": rec["Id"],
        "repoId": rec["RepoId"],
        "repo": rec["Repo"],
        "file": rec.get("File", ""),
        "ext": rec.get("Extension", ""),
        "folders": rec.get("Folder", []) or [],
        "size": normalize_size(rec.get("Size", 0)),
    }


def build_detail_record(rec: dict) -> dict:
    return {
        "id": rec["Id"],
        "link": rec.get("Link", ""),
        "path": rec.get("Path", ""),
        "hasTxt": bool(rec.get("HasTxt", False)),
    }


def write_json_gz(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(json_text, encoding="utf-8")
    path.with_suffix(path.suffix + ".gz").write_bytes(gzip.compress(json_text.encode("utf-8"), compresslevel=9))


def export_outputs(records: list[dict], output_dir: Path = OUTPUT_DIR) -> dict:
    normalized_records = []
    for idx, rec in enumerate(records, start=1):
        item = dict(rec)
        item["Id"] = int(item.get("Id") or idx)
        item["RepoId"] = item.get("RepoId") or repo_to_id(item.get("Repo", ""))
        item["Folder"] = item.get("Folder", []) or []
        item["Size"] = normalize_size(item.get("Size", 0))
        item["HasTxt"] = bool(item.get("HasTxt", False))
        normalized_records.append(item)

    output_dir.mkdir(parents=True, exist_ok=True)

    records_by_repo: dict[str, list[dict]] = defaultdict(list)
    ext_counts: dict[str, int] = {}
    for rec in normalized_records:
        records_by_repo[rec["Repo"]].append(rec)
        ext = (rec.get("Extension") or "").lower()
        if ext:
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

    repos_meta = []
    for repo in sorted(records_by_repo.keys()):
        repo_records = records_by_repo[repo]
        repos_meta.append({
            "id": repo_to_id(repo),
            "name": repo,
            "count": len(repo_records),
        })

    meta = {
        "version": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "totalRecords": len(normalized_records),
        "repoCount": len(repos_meta),
        "repos": repos_meta,
        "extensions": [
            {"name": ext, "count": ext_counts[ext]}
            for ext in sorted(ext_counts.keys())
        ],
    }
    write_json_gz(output_dir / "meta.json", meta)

    search_dir = output_dir / "search"
    repos_dir = output_dir / "repos"
    search_global = [build_search_record(rec) for rec in normalized_records]
    write_json_gz(search_dir / "global.json", search_global)

    for repo in sorted(records_by_repo.keys()):
        repo_records = records_by_repo[repo]
        repo_id = repo_to_id(repo)
        write_json_gz(search_dir / f"{repo_id}.json", [build_search_record(rec) for rec in repo_records])
        write_json_gz(repos_dir / repo_id / "details.json", [build_detail_record(rec) for rec in repo_records])
        repo_tree, repo_browser = build_repo_tree_browser(repo, repo_records)
        write_json_gz(repos_dir / repo_id / "tree.json", repo_tree)
        write_json_gz(repos_dir / repo_id / "browser.json", repo_browser)

    legacy_records = [
        {
            "Repo": rec["Repo"],
            "RepoId": rec["RepoId"],
            "Id": rec["Id"],
            "File": rec.get("File", ""),
            "Extension": rec.get("Extension", ""),
            "Link": rec.get("Link", ""),
            "Path": rec.get("Path", ""),
            "Folder": rec.get("Folder", []) or [],
            "Size": rec.get("Size", 0),
            "HasTxt": rec.get("HasTxt", False),
        }
        for rec in normalized_records
    ]
    legacy_folder_meta = build_legacy_folder_tree(normalized_records)
    write_json_gz(output_dir / "search_data.json", legacy_records)
    write_json_gz(output_dir / "folder_tree.json", legacy_folder_meta["tree"])
    write_json_gz(output_dir / "folder_browser.json", legacy_folder_meta["browser"])

    legacy_dir = output_dir / "legacy"
    write_json_gz(legacy_dir / "search_data.json", legacy_records)
    write_json_gz(legacy_dir / "folder_tree.json", legacy_folder_meta["tree"])
    write_json_gz(legacy_dir / "folder_browser.json", legacy_folder_meta["browser"])

    return {
        "meta": meta,
        "repos": repos_meta,
        "searchGlobalCount": len(search_global),
        "repoCount": len(repos_meta),
    }

# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

def main():
    token = os.environ.get("HF_TOKEN", "")

    print("=" * 60)
    print("📂 VoiceOfML Search Pipeline — fetch_and_parse")
    print("=" * 60)

    # ── 1. 读取上次 commit 状态 ────────────────────────
    old_state = {}
    if STATE_FILE.exists():
        try:
            old_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            print(f"📖 已读取上次状态: {len(old_state)} 个仓库")
        except json.JSONDecodeError:
            print("⚠ 状态文件损坏，将全量重新生成")
    else:
        print("📖 无历史状态文件，将全量生成")

    # ── 2. 检查每个仓库的最新 commit SHA ───────────────
    new_state = {}
    changed_repos = []

    for repo in REPOS:
        print(f"\n🔍 检查仓库: {repo}")
        sha = get_repo_sha(repo, token)
        new_state[repo] = sha

        if not sha:
            print(f"  ⚠ 无法获取 SHA，跳过")
            continue

        old_sha = old_state.get(repo, "")
        if sha != old_sha or not old_sha:
            print(f"  🔄 有变更: {old_sha[:8] if old_sha else '(新)'} → {sha[:8]}")
            changed_repos.append(repo)
        else:
            print(f"  ✅ 无变更: {sha[:8]}")

    # ── 3. 判断是否需要重新生成 ─────────────────────────
    # 检查 output 是否有效
    output_is_valid = OUTPUT_FILE.exists()
    if output_is_valid:
        try:
            existing = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            if not isinstance(existing, list) or len(existing) == 0:
                output_is_valid = False
        except Exception:
            output_is_valid = False
 
    if not changed_repos and output_is_valid:
        print("\n✅ 所有仓库无变更，跳过生成。")
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(new_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 0

    if not changed_repos and not output_is_valid:
        print("\n⚠ 仓库无变更，但 output 为空，强制重新生成...")

        print(f"\n🔄 {len(changed_repos)} 个仓库有变更，开始重新生成...")

    # ── 4. 拉取 txt 并解析 ─────────────────────────────
    all_records = []

    for repo in REPOS:
        print(f"\n📥 处理仓库: {repo}")

        # 1. 下载 txt
        txt_url = f"{RAW_BASE}/{repo}/resolve/main/{urllib.parse.quote('直接目录.txt')}"
        print(f"   📄 下载: {txt_url}")
        txt_content = http_get_text(txt_url, token)
        if not txt_content:
            print(f"   ⚠ 无法下载，跳过")
            continue

        # 2. 先解析（Size 暂时为空）
        lines = txt_content.split("\n")
        repo_records = []
        all_paths = []
        for line in lines:
            rec = parse_one_line(line, repo, {})  # 空 size_map
            if rec:
                repo_records.append(rec)
                # 提取相对路径用于 paths-info 请求
                rel_path = "/".join(rec["Folder"] + [rec["File"] + ("." + rec["Extension"] if rec["Extension"] else "")])
                all_paths.append(rel_path)

        print(f"   ✅ 解析到 {len(repo_records)} 条记录")

        # 3. 批量获取大小
        print(f"   📏 批量获取文件大小 ({len(all_paths)} 个文件)...")
        size_map = batch_get_sizes(repo, all_paths, token)
        
        # 4. 补全 Size
        filled = 0
        for i, rec in enumerate(repo_records):
            path_key = all_paths[i]
            sz = size_map.get(path_key, "")
            if sz:
                rec["Size"] = normalize_size(sz)
                filled += 1
        print(f"   ✅ 补全了 {filled} 个文件的大小")

        all_records.extend(repo_records)
        time.sleep(0.5)
    # ── 5. 分配 ID 并导出新旧产物 ──────────────────────
    for idx, rec in enumerate(all_records, start=1):
        rec["Id"] = idx
        rec["RepoId"] = repo_to_id(rec.get("Repo", ""))
        rec["Size"] = normalize_size(rec.get("Size", 0))

    summary = export_outputs(all_records, OUTPUT_DIR)
    legacy_search = OUTPUT_DIR / "legacy" / "search_data.json.gz"
    search_global = OUTPUT_DIR / "search" / "global.json"
    print(f"💾 已写入 {META_FILE} / meta.json.gz")
    print(f"💾 已写入 {search_global} / global.json.gz")
    print(f"💾 已写入 {legacy_search} / search_data.json.gz")
    print(f"💾 已生成 {summary['repoCount'] if 'repoCount' in summary else len(summary['repos'])} 个 repo 分片")

    # ── 6. 更新 state/commits.json ─────────────────────
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(new_state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"💾 已更新 {STATE_FILE}")

    print("\n✅ fetch_and_parse 完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
