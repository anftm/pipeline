#!/usr/bin/env python3
"""
add_tokens — 用 Python jieba 对 search_data.json 中的每条记录预计算分词，
写入 _Tokens 字段。确保 GitHub Pages 静态版和 HF Spaces 服务器版使用完全相同的 token。

用法:
    python scripts/add_tokens.py [input.json] [output.json]

默认:
    input  = output/search_data.json
    output = output/search_data.json (原地覆盖)
"""

import json
import gzip
import re
import sys
from pathlib import Path

try:
    import jieba
except ImportError:
    print("错误: 请先安装 jieba: pip install jieba", file=sys.stderr)
    sys.exit(1)

# jieba 内部使用的正字拆分正则（兼容中文和新版 jieba）
_RE_HAN = re.compile(r'([\u4E00-\u9FFF\u3400-\u4DBFa-zA-Z0-9+#&\._%\-]+)')
_RE_SKIP = re.compile(r'(\r\n|\s)', re.U)


def tokenize(text: str) -> list[str]:
    """与 HF 版 app.py 中 tokenize 完全一致的分词逻辑"""
    tokens_set: set[str] = set()
    lower = text.lower()

    # Alpha tokens
    for m in re.finditer(r'[a-z0-9]+', lower):
        tokens_set.add(m.group())

    # 提取纯中文文本
    chinese_text = re.sub(r'[a-z0-9\s]+', ' ', lower)
    chinese_text = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf\s]+', ' ', chinese_text)

    try:
        jieba_tokens = jieba.lcut(chinese_text)
        for t in jieba_tokens:
            t = t.strip()
            if not t:
                continue
            if re.search(r'[a-z0-9\u4e00-\u9fff\u3400-\u4dbf]', t):
                tokens_set.add(t)
    except Exception:
        pass

    # 若 jieba 未产生任何 CJK token，回退到逐字切分
    has_cjk = any(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf]', tok) for tok in tokens_set)
    if not has_cjk:
        for ch in lower:
            if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
                tokens_set.add(ch)

    return sorted(tokens_set)


def process_record(rec: dict) -> dict:
    """为一条记录添加 _Tokens 字段"""
    file_name = rec.get("File", "")
    folders = rec.get("Folder", [])

    text_parts = [file_name]
    if folders:
        text_parts.extend(folders)
    full_text = " ".join(text_parts)

    rec["_Tokens"] = tokenize(full_text)
    return rec


def load_records(path: str) -> list[dict]:
    """加载 JSON 或 JSON.gz 文件"""
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    else:
        return json.loads(path.read_text(encoding="utf-8"))


def save_records(records: list[dict], path: str):
    """保存为 JSON 或 JSON.gz 文件（根据后缀自动选择）"""
    path = Path(path)
    text = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    if path.suffix == ".gz":
        import gzip as gz
        with gz.open(path, "wt", encoding="utf-8") as f:
            f.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    size = path.stat().st_size
    print(f"✅ 已写入 {len(records)} 条记录 → {path} ({size / 1024:.0f} KB)")


def main():
    input_path = sys.argv[1] if len(sys.argv) > 1 else "output/search_data.json"
    output_path = sys.argv[2] if len(sys.argv) > 2 else input_path

    print(f"📖 加载数据: {input_path}")
    records = load_records(input_path)
    print(f"   共 {len(records)} 条记录")

    print(f"🔪 用 jieba 预计算 _Tokens ...")
    for i, rec in enumerate(records):
        process_record(rec)
        if (i + 1) % 10000 == 0:
            print(f"   已处理 {i + 1}/{len(records)} ...")

    # 统计 token 增长
    total_tokens = sum(len(rec.get("_Tokens", [])) for rec in records)
    avg = total_tokens / len(records) if records else 0
    print(f"   平均每条 {avg:.1f} 个 token")

    save_records(records, output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
