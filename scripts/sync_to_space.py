#!/usr/bin/env python3
"""
将 output/search_data.json 推送到 HF Space VoiceOfML/Search。

流程:
  1. 读取本地 output/search_data.json
  2. git clone VoiceOfML/Search（使用 HF_TOKEN 认证）
  3. 扫描 Space 中 txt/ 目录，匹配设置 HasTxt 字段（规则 A）
  4. 将 search_data.json 复制到 data/ 目录
  5. git commit & push（仅当有变化时）
  6. 清理临时目录

用法:
  python scripts/sync_to_space.py
  需要环境变量: HF_TOKEN
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

SOURCE_JSON = Path("output/search_data.json")
SPACE_REPO = "VoiceOfML/Search"


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def run(cmd: str, cwd: str = None, env: dict = None) -> tuple[int, str, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=merged_env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def scan_txt_directory(space_dir: Path) -> set:
    """
    扫描 Space 仓库中 txt/ 目录，返回所有 txt 文件对应的 stem 集合。

    规则 A：txt 文件名 = 原始文件去掉扩展名后加 .txt
    例如:
      txt/•重要资料/《国际歌》.txt
      → stem = "•重要资料/《国际歌》"（去掉 .txt 后缀）
      这个 stem 会与原始文件去掉扩展名后的名称比对
    """
    txt_dir = space_dir / "txt"
    result = set()
    if not txt_dir.exists():
        return result
    for f in txt_dir.rglob("*.txt"):
        if not f.is_file():
            continue
        try:
            rel = str(f.relative_to(txt_dir))
            if rel.endswith(".txt"):
                stem = rel[:-4]
                result.add(stem)
        except Exception:
            continue
    return result


def get_stem_from_raw_path(raw_path: str) -> str:
    """
    去掉原始文件路径的扩展名，得到 stem。
    例如:
      •重要资料/《国际歌》.pdf → •重要资料/《国际歌》
      •重要资料/README → •重要资料/README（无扩展名原样返回）
    """
    if "." in raw_path:
        last_dot = raw_path.rfind(".")
        # 确保不是路径中 / 之后的隐藏文件误判
        slash_after_dot = raw_path.find("/", last_dot)
        if slash_after_dot == -1:
            return raw_path[:last_dot]
    return raw_path


def has_txt_for_record(record: dict, txt_set: set) -> bool:
    """
    判断某条记录是否有对应的 txt 文件。

    从 Link 反推原始相对路径，去掉扩展名得到 stem，
    与 txt_set（txt 文件去掉 .txt 后的 stem 集合）比对。
    """
    repo = record.get("Repo", "")
    link = record.get("Link", "")
    prefix = f"https://huggingface.co/datasets/{repo}/resolve/main/"
    if not link.startswith(prefix):
        return False

    encoded_path = link[len(prefix):]
    try:
        raw_path = urllib.parse.unquote(encoded_path)
    except Exception:
        return False

    stem = get_stem_from_raw_path(raw_path)
    return stem in txt_set


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
        print(f"   ❌ 创建 Space 失败: HTTP {e.code} {body}")
        return False
    except Exception as e:
        print(f"   ❌ 创建 Space 异常: {e}")
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

    print("=" * 60)
    print("📤 VoiceOfML Search Pipeline — sync_to_space")
    print("=" * 60)

    # ── 1. 读取本地 search_data.json ───────────────────
    print(f"\n📖 读取 {SOURCE_JSON} ...")
    records = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    print(f"   共 {len(records)} 条记录")

    # ── 2. 克隆 Space 仓库 ─────────────────────────────
    print(f"\n📥 克隆 Space 仓库: {SPACE_REPO} ...")
    tmpdir = tempfile.mkdtemp(prefix="hf_space_sync_")
    clone_url = f"https://{username}:{token}@huggingface.co/spaces/{SPACE_REPO}"

    ret, out, err = run(f"git clone --depth 1 {clone_url} {tmpdir}")

    if ret != 0:
        print(f"   ⚠ 克隆失败: {err[:200]}")
        print("   尝试创建 Space（可能不存在）...")
        ok = create_space_if_missing(token, username)
        if not ok:
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(1)

        time.sleep(2)
        ret, out, err = run(f"git clone --depth 1 {clone_url} {tmpdir}")
        if ret != 0:
            print(f"   ❌ 重试克隆仍然失败: {err[:200]}")
            shutil.rmtree(tmpdir, ignore_errors=True)
            sys.exit(1)

    print("   ✅ 克隆成功")

    # ── 3. 扫描 txt/ 目录，设置 HasTxt ─────────────────
    print(f"\n📂 扫描 txt/ 目录...")
    txt_set = scan_txt_directory(Path(tmpdir))
    print(f"   找到 {len(txt_set)} 个 txt 文件")

    has_txt_count = 0
    for record in records:
        if has_txt_for_record(record, txt_set):
            record["HasTxt"] = True
            has_txt_count += 1

    print(f"   设置了 {has_txt_count} 个 HasTxt = True")

    # ── 4. 写入 data/search_data.json ──────────────────
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    dest_json = data_dir / "search_data.json"
    json_text = json.dumps(records, ensure_ascii=False, indent=2)
    dest_json.write_text(json_text, encoding="utf-8")
    file_size_mb = len(json_text.encode("utf-8")) / 1024 / 1024
    print(f"\n💾 已写入 {dest_json} ({file_size_mb:.1f} MB)")

    # ── 5. Git commit & push ───────────────────────────
    print(f"\n📤 提交并推送...")

    run('git config user.email "github-actions[bot]@users.noreply.github.com"', cwd=tmpdir)
    run('git config user.name "github-actions[bot]"', cwd=tmpdir)

    ret, out, err = run("git add data/search_data.json", cwd=tmpdir)
    if ret != 0:
        print(f"   ⚠ git add 失败: {err}")

    ret, out, err = run("git status --porcelain", cwd=tmpdir)
    if not out:
        print("   ⚠ 文件无变化，跳过提交")
    else:
        ret, out, err = run(
            'git commit -m "chore: update search data [skip ci]"',
            cwd=tmpdir,
        )
        if ret != 0:
            print(f"   ⚠ git commit 失败: {err}")

        for attempt 在 range(2):
            ret, out, err = run("git push", cwd=tmpdir)
            if ret == 0:
                print("   ✅ 推送成功")
                break
            print(f"   ⚠ 推送失败 (第{attempt + 1}次): {err[:200]}")
            if attempt == 0:
                run("git pull --rebase", cwd=tmpdir)
                time.sleep(2)

    # ── 6. 清理临时目录 ────────────────────────────────
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n🧹 已清理临时目录")
    print("\n✅ sync_to_space 完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
