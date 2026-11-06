import calendar
import hashlib
import json
import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ParseOCR(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_thresholds: list[float] | None = Field(default=None, min_length=4, max_length=4)
    auto_vsplit: bool | None = None
    vsplit: float | None = Field(default=None, ge=0, le=1)


class ParseDate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int | None = Field(default=None, ge=1, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)


class ParseArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    authors: list[str] = Field(default_factory=list, max_length=50)
    dates: list[ParseDate] = Field(default_factory=list[ParseDate], max_length=50)
    is_range_date: bool = False
    page_start: int = Field(ge=1, le=10000)
    page_end: int = Field(ge=1, le=10000)
    content: str = Field(min_length=1, max_length=200000)
    base_part_count: int = Field(ge=1, le=15000)
    ocr: ParseOCR | None = None
    ocr_exceptions: dict[str, ParseOCR] = Field(default_factory=dict, max_length=100)


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str = Field(min_length=1, max_length=512)
    base_content_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")
    articles: list[ParseArticle] = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


def queue_item(source: dict[str, Any]) -> dict[str, Any]:
    article: dict[str, Any] = source.get("article") or {}
    return {
        "doc_id": source.get("doc_id"),
        "archive_id": source.get("archive_id"),
        "publication_id": source.get("publication_id"),
        "publication_name": source.get("publication_name"),
        "publication_type": source.get("publication_type"),
        "title": source.get("title"),
        "authors": source.get("authors") or [],
        "date_display": source.get("date_display") or [],
        "page_start": article.get("page_start"),
        "page_end": article.get("page_end"),
        "source_files": source.get("source_files") or [],
    }


def is_queue_source(source: dict[str, Any] | None, archive_id: int, prefix: str) -> bool:
    return bool(
        source
        and source.get("archive_id") == archive_id
        and str(source.get("title") or "").startswith(prefix)
    )


def validate_articles(
    articles: list[ParseArticle],
    source: dict[str, Any],
    queue_prefix: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    current: dict[str, Any] = source.get("article") or {}
    source_start = int(current.get("page_start") or 0)
    source_end = int(current.get("page_end") or 0)
    normalized: list[dict[str, Any]] = []
    covered: set[int] = set()
    article_ids: set[str] = set()
    for article in articles:
        if not article.title.strip():
            return None, "article title cannot be empty"
        if article.title.strip().startswith(queue_prefix):
            return None, "article title must replace the placeholder"
        if not article.content.strip():
            return None, "article content cannot be empty"
        if article.page_start > article.page_end or article.page_start < source_start or article.page_end > source_end:
            return None, "article page range is outside the source range"
        if any(not author.strip() or len(author.strip()) > 300 for author in article.authors):
            return None, "article author is invalid"
        for page in range(article.page_start, article.page_end + 1):
            covered.add(page)
        item: dict[str, Any] = article.model_dump(exclude_none=True)
        item["title"] = item["title"].strip()
        item["authors"] = [author.strip() for author in item.get("authors", [])]
        dates_data: list[dict[str, int]] = item.get("dates", [])
        dates_data = [date for date in dates_data if date]
        item["dates"] = dates_data
        for date in dates_data:
            year, month, day = date.get("year"), date.get("month"), date.get("day")
            if day is not None and month is None:
                return None, "article day requires a month"
            if month is not None and year is None:
                return None, "article month requires a year"
            if day is not None and month is not None and year is not None and day > calendar.monthrange(year, month)[1]:
                return None, "article date is not a real calendar date"
        ocr_values = ([article.ocr] if article.ocr is not None else []) + list(article.ocr_exceptions.values())
        for options in ocr_values:
            thresholds = options.content_thresholds
            if thresholds is not None and (
                any(not math.isfinite(value) or value < 0 or value > 1 for value in thresholds)
                or thresholds[0] + thresholds[1] >= 1
                or thresholds[2] + thresholds[3] >= 1
            ):
                return None, "article OCR crop is invalid"
        for raw_page in article.ocr_exceptions:
            if not re.fullmatch(r"[1-9][0-9]*", raw_page) or not article.page_start <= int(raw_page) <= article.page_end:
                return None, "article OCR exception page is invalid"
        dates = sorted(
            f"{date.get('year') or '0000'}-{int(date.get('month') or 0):02d}-{int(date.get('day') or 0):02d}"
            for date in dates_data
        )
        identity = hashlib.md5(json.dumps([
            item["title"], dates, bool(item.get("is_range_date")), sorted(item["authors"]), "",
        ], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:10]
        if identity in article_ids:
            return None, "articles would generate duplicate article IDs"
        article_ids.add(identity)
        normalized.append(item)
    expected = set(range(source_start, source_end + 1))
    if covered != expected:
        missing = sorted(expected - covered)
        return None, f"article ranges do not cover pages: {', '.join(map(str, missing))}"
    return normalized, None
