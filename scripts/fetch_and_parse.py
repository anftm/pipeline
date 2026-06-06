#!/usr/bin/env python3
"""
每天拉取 9 个 HF 仓库的 直接目录.txt，生成 search_data.json。
仅当有仓库 commit SHA 变化时才重新生成。

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
    """GET JSON 端点，成功返回 dict/list，失败返回空 dict。"""
    try:
        with urllib.request.urlopen(_make_request(url, token), timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except Exception as e:
        print(f"  ⚠ HTTP GET JSON 失败 [{url}]: {e}")
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


def build_size_map(siblings: list) -> dict:
    """
    从 HF API siblings 列表中提取 {相对路径: 文件大小(整数)} 映射。
    siblings 中每项格式: {"rfilename": "path/to/file.pdf", "size": 12345}
    """
    size_map = {}
    for sib in siblings:
        rfname = sib.get("rfilename", "")
        fsize = sib.get("size", 0)
        if rfname and fsize:
            size_map[rfname] = fsize
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
    size = size_map.get(rel_path, "")

    return {
        "Repo": repo,
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


def get_repo_siblings(repo: str, token: str) -> list:
    """
    通过 HF API 获取仓库文件列表 (siblings)。
    返回 list，失败返回空 list。
    """
    url = f"{API_DATASETS}/{repo}"
    data = http_get_json(url, token)
    if isinstance(data, dict):
        return data.get("siblings", [])
    return []


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
        # 仍然更新 state（以防某些仓库之前没记录）
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(new_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 0

    print(f"\n🔄 {len(changed_repos)} 个仓库有变更，开始重新生成...")

    # ── 4. 拉取 txt 并解析 ─────────────────────────────
    all_records = []

    for repo in REPOS:
        print(f"\n📥 处理仓库: {repo}")

        # 4a. 获取 siblings 构建 size_map
        print("   📏 获取文件大小映射...")
        siblings = get_repo_siblings(repo, token)
        size_map = build_size_map(siblings)
        print(f"   ✅ 获取到 {len(size_map)} 个文件的大小信息")

        # 4b. 下载 直接目录.txt
        txt_url = f"{RAW_BASE}/{repo}/raw/main/直接目录.txt"
        print(f"   📄 下载: {txt_url}")
        txt_content = http_get_text(txt_url, token)

        if not txt_content:
            print(f"   ⚠ 无法下载 直接目录.txt，跳过该仓库")
            continue

        # 4c. 逐行解析
        lines = txt_content.split("\n")
        repo_count = 0
        for line in lines:
            record = parse_one_line(line, repo, size_map)
            if record:
                all_records.append(record)
                repo_count += 1

        print(f"   ✅ 解析到 {repo_count} 条记录")

        # 避免请求过快
        time.sleep(0.3)

    print(f"\n📊 总计生成 {len(all_records)} 条记录")

    # ── 5. 写入 output/search_data.json ────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_text = json.dumps(all_records, ensure_ascii=False, indent=2)
    OUTPUT_FILE.write_text(json_text, encoding="utf-8")
    file_size_mb = len(json_text.encode("utf-8")) / 1024 / 1024
    print(f"💾 已写入 {OUTPUT_FILE} ({file_size_mb:.1f} MB)")

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
