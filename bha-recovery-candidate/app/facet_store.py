import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any
from .config import DATA_ROOT, SQLITE_MMAP_BYTES
FACET_DB = DATA_ROOT / "facets.sqlite"
FACET_KINDS = {"source", "author", "tag", "type", "archive"}
FACET_STORE_SCHEMA_VERSION = "1"

def reset_db(db_path: Path = FACET_DB) -> None:
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            path.unlink()

def connect(db_path: Path = FACET_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def connect_readonly(db_path: Path = FACET_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if SQLITE_MMAP_BYTES:
        conn.execute(f"PRAGMA mmap_size={int(SQLITE_MMAP_BYTES)}")
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS facets (
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            value TEXT,
            count INTEGER NOT NULL,
            PRIMARY KEY (kind, name)
        );
        CREATE INDEX IF NOT EXISTS idx_facets_kind_name ON facets(kind, name);
        CREATE INDEX IF NOT EXISTS idx_facets_kind_name_nocase ON facets(kind, name COLLATE NOCASE);
        """
    )

def write_facets(counters: dict[str, Counter[str]], db_path: Path = FACET_DB) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        conn.execute("DELETE FROM facets")
        rows: list[tuple[str, str, str, int]] = []
        for kind, counter in counters.items():
            for name, count in counter.items():
                clean_name = str(name).strip()
                if not clean_name:
                    continue
                rows.append((kind, clean_name, clean_name, int(count)))
        conn.executemany("INSERT OR REPLACE INTO facets(kind, name, value, count) VALUES (?, ?, ?, ?)", rows)
        conn.commit()

def apply_facet_deltas(
    removed: dict[str, Counter[str]],
    added: dict[str, Counter[str]],
    db_path: Path = FACET_DB,
) -> None:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        keys: set[tuple[str, str]] = set()
        for counters in (removed, added):
            keys.update((kind, str(name).strip()) for kind, counter in counters.items() for name in counter if str(name).strip())
        for kind, name in keys:
            added_counts = added.get(kind)
            removed_counts = removed.get(kind)
            delta = (int(added_counts.get(name, 0)) if added_counts is not None else 0) - (
                int(removed_counts.get(name, 0)) if removed_counts is not None else 0
            )
            if not delta:
                continue
            conn.execute(
                """
                INSERT INTO facets(kind, name, value, count) VALUES (?, ?, ?, ?)
                ON CONFLICT(kind, name) DO UPDATE SET count = count + excluded.count
                """,
                (kind, name, name, delta),
            )
        conn.execute("DELETE FROM facets WHERE count <= 0")
        conn.commit()

def list_facets(kind: str, page: int = 1, page_size: int = 200, keyword: str = "") -> dict[str, Any]:
    if kind not in FACET_KINDS:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    page = max(1, page)
    page_size = min(max(1, page_size), 500)
    offset = (page - 1) * page_size
    params: list[Any] = [kind]
    where = "kind = ?"
    if keyword:
        where += " AND name LIKE ? ESCAPE '\\'"
        params.append(f"%{escape_like(keyword)}%")
    try:
        with closing(connect_readonly()) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM facets WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT name, value, count FROM facets WHERE {where} ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                [*params, page_size, offset],
            ).fetchall()
    except Exception:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    return {
        "items": [{"name": row["name"], "value": row["value"], "count": row["count"]} for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": offset + len(rows) < total,
    }

def sources_payload() -> dict[str, Any]:
    return {
        "sources": [],
        "authors": [],
        "tags": [],
        "types": list_facets("type", page_size=500)["items"],
        "archives": [{"name": item["name"], "value": int(item["value"]), "count": item["count"]} for item in list_facets("archive", page_size=100)["items"] if str(item["value"]).isdigit()],
    }

def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
