from functools import lru_cache
from html import escape
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from .data_loader import search_words


MAX_HIGHLIGHT_FRAGMENT_CHARS = 1000


class SearchRequest(BaseModel):
    q: str = Field(default="", max_length=256)
    page: int = 1
    page_size: int = 20
    exact: bool = True
    fulltext: bool = True
    source: str | None = None
    sources: list[str] = Field(default_factory=list, max_length=500)
    exclude_sources: list[str] = Field(default_factory=list, max_length=500)
    author: str | None = None
    authors: list[str] = Field(default_factory=list, max_length=500)
    exclude_authors: list[str] = Field(default_factory=list, max_length=500)
    tag: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=500)
    exclude_tags: list[str] = Field(default_factory=list, max_length=500)
    archive_id: int | None = None
    publication_type: str | None = None
    date_from: int | None = None
    date_to: int | None = None
    date_terms: list[dict[str, int | str | None]] = Field(default_factory=list[dict[str, int | str | None]])
    sort: Literal["relevance", "date_desc", "date_asc", "source", "title"] = "relevance"


def literal_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")
    return f"*{escaped}*"


def has_query_punctuation(query: str) -> bool:
    # `*` and `?` are literal characters in this project, not punctuation; they
    # must keep the exact path so literal wildcard-character search still works.
    # Everything else that is not alphanumeric or whitespace triggers the slow
    # SQLite literal verification, so it is executed as normal search instead.
    return any(
        not character.isalnum() and not character.isspace() and character not in "*?"
        for character in query.strip()
    )


def effective_exact(query: str, exact: bool) -> bool:
    # The UI toggle and URL still say "exact"; punctuation queries are executed
    # as normal search because their literal verification scans every candidate
    # the analyzer produced. This is an execution-mode override only.
    return bool(exact) and not has_query_punctuation(query)


def requires_literal_verification(query: str, exact: bool) -> bool:
    return bool(exact) and any(
        not character.isalnum() and not character.isspace()
        for character in query.strip()
    )


def content_literal_query(query: str) -> dict[str, Any]:
    return {"match_phrase": {"content": {"query": query, "boost": 1}}}


def literal_query(query: str, fulltext: bool) -> dict[str, Any]:
    fields = [
        ("title.literal", 4.0),
        ("publication_name.literal", 2.0),
        ("authors.literal", 2.0),
        ("tag_names.keyword", 1.0),
    ]
    pattern = literal_pattern(query)
    queries: list[dict[str, Any]] = [
        {
            "constant_score": {
                "filter": {
                    "wildcard": {
                        field: {
                            "value": pattern,
                            "case_insensitive": True,
                        }
                    }
                },
                "boost": boost,
            }
        }
        for field, boost in fields
    ]
    if fulltext:
        queries.append(content_literal_query(query))
    return {"dis_max": {"queries": queries}}


def normal_query(query: str, fulltext: bool) -> dict[str, Any]:
    word_fields = [
        "title_words^4",
        "authors_words^2",
        "publication_name_words^2",
        "tag_names_words",
    ]
    cjk_fields = ["title.cjk^4", "publication_name.cjk^2"]
    phrase_word_fields = ["title_words^4", "publication_name_words^2"]
    phrase_cjk_fields = ["title.cjk^4", "publication_name.cjk^2"]
    if fulltext:
        cjk_fields.append("content")
        phrase_cjk_fields.append("content")
    words = search_words(query)
    routes: list[dict[str, object]] = []
    if words:
        routes.append({
            "bool": {
                "must": [{
                    "multi_match": {
                        "query": words,
                        "fields": word_fields,
                        "operator": "AND",
                        "type": "cross_fields",
                    }
                }],
                "should": [{
                    "multi_match": {
                        "query": words,
                        "type": "phrase",
                        "fields": phrase_word_fields,
                        "boost": 3,
                    }
                }],
            }
        })
    routes.append({
        "bool": {
            "must": [{
                "multi_match": {
                    "query": query,
                    "fields": cjk_fields,
                    "operator": "AND",
                    "type": "cross_fields",
                }
            }],
            "should": [{
                "multi_match": {
                    "query": query,
                    "type": "phrase",
                    "fields": phrase_cjk_fields,
                    "boost": 2,
                }
            }],
        }
    })
    return {
        "bool": {
            "should": routes,
            "minimum_should_match": 1,
        }
    }


def build_query(body: SearchRequest) -> dict[str, Any]:
    q = body.q.strip()
    filters: list[dict[str, Any]] = []
    must_not: list[dict[str, Any]] = [{"ids": {"values": ["__meta__"]}}]
    source_values = clean_values(body.sources or ([body.source] if body.source else []))
    author_values = clean_values(body.authors or ([body.author] if body.author else []))
    tag_values = clean_values(body.tags or ([body.tag] if body.tag else []))
    exclude_source_values = clean_values(body.exclude_sources)
    exclude_author_values = clean_values(body.exclude_authors)
    exclude_tag_values = clean_values(body.exclude_tags)
    if source_values:
        filters.append({"terms": {"publication_name.keyword": source_values}})
    if author_values:
        filters.append({"terms": {"authors.keyword": author_values}})
    if tag_values:
        filters.append({"terms": {"tag_names.keyword": tag_values}})
    if exclude_source_values:
        must_not.append({"terms": {"publication_name.keyword": exclude_source_values}})
    if exclude_author_values:
        must_not.append({"terms": {"authors.keyword": exclude_author_values}})
    if exclude_tag_values:
        must_not.append({"terms": {"tag_names.keyword": exclude_tag_values}})
    if body.archive_id is not None:
        filters.append({"term": {"archive_id": body.archive_id}})
    if body.publication_type:
        filters.append({"term": {"publication_type": body.publication_type}})
    filters.extend(date_filters(body))
    if not q:
        query: dict[str, Any] = {"match_all": {}}
    elif body.exact:
        query = literal_query(q, body.fulltext)
    else:
        query = normal_query(q, body.fulltext)
    bool_query: dict[str, Any] = {"must": [query]}
    if filters:
        bool_query["filter"] = filters
    if must_not:
        bool_query["must_not"] = must_not
    return {"bool": bool_query}


def clean_values(values: list[str]) -> list[str]:
    return [str(item).strip() for item in values if str(item).strip()]


def clean_date_value(value: int | None) -> int | None:
    if value is None:
        return None
    if 10000101 <= value <= 99991231:
        return value
    return None


def clean_int_range(value: int | str | None, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if minimum <= value <= maximum:
        return value
    return None


def date_term_clause(field: str, value: str | int) -> dict[str, Any]:
    return {"term": {field: value}}


def point_date_filter(year: int | None, month: int | None, day: int | None) -> list[dict[str, Any]]:
    # Each selected part maps to the narrowest indexed field for that exact shape.
    # Do not add broader fallbacks here; they turn an exact date into a year/month hit.
    if year is not None and month is not None and day is not None:
        return [date_term_clause("date_year_month_days", f"{year:04d}-{month:02d}-{day:02d}")]
    if year is not None and month is not None:
        return [date_term_clause("date_year_months", f"{year:04d}-{month:02d}")]
    if year is not None and day is not None:
        return [date_term_clause("date_year_days", f"{year:04d}-{day:02d}")]
    if year is not None:
        return [date_term_clause("date_years", year)]
    if month is not None and day is not None:
        return [date_term_clause("date_month_days", f"{month:02d}-{day:02d}")]
    if month is not None:
        return [date_term_clause("date_months", month)]
    if day is not None:
        return [date_term_clause("date_days", day)]
    return []


def display_date_clause(display: str | None) -> dict[str, Any] | None:
    if not display:
        return None
    text = str(display).strip()
    if not text:
        return None
    return date_term_clause("date_display", text)


def date_filters(body: SearchRequest) -> list[dict[str, Any]]:
    date_from = clean_date_value(body.date_from)
    date_to = clean_date_value(body.date_to)
    filters: list[dict[str, Any]] = []
    if date_from is not None or date_to is not None:
        if date_from is not None:
            filters.append({"range": {"date_max": {"gte": date_from}}})
        if date_to is not None:
            filters.append({"range": {"date_min": {"lte": date_to}}})
    term_filters: list[dict[str, Any]] = []
    for term in body.date_terms:
        year = clean_int_range(term.get("year"), 1, 9999)
        month = clean_int_range(term.get("month"), 1, 12)
        day = clean_int_range(term.get("day"), 1, 31)
        display = term.get("display")
        if not isinstance(display, str):
            display = None
        if display:
            clauses: list[dict[str, Any]] = []
            display_clause = display_date_clause(display)
            if display_clause is not None:
                clauses.append(display_clause)
        else:
            clauses = point_date_filter(year, month, day)
        if len(clauses) == 1:
            term_filters.append(clauses[0])
    if len(term_filters) == 1:
        filters.append(term_filters[0])
    elif len(term_filters) > 1:
        filters.append({"bool": {"should": term_filters, "minimum_should_match": 1}})
    return filters


def highlight_terms(query: str, exact: bool) -> list[str]:
    if not query.strip():
        return []
    if exact:
        return [query.strip()]
    return [item for item in search_words(query).split() if item]


@lru_cache(maxsize=128)
def _prepared_highlights(query: str, exact: bool) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    terms = highlight_terms(query, exact)
    return (
        tuple((term, term.casefold()) for term in terms),
        tuple(escape(term) for term in sorted(terms, key=len, reverse=True)),
    )


def trim_hit(
    hit: dict[str, Any],
    stored_doc: dict[str, Any] | None = None,
    query: str = "",
    fulltext: bool = True,
    exact: bool = True,
) -> dict[str, Any]:
    source = stored_doc or {}
    highlight_value: object = hit.get("highlight", {}) or {}
    if not isinstance(highlight_value, dict):
        raise TypeError("search highlight must be an object")
    highlight_content: object = cast(dict[object, object], highlight_value).get("content") or []
    if not isinstance(highlight_content, list):
        raise TypeError("search highlight content must be a list")
    fragments = cast(list[object], highlight_content)
    if not all(isinstance(fragment, str) for fragment in fragments):
        raise TypeError("search highlight fragments must be strings")
    text_fragments = cast(list[str], fragments)
    snippets = [
        sanitize_highlight_fragment(snippet)
        for snippet in text_fragments
        if len(snippet) <= MAX_HIGHLIGHT_FRAGMENT_CHARS
    ]
    result = {
        "doc_id": hit.get("_id") or source.get("doc_id"),
        "publication_name": source.get("publication_name"),
        "publication_author": source.get("publication_author"),
        "publication_type": source.get("publication_type"),
        "source_file_count": int(source.get("source_file_count") or len(source.get("source_files") or [])),
        "date_display": source.get("date_display") or [],
        "tag_names": source.get("tag_names") or [],
        "title": source.get("title"),
        "authors": source.get("authors") or [],
        "snippet": snippets[:3] or local_snippets(
            str(source.get("content") or ""),
            query if fulltext else "",
            exact,
            int(source.get("content_window_start") or 1),
            int(source.get("content_length") or len(str(source.get("content") or ""))),
        ),
    }
    title_highlight = highlight_text(str(source.get("title") or ""), query, exact)
    if "<mark>" in title_highlight:
        result["title_highlight"] = title_highlight
    return result


def sanitize_highlight_fragment(fragment: str) -> str:
    safe = escape(fragment)
    return safe.replace("&lt;mark&gt;", "<mark>").replace("&lt;/mark&gt;", "</mark>")


def local_snippets(
    content: str,
    query: str,
    exact: bool = True,
    window_start: int = 1,
    total_length: int | None = None,
) -> list[str]:
    if not content:
        return [""]
    q = query.strip()
    if not q:
        return [escape(content[:220])]
    terms, replacements = _prepared_highlights(q, exact)
    lower_content = content.casefold()
    matches = [
        (index, term)
        for term, folded in terms or [(q, q.casefold())]
        if (index := lower_content.find(folded)) >= 0
    ]
    if matches:
        index, needle = min(matches, key=lambda item: item[0])
    else:
        index, needle = -1, ""
    if index < 0:
        return [escape(content[:220])]
    start = max(0, index - 80)
    end = min(len(content), index + len(needle) + 140)
    snippet = escape(content[start:end])
    for safe in replacements or (escape(q),):
        if safe:
            snippet = snippet.replace(safe, f"<mark>{safe}</mark>")
    absolute_start = max(0, window_start - 1) + start
    absolute_end = max(0, window_start - 1) + end
    complete_length = len(content) if total_length is None else max(0, total_length)
    prefix = "..." if absolute_start else ""
    suffix = "..." if absolute_end < complete_length else ""
    return [f"{prefix}{snippet}{suffix}"]


def highlight_text(text: str, query: str, exact: bool = True) -> str:
    if not text:
        return ""
    _, replacements = _prepared_highlights(query.strip(), exact)
    result = escape(text)
    for safe in replacements:
        if safe:
            result = result.replace(safe, f"<mark>{safe}</mark>")
    return result


def sort_clause(sort_key: str) -> list[dict[str, Any]] | None:
    if sort_key == "date_asc":
        return [{"date_min": {"order": "asc", "missing": "_last"}}, {"_score": "desc"}]
    if sort_key == "date_desc":
        return [{"date_max": {"order": "desc", "missing": "_last"}}, {"_score": "desc"}]
    if sort_key == "source":
        return [{"publication_name.keyword": {"order": "asc", "missing": "_last"}}, {"title.keyword": "asc"}]
    if sort_key == "title":
        return [{"title.keyword": {"order": "asc", "missing": "_last"}}, {"publication_name.keyword": "asc"}]
    return None
