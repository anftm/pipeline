#!/usr/bin/env python3
"""
补全 search_data.json 中缺失的 Size 字段。

通过并发 HEAD 请求获取每个文件的 Content-Length。
并发数：30，避免触发 HF 频率限制。

用法:
  python scripts/backfill_sizes.py                    # 补全 output/search_data.json
  python scripts/backfill_sizes.py --input path.json  # 指定输入文件
  python scripts/backfill_sizes.py --dry-run           # 只统计不修改

需要环境变量: HF_TOKEN（可选）
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp

# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

CONCURRENCY = 30
REQUEST_TIMEOUT = 15  # 秒
DEFAULT_INPUT = Path("output/search_data.json")


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(description="补全 search_data.json 的文件大小")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="输入 JSON 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计不修改")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY, help="并发数")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0=全部）")
    return parser.parse_args()


def count_missing(records: list) -> int:
    """统计 Size 为空的记录数。"""
    return sum(1 for r in records if not r.get("Size") and r.get("Size") != 0)


async def fetch_size(
    session: aiohttp.ClientSession,
    record: dict,
    semaphore: asyncio.Semaphore,
    token: str,
) -> bool:
    """
    通过 HEAD 请求获取单个文件的大小。
    成功则更新 record["Size"]，返回 True。
    失败则返回 False。
    """
    link = record.get("Link", "")
    if not link:
        return False

    headers = {"User-Agent": "VoiceOfML-Backfill/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with semaphore:
        try:
            async with session.head(
                link,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    content_length = resp.headers.get("Content-Length")
                    if content_length:
                        record["Size"] = int(content_length)
                        return True
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False


async def backfill(records: list, concurrency: int, token: str, limit: int) -> tuple[int, int]:
    """
    并发补全所有 Size 为空的记录。
    返回 (成功数, 失败数)。
    """
    # 筛选需要补全的记录
    to_fetch = [
        (idx, rec)
        for idx, rec in enumerate(records)
        if not rec.get("Size") and rec.get("Size") != 0 and rec.get("Link")
    ]

    if limit and limit > 0:
        to_fetch = to_fetch[:limit]

    total = len(to_fetch)
    if total == 0:
        return 0, 0

    print(f"   待补全: {total} 条")
    print(f"   并发数: {concurrency}")

    semaphore = asyncio.Semaphore(concurrency)
    success = 0
    failed = 0
    done = 0

    async with aiohttp.ClientSession() as session:
        tasks = []

        async def worker(idx: int, rec: dict):
            nonlocal done, success, failed
            ok = await fetch_size(session, rec, semaphore, token)
            done += 1
            if ok:
                success += 1
            else:
                failed += 1
            if done % 50 == 0 or done == total:
                print(f"   进度: {done}/{total} (成功: {success}, 失败: {failed})")

        for idx, rec in to_fetch:
            tasks.append(asyncio.create_task(worker(idx, rec)))

        await asyncio.gather(*tasks)

    return success, failed


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

async def main_async():
    args = parse_args()
    token = os.environ.get("HF_TOKEN", "")

    print("=" * 60)
    print("📏 VoiceOfML Search Pipeline — backfill_sizes")
    print("=" * 60)

    if not args.input.exists():
        print(f"❌ 输入文件不存在: {args.input}")
        sys.exit(1)

    # 读取 JSON
    print(f"\n📖 读取 {args.input} ...")
    records = json.loads(args.input.read_text(encoding="utf-8"))
    print(f"   共 {len(records)} 条记录")

    # 统计
    missing_before = count_missing(records)
    print(f"   缺失 Size: {missing_before} 条 ({missing_before / max(len(records), 1) * 100:.1f}%)")

    if missing_before == 0:
        print("\n✅ 所有记录已有 Size，无需补全。")
        return

    if args.dry_run:
        print(f"\n🔍 Dry-run 模式，不修改文件。")
        return

    # 补全
    print(f"\n🔍 开始补全...")
    start_time = time.time()

    success, failed = await backfill(
        records,
        concurrency=args.concurrency,
        token=token,
        limit=args.limit,
    )

    elapsed = time.time() - start_time
    missing_after = count_missing(records)

    print(f"\n📊 补全结果:")
    print(f"   成功: {success}")
    print(f"   失败: {failed}")
    print(f"   剩余缺失: {missing_after}")
    print(f"   耗时: {elapsed:.1f}s")

    # 写入
    if success > 0:
        print(f"\n💾 写入 {args.input} ...")
        json_text = json.dumps(records, ensure_ascii=False, indent=2)
        args.input.write_text(json_text, encoding="utf-8")
        file_size_mb = len(json_text.encode("utf-8")) / 1024 / 1024
        print(f"   文件大小: {file_size_mb:.1f} MB")
        print("✅ 已保存！")
    else:
        print("\n⚠ 无成功补全，不修改文件。")

    print("\n✅ backfill_sizes 完成！")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
