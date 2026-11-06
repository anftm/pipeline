import json
import random
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from .config import DATA_ROOT, SQLITE_MMAP_BYTES
DOC_DB = DATA_ROOT / "docs.sqlite"
DOC_STORE_SCHEMA_VERSION = "1"

def connect(db_path: Path = DOC_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def connect_readonly(db_path: Path = DOC_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if SQLITE_MMAP_BYTES:
        # mmap_size is a maximum window; unused pages cost neither disk I/O nor
        # resident memory until they are touched.
        conn.execute(f"PRAGMA mmap_size={int(SQLITE_MMAP_BYTES)}")
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS docs (
            doc_id TEXT PRIMARY KEY,
            article_id TEXT,
            publication_id TEXT,
            publication_name TEXT,
            publication_author TEXT,
            publication_type TEXT,
            archive_id INTEGER,
            title TEXT,
            authors_json TEXT,
            tag_names_json TEXT,
            date_display_json TEXT,
            source_files_json TEXT,
            source_file_count INTEGER,
            content TEXT,
            path TEXT,
            article_json TEXT
        );
        CREATE TABLE IF NOT EXISTS years (
            year INTEGER PRIMARY KEY
        );
        CREATE INDEX IF NOT EXISTS idx_docs_title ON docs(title);
        CREATE INDEX IF NOT EXISTS idx_docs_publication_name ON docs(publication_name);
        CREATE INDEX IF NOT EXISTS idx_docs_archive_id ON docs(archive_id);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(docs)")}
    if "article_json" not in columns:
        conn.execute("ALTER TABLE docs ADD COLUMN article_json TEXT")
    if "publication_author" not in columns:
        conn.execute("ALTER TABLE docs ADD COLUMN publication_author TEXT")

def reset_db(db_path: Path = DOC_DB) -> None:
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            path.unlink()

def write_docs_to_conn(conn: sqlite3.Connection, docs: list[dict[str, Any]]) -> None:
    if not docs:
        return
    rows = [doc_row(doc) for doc in docs]
    year_rows = [(year,) for year in sorted(years_from_docs(docs))]
    conn.executemany(
        """
        INSERT OR REPLACE INTO docs(
            doc_id, article_id, publication_id, publication_name, publication_author, publication_type, archive_id,
            title, authors_json, tag_names_json, date_display_json, source_files_json,
            source_file_count, content, path, article_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    if year_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO years(year) VALUES (?)",
            year_rows,
        )

def delete_docs_to_conn(conn: sqlite3.Connection, doc_ids: list[str]) -> None:
    if not doc_ids:
        return
    placeholders = ",".join("?" for _ in doc_ids)
    conn.execute(f"DELETE FROM docs WHERE doc_id IN ({placeholders})", doc_ids)

def update_years_to_conn(conn: sqlite3.Connection, affected_docs: list[dict[str, Any]]) -> None:
    affected: set[int] = set()
    for doc in affected_docs:
        dates: list[object] = doc.get("date_years") or []
        for year in dates:
            if isinstance(year, int):
                affected.add(year)
        displays: list[object] = doc.get("date_display") or []
        for value in displays:
            raw_year = str(value).split("/", 1)[0]
            if raw_year.isdigit():
                affected.add(int(raw_year))
    if not affected:
        return
    placeholders = ",".join("?" for _ in affected)
    conn.execute(f"DELETE FROM years WHERE year IN ({placeholders})", sorted(affected))
    for year in sorted(affected):
        if conn.execute(
            "SELECT 1 FROM docs WHERE date_display_json LIKE ? LIMIT 1",
            (f'%"{year}/%',),
        ).fetchone():
            conn.execute("INSERT INTO years(year) VALUES (?)", (year,))

def years_from_docs(docs: list[dict[str, Any]]) -> set[int]:
    years: set[int] = set()
    for doc in docs:
        dates: list[object] = doc.get("date_years") or []
        for year in dates:
            if isinstance(year, int):
                years.add(year)
    return years

def doc_row(doc: dict[str, Any]) -> tuple[Any, ...]:
    source_files: list[object] = doc.get("source_files") or []
    return (
        doc.get("doc_id"),
        doc.get("article_id"),
        doc.get("publication_id"),
        doc.get("publication_name"),
        doc.get("publication_author"),
        doc.get("publication_type"),
        doc.get("archive_id"),
        doc.get("title"),
        json.dumps(doc.get("authors") or [], ensure_ascii=False),
        json.dumps(doc.get("tag_names") or [], ensure_ascii=False),
        json.dumps(doc.get("date_display") or [], ensure_ascii=False),
        json.dumps(source_files, ensure_ascii=False),
        len(source_files),
        doc.get("content") or "",
        doc.get("path"),
        json.dumps(doc.get("article") or {}, ensure_ascii=False),
    )

def get_doc(doc_id: str) -> dict[str, Any] | None:
    try:
        with closing(connect_readonly()) as conn:
            row = conn.execute("SELECT * FROM docs WHERE doc_id = ?", (doc_id,)).fetchone()
    except Exception:
        return None
    return row_to_doc(row) if row else None

def parse_queue_docs(archive_id: int, prefix: str, page: int, page_size: int) -> tuple[int, list[dict[str, Any]]]:
    offset = (page - 1) * page_size
    try:
        with closing(connect_readonly()) as conn:
            total = int(conn.execute(
                "SELECT COUNT(*) FROM docs WHERE archive_id = ? AND title LIKE ?",
                (archive_id, f"{prefix}%"),
            ).fetchone()[0])
            rows = conn.execute(
                """
                SELECT doc_id, article_id, publication_id, publication_name, publication_author,
                       publication_type, archive_id, title, authors_json, tag_names_json,
                       date_display_json, source_files_json, source_file_count, path, article_json
                FROM docs
                WHERE archive_id = ? AND title LIKE ?
                ORDER BY publication_name COLLATE NOCASE, publication_id
                LIMIT ? OFFSET ?
                """,
                (archive_id, f"{prefix}%", page_size, offset),
            ).fetchall()
    except Exception:
        return 0, []
    return total, [row_to_doc(row) for row in rows]

def random_doc() -> dict[str, Any] | None:
    try:
        with closing(connect_readonly()) as conn:
            max_rowid = conn.execute("SELECT MAX(rowid) FROM docs").fetchone()[0]
            if max_rowid is None:
                return None
            start = random.randint(1, max_rowid)
            row = conn.execute("SELECT * FROM docs WHERE rowid >= ? ORDER BY rowid LIMIT 1", (start,)).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM docs ORDER BY rowid LIMIT 1").fetchone()
    except Exception:
        return None
    return row_to_doc(row) if row else None

def warmup_db() -> bool:
    try:
        with closing(connect_readonly()) as conn:
            max_rowid = conn.execute("SELECT MAX(rowid) FROM docs").fetchone()[0]
            if max_rowid is None:
                return False
            start = random.randint(1, max_rowid)
            rows = conn.execute(
                "SELECT doc_id, title, publication_name, substr(content, 1, 4096) AS content FROM docs WHERE rowid >= ? ORDER BY rowid LIMIT 20",
                (start,),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT doc_id, title, publication_name, substr(content, 1, 4096) AS content FROM docs ORDER BY rowid LIMIT 20"
                ).fetchall()
            if rows:
                conn.execute("SELECT doc_id, content FROM docs WHERE doc_id = ?", (rows[0]["doc_id"],)).fetchone()
                conn.execute("SELECT doc_id FROM docs WHERE title = ? LIMIT 20", (rows[0]["title"],)).fetchall()
    except Exception:
        return False
    return bool(rows)

def variants_for_title(title: str, current_doc_id: str, current_publication: str | None, limit: int = 20) -> list[dict[str, Any]]:
    try:
        with closing(connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT doc_id, publication_name, publication_id, authors_json, date_display_json
                FROM docs
                WHERE title = ? AND doc_id != ?
                ORDER BY publication_name COLLATE NOCASE
                LIMIT ?
                """,
                (title, current_doc_id, limit * 3),
            ).fetchall()
    except Exception:
        return []
    seen = {current_publication}
    variants: list[dict[str, Any]] = []
    for row in rows:
        publication_name = row["publication_name"]
        if not publication_name or publication_name in seen:
            continue
        seen.add(publication_name)
        variants.append({
            "doc_id": row["doc_id"],
            "publication_name": publication_name,
            "publication_id": row["publication_id"],
            "authors": loads(row["authors_json"], []),
            "date_display": loads(row["date_display_json"], []),
        })
        if len(variants) >= limit:
            break
    return variants

def available_years() -> list[int]:
    try:
        with closing(connect_readonly()) as conn:
            rows = conn.execute("SELECT year FROM years ORDER BY year").fetchall()
            if rows:
                return [int(row["year"]) for row in rows]
            rows = conn.execute("SELECT date_display_json FROM docs WHERE date_display_json IS NOT NULL").fetchall()
    except Exception:
        return []
    years: set[int] = set()
    for row in rows:
        for value in loads(row["date_display_json"], []):
            year = str(value).split("/", 1)[0]
            if year.isdigit():
                years.add(int(year))
    return sorted(years)

def row_to_doc(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    def value(key: str) -> Any:
        return row[key] if key in keys else None
    return {
        "doc_id": row["doc_id"],
        "article_id": value("article_id"),
        "publication_id": value("publication_id"),
        "publication_name": value("publication_name"),
        "publication_author": value("publication_author"),
        "publication_type": value("publication_type"),
        "archive_id": value("archive_id"),
        "title": value("title"),
        "authors": loads(value("authors_json"), []),
        "tag_names": loads(value("tag_names_json"), []),
        "date_display": loads(value("date_display_json"), []),
        "source_files": loads(row["source_files_json"], []) if "source_files_json" in keys else [],
        "source_file_count": value("source_file_count"),
        "content": (row["content"] or "") if "content" in keys else "",
        "path": value("path"),
        "article": loads(value("article_json"), {}),
    }

def loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback
