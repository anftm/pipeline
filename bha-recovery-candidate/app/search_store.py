"""Bounded SQLite reads used only for search result snippets."""

from __future__ import annotations

from contextlib import closing
from typing import Any

from . import doc_store
from .data_loader import search_words

SNIPPET_CONTEXT_BEFORE = 80
SNIPPET_CONTEXT_AFTER = 140
SNIPPET_PREFIX_CHARS = 220


class SearchStoreError(RuntimeError):
    pass


def snippet_terms(query: str, exact: bool) -> list[str]:
    text = query.strip()
    if not text:
        return []
    values = [text] if exact else [term for term in search_words(text).split() if term]
    return list(dict.fromkeys(values))


def get_search_docs(doc_ids: list[str], query: str, fulltext: bool, exact: bool) -> dict[str, dict[str, Any]]:
    if not doc_ids:
        return {}
    terms = snippet_terms(query, exact) if fulltext else []
    requested_values = ",".join("(?, ?)" for _ in doc_ids)
    requested_params: list[Any] = [value for order, doc_id in enumerate(doc_ids) for value in (doc_id, order)]
    if terms:
        positions = " UNION ALL ".join(
            "SELECT instr(lower(d.content), lower(?)) AS pos"
            if any(character.lower() != character.upper() for character in term)
            else "SELECT instr(d.content, ?) AS pos"
            for term in terms
        )
        match_sql = f"(SELECT MIN(NULLIF(pos, 0)) FROM ({positions}))"
        match_params: list[Any] = terms
        window_chars = SNIPPET_CONTEXT_BEFORE + max(map(len, terms)) + SNIPPET_CONTEXT_AFTER
    else:
        match_sql = "NULL"
        match_params = []
        window_chars = SNIPPET_PREFIX_CHARS
    try:
        with closing(doc_store.connect_readonly()) as conn:
            # OFFSET blocks flattening on older SQLite; LIMIT -1 keeps every result.
            # Materialize only IDs and offsets, never the article bodies.
            rows = conn.execute(
                f"""
                WITH requested(doc_id, result_order) AS (VALUES {requested_values}),
                located AS {"MATERIALIZED" if terms else "NOT MATERIALIZED"} (
                    SELECT r.doc_id, r.result_order, {match_sql} AS match_pos
                    FROM requested r
                    {"JOIN docs d ON d.doc_id = r.doc_id" if terms else ""}
                    {"LIMIT -1 OFFSET 0" if terms else ""}
                )
                SELECT d.doc_id, d.publication_name, d.publication_type, d.title, d.authors_json,
                       d.tag_names_json, d.date_display_json, d.source_file_count,
                       substr(
                           d.content,
                           CASE WHEN match_pos IS NULL THEN 1 ELSE max(1, match_pos - ?) END,
                           CASE WHEN match_pos IS NULL THEN ? ELSE ? END
                       ) AS content,
                       CASE WHEN match_pos IS NULL THEN 1 ELSE max(1, match_pos - ?) END AS content_window_start,
                       length(d.content) AS content_length
                FROM located JOIN docs d ON d.doc_id = located.doc_id
                ORDER BY result_order
                """,
                [
                    *requested_params, *match_params,
                    SNIPPET_CONTEXT_BEFORE, SNIPPET_PREFIX_CHARS, window_chars, SNIPPET_CONTEXT_BEFORE,
                ],
            ).fetchall()
    except Exception as exc:
        raise SearchStoreError("search sidecar query failed") from exc
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = doc_store.row_to_doc(row)
        item["content_window_start"] = int(row["content_window_start"] or 1)
        item["content_length"] = int(row["content_length"] or 0)
        result[str(row["doc_id"])] = item
    return result


def literal_match_ids(doc_ids: list[str], query: str, batch_size: int = 500, row_order: bool = False) -> set[str]:
    if not doc_ids or not query:
        return set()
    matches: set[str] = set()
    try:
        with closing(doc_store.connect_readonly()) as conn:
            case_insensitive = any(character.lower() != character.upper() for character in query)
            if case_insensitive:
                def casefold_value(value: object) -> str:
                    return str(value or "").casefold()

                conn.create_function("casefold", 1, casefold_value)
                value_expression = "casefold({})"
                needle = query.casefold()
            else:
                value_expression = "{}"
                needle = query
            selector = "doc_id"
            ordered_ids: list[str | int] = list(doc_ids)
            if row_order:
                conn.execute("BEGIN")
                # Resolve IDs through the covering index before reading article bodies.
                numbers: set[int] = set()
                for offset in range(0, len(doc_ids), batch_size):
                    batch = doc_ids[offset:offset + batch_size]
                    rows = conn.execute(
                        f"SELECT rowid FROM docs WHERE doc_id IN ({','.join('?' for _ in batch)})", batch,
                    ).fetchall()
                    numbers.update(int(row[0]) for row in rows)
                selector = "rowid"
                ordered_ids = sorted(numbers)
            for offset in range(0, len(ordered_ids), batch_size):
                batch = ordered_ids[offset:offset + batch_size]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT doc_id
                    FROM docs
                    WHERE {selector} IN ({placeholders})
                      AND (
                          instr({value_expression.format('title')}, ?) > 0
                          OR instr({value_expression.format('publication_name')}, ?) > 0
                          OR EXISTS (
                              SELECT 1 FROM json_each(COALESCE(authors_json, '[]'))
                              WHERE instr({value_expression.format('json_each.value')}, ?) > 0
                          )
                          OR EXISTS (
                              SELECT 1 FROM json_each(COALESCE(tag_names_json, '[]'))
                              WHERE instr({value_expression.format('json_each.value')}, ?) > 0
                          )
                          OR instr({value_expression.format('content')}, ?) > 0
                      )
                    """,
                    [*batch, needle, needle, needle, needle, needle],
                ).fetchall()
                matches.update(str(row["doc_id"]) for row in rows)
    except Exception as exc:
        raise SearchStoreError("literal sidecar verification failed") from exc
    return matches
