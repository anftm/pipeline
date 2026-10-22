#!/usr/bin/env python3
"""Send a fixed monthly maintenance checklist by email.

This intentionally does not diff upstream revisions. On the first day of each
month it reminds the maintainer to check:

- 中马库 update log and local library refresh
- the two Baidu Netdisk upstreams
- 毛泽东博览 update log / crawler
- pinned Python requirements and GitHub Actions dependencies

SMTP settings and links come from environment variables (GitHub Actions
secrets). No state is written and no repository content is changed.
"""

from __future__ import annotations

import argparse
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage

TIMEOUT = 30
REQUIREMENTS = (
    "scripts/requirements.txt",
    "scripts/requirements-pdf-ocr.txt",
    "scripts/requirements-spreadsheet-render.txt",
)


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def build_message(now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    baidu_1 = env("BAIDU_UPSTREAM_1_URL")
    baidu_2 = env("BAIDU_UPSTREAM_2_URL")
    maozedong = env("MAOZEDONG_UPDATE_URL")

    subject = f"[月度提醒] {month} 中马库 / 百度网盘 / 毛泽东博览 / 依赖"
    lines = [
        f"每月维护提醒（{month}）",
        "",
        "这个月请检查并更新以下内容：",
        "",
        "1. 中马库",
        "   - 更新日志：https://www.marxists.org/chinese/feed.xml",
        "   - 本地目录：/sdcard/Download/压书/marxists-library",
        "   - 检查：python3 -m pipeline.update --check",
        "   - 增量更新：python3 -m pipeline.update --run",
        "   - 全量重建只在目录结构变化时用：python3 -m pipeline.update --full",
        "",
        "2. 百度网盘上游",
    ]
    if baidu_1:
        lines.append(f"   - 上游 1：{baidu_1}")
    else:
        lines.append("   - 上游 1：未配置 BAIDU_UPSTREAM_1_URL secret")
    if baidu_2:
        lines.append(f"   - 上游 2：{baidu_2}")
    else:
        lines.append("   - 上游 2：未配置 BAIDU_UPSTREAM_2_URL secret")
    lines += [
        "   - 百度网盘没有稳定的公开 revision API，请人工检查分享内容/提取码。",
        "   - 检查后更新对应的本地/镜像数据，并记录处理日期。",
        "",
        "3. 毛泽东博览",
    ]
    if maozedong:
        lines.append(f"   - 更新日志/入口：{maozedong}")
    else:
        lines.append("   - 更新日志：未配置 MAOZEDONG_UPDATE_URL secret")
    lines += [
        "   - 检查是否有新文章/新卷，按现有爬取和发布流程更新。",
        "",
        "4. 依赖",
        *[f"   - {path}" for path in REQUIREMENTS],
        "   - 检查 PyPI 新版本、GitHub Actions 版本和测试兼容性。",
        "   - 更新后运行 pipeline 的快速测试和 compileall。",
        "",
        "完成本月处理后，把结果记录到项目报告或下一次提交说明里。",
    ]
    return subject, "\n".join(lines) + "\n"


def send_email(subject: str, body: str) -> None:
    host = env("SMTP_HOST")
    recipients = [
        value.strip()
        for value in re.split(r"[,;]+", env("MAIL_TO"))
        if value.strip()
    ]
    sender = env("MAIL_FROM") or env("SMTP_USER")
    if not host:
        raise RuntimeError("缺少 SMTP_HOST")
    if not recipients:
        raise RuntimeError("缺少 MAIL_TO")
    if not sender:
        raise RuntimeError("缺少 MAIL_FROM 或 SMTP_USER")

    try:
        port = int(env("SMTP_PORT") or "465")
    except ValueError as exc:
        raise RuntimeError(f"SMTP_PORT 不是整数：{env('SMTP_PORT')}") from exc
    security = (env("SMTP_SECURITY") or ("ssl" if port == 465 else "starttls")).lower()
    prefix = env("MAIL_SUBJECT_PREFIX") or "[pipeline]"

    message = EmailMessage()
    message["Subject"] = f"{prefix} {subject}".strip()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    if security == "ssl":
        server = smtplib.SMTP_SSL(
            host, port, timeout=TIMEOUT, context=ssl.create_default_context()
        )
    elif security in {"starttls", "tls"}:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    elif security == "none":
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    else:
        raise RuntimeError(f"未知 SMTP_SECURITY：{security}")

    try:
        if security in {"starttls", "tls"}:
            server.starttls(context=ssl.create_default_context())
        if env("SMTP_USER"):
            server.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - best-effort close
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印邮件，不发送")
    args = parser.parse_args(argv)

    subject, body = build_message()
    if args.dry_run:
        print(subject)
        print()
        print(body)
        return 0
    try:
        send_email(subject, body)
    except Exception as exc:  # noqa: BLE001 - print the checklist for diagnosis
        print(body)
        print(f"邮件发送失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"已发送：{subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
