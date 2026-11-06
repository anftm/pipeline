import unittest
from unittest.mock import patch

from app import search_logic
from app.data_loader import search_words
from app.indexer import BULK_CHUNK_SIZE, BULK_MAX_BYTES, index_mapping
from app.search_logic import (
    SearchRequest,
    build_query,
    content_literal_query,
    date_filters,
    highlight_text,
    literal_pattern,
    local_snippets,
    requires_literal_verification,
    sort_clause,
    trim_hit,
)


class SearchQueryTests(unittest.TestCase):
    def test_search_highlights_reject_malformed_backend_payloads(self):
        for malformed in ("text", 7, {"content": "text"}, {"content": [42]}):
            with self.subTest(malformed=malformed), self.assertRaises(TypeError):
                trim_hit({"_id": "1", "highlight": malformed})
        result = trim_hit({"_id": "1", "highlight": {"content": ["a <mark>hit</mark>"]}})
        self.assertEqual(result["snippet"], ["a <mark>hit</mark>"])

    def test_prepared_highlights_reuse_terms_without_sharing_mutable_values(self):
        search_logic._prepared_highlights.cache_clear()
        self.addCleanup(search_logic._prepared_highlights.cache_clear)
        with patch.object(search_logic, "highlight_terms", wraps=search_logic.highlight_terms) as terms:
            title = highlight_text("文化 革命", "  文化 革命  ", False)
            snippet = local_snippets("文化 革命", "文化 革命", False)
            self.assertEqual(terms.call_count, 1)
            self.assertEqual(title, "<mark>文化</mark> <mark>革命</mark>")
            self.assertEqual(snippet, [title])
            self.assertEqual(highlight_text("文化 革命", "文化 革命", True), "<mark>文化 革命</mark>")
            self.assertEqual(terms.call_count, 2)
        values = search_logic.highlight_terms("文化 革命", False)
        values.append("changed")
        self.assertNotIn("changed", search_logic.highlight_terms("文化 革命", False))
        self.assertEqual(search_logic._prepared_highlights.cache_parameters()["maxsize"], 128)
        for number in range(160):
            highlight_text("title", str(number), True)
        self.assertEqual(search_logic._prepared_highlights.cache_info().currsize, 128)

    def test_prepared_highlights_preserve_escaping_and_casefold_behavior(self):
        for query in ("<tag>", "&", '"', "手*机", "手?机"):
            with self.subTest(query=query):
                safe = search_logic.escape(query)
                self.assertEqual(highlight_text(query, query, True), f"<mark>{safe}</mark>")
                self.assertEqual(local_snippets(query, query, True), [f"<mark>{safe}</mark>"])
        self.assertEqual(highlight_text("ABC", "abc", True), "ABC")
        self.assertEqual(local_snippets("ABC", "abc", True), ["ABC"])
        self.assertEqual(local_snippets("Straße", "STRASSE", True), ["Straße"])
        self.assertEqual(highlight_text("ＡＢＣ", "ABC", True), "ＡＢＣ")
        self.assertEqual(local_snippets("<tag>", "unmatched", True), ["&lt;tag&gt;"])

    def test_word_tokenization_preserves_chinese_words_and_normalizes_latin(self):
        self.assertEqual(search_words("手机 ABC-123"), "手机 abc 123")
        self.assertEqual(search_words("手。机"), "手 机")
        self.assertEqual(search_words("ＡＢＣ１２３"), "abc123")

    def test_normal_search_requires_all_terms_per_route(self):
        query = build_query(SearchRequest(q="文化 革命", exact=False))
        routes = query["bool"]["must"][0]["bool"]["should"]
        self.assertEqual(query["bool"]["must"][0]["bool"]["minimum_should_match"], 1)
        for route in routes:
            self.assertEqual(route["bool"]["must"][0]["multi_match"]["operator"], "AND")
            self.assertEqual(len(route["bool"]["should"]), 1)

    def test_exact_search_uses_literal_fields_and_safe_wildcards(self):
        query = build_query(SearchRequest(q="手。机", exact=True))
        exact_query = query["bool"]["must"][0]
        fields = [
            next(iter(item["constant_score"]["filter"]["wildcard"]))
            for item in exact_query["dis_max"]["queries"]
            if "constant_score" in item
        ]
        self.assertEqual(fields, ["title.literal", "publication_name.literal", "authors.literal", "tag_names.keyword"])
        self.assertIn("match_phrase", exact_query["dis_max"]["queries"][-1])
        self.assertEqual(literal_pattern("手。机"), "*手。机*")
        self.assertEqual(literal_pattern("手*机"), "*手\\*机*")

    def test_content_literal_query_uses_literal_phrase_matching(self):
        self.assertEqual(
            content_literal_query("手机"),
            {"match_phrase": {"content": {"query": "手机", "boost": 1}}},
        )
        self.assertEqual(content_literal_query("手*机"), {"match_phrase": {"content": {"query": "手*机", "boost": 1}}})
        self.assertEqual(content_literal_query("手?机"), {"match_phrase": {"content": {"query": "手?机", "boost": 1}}})

    def test_literal_verification_targets_punctuation_but_not_phrase_spaces(self):
        self.assertTrue(requires_literal_verification("手。机", True))
        self.assertTrue(requires_literal_verification("手*机", True))
        self.assertTrue(requires_literal_verification("手?机", True))
        self.assertFalse(requires_literal_verification("文化 革命", True))
        self.assertFalse(requires_literal_verification("手。机", False))

    def test_fulltext_off_excludes_content_fields(self):
        normal = str(build_query(SearchRequest(q="手机", exact=False, fulltext=False)))
        exact = str(build_query(SearchRequest(q="手机", exact=True, fulltext=False)))
        self.assertNotIn("content", normal)
        self.assertNotIn("content", exact)
        self.assertNotIn("content_words", str(build_query(SearchRequest(q="手机", exact=False, fulltext=True))))

    def test_empty_query_keeps_filters(self):
        query = build_query(SearchRequest(q="", sources=["刊物"], archive_id=3))
        self.assertEqual(query["bool"]["must"], [{"match_all": {}}])
        self.assertIn({"terms": {"publication_name.keyword": ["刊物"]}}, query["bool"]["filter"])
        self.assertIn({"term": {"archive_id": 3}}, query["bool"]["filter"])

    def test_every_query_excludes_only_internal_metadata_document(self):
        query = build_query(SearchRequest(q="文化革命", exact=True))["bool"]
        self.assertIn({"ids": {"values": ["__meta__"]}}, query["must_not"])
        self.assertNotIn("exists", str(query))

    def test_include_exclude_and_date_filters(self):
        body = SearchRequest(
            q="手机",
            exact=False,
            authors=["甲"],
            exclude_tags=["乙"],
            date_from=19600101,
            date_to=19691231,
        )
        query = build_query(body)["bool"]
        self.assertIn({"terms": {"authors.keyword": ["甲"]}}, query["filter"])
        self.assertIn({"terms": {"tag_names.keyword": ["乙"]}}, query["must_not"])
        self.assertIn({"range": {"date_max": {"gte": 19600101}}}, query["filter"])
        self.assertIn({"range": {"date_min": {"lte": 19691231}}}, query["filter"])

    def test_invalid_dates_are_ignored(self):
        self.assertEqual(date_filters(SearchRequest(date_from=123, date_to=99999999)), [])

    def test_string_date_term_is_normalized(self):
        filters = date_filters(SearchRequest(date_terms=[{"year": "1966", "month": "5"}]))
        self.assertEqual(filters, [{"term": {"date_year_months": "1966-05"}}])

    def test_every_exact_date_shape_uses_its_narrowest_field(self):
        cases = [
            ({"year": 1966}, "date_years", 1966),
            ({"month": 5}, "date_months", 5),
            ({"day": 16}, "date_days", 16),
            ({"year": 1966, "month": 5}, "date_year_months", "1966-05"),
            ({"year": 1966, "day": 16}, "date_year_days", "1966-16"),
            ({"month": 5, "day": 16}, "date_month_days", "05-16"),
            ({"year": 1966, "month": 5, "day": 16}, "date_year_month_days", "1966-05-16"),
        ]
        for term, field, value in cases:
            with self.subTest(term=term):
                self.assertEqual(date_filters(SearchRequest(date_terms=[term])), [{"term": {field: value}}])

    def test_multiple_date_terms_are_or_but_other_filters_remain_and(self):
        query = build_query(SearchRequest(
            sources=["刊物"],
            date_terms=[{"year": 1966}, {"month": 5, "day": 16}],
        ))["bool"]
        date_or = next(item["bool"] for item in query["filter"] if "bool" in item)
        self.assertEqual(date_or["minimum_should_match"], 1)
        self.assertEqual(len(date_or["should"]), 2)
        self.assertIn({"terms": {"publication_name.keyword": ["刊物"]}}, query["filter"])

    def test_filter_values_are_trimmed_and_not_interpreted_as_dsl(self):
        query = build_query(SearchRequest(
            q='title:(*) OR *:*', exact=True, fulltext=False,
            sources=["  刊物  ", ""], exclude_authors=[" 作者 "],
        ))
        encoded = str(query)
        self.assertIn("title.literal", encoded)
        self.assertNotIn("query_string", encoded)
        self.assertIn({"terms": {"publication_name.keyword": ["刊物"]}}, query["bool"]["filter"])
        self.assertIn({"terms": {"authors.keyword": ["作者"]}}, query["bool"]["must_not"])

    def test_cjk_fields_use_boundary_aware_analyzer(self):
        properties = index_mapping()["mappings"]["properties"]
        self.assertEqual(properties["title"]["fields"]["cjk"]["analyzer"], "cjk")
        self.assertEqual(properties["content"]["analyzer"], "cjk")
        self.assertTrue(properties["content"]["index_phrases"])
        self.assertEqual(properties["title"]["fields"]["literal"]["type"], "wildcard")
        self.assertEqual(properties["authors"]["fields"]["literal"]["type"], "wildcard")
        self.assertNotIn("literal", properties["tag_names"]["fields"])
        settings = index_mapping()["settings"]["index"]
        self.assertEqual(settings["refresh_interval"], "30s")
        self.assertNotIn("translog.durability", settings)
        self.assertNotIn("translog.sync_interval", settings)

    def test_indexing_bulk_is_large_enough_for_remote_es(self):
        self.assertEqual(BULK_CHUNK_SIZE, 100)
        self.assertEqual(BULK_MAX_BYTES, 16 * 1024 * 1024)

    def test_exact_highlight_keeps_punctuation_boundary(self):
        self.assertEqual(highlight_text("手机 手。机", "手机", True), "<mark>手机</mark> 手。机")
        self.assertIn("<mark>手?机</mark>", local_snippets("这是手?机测试", "手?机", True)[0])

    def test_highlight_escapes_html(self):
        self.assertEqual(highlight_text("<手机>", "手机", True), "&lt;<mark>手机</mark>&gt;")
        snippet = local_snippets('<script>alert("手机")</script>', "手机", True)[0]
        self.assertNotIn("<script>", snippet)
        self.assertIn("&lt;script&gt;", snippet)

    def test_es_highlight_escapes_html_but_preserves_plain_mark_tags(self):
        hit = {"_id": "doc", "highlight": {"content": ["<script>x</script><mark>手机</mark>"]}}
        result = trim_hit(hit, {"doc_id": "doc", "title": "标题"}, "手机", True, True)
        self.assertEqual(result["snippet"], ["&lt;script&gt;x&lt;/script&gt;<mark>手机</mark>"])

    def test_oversized_es_highlight_falls_back_to_local_snippet(self):
        hit = {"_id": "doc", "highlight": {"content": ["甲" * 1001]}}
        stored = {"doc_id": "doc", "title": "标题", "content": "前文手机后文"}
        result = trim_hit(hit, stored, "手机", True, True)
        self.assertNotIn("甲" * 1001, result["snippet"])
        self.assertIn("<mark>手机</mark>", result["snippet"][0])

    def test_oversized_literal_snippet_uses_short_term_snippet(self):
        content = "手" + "甲" * 1200 + "机"
        snippet = local_snippets(content, "手?机", True)[0]
        self.assertLessEqual(len(snippet), 400)
        self.assertNotIn("甲" * 1000, snippet)

    def test_sort_clauses(self):
        self.assertIsNone(sort_clause("relevance"))
        self.assertEqual(sort_clause("title")[0], {"title.keyword": {"order": "asc", "missing": "_last"}})
        self.assertEqual(sort_clause("date_asc")[0], {"date_min": {"order": "asc", "missing": "_last"}})
        self.assertEqual(sort_clause("date_desc")[0], {"date_max": {"order": "desc", "missing": "_last"}})
        self.assertEqual(sort_clause("source")[0], {"publication_name.keyword": {"order": "asc", "missing": "_last"}})
        self.assertIsNone(sort_clause("unsupported"))


if __name__ == "__main__":
    unittest.main()
