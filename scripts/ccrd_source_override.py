#!/usr/bin/env python3
"""Shared CCRD source override and CW corpus migration.

The upstream Prolet source still uses a legacy filename under `CW/1/`.  The
public corpus uses a stable name at the CW root.  The public CW layout also
drops the superseded `CW/1` collection and the four `CW/2` full-set collections,
then promotes the remaining dated directories to the CW root.

Both the index publisher and the Space updater call the same migration code so
metadata, original files, and the published index describe one generation.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from . import shared
except ImportError:
    import shared


UPSTREAM_REPO = "https://raw.githubusercontent.com/ProletRevDicta/Prolet"
UPSTREAM_REF = "master"
UPSTREAM_PATHS = (
    "A4 毛泽东主席/Some of Chairman Mao ‘s Instructions 毛主席1975-1976年部分指示补遗.txt",
    "A4 毛泽东主席/Some of Chairman Mao ‘s Instructions 毛主席1975-1976年部分指示补遗【待修改】.txt",
)

CORPUS_PATH = Path("CW/Some of Chairman Mao's Instructions 毛主席1975-1976年部分指示补遗.txt")
SPACE_PATH = CORPUS_PATH
LEGACY_CORPUS_PATH = Path("CW/1/Some of Chairman Mao ‘s Instructions 毛主席1975 1976年部分指示补遗【待修改】.txt")
LEGACY_SPACE_PATH = LEGACY_CORPUS_PATH

# Bump when the deterministic CW layout/metadata migration changes.
CORPUS_MIGRATION_VERSION = "cw-root-v1"
DROP_CW2_DIRS = frozenset({"S新编", "M²", "MEi", "Lii"})

SEARCH_DATA = Path("data/search_data.json.gz")
FOLDER_TREE = Path("data/folder_tree.json.gz")
FOLDER_BROWSER = Path("data/folder_browser.json.gz")
SOURCES_PATH = Path("data/sources.json.gz")
PAGE_SIZE = 100
SUMMARY_CHARS = 700


def source_urls() -> tuple[str, ...]:
    return tuple(
        f"{UPSTREAM_REPO}/{UPSTREAM_REF}/{urllib.parse.quote(path, safe='/')}"
        for path in UPSTREAM_PATHS
    )


def sha256(path: Path) -> str:
    return shared.hash_file(path)[0]


def download_override(destination: Path) -> dict[str, str | int]:
    """Download the preferred upstream name, with a transition fallback."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix="ccrd-source-", suffix=".tmp", dir=destination.parent
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    selected_url = ""
    try:
        for url in source_urls():
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "VoiceOfML-CCRD-Source-Pipeline/1.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
                selected_url = url
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise RuntimeError(f"failed to download CCRD source: HTTP {exc.code}") from exc
        if not selected_url:
            raise RuntimeError("none of the configured Prolet source filenames exists")
        if temporary.stat().st_size == 0:
            raise RuntimeError("downloaded CCRD source is empty")
        temporary.read_text(encoding="utf-8")
        digest = sha256(temporary)
        size = temporary.stat().st_size
        temporary.replace(destination)
        return {"url": selected_url, "sha256": digest, "bytes": size}
    finally:
        temporary.unlink(missing_ok=True)


def _load_gz(path: Path):
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def _write_gz(path: Path, value) -> None:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0))


def _is_dropped(rel_path: str) -> bool:
    if rel_path.startswith("1/"):
        return True
    parts = rel_path.split("/")
    return len(parts) >= 2 and parts[0] == "2" and parts[1] in DROP_CW2_DIRS


def _remap_rel_path(rel_path: str) -> str:
    return rel_path[2:] if rel_path.startswith("2/") else rel_path


def _remap_dirs(dirs: list[str]) -> list[str]:
    return dirs[1:] if dirs and dirs[0] == "2" else list(dirs)


def _build_tree(source: str, records: list[dict]) -> dict:
    root = {
        "name": source,
        "path": "",
        "count": 0,
        "hasDirectFiles": False,
        "hasChildren": False,
        "showSelfToggle": False,
        "children": [],
    }
    nodes = {"": root}
    for record in records:
        dirs = list(record.get("display_dirs") or [])
        path = ""
        parent = root
        root["count"] += 1
        if not dirs:
            root["hasDirectFiles"] = True
        for index, name in enumerate(dirs):
            path = f"{path}/{name}" if path else name
            node = nodes.get(path)
            if node is None:
                node = {
                    "name": name,
                    "path": path,
                    "count": 0,
                    "hasDirectFiles": False,
                    "hasChildren": False,
                    "showSelfToggle": False,
                    "children": [],
                }
                nodes[path] = node
                parent["children"].append(node)
                parent["hasChildren"] = True
            node["count"] += 1
            if index == len(dirs) - 1:
                node["hasDirectFiles"] = True
            parent = node

    def finalize(node: dict) -> None:
        node["children"].sort(key=lambda child: child["name"])
        for child in node["children"]:
            finalize(child)

    finalize(root)
    return root


def _build_browser(source: str, records: list[dict]) -> dict:
    browser: dict[str, dict] = {"": {"folders": [], "files": []}}
    dir_counts: dict[str, int] = {}
    for record in records:
        dirs = list(record.get("display_dirs") or [])
        path = ""
        if not dirs:
            browser[""].setdefault("files", []).append(_file_entry(record))
        for name in dirs:
            path = f"{path}/{name}" if path else name
            dir_counts[path] = dir_counts.get(path, 0) + 1
            browser.setdefault(path, {"folders": [], "files": []})
        if dirs:
            browser[path].setdefault("files", []).append(_file_entry(record))

    for path in sorted(dir_counts):
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        browser.setdefault(parent, {"folders": [], "files": []})
        browser[parent].setdefault("folders", []).append({
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "count": dir_counts[path],
        })
    for entry in browser.values():
        entry.setdefault("folders", [])
        entry.setdefault("files", [])
        entry["folders"].sort(key=lambda item: item["name"])
        entry["files"].sort(key=lambda item: item["name"])
    return browser


def _file_entry(record: dict) -> dict:
    return {
        "name": record["display_name"],
        "ext": record.get("extension", "txt"),
        "size": record.get("size", 0),
        "doc_id": record["doc_id"],
    }


def _summary(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="ignore")
    return re.sub(r"\s+", " ", text).strip()[:SUMMARY_CHARS]


def migrate_cw_tree(root: Path) -> list[str]:
    """Delete the retired CW collections and promote the remaining roots."""
    cw = root / "CW"
    if not cw.is_dir():
        raise RuntimeError(f"corpus lacks CW directory: {cw}")
    target = root / CORPUS_PATH
    if not target.is_file():
        raise RuntimeError(f"refusing to delete CW/1 before the new source exists: {target}")
    actions: list[str] = []

    legacy_one = cw / "1"
    if legacy_one.is_dir():
        shutil.rmtree(legacy_one)
        actions.append("deleted CW/1")

    legacy_two = cw / "2"
    if legacy_two.is_dir():
        for source in sorted(legacy_two.iterdir()):
            if source.name in DROP_CW2_DIRS:
                if source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()
                actions.append(f"deleted CW/2/{source.name}")
                continue
            destination = cw / source.name
            if destination.exists():
                raise RuntimeError(f"refusing to merge into existing CW directory: {destination.name}")
            shutil.move(str(source), str(destination))
            actions.append(f"moved CW/2/{source.name} -> CW/{source.name}")
        legacy_two.rmdir()

    if legacy_one.exists() or legacy_two.exists():
        raise RuntimeError("legacy CW/1 or CW/2 still exists after migration")
    return actions


def rewrite_metadata(root: Path) -> dict[str, int]:
    """Rewrite search data and browse metadata for the migrated CW layout."""
    search_path = root / SEARCH_DATA
    if not search_path.is_file():
        raise RuntimeError(f"corpus lacks {SEARCH_DATA}")
    payload = _load_gz(search_path)
    records = payload.get("records", [])
    cw_records: list[dict] = []
    for record in records:
        if record.get("source") != "CW":
            continue
        rel_path = record.get("storage_rel_path", "")
        if _is_dropped(rel_path):
            continue
        remapped = _remap_rel_path(rel_path)
        dirs = _remap_dirs(list(record.get("display_dirs") or []))
        record = dict(record)
        record["storage_rel_path"] = remapped
        record["display_dirs"] = dirs
        record["display_rel_path"] = remapped
        record["display_name"] = Path(remapped).stem
        file_path = root / record["storage_root"] / remapped
        if file_path.is_file():
            record["size"] = file_path.stat().st_size
        cw_records.append(record)

    # The changing Mao source keeps the stable CW:0 ID.
    source_file = root / CORPUS_PATH
    if not source_file.is_file():
        raise RuntimeError(f"corpus lacks migrated source: {source_file}")
    cw_records = [item for item in cw_records if item.get("doc_id") != "CW:0"]
    cw_records.insert(0, {
        "doc_id": "CW:0",
        "source": "CW",
        "source_name": "CW",
        "storage_root": "CW",
        "storage_rel_path": str(CORPUS_PATH).removeprefix("CW/"),
        "display_dirs": [],
        "display_name": CORPUS_PATH.stem,
        "display_rel_path": str(CORPUS_PATH).removeprefix("CW/"),
        "size": source_file.stat().st_size,
        "extension": "txt",
        "has_preview": True,
        "has_fulltext": True,
    })

    payload["records"] = [record for record in records if record.get("source") != "CW"] + cw_records
    counts = {"CCRD": 0, "CW": len(cw_records)}
    for record in payload["records"]:
        if record.get("source") == "CCRD":
            counts["CCRD"] += 1
    payload["sources"] = [
        {"slug": "CCRD", "name": "CCRD", "count": counts["CCRD"]},
        {"slug": "CW", "name": "CW", "count": counts["CW"]},
    ]
    _write_gz(search_path, payload)

    tree_path = root / FOLDER_TREE
    tree = _load_gz(tree_path) if tree_path.is_file() else {}
    tree["CW"] = [_build_tree("CW", cw_records)]
    _write_gz(tree_path, tree)

    browser_path = root / FOLDER_BROWSER
    browser = _load_gz(browser_path) if browser_path.is_file() else {}
    browser["CW"] = _build_browser("CW", cw_records)
    _write_gz(browser_path, browser)

    _write_gz(root / SOURCES_PATH, payload["sources"])
    return counts


def _payload_for_records(root: Path, selected: list[dict], total: int, source: str | None) -> dict:
    results = []
    for record in selected[:PAGE_SIZE]:
        item = {
            "doc_id": record["doc_id"],
            "Source": record["source"],
            "SourceName": record["source_name"],
            "File": record["display_name"],
            "Extension": record["extension"],
            "Folder": record["display_dirs"],
            "DisplayPath": record["display_rel_path"],
            "Size": record["size"],
            "HasTxt": True,
            "snippet": _summary(root / record["storage_root"] / record["storage_rel_path"]),
        }
        results.append(item)
    return {
        "q": "",
        "source": source,
        "sources": [source] if source else [],
        "folders": [],
        "min_size": None,
        "max_size": None,
        "page": 1,
        "page_size": PAGE_SIZE,
        "sort": "relevance",
        "exact": True,
        "search_paths": True,
        "fulltext": True,
        "results": results,
        "total": total,
    }


def write_space_payloads(root: Path) -> None:
    """Regenerate the Space's first-page and sidebar payloads."""
    payload = _load_gz(root / SEARCH_DATA)
    browser = _load_gz(root / FOLDER_BROWSER) if (root / FOLDER_BROWSER).is_file() else {}
    records = payload["records"]
    sources = payload["sources"]
    by_source: dict[str, list[dict]] = {}
    for record in records:
        by_source.setdefault(record["source"], []).append(record)

    initial = root / "data/initial"
    initial_sources = initial / "sources"
    initial_sources.mkdir(parents=True, exist_ok=True)
    (initial / "global.json").write_text(
        json.dumps(_payload_for_records(root, records, len(records), None), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    urls = ["/data/initial/global.json"]
    for source in sources:
        slug = source["slug"]
        source_records = by_source.get(slug, [])
        (initial_sources / f"{slug}.json").write_text(
            json.dumps(_payload_for_records(root, source_records, len(source_records), slug),
                       ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        urls.append(f"/data/initial/sources/{slug}.json")
    (initial / "manifest.json").write_text(
        json.dumps({"urls": urls, "count": len(urls), "page_size": PAGE_SIZE},
                   ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    sidebar = root / "data/sidebar"
    sidebar_sources = sidebar / "sources"
    sidebar_sources.mkdir(parents=True, exist_ok=True)
    (sidebar / "global.json").write_text(
        json.dumps({"sources": sources}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    sidebar_urls = ["/data/sidebar/global.json"]
    for source in sources:
        slug = source["slug"]
        entry = dict(browser.get(slug, {}).get("", {"folders": [], "files": []}))
        entry["source"] = slug
        entry["path"] = ""
        (sidebar_sources / f"{slug}.json").write_text(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        sidebar_urls.append(f"/data/sidebar/sources/{slug}.json")
    (sidebar / "manifest.json").write_text(
        json.dumps({"urls": sidebar_urls, "count": len(sidebar_urls)},
                   ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _verify(root: Path) -> None:
    if (root / "CW/1").exists() or (root / "CW/2").exists():
        raise RuntimeError("legacy CW directories remain after migration")
    if not (root / CORPUS_PATH).is_file():
        raise RuntimeError("migrated changing source is missing")
    payload = _load_gz(root / SEARCH_DATA)
    cw = [record for record in payload["records"] if record.get("source") == "CW"]
    if not cw:
        raise RuntimeError("migrated search data has no CW records")
    if any(record.get("storage_rel_path", "").startswith(("1/", "2/")) for record in cw):
        raise RuntimeError("migrated search data still contains legacy CW paths")
    if not any(record.get("doc_id") == "CW:0"
               and record.get("storage_rel_path") == CORPUS_PATH.name for record in cw):
        raise RuntimeError("migrated search data lacks the new CW:0 source")


def migrate_corpus(root: Path) -> dict[str, int]:
    """Migrate an extracted corpus checkout and rewrite its metadata."""
    migrate_cw_tree(root)
    counts = rewrite_metadata(root)
    _verify(root)
    return counts


def migrate_space(root: Path) -> dict[str, int]:
    """Migrate a cloned Space checkout and regenerate its UI payloads."""
    counts = migrate_corpus(root)
    write_space_payloads(root)
    return counts


def publish_source(root: Path) -> dict[str, str | int]:
    """Download the changing TXT to its final CW-root path."""
    return download_override(root / SPACE_PATH)
