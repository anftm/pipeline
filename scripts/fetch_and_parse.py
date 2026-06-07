#!/usr/bin/env python3
"""
每天拉取 9 个 HF 仓库的 直接目录.txt，生成 search_data.json。
仅当有仓库 commit SHA 变化时才重新生成。
文件大小通过 huggingface_hub 的 get_paths_info 批量获取（每次最多 500 条路径）。

用法:
  python scripts/fetch_and_parse.py
  可选环境变量: HF_TOKEN（提升 API 频率限制）
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from huggingface_hub import HfApi

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
    "VoiceOfML/IMPMaterial",
]

STATE_FILE = Path("state/commits.json")
OUTPUT_DIR = Path("output")
OUTPUT_FILE = OUTPUT_DIR / "search_data.json"

API_DATASETS = "https://huggingface.co/api/datasets"
RAW_BASE = "https://huggingface.co/datasets"
PATHS_INFO_BATCH_SIZE = 500


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def _make_request(url: str, token: str) -> urllib.request.Request:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "VoiceOfML-Search-Pipeline/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def http_get_json(url: str, token: str = "") -> dict | list:
    try:
        with urllib.request.urlopen(_make_request(url, token), timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  ⚠ HTTP GET JSON 失败 [{url}]: {e}")
        return {}


def http_get_text(url: str, token: str = "") -> str:
    try:
        with urllib.request.urlopen(_make_request(url, token), timeout=30) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"  ⚠ HTTP GET TEXT 失败 [{url}]: {e}")
        return ""


def encode_url_path(raw_path: str) -> str:
    return urllib.parse.quote(raw_path, safe="/")


def parse_one_line(line: str, repo: str) -> dict | None:
    """
    解析 txt 中的一行路径，返回一条 JSON 记录。
    Size 暂留空，稍后批量填充。
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    if line.startswith("./"):
        rel_path = line[2:]
    elif line.startswith("/"):
        rel_path = line[1:]
    else:
        rel_path = line

    if not rel_path:
        return None

    parts = rel_path.split("/")
    filename_part = parts[-1] if parts else ""
    folder_parts = parts[:-1] if len(parts) > 1 else []

    if "." in filename_part and not filename_part.startswith("."):
        last_dot = filename_part.rfind(".")
        file_name = filename_part[:last_dot]
        extension = filename_part[last_dot + 1:]
    elif filename_part.startswith(".") and filename_part.count(".") >= 2:
        last_dot = filename_part.rfind(".")
        file_name = filename_part[:last_dot]
        extension = filename_part[last_dot + 1:]
    else:
        file_name = filename_part
        extension = ""

    encoded_rel = encode_url_path(rel_path)

    link = f"https://huggingface.co/datasets/{repo}/resolve/main/{encoded_rel}"
    path_url = f"https://huggingface.co/datasets/{repo}/blob/main/{encoded_rel}"

    return {
        "Repo": repo,
        "File": file_name,
        "Extension": extension,
        "Link": link,
        "Path": path_url,
        "Folder": folder_parts,
        "Size": "",          # 稍后填充
        "HasTxt": False,
        "_rel_path": rel_path,  # 临时字段，填充完 Size 后删除
    }


def get_repo_sha(repo: str, token: str) -> str:
    url = f"{API_DATASETS}/{repo}"
    data = http_get_json(url, token)
    if isinstance(data, dict):
        return data.get("sha", "")
    return ""


def batch_fetch_sizes(
    api: HfApi,
    repo: str,
    paths: list[str],
) -> dict[str, int]:
    """
    分批调用 get_paths_info，返回 {路径: 文件大小} 映射。
    """
    size_map: dict[str, int] = {}
    total = len(paths)

    for start in range(0, total, PATHS_INFO_BATCH_SIZE):
        batch = paths[start:start + PATHS_INFO_BATCH_SIZE]
        end = min(start + PATHS_INFO_BATCH_SIZE, total)
        print(f"   📏 获取文件大小 {start + 1}-{end}/{total} ...")

        try:
            infos = api.get_paths_info(
                repo_id=repo,
                repo_type="dataset",
                paths=batch,
            )
            for info in infos:
                if info.size is not None:
                    size_map[info.path] = info.size
            print(f"      本批匹配 {len([i for i in infos if i.size is not None])} 个")
        except Exception as e:
            print(f"   ⚠ 获取大小失败: {e}")
            # 等待后重试一次
            time.sleep(5)
            try:
                infos = api.get_paths_info(
                    repo_id=repo,
                    repo_type="dataset",
                    paths=batch,
                )
                for info in infos:
                    if info.size is not None:
                        size_map[info.path] = info.size
                print(f"      重试成功")
            except Exception as e2:
                print(f"   ❌ 重试也失败: {e2}")

    return size_map


def fill_sizes(records: list[dict], size_map: dict[str, int]):
    """用 size_map 填充每条记录的 Size 字段，并删除临时 _rel_path。"""
    count = 0
    for rec in records:
        rel_path = rec.pop("_rel_path", "")
        if rel_path in size_map:
            rec["Size"] = size_map[rel_path]
            count += 1
    return count


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
    if not changed_repos:
        print("\n✅ 所有仓库无变更，跳过生成。")
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(new_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 0

    print(f"\n🔄 {len(changed_repos)} 个仓库有变更，开始重新生成...")

    # ── 4. 拉取 txt 并解析 ─────────────────────────────
    api = HfApi(token=token)
    all_records = []

    for repo in REPOS:
        print(f"\n📥 处理仓库: {repo}")

        # 4a. 下载 直接目录.txt
        txt_url = f"{RAW_BASE}/{repo}/raw/main/直接目录.txt"
        print(f"   📄 下载: {txt_url}")
        txt_content = http_get_text(txt_url, token)

        if not txt_content:
            print(f"   ⚠ 无法下载 直接目录.txt，跳过该仓库")
            continue

        # 4b. 逐行解析
        lines = txt_content.split("\n")
        repo_records = []
        repo_paths: list[str] = []
        for line in lines:
            record = parse_one_line(line, repo)
            if record:
                repo_records.append(record)
                repo_paths.append(record["_rel_path"])

        print(f"   ✅ 解析到 {len(repo_records)} 条记录")

        # 4c. 批量获取文件大小
        print(f"   📏 批量获取文件大小（共 {len(repo_paths)} 条路径）...")
        size_map = batch_fetch_sizes(api, repo, repo_paths)
        print(f"   ✅ 获取到 {len(size_map)} 个文件的大小信息")

        # 4d. 填充 Size
        filled = fill_sizes(repo_records, size_map)
        print(f"   ✅ 成功填充 {filled}/{len(repo_records)} 条 Size")

        all_records.extend(repo_records)
        time.sleep(1)  # 仓库之间间隔

    print(f"\n📊 总计生成 {len(all_records)} 条记录")

    # ── 5. 写入 output/search_data.json ────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(all_records, ensure_ascii=False, indent=2)
    OUTPUT_FILE.write_text(json_text, encoding="utf-8")
    file_size_mb = len(json_text.encode("utf-8")) / 1024 / 1024
    print(f"💾 已写入 {OUTPUT_FILE} ({file_size_mb:.1f} MB)")

    # 统计 Size 覆盖率
    total = len(all_records)
    with_size = sum(1 for r in all_records if r.get("Size"))
    print(f"📏 Size 覆盖率: {with_size}/{total} ({with_size / max(total, 1) * 100:.1f}%)")

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
