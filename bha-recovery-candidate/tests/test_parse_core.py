"""Validate split-article input without dispatching or changing source files."""
import unittest
from unittest.mock import patch

from app import parse_api
from app.parse_core import ParseArticle, is_queue_source, queue_item, validate_articles


class ParseCoreTests(unittest.TestCase):
    def setUp(self):
        self.source = {"article": {"page_start": 1, "page_end": 2}}
        self.base = {"title": "新文章", "authors": [" 作者 "], "dates": [{"year": 2024, "month": 2, "day": 29}],
                     "page_start": 1, "page_end": 2, "content": "正文", "base_part_count": 1}

    def validate(self, values):
        return validate_articles([ParseArticle(**value) for value in values], self.source, "【文章待拆分】")

    def test_normalizes_dates_and_preserves_ocr_configuration(self):
        value = {**self.base, "ocr": {"content_thresholds": [0.1, 0.2, 0.1, 0.2]},
                 "ocr_exceptions": {"2": {"auto_vsplit": True}}}
        articles, error = self.validate([value])
        self.assertIsNone(error)
        self.assertEqual(articles[0]["authors"], ["作者"])
        self.assertEqual(articles[0]["dates"], [{"year": 2024, "month": 2, "day": 29}])
        self.assertEqual(articles[0]["ocr_exceptions"], {"2": {"auto_vsplit": True}})

    def test_rejects_invalid_calendar_coverage_ocr_and_duplicate_ids(self):
        cases = [
            ([{**self.base, "dates": [{"year": 2023, "month": 2, "day": 29}]}], "calendar"),
            ([{**self.base, "dates": [{"year": 2024, "day": 3}]}], "requires a month"),
            ([{**self.base, "page_end": 1}], "do not cover"),
            ([{**self.base, "ocr": {"content_thresholds": [0.5, 0.5, 0, 0]}}], "crop"),
            ([{**self.base, "ocr_exceptions": {"3": {"auto_vsplit": True}}}], "exception page"),
            ([{**self.base, "page_end": 1}, {**self.base, "page_start": 2}], "duplicate"),
        ]
        for articles, expected in cases:
            with self.subTest(expected=expected):
                result, error = self.validate(articles)
                self.assertIsNone(result)
                self.assertIn(expected, error)

    def test_invalid_proxy_settings_fail_closed(self):
        with patch.dict("os.environ", {"BHA_TRUST_PROXY_CIDRS": "not-a-network", "BHA_TRUST_PROXY_HOPS": "bad"}):
            self.assertEqual(parse_api.proxy_networks(), ())
            self.assertEqual(parse_api.proxy_hops(), 1)
        with patch.dict("os.environ", {"BHA_TRUST_PROXY_CIDRS": "10.0.0.0/8,2001:db8::/32", "BHA_TRUST_PROXY_HOPS": "2"}):
            self.assertEqual(len(parse_api.proxy_networks()), 2)
            self.assertEqual(parse_api.proxy_hops(), 2)

    def test_queue_contract_preserves_fields_and_checks_archive_and_prefix(self):
        source = {"doc_id": "25:1:a:p", "archive_id": 25, "publication_id": "p", "publication_name": "小报",
                  "publication_type": "img", "title": "【文章待拆分】小报", "authors": ["作者"],
                  "date_display": ["1967/1/1"], "article": {"page_start": 1, "page_end": 2},
                  "source_files": ["first", "second"]}
        self.assertTrue(is_queue_source(source, 25, "【文章待拆分】"))
        self.assertFalse(is_queue_source(source, 24, "【文章待拆分】"))
        self.assertFalse(is_queue_source({**source, "title": "已拆分"}, 25, "【文章待拆分】"))
        self.assertFalse(is_queue_source(None, 25, "【文章待拆分】"))
        self.assertEqual(queue_item(source), {key: source[key] for key in (
            "doc_id", "archive_id", "publication_id", "publication_name", "publication_type", "title", "authors", "date_display", "source_files")}
            | {"page_start": 1, "page_end": 2})
        self.assertEqual(queue_item({})["authors"], [])
        self.assertEqual(queue_item({})["date_display"], [])
        self.assertEqual(queue_item({})["source_files"], [])

    def test_rejects_each_individual_page_author_date_and_crop_boundary(self):
        cases = [
            ({"title": "   "}, "title cannot be empty"),
            ({"title": "【文章待拆分】标题"}, "placeholder"),
            ({"content": "   "}, "empty"),
            ({"page_start": 2, "page_end": 1}, "outside"),
            ({"page_start": 1, "page_end": 3}, "outside"),
            ({"authors": ["   "]}, "author"),
            ({"authors": ["甲" * 301]}, "author"),
            ({"dates": [{"month": 2}]}, "requires a year"),
            ({"ocr": {"content_thresholds": [-0.1, 0, 0, 0]}}, "crop"),
            ({"ocr": {"content_thresholds": [0, 0, 0, 1.1]}}, "crop"),
            ({"ocr": {"content_thresholds": [0, 0, 0.5, 0.5]}}, "crop"),
            ({"ocr": {"content_thresholds": [float("nan"), 0, 0, 0]}}, "crop"),
            ({"ocr": {"content_thresholds": [float("inf"), 0, 0, 0]}}, "crop"),
            ({"ocr_exceptions": {"01": {"auto_vsplit": True}}}, "exception page"),
        ]
        for changed, expected in cases:
            with self.subTest(changed=changed):
                result, error = self.validate([{**self.base, **changed}])
                self.assertIsNone(result)
                self.assertIn(expected, error)
        result, error = self.validate([{**self.base, "ocr": {"content_thresholds": [0, 0.99, 0, 0.99]}}])
        self.assertIsNone(error)
        self.assertEqual(result[0]["ocr"]["content_thresholds"], [0, 0.99, 0, 0.99])

    def test_distinct_range_flag_changes_identity_and_duplicate_authors_are_order_independent(self):
        first = {**self.base, "authors": ["甲", "乙"], "page_start": 1, "page_end": 1}
        other = {**self.base, "authors": ["乙", "甲"], "page_start": 2, "page_end": 2}
        self.assertIn("duplicate", self.validate([first, other])[1])
        result, error = self.validate([first, {**other, "is_range_date": True}])
        self.assertIsNone(error)
        self.assertEqual(len(result), 2)
        self.assertTrue(result[1]["is_range_date"])

    def test_normalized_title_author_length_and_exception_first_page(self):
        result, error = self.validate([{**self.base, "title": "  标题  ", "authors": ["甲" * 300],
                                        "ocr_exceptions": {"1": {"auto_vsplit": True}}}])
        self.assertIsNone(error)
        self.assertEqual(result[0]["title"], "标题")
        self.assertEqual(result[0]["authors"], ["甲" * 300])
        self.assertEqual(result[0]["ocr_exceptions"]["1"], {"auto_vsplit": True})

    def test_duplicate_identity_distinguishes_year_month_and_day(self):
        first = {**self.base, "title": "同题", "page_start": 1, "page_end": 1}
        second = {**self.base, "title": "同题", "page_start": 2, "page_end": 2}
        for dates in ([{"year": 2023, "month": 2, "day": 28}],
                      [{"year": 2024, "month": 1, "day": 29}],
                      [{"year": 2024, "month": 2, "day": 28}]):
            with self.subTest(dates=dates):
                result, error = self.validate([first, {**second, "dates": dates}])
                self.assertIsNone(error)
                self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
