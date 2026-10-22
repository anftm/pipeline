#!/usr/bin/env python3
"""Watch dependency releases and upstream datasets, then email changes.

Runs on a GitHub-hosted runner, so no local scheduler or mail client is needed.
The SMTP credentials are read from environment variables (GitHub Actions
secrets). State is written only after a successful email, so a failed run is
retried on the next schedule.

Check types in scripts/maintenance-watch.json:

- requirements: pinned PyPI packages, compared with each package's latest release
- hf_datasets: Hugging Face dataset `sha` (public or token-readable)
- hf_buckets: Hugging Face bucket `updatedAt`/size
- http_pages: a page fingerprint (or regex capture) for update logs
- manual_checks: reminders for upstreams without a stable API, e.g. Baidu Netdisk

Baidu Netdisk shares do not expose a stable, unauthenticated revision API. The
manual check therefore asks a human to confirm the share and resets the timer
with `--ack NAME` (or the workflow's `ack` input); it does not pretend to know
whether the files changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = "scripts/maintenance-watch.json"
DEFAULT_STATE = "state/maintenance-watch.json"
USER_AGENT = "anftm-pipeline-maintenance-watch/1.0"
TIMEOUT = 30
MAX_PAGE_BYTES = 8 * 1024 * 1024
REQUIREMENT_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^\]]+\])?\s*==\s*([^;\s]+)"
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def resolve(path: str) -> Path:
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def version_key(value: str) -> tuple:
    """Numeric-aware comparison that also keeps pre-release suffixes stable."""
    parts = re.findall(r"\d+|[A-Za-z]+", (value or "").lower())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def is_newer(candidate: str, previous: str) -> bool:
    if not candidate:
        return False
    if not previous:
        return True
    return version_key(candidate) > version_key(previous)


def http_json(url: str, token: str = "") -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def http_body(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read(MAX_PAGE_BYTES + 1)
        last_modified = response.headers.get("Last-Modified", "")
    if len(raw) > MAX_PAGE_BYTES:
        raise ValueError(f"页面超过 {MAX_PAGE_BYTES} 字节")
    return raw, last_modified


def parse_requirements(path: Path) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = REQUIREMENT_RE.match(line)
        if match:
            result.append((match.group(1), match.group(2)))
    return result


def pypi_latest(name: str) -> str:
    data = http_json(f"https://pypi.org/pypi/{name}/json")
    return str((data.get("info") or {}).get("version") or "")


def collect_dependencies(config: dict) -> tuple[dict, list[str]]:
    records: dict[str, dict] = {}
    errors: list[str] = []
    for rel in config.get("requirements") or []:
        path = resolve(str(rel))
        if not path.is_file():
            errors.append(f"requirements 文件不存在：{rel}")
            continue
        for name, pinned in parse_requirements(path):
            key = re.sub(r"[-_.]+", "-", name).lower()
            record = records.setdefault(
                key, {"name": name, "pinned": pinned, "files": [], "latest": ""}
            )
            if rel not in record["files"]:
                record["files"].append(rel)
            if record["pinned"] != pinned:
                errors.append(f"{name} 在 {rel} 中版本不一致：{record['pinned']} / {pinned}")

    for record in records.values():
        try:
            record["latest"] = pypi_latest(record["name"])
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"PyPI 查询失败 {record['name']}: {type(exc).__name__}: {exc}")
        record["files"].sort()
    return records, errors


def collect_hf_datasets(config: dict) -> tuple[dict, list[str]]:
    records: dict[str, dict] = {}
    errors: list[str] = []
    token = os.environ.get("HF_TOKEN", "")
    for repo in config.get("hf_datasets") or []:
        try:
            data = http_json(f"https://huggingface.co/api/datasets/{repo}", token)
            revision = str(data.get("sha") or "")
            if not revision:
                raise ValueError("API 未返回 sha")
            records[repo] = {
                "revision": revision,
                "last_modified": str(data.get("lastModified") or ""),
                "url": f"https://huggingface.co/datasets/{repo}",
            }
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"HF 数据集查询失败 {repo}: {type(exc).__name__}: {exc}")
    return records, errors


def collect_hf_buckets(config: dict) -> tuple[dict, list[str]]:
    records: dict[str, dict] = {}
    errors: list[str] = []
    token = os.environ.get("HF_TOKEN", "")
    for repo in config.get("hf_buckets") or []:
        try:
            data = http_json(f"https://huggingface.co/api/buckets/{repo}", token)
            updated = str(data.get("updatedAt") or "")
            if not updated:
                raise ValueError("API 未返回 updatedAt")
            records[repo] = {
                "updated_at": updated,
                "size": data.get("size"),
                "total_files": data.get("totalFiles"),
                "url": f"https://huggingface.co/buckets/{repo}",
            }
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"HF bucket 查询失败 {repo}: {type(exc).__name__}: {exc}")
    return records, errors


def collect_http_pages(config: dict) -> tuple[dict, list[str]]:
    records: dict[str, dict] = {}
    errors: list[str] = []
    for entry in config.get("http_pages") or []:
        name = str(entry.get("name") or entry.get("url") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not name or not url:
            errors.append("http_pages 条目缺少 name 或 url")
            continue
        try:
            raw, last_modified = http_body(url)
            text = raw.decode("utf-8", "replace")
            pattern = entry.get("fingerprint_regex")
            if pattern:
                match = re.search(str(pattern), text, re.S)
                if not match:
                    raise ValueError(f"fingerprint_regex 未匹配：{pattern}")
                fingerprint = match.group(1) if match.groups() else match.group(0)
            else:
                fingerprint = hashlib.sha256(raw).hexdigest()
            records[name] = {
                "url": url,
                "fingerprint": fingerprint,
                "last_modified": last_modified,
                "checked_at": utcnow(),
            }
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"网页查询失败 {name}: {type(exc).__name__}: {exc}")
    return records, errors


def collect_manual_checks(config: dict) -> tuple[dict, list[str]]:
    records: dict[str, dict] = {}
    errors: list[str] = []
    for entry in config.get("manual_checks") or []:
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        url_env = str(entry.get("url_env") or "").strip()
        if not url and url_env:
            url = os.environ.get(url_env, "").strip()
        if not name:
            errors.append("manual_checks 条目缺少 name")
            continue
        if name in records:
            errors.append(f"manual_checks 名称重复：{name}")
            continue
        try:
            interval = int(entry.get("interval_days") or 30)
        except (TypeError, ValueError):
            errors.append(f"manual_checks 的 interval_days 无效：{name}")
            interval = 30
        records[name] = {
            "url": url,
            "note": str(entry.get("note") or ""),
            "interval_days": max(1, interval),
        }
    return records, errors


def collect_github_repos(config: dict) -> tuple[dict, list[str]]:
    records: dict[str, dict] = {}
    errors: list[str] = []
    token = os.environ.get("GH_PAT", "")
    for repo in config.get("github_repos") or []:
        try:
            data = http_json(
                f"https://api.github.com/repos/{repo}/commits?per_page=1", token
            )
            if not isinstance(data, list) or not data:
                raise ValueError("API 未返回 commit")
            records[repo] = {
                "revision": str(data[0].get("sha") or ""),
                "url": f"https://github.com/{repo}",
            }
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"GitHub 仓库查询失败 {repo}: {type(exc).__name__}: {exc}")
    return records, errors


def collect_current(config: dict) -> tuple[dict, list[str]]:
    dependencies, dep_errors = collect_dependencies(config)
    datasets, dataset_errors = collect_hf_datasets(config)
    buckets, bucket_errors = collect_hf_buckets(config)
    pages, page_errors = collect_http_pages(config)
    manual, manual_errors = collect_manual_checks(config)
    github, github_errors = collect_github_repos(config)
    current = {
        "version": 1,
        "checked_at": utcnow(),
        "dependencies": dependencies,
        "datasets": datasets,
        "hf_buckets": buckets,
        "http_pages": pages,
        "manual_checks": manual,
        "github": github,
    }
    return current, (
        dep_errors + dataset_errors + bucket_errors + page_errors
        + manual_errors + github_errors
    )


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def detect_changes(previous: dict, current: dict, now: datetime | None = None) -> tuple[list[dict], bool]:
    now = now or now_dt()
    baseline = not previous
    changes: list[dict] = []

    previous_deps = previous.get("dependencies") or {}
    for key, record in (current.get("dependencies") or {}).items():
        latest = record.get("latest") or ""
        pinned = record.get("pinned") or ""
        if not latest:
            continue
        old = previous_deps.get(key) or {}
        old_latest = str(old.get("latest") or "")
        if baseline:
            if is_newer(latest, pinned):
                changes.append({
                    "type": "dependency",
                    "name": record.get("name") or key,
                    "old": pinned,
                    "new": latest,
                    "pinned": pinned,
                    "files": record.get("files") or [],
                    "url": f"https://pypi.org/project/{record.get('name') or key}/",
                    "baseline": True,
                })
        elif is_newer(latest, old_latest):
            changes.append({
                "type": "dependency",
                "name": record.get("name") or key,
                "old": old_latest,
                "new": latest,
                "pinned": pinned,
                "files": record.get("files") or [],
                "url": f"https://pypi.org/project/{record.get('name') or key}/",
            })

    previous_datasets = previous.get("datasets") or {}
    for repo, record in (current.get("datasets") or {}).items():
        old = str((previous_datasets.get(repo) or {}).get("revision") or "")
        new = str(record.get("revision") or "")
        if not baseline and new and new != old:
            changes.append({
                "type": "dataset",
                "name": repo,
                "old": old,
                "new": new,
                "url": record.get("url") or f"https://huggingface.co/datasets/{repo}",
            })

    previous_buckets = previous.get("hf_buckets") or {}
    for repo, record in (current.get("hf_buckets") or {}).items():
        old = str((previous_buckets.get(repo) or {}).get("updated_at") or "")
        new = str(record.get("updated_at") or "")
        if not baseline and new and new != old:
            changes.append({
                "type": "hf_bucket",
                "name": repo,
                "old": old,
                "new": new,
                "url": record.get("url") or f"https://huggingface.co/buckets/{repo}",
            })

    previous_pages = previous.get("http_pages") or {}
    for name, record in (current.get("http_pages") or {}).items():
        old = str((previous_pages.get(name) or {}).get("fingerprint") or "")
        new = str(record.get("fingerprint") or "")
        if not baseline and new and new != old:
            changes.append({
                "type": "http",
                "name": name,
                "old": old,
                "new": new,
                "url": record.get("url") or "",
            })

    previous_manual = previous.get("manual_checks") or {}
    for name, record in (current.get("manual_checks") or {}).items():
        old = previous_manual.get(name) or {}
        interval = timedelta(days=int(record.get("interval_days") or 30))
        last_confirmed = parse_time(old.get("last_confirmed"))
        last_reminded = parse_time(old.get("last_reminded"))
        due = last_confirmed is None or now - last_confirmed >= interval
        recently_reminded = last_reminded is not None and now - last_reminded < interval
        if due and not recently_reminded:
            changes.append({
                "type": "manual",
                "name": name,
                "old": old.get("last_confirmed") or "",
                "new": "待人工确认",
                "url": record.get("url") or "",
                "note": record.get("note") or "",
                "interval_days": int(record.get("interval_days") or 30),
            })

    previous_github = previous.get("github") or {}
    for repo, record in (current.get("github") or {}).items():
        old = str((previous_github.get(repo) or {}).get("revision") or "")
        new = str(record.get("revision") or "")
        if not baseline and new and new != old:
            changes.append({
                "type": "github",
                "name": repo,
                "old": old,
                "new": new,
                "url": record.get("url") or f"https://github.com/{repo}",
            })
    return changes, baseline


def prepare_state(current: dict, previous: dict, changes: list[dict], now: datetime | None = None) -> dict:
    now = now or now_dt()
    state = json.loads(json.dumps(current))
    previous_manual = previous.get("manual_checks") or {}
    # State is committed to the repository. Keep only timestamps; URLs can be
    # supplied by repository secrets and must not be persisted here.
    manual_state: dict[str, dict] = {}
    for name in (state.get("manual_checks") or {}):
        old = previous_manual.get(name) or {}
        manual_state[name] = {
            "last_confirmed": old.get("last_confirmed") or "",
            "last_reminded": old.get("last_reminded") or "",
        }
    for change in changes:
        if change.get("type") != "manual":
            continue
        record = manual_state.get(change.get("name"))
        if record is not None:
            record["last_reminded"] = now.isoformat(timespec="seconds")
    state["manual_checks"] = manual_state
    return state


def acknowledge(config: dict, state_path: Path, names: list[str]) -> int:
    state = load_state(state_path)
    if not state:
        print("state 不存在；先运行一次正常检查建立基线，再执行 --ack。", file=sys.stderr)
        return 1
    configured = {}
    for entry in config.get("manual_checks") or []:
        name = str(entry.get("name") or "")
        url = str(entry.get("url") or "").strip()
        url_env = str(entry.get("url_env") or "").strip()
        if not url and url_env:
            url = os.environ.get(url_env, "").strip()
        configured[name] = url
    if names == ["all"]:
        names = sorted(configured)
    unknown = [name for name in names if name not in configured]
    if unknown:
        print(f"未知 manual_checks：{', '.join(unknown)}", file=sys.stderr)
        return 1
    state.setdefault("manual_checks", {})
    now = utcnow()
    for name in names:
        record = state["manual_checks"].setdefault(name, {})
        record["last_confirmed"] = now
        record["last_reminded"] = now
    write_state(state_path, state)
    print(f"已确认：{', '.join(names)}（{now}）")
    return 0


def _short_revision(value: str) -> str:
    return value[:12] if value else "-"


def build_message(current: dict, changes: list[dict], errors: list[str], baseline: bool) -> tuple[str, str]:
    changes_count = len(changes)
    if errors and not changes_count:
        subject = "[维护] 上游检查失败"
    elif baseline:
        subject = "[维护] 上游基线已建立"
    elif changes_count:
        subject = f"[维护] {changes_count} 项待处理"
    else:
        subject = "[维护] 暂无上游变化"

    lines = [
        "中马库 / 搜索上游维护检查",
        f"检查时间：{current.get('checked_at')}",
        "",
    ]
    if baseline:
        lines += [
            "这是首次运行，已记录依赖版本和数据集 revision 作为基线。",
            "以后只在检测到新版本、新 revision 或人工检查到期时发送邮件。",
            "",
        ]

    groups = {
        "dependency": ("依赖可更新", "依赖"),
        "dataset": ("HF 数据集 revision 变化", "数据集"),
        "hf_bucket": ("HF bucket 更新时间变化", "bucket"),
        "http": ("网页指纹变化", "网页"),
        "manual": ("需要人工确认的上游", "人工"),
        "github": ("GitHub 上游 revision 变化", "GitHub"),
    }
    for kind, (title, _label) in groups.items():
        items = [change for change in changes if change["type"] == kind]
        if not items:
            continue
        lines += [f"{title}：", ""]
        for change in items:
            if kind == "dependency":
                files = "、".join(change.get("files") or [])
                suffix = "（首次基线）" if change.get("baseline") else ""
                lines.append(
                    f"- {change['name']}: {change.get('old') or '-'} -> {change['new']} "
                    f"(pinned {change.get('pinned') or '-'}){suffix} [{files}]"
                )
            elif kind == "manual":
                last = change.get("old") or "从未确认"
                lines.append(
                    f"- {change['name']}: {last}；每 {change.get('interval_days', 30)} 天提醒"
                )
                if change.get("note"):
                    lines.append(f"  说明：{change['note']}")
            else:
                lines.append(
                    f"- {change['name']}: {_short_revision(change.get('old') or '')} -> "
                    f"{_short_revision(change['new'])}"
                )
            if change.get("url"):
                lines.append(f"  {change['url']}")
        if kind == "manual":
            lines.append("  确认后运行：--ack \"名称\"（或 workflow 的 ack 输入）")
        lines.append("")

    if not changes and not errors:
        lines += ["没有发现新的依赖版本、数据集 revision 或到期的人工检查。", ""]
    if errors:
        lines += ["检查失败（不会更新基线，下次会重试）：", ""]
        lines += [f"- {error}" for error in errors]
        lines.append("")

    if baseline:
        lines += ["当前数据集基线：", ""]
        for repo, record in (current.get("datasets") or {}).items():
            lines.append(
                f"- {repo}: {_short_revision(record.get('revision') or '')} "
                f"{record.get('last_modified') or ''}".rstrip()
            )
        lines.append("")

    lines += [
        "配置文件：scripts/maintenance-watch.json",
        "手动触发：GitHub Actions -> 运维 · Maintenance Watch -> Run workflow",
        "百度网盘没有稳定的公开 revision API；这里只做到期人工提醒，确认后用 ack 重置计时。",
        "邮件发送失败或检查失败时，state 不会更新，下一次定时任务会重试。",
    ]
    return subject, "\n".join(lines) + "\n"


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    recipients = [
        value.strip()
        for value in re.split(r"[,;]+", os.environ.get("MAIL_TO", ""))
        if value.strip()
    ]
    sender = (os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or "").strip()
    if not host:
        raise RuntimeError("缺少 SMTP_HOST")
    if not recipients:
        raise RuntimeError("缺少 MAIL_TO")
    if not sender:
        raise RuntimeError("缺少 MAIL_FROM 或 SMTP_USER")

    try:
        port = int(os.environ.get("SMTP_PORT") or "465")
    except ValueError as exc:
        raise RuntimeError(f"SMTP_PORT 不是整数：{os.environ.get('SMTP_PORT')}") from exc
    security = (os.environ.get("SMTP_SECURITY") or ("ssl" if port == 465 else "starttls")).lower()
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    prefix = (os.environ.get("MAIL_SUBJECT_PREFIX") or "[pipeline]").strip()

    message = EmailMessage()
    message["Subject"] = f"{prefix} {subject}".strip()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
    elif security in {"starttls", "tls"}:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    elif security == "none":
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    else:
        raise RuntimeError(f"未知 SMTP_SECURITY：{security}")

    try:
        if security in {"starttls", "tls"}:
            server.starttls(context=ssl.create_default_context())
        if user:
            server.login(user, password)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - best-effort close
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--dry-run", action="store_true", help="只打印邮件，不发送、不写 state")
    parser.add_argument("--force-email", action="store_true", help="即使没有变化也发送")
    parser.add_argument(
        "--ack", action="append", default=[],
        help="人工确认一个 manual_checks 名称；all 表示全部；可重复或逗号分隔",
    )
    args = parser.parse_args(argv)

    config_path = resolve(args.config)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"无法读取配置 {config_path}: {exc}", file=sys.stderr)
        return 1

    state_path = resolve(args.state)
    ack_names = [
        name.strip()
        for value in args.ack
        for name in value.split(",")
        if name.strip()
    ]
    if ack_names:
        return acknowledge(config, state_path, ack_names)

    current, errors = collect_current(config)
    previous = load_state(state_path)
    changes, baseline = detect_changes(previous, current)
    if not (changes or errors or baseline or args.force_email):
        print("没有发现新的依赖版本、数据集 revision 或到期的人工检查。")
        return 0

    subject, body = build_message(current, changes, errors, baseline)
    if args.dry_run:
        print(subject)
        print()
        print(body)
        return 1 if errors else 0

    try:
        send_email(subject, body)
    except Exception as exc:  # noqa: BLE001 - include body for a failed run
        print(body)
        print(f"邮件发送失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if errors:
        print("检查有错误，邮件已发送，但不会更新 state；下次会重试。", file=sys.stderr)
        return 1

    write_state(state_path, prepare_state(current, previous, changes))
    print(f"已发送：{subject}")
    print(f"state 已更新：{state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
