import asyncio
import concurrent.futures
import json
import re
import threading
import time
import unittest
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from elasticsearch.exceptions import BadRequestError
import brotli
from starlette.requests import Request

from app import bootstrap_api, doc_store, document_identity, facet_store, main, parse_api, pipeline_dispatch, preview_api, proofread_api, proofread_core, reindex_api, runtime, search_api, search_logic, source_files


class SearchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def setUp(self):
        submissions = patch.dict('os.environ', {'BHA_PIPELINE_SUBMISSIONS_PAUSED': '0'})
        submissions.start()
        self.addCleanup(submissions.stop)
        self.index_status_patch = patch.object(runtime, "index_status", return_value="ready")
        self.search_lock_patch = patch.object(search_api, "serving_lock", return_value=nullcontext())
        self.runtime_lock_patch = patch.object(runtime, "serving_lock", return_value=nullcontext())
        self.index_status_patch.start()
        self.search_lock_patch.start()
        self.runtime_lock_patch.start()
        self.addCleanup(self.index_status_patch.stop)
        self.addCleanup(self.search_lock_patch.stop)
        self.addCleanup(self.runtime_lock_patch.stop)
        runtime.search_response_cache.clear()
        runtime.literal_search_cache.clear()
        runtime.search_inflight.clear()
        bootstrap_api.bootstrap_cache = None
        proofread_api.proofread_requests.clear()
        proofread_api.proofread_dispatches.clear()
        parse_api.parse_requests.clear()
        parse_api.parse_dispatches.clear()

    def test_preview_etag_and_proofread_content_hash(self):
        stored = {"doc_id": "doc-1", "title": "标题", "article": {"parts": [{"text": "正文"}]}}
        content_hash = document_identity.content_hash(stored)
        payload = {"content_hash": content_hash, "source_files": []}
        with patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(preview_api, "serving_lock", return_value=nullcontext()), \
                patch.object(preview_api, "get_doc", return_value=stored), \
                patch.object(preview_api, "preview_payload", AsyncMock(return_value=payload)):
            response = self.client.get("/api/preview/doc-1")
            cached = self.client.get("/api/preview/doc-1", headers={"If-None-Match": response.headers["etag"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content_hash"], content_hash)
        self.assertEqual(cached.status_code, 304)

    def test_preview_etag_invalidates_when_identity_changes_with_same_content(self):
        stored = {"doc_id": "doc-etag", "archive_id": 3, "article_id": "article", "publication_id": "publication",
                  "title": "标题", "article": {"parts": [{"text": "正文"}], "comments": []}, "source_files": []}
        current = dict(stored)
        with patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(preview_api, "serving_lock", return_value=nullcontext()), \
                patch.object(preview_api, "get_doc", side_effect=lambda _doc: current), \
                patch.object(preview_api, "variants_for_title", return_value=[]):
            first = self.client.get("/api/preview/doc-etag")
            first_hash = first.json()["content_hash"]
            current["publication_id"] = "new-publication"
            changed = self.client.get("/api/preview/doc-etag", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual((first.status_code, changed.status_code), (200, 200))
        self.assertNotEqual(first.headers["etag"], changed.headers["etag"])
        self.assertNotEqual(first_hash, changed.json()["content_hash"])
        self.assertEqual(changed.json()["publication_id"], "new-publication")

    def test_preview_payload_preserves_publication_author_for_proofread(self):
        stored = {"doc_id": "doc-1", "title": "标题", "publication_author": "原作者",
                  "article": {"parts": [{"text": "正文"}]}, "source_files": []}
        with patch.object(preview_api, "variants_for_title", return_value=[]):
            payload = asyncio.run(preview_api.preview_payload(stored))
        self.assertEqual(payload["publication_author"], "原作者")

    def test_proofread_rejects_stale_hash_and_deduplicates_dispatch(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "标题",
            "article": {"parts": [{"text": "旧", "type": "paragraph"}], "comments": []},
        }
        request = {
            "doc_id": "doc-1",
            "patch": {"version": 2, "parts": {"0": {"diff": "-1\t+新"}}, "comments": {}, "description": ""},
        }
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            stale = self.client.post("/api/proofread", json={**request, "base_content_hash": "0" * 64})
            current_hash = document_identity.content_hash(stored)
            first = self.client.post("/api/proofread", json={**request, "base_content_hash": current_hash})
            duplicate = self.client.post("/api/proofread", json={**request, "base_content_hash": current_hash})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["status"], "duplicate")
        self.assertEqual(first.json()["request_id"], duplicate.json()["request_id"])
        dispatch.assert_awaited_once()

    def test_proofread_rate_limit_does_not_reserve_duplicate_id(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "标题",
            "article": {"parts": [{"text": "旧", "type": "paragraph"}], "comments": []},
        }
        body = {
            "doc_id": "doc-1",
            "patch": {"version": 2, "parts": {"0": {"diff": "-1\t+新"}}, "comments": {}},
            "base_content_hash": document_identity.content_hash(stored),
        }
        dispatch = AsyncMock()
        limited = proofread_api.JSONResponse({"error": "rate limit"}, status_code=429)
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "proofread_rate_error", side_effect=[limited, None]), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            first = self.client.post("/api/proofread", json=body)
            second = self.client.post("/api/proofread", json=body)
        self.assertEqual(first.status_code, 429)
        self.assertEqual(second.status_code, 202)
        dispatch.assert_awaited_once()

    def test_proofread_requires_server_github_token(self):
        with patch.object(proofread_api, "GITHUB_TOKEN", ""):
            response = self.client.post("/api/proofread", json={"doc_id": "doc-1", "patch": {"version": 2}})
        self.assertEqual(response.status_code, 503)

    def test_parse_queue_uses_placeholder_store(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 25, "publication_id": "a" * 36,
            "publication_name": "小报", "title": "【文章待拆分】小报",
            "article": {"page_start": 1, "page_end": 2}, "source_files": ["one", "two"],
        }
        with patch.object(parse_api, "serving_lock", return_value=nullcontext()), \
                patch.object(parse_api, "parse_queue_docs", return_value=(1, [stored])):
            response = self.client.get("/api/parse/queue?page=1&page_size=20")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["items"][0]["page_end"], 2)

    def test_parse_queue_storage_failure_returns_stable_empty_shape(self):
        with patch.object(parse_api, "serving_lock", return_value=nullcontext()), \
                patch.object(parse_api, "parse_queue_docs", return_value=(0, [])):
            response = self.client.get("/api/parse/queue")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])

    def test_parse_submission_dispatches_structured_articles(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 25, "article_id": "placeholder",
            "publication_id": "052417de-42fb-4781-9885-af4fb006e9b6",
            "publication_name": "革命楼通讯", "title": "【文章待拆分】革命楼通讯",
            "source_files": ["one", "two"],
            "article": {
                "title": "【文章待拆分】革命楼通讯", "page_start": 1, "page_end": 2,
                "dates": [{"year": 1967, "month": 8, "day": 29}],
            },
        }
        dispatch = AsyncMock()
        articles = [
            {"title": "第一篇", "authors": ["作者甲"], "dates": [{"year": 1967, "month": 8, "day": 29}], "page_start": 1, "page_end": 1, "content": "第一篇正文", "base_part_count": 12, "ocr": {"content_thresholds": [0.1, 0.05, 0.08, 0.07]}, "ocr_exceptions": {"1": {"content_thresholds": [0.12, 0.05, 0.08, 0.07]}}},
            {"title": "第二篇", "authors": [], "dates": [{"year": 1967, "month": 8, "day": 29}], "page_start": 2, "page_end": 2, "content": "第二篇正文", "base_part_count": 15},
        ]
        with patch.object(parse_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(parse_api, "dispatch_parse", dispatch):
            response = self.client.post("/api/parse", json={
                "doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored),
                "articles": articles, "description": "拆分",
            })
        self.assertEqual(response.status_code, 202)
        dispatch.assert_awaited_once()
        payload = dispatch.await_args.args[0]
        self.assertEqual(payload["kind"], "parse")
        self.assertEqual(payload["archive_id"], 25)
        self.assertEqual(payload["base_content_hash"], document_identity.content_hash(stored))
        self.assertEqual(len(payload["body"]["articles"]), 2)
        self.assertEqual(payload["body"]["locator"]["page_end"], 2)
        self.assertEqual(payload["body"]["source_files"], ["one", "two"])
        self.assertEqual(payload["body"]["articles"][0]["ocr"]["content_thresholds"], [0.1, 0.05, 0.08, 0.07])
        self.assertIn("1", payload["body"]["articles"][0]["ocr_exceptions"])

    def test_parse_rejects_worker_incompatible_source_and_blank_title_before_dispatch(self):
        stored = {"doc_id": "doc-parse", "archive_id": 25, "publication_id": "publication",
                  "title": "【文章待拆分】fixture", "source_files": ["synthetic"],
                  "article": {"page_start": 1, "page_end": 1}}
        body = {"doc_id": "doc-parse", "articles": [
            {"title": "title", "page_start": 1, "page_end": 1, "content": "text", "base_part_count": 1}]}
        dispatch = AsyncMock()
        for changes in ({"source_files": []}, {"source_files": [""]}, {"publication_id": "invalid/name"}):
            source = {**stored, **changes}
            with self.subTest(changes=changes), patch.object(parse_api, "GITHUB_TOKEN", "synthetic"), \
                    patch.object(source_files, "get_source_doc", return_value=source), \
                    patch.object(parse_api, "dispatch_parse", dispatch):
                response = self.client.post("/api/parse", json={**body, "base_content_hash": document_identity.content_hash(source)})
                self.assertEqual(response.status_code, 409)
        with patch.object(parse_api, "GITHUB_TOKEN", "synthetic"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(parse_api, "dispatch_parse", dispatch):
            response = self.client.post("/api/parse", json={**body, "base_content_hash": document_identity.content_hash(stored),
                                        "articles": [{**body["articles"][0], "title": "   "}]})
        self.assertEqual(response.status_code, 400)
        dispatch.assert_not_awaited()

    def test_source_identity_mismatch_stops_both_routes_even_with_matching_hash(self):
        proof = {"doc_id": "other", "archive_id": 3, "article_id": "article", "publication_id": "pub",
                 "article": {"parts": [{"text": "old", "type": "paragraph"}], "comments": []}}
        split = {"doc_id": "other", "archive_id": 25, "publication_id": "pub", "title": "【文章待拆分】fixture",
                 "source_files": ["synthetic"], "article": {"page_start": 1, "page_end": 1}}
        cases = ((proof, proofread_api, "dispatch_proofread", "/api/proofread",
                  {"doc_id": "requested", "patch": {"version": 2, "parts": {"0": {"diff": "-3\t+new"}}, "comments": {}}}),
                 (split, parse_api, "dispatch_parse", "/api/parse",
                  {"doc_id": "requested", "articles": [{"title": "title", "page_start": 1, "page_end": 1,
                                                            "content": "text", "base_part_count": 1}]}))
        for source, module, sender, route, body in cases:
            dispatch = AsyncMock()
            with self.subTest(route=route), patch.object(module, "GITHUB_TOKEN", "synthetic"), \
                    patch.object(source_files, "get_source_doc", return_value=source), \
                    patch.object(module, sender, dispatch):
                response = self.client.post(route, json={**body, "base_content_hash": document_identity.content_hash(source)})
                self.assertEqual(response.status_code, 409)
                dispatch.assert_not_awaited()

    def test_parse_submission_rejects_missing_pages_and_duplicate_ids(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 25,
            "publication_id": "052417de-42fb-4781-9885-af4fb006e9b6",
            "title": "【文章待拆分】革命楼通讯",
            "article": {"title": "【文章待拆分】革命楼通讯", "page_start": 1, "page_end": 2},
        }
        base = {"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "articles": [{"title": "同题", "page_start": 1, "page_end": 1, "content": "正文", "base_part_count": 1}]}
        duplicate = {"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "articles": [
            {"title": "同题", "page_start": 1, "page_end": 1, "content": "正文一", "base_part_count": 1},
            {"title": "同题", "page_start": 2, "page_end": 2, "content": "正文二", "base_part_count": 1},
        ]}
        with patch.object(parse_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored):
            missing = self.client.post("/api/parse", json=base)
            collision = self.client.post("/api/parse", json=duplicate)
        self.assertEqual(missing.status_code, 400)
        self.assertIn("cover", missing.json()["error"])
        self.assertEqual(collision.status_code, 400)
        self.assertIn("duplicate", collision.json()["error"])

    def test_parse_submission_blocks_retry_after_uncertain_dispatch(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 25,
            "publication_id": "052417de-42fb-4781-9885-af4fb006e9b6",
            "title": "【文章待拆分】小报", "source_files": ["one"],
            "article": {"title": "【文章待拆分】小报", "page_start": 1, "page_end": 1},
        }
        body = {"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "articles": [
            {"title": "第一篇", "page_start": 1, "page_end": 1, "content": "正文", "base_part_count": 1},
        ]}
        dispatch = AsyncMock(side_effect=[RuntimeError("temporary"), None])
        with patch.object(parse_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(parse_api, "dispatch_parse", dispatch):
            failed = self.client.post("/api/parse", json=body)
            accepted = self.client.post("/api/parse", json=body)
            duplicate = self.client.post("/api/parse", json=body)
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(accepted.status_code, 409)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["status"], "uncertain")
        self.assertEqual(dispatch.await_count, 1)

    def test_parse_submission_is_rate_limited(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 25,
            "publication_id": "052417de-42fb-4781-9885-af4fb006e9b6",
            "title": "【文章待拆分】小报", "source_files": ["one"],
            "article": {"title": "【文章待拆分】小报", "page_start": 1, "page_end": 1},
        }
        base = {"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "articles": [
            {"title": "第一篇", "page_start": 1, "page_end": 1, "content": "正文", "base_part_count": 1},
        ]}
        with patch.object(parse_api, "GITHUB_TOKEN", "secret"), \
                patch.object(parse_api, "PARSE_RATE_LIMIT", 1), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(parse_api, "dispatch_parse", AsyncMock()):
            accepted = self.client.post("/api/parse", json={**base, "description": "one"})
            limited = self.client.post("/api/parse", json={**base, "description": "two"})
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(limited.status_code, 429)

    def test_proofread_rate_limit_uses_forwarded_client_on_space(self):
        request = MagicMock()
        request.headers = {"x-forwarded-for": "198.51.100.8, 203.0.113.7"}
        request.client.host = "10.0.0.2"
        with patch.object(proofread_api, "TRUST_PROXY", True), patch.object(proofread_api, "PROXY_NETWORKS", (proofread_api.ipaddress.ip_network("10.0.0.0/8"),)):
            self.assertIsNone(proofread_api.proofread_rate_error(request))
        self.assertIn("198.51.100.8", proofread_api.proofread_requests)

    def test_proofread_dispatches_body_and_metadata_tasks(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "旧标题",
            "article": {"title": "旧标题", "page_start": 1, "page_end": 2, "parts": [{"text": "旧", "type": "paragraph"}], "comments": []},
        }
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={
                "doc_id": "doc-1",
                "base_content_hash": document_identity.content_hash(stored),
                "patch": {"version": 2, "parts": {"0": {"diff": "-1\t+新"}}, "comments": {}, "description": ""},
                "metadata": {"article": {"title": "新标题", "authors": ["作者"], "dates": [{"year": 1967}], "tags": [{"name": "标签", "type": "主题/事件"}]}},
            })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["tasks"], 1)
        dispatch.assert_awaited_once()
        payload = dispatch.await_args.args[0]
        self.assertEqual(payload["kind"], "proofread")
        self.assertEqual(payload["archive_id"], 3)
        self.assertLessEqual(len(payload), 10)
        self.assertNotIn("patch", payload)
        self.assertNotIn("metadata", payload)
        self.assertNotIn("locator", payload)
        self.assertIn("patch", payload["body"])
        self.assertIn("metadata", payload["body"])
        self.assertIn("locator", payload["body"])
        self.assertFalse(payload.get("auto_merge"))
        changed = payload["changed"]
        self.assertEqual(changed[0], {"kind": "part", "index": 1, "original": "旧", "edited": "新"})
        self.assertEqual(changed[1], {"kind": "metadata", "field": "title", "old": "旧标题", "new": "新标题"})
        self.assertEqual(changed[2]["field"], "authors")
        self.assertEqual(changed[3]["field"], "dates")
        self.assertEqual(changed[4]["field"], "tags")

    def test_proofread_locator_keeps_database_article_identity_fields(self):
        article = {
            "title": "报纸文章", "authors": ["旧作者"],
            "dates": [{"year": 1969, "month": 6, "day": 5}],
            "page_start": 2, "page_end": 2, "parts": [{"text": "正文", "type": "paragraph"}],
            "comments": [],
        }
        stored = {
            "doc_id": "doc-db", "archive_id": 24, "article_id": "article-db",
            "publication_id": "jfjb", "title": article["title"], "article": article,
        }
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={
                "doc_id": "doc-db", "base_content_hash": document_identity.content_hash(stored),
                "metadata": {"article": {"authors": ["新作者"]}},
            })
        self.assertEqual(response.status_code, 202)
        locator = dispatch.await_args.args[0]["body"]["locator"]
        self.assertEqual(locator["authors"], ["旧作者"])
        self.assertEqual(locator["dates"], [{"year": 1969, "month": 6, "day": 5}])
        self.assertFalse(locator["is_range_date"])

    def test_proofread_change_preview_covers_comments_description_and_inserts(self):
        patch = {
            "version": 2,
            "parts": {"0": {"diff": "=1\t-1\t+新", "insertAfter": [{"text": "插入段", "type": "paragraph"}]}, "2": {"delete": True}},
            "comments": {"1": {"diff": "=1\t+注\t=1"}},
            "description": "+描述",
        }
        article = {
            "parts": [{"text": "旧文", "type": "paragraph"}, {"text": "二", "type": "paragraph"}, {"text": "三", "type": "paragraph"}],
            "comments": ["注释", "已删注释"],
            "description": "",
        }
        source = {"publication_name": "来源甲", "publication_author": "著者", "publication_type": "书", "source_files": [{"url": "https://x/a"}]}
        changes = proofread_core.proofread_change_preview(patch, article, {"source": {"name": "来源乙"}}, source)
        self.assertEqual(changes[0]["kind"], "part")
        self.assertEqual(changes[0]["index"], 1)
        self.assertEqual(changes[0]["original"], "旧文")
        self.assertEqual(changes[0]["edited"], "旧新")
        self.assertEqual(changes[1], {"kind": "part", "index": 1, "insert": True, "text": "插入段", "part_type": "paragraph"})
        self.assertEqual(changes[2], {"kind": "part", "index": 3, "delete": True, "original": "三"})
        self.assertEqual(changes[3], {"kind": "comment", "index": 1, "original": "注释", "edited": "注注释"})
        self.assertEqual(changes[4], {"kind": "description", "original": "", "edited": "描述"})
        self.assertEqual(changes[5], {"kind": "metadata", "field": "name", "old": "来源甲", "new": "来源乙"})

    def test_multi_operation_delta_replays_moved_text_and_utf16(self):
        self.assertEqual(proofread_core.apply_text_delta("甲乙丙丁", "-2\t=2\t+甲乙"), "丙丁甲乙")
        self.assertEqual(proofread_core.apply_text_delta("甲😀乙", "=1\t-2\t+校\t=1"), "甲校乙")

    def test_change_preview_includes_type_and_new_comment_but_skips_unchanged_metadata(self):
        article = {
            "title": "标题",
            "parts": [{"text": "正文", "type": "paragraph"}],
            "comments": [],
        }
        changes = proofread_core.proofread_change_preview(
            {"version": 2, "parts": {"0": {"type": "quotation"}}, "comments": {}, "description": "", "newComments": ["新增注释"]},
            article,
            {"article": {"title": "标题"}},
            {},
        )
        self.assertEqual(changes, [
            {"kind": "part_type", "index": 1, "old": "paragraph", "new": "quotation"},
            {"kind": "new_comment", "index": 1, "text": "新增注释"},
        ])

    def test_change_preview_keeps_full_text_within_budget(self):
        long_text = "长文" * 1000
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "标题",
            "article": {"title": "标题", "page_start": 1, "page_end": 1, "parts": [{"text": long_text, "type": "paragraph"}], "comments": []},
        }
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={
                "doc_id": "doc-1",
                "base_content_hash": document_identity.content_hash(stored),
                "patch": {"version": 2, "parts": {"0": {"diff": "=1999\t-1\t+新"}}, "comments": {}, "description": ""},
            })
        self.assertEqual(response.status_code, 202)
        changed = dispatch.await_args.args[0]["changed"]
        self.assertEqual(len(changed[0]["original"]), 2000)
        self.assertNotIn("…", changed[0]["original"])
        self.assertEqual(changed[0]["edited"], long_text[:1999] + "新")

    def test_oversized_change_preview_is_clipped_before_dispatch(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "标题",
            "article": {"title": "标题", "page_start": 1, "page_end": 1, "parts": [{"text": "长文" * 15000, "type": "paragraph"}], "comments": []},
        }
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={
                "doc_id": "doc-1",
                "base_content_hash": document_identity.content_hash(stored),
                "patch": {"version": 2, "parts": {"0": {"diff": "=29999\t-1\t+新"}}, "comments": {}, "description": ""},
            })
        self.assertEqual(response.status_code, 202)
        payload = dispatch.await_args.args[0]
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False)), proofread_api.CHANGE_PREVIEW_BUDGET_CHARACTERS)
        original = payload["changed"][0]["original"]
        self.assertEqual(len(original), 2001)
        self.assertTrue(original.endswith("…"))

    def test_unicode_payload_over_old_byte_limit_is_accepted(self):
        inserted = "校" * 18000
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "标题",
            "article": {"title": "标题", "parts": [{"text": "", "type": "paragraph"}], "comments": []},
        }
        dispatch = AsyncMock()
        request = {
            "doc_id": "doc-1",
            "base_content_hash": document_identity.content_hash(stored),
            "patch": {"version": 2, "parts": {"0": {"diff": f"+{inserted}"}}, "comments": {}, "description": ""},
        }
        self.assertGreater(len(json.dumps(request, ensure_ascii=False).encode("utf-8")), 50 * 1024)
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json=request)
        self.assertEqual(response.status_code, 202)
        dispatch.assert_awaited_once()

    def test_dispatch_segments_oversized_payload_into_git_blobs(self):
        calls = []

        class Response:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload or {}

            def json(self):
                return self._payload

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                calls.append((url, kwargs["json"]))
                if url.endswith("/git/blobs"):
                    return Response(201, {"sha": f"{len(calls):040x}"})
                return Response(204)

        payload = {"request_id": "request-1", "body": {"patch": "中文" * 100}}
        with patch.object(proofread_api, "DISPATCH_MAX_CHARACTERS", 80), \
                patch.object(proofread_api, "BLOB_CHUNK_CHARACTERS", 40), \
                patch.object(pipeline_dispatch.httpx, "AsyncClient", return_value=Client()):
            asyncio.run(proofread_api.dispatch_proofread(payload))
        blob_calls = [item for item in calls if item[0].endswith("/git/blobs")]
        self.assertGreater(len(blob_calls), 1)
        serialized = "".join(item[1]["content"] for item in blob_calls)
        self.assertEqual(json.loads(serialized), payload)
        dispatch = calls[-1][1]["client_payload"]
        self.assertEqual(dispatch["payload_characters"], len(serialized))
        self.assertEqual(dispatch["payload_sha256"], proofread_api.hashlib.sha256(serialized.encode()).hexdigest())
        self.assertEqual(dispatch["request_id"], "request-1")

        calls.clear()
        property_payload = {f"field_{index}": index for index in range(11)}
        property_payload["request_id"] = "request-2"
        with patch.object(proofread_api, "DISPATCH_MAX_CHARACTERS", 1_000_000), \
                patch.object(pipeline_dispatch.httpx, "AsyncClient", return_value=Client()):
            asyncio.run(proofread_api.dispatch_proofread(property_payload))
        self.assertTrue(any(item[0].endswith("/git/blobs") for item in calls))
        self.assertEqual(calls[-1][1]["client_payload"]["request_id"], "request-2")

    def test_small_body_correction_is_not_marked_for_auto_merge(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "标题",
            "article": {
                "title": "标题", "page_start": 1, "page_end": 1,
                "parts": [{"text": "旧文", "type": "paragraph"}],
                "comments": [], "comment_pivots": [], "description": "",
            },
        }
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={
                "doc_id": "doc-1",
                "base_content_hash": document_identity.content_hash(stored),
                "patch": {"version": 2, "parts": {"0": {"diff": "=1\t-1\t+新"}}, "comments": {}, "description": ""},
            })
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["auto_merge"])
        self.assertNotIn("auto_merge", dispatch.await_args.args[0])
        self.assertNotIn("auto_merge_policy", dispatch.await_args.args[0])

    def test_client_auto_merge_fields_cannot_override_manual_review(self):
        stored = {
            "doc_id": "manual-only", "archive_id": 3, "article_id": "article",
            "publication_id": "publication", "title": "fixture",
            "article": {"parts": [{"text": "old", "type": "paragraph"}], "comments": []},
        }
        body = {"doc_id": stored["doc_id"], "base_content_hash": document_identity.content_hash(stored),
                "patch": {"version": 2, "parts": {"0": {"diff": "-3\t+new"}}, "comments": {}},
                "auto_merge": True, "auto_merge_policy": proofread_api.AUTO_MERGE_POLICY}
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "synthetic"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            first = self.client.post("/api/proofread", json=body)
            duplicate = self.client.post("/api/proofread", json=body)
        self.assertEqual((first.status_code, duplicate.status_code), (202, 200))
        self.assertFalse(first.json()["auto_merge"])
        self.assertFalse(duplicate.json()["auto_merge"])
        dispatch.assert_awaited_once()
        self.assertNotIn("auto_merge", dispatch.await_args.args[0])
        self.assertNotIn("auto_merge_policy", dispatch.await_args.args[0])

    def test_proofread_invalid_worker_identity_fails_before_dispatch(self):
        stored = {"doc_id": "doc-proof", "archive_id": 3, "article_id": "article", "publication_id": "publication",
                  "article": {"parts": [{"text": "old", "type": "paragraph"}], "comments": []}}
        body = {"doc_id": "doc-proof", "patch": {"version": 2, "parts": {"0": {"diff": "-3\t+new"}}, "comments": {}}}
        dispatch = AsyncMock()
        for changes in ({"archive_id": True}, {"archive_id": 32}, {"article_id": "invalid/name"}, {"publication_id": None}):
            source = {**stored, **changes}
            with self.subTest(changes=changes), patch.object(proofread_api, "GITHUB_TOKEN", "synthetic"), \
                    patch.object(source_files, "get_source_doc", return_value=source), \
                    patch.object(proofread_api, "dispatch_proofread", dispatch):
                response = self.client.post("/api/proofread", json={**body, "base_content_hash": document_identity.content_hash(source)})
                self.assertEqual(response.status_code, 409)
        dispatch.assert_not_awaited()

    def test_multi_part_correction_with_type_and_insert_is_not_marked_for_auto_merge(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "标题",
            "article": {
                "title": "标题", "page_start": 1, "page_end": 1,
                "parts": [{"text": "第一段", "type": "paragraph"}, {"text": "第二段", "type": "paragraph"}],
                "comments": [], "comment_pivots": [], "description": "",
            },
        }
        dispatch = AsyncMock()
        patch_doc = {
            "version": 2,
            "parts": {
                "0": {"diff": "=2\t-1\t+改", "insertAfter": [{"text": "插入段", "type": "paragraph"}]},
                "1": {"type": "quotation"},
            },
            "comments": {}, "description": "",
        }
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "patch": patch_doc})
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["auto_merge"])
        self.assertNotIn("auto_merge", dispatch.await_args.args[0])
        self.assertNotIn("auto_merge_policy", dispatch.await_args.args[0])

    def test_all_auto_merge_thresholds_remain_manual_review(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "标题",
            "article": {
                "title": "标题", "page_start": 1, "page_end": 1,
                "parts": [{"text": "第一段", "type": "paragraph"}],
                "comments": [], "comment_pivots": [], "description": "",
            },
        }
        dispatch = AsyncMock()
        cases = [
            ({"0": {"diff": "=3\t+" + "改" * 500}}, True),
            ({"0": {"diff": "=3\t+" + "改" * 501}}, False),
            ({"0": {"insertAfter": [{"text": "改" * 500, "type": "paragraph"}]}}, True),
            ({"0": {"insertAfter": [{"text": "改" * 501, "type": "paragraph"}]}}, False),
            ({"0": {"diff": "=3\t+改", "insertAfter": [{"text": "a", "type": "paragraph"} for _ in range(4)]}}, False),
        ]
        for patch_doc, formerly_auto_merge in cases:
            with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                    patch.object(source_files, "get_source_doc", return_value=stored), \
                    patch.object(proofread_api, "dispatch_proofread", dispatch):
                response = self.client.post("/api/proofread", json={"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "patch": {"version": 2, "parts": patch_doc, "comments": {}, "description": ""}})
            self.assertEqual(response.status_code, 202)
            self.assertFalse(response.json()["auto_merge"], msg=f"former policy: {formerly_auto_merge}")
            self.assertNotIn("auto_merge", dispatch.await_args.args[0])
            self.assertNotIn("auto_merge_policy", dispatch.await_args.args[0])

    def test_malformed_patch_is_rejected_before_dispatch(self):
        stored = {"doc_id": "doc-1", "archive_id": 3, "article_id": "a", "publication_id": "p", "article": {"parts": [], "comments": []}}
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), patch.object(source_files, "get_source_doc", return_value=stored), patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "patch": {"version": 2}})
        self.assertEqual(response.status_code, 400)
        dispatch.assert_not_awaited()

    def test_semantically_empty_patch_is_rejected_before_dispatch(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "a", "publication_id": "p",
            "article": {"parts": [{"text": "正文", "type": "paragraph"}], "comments": ["注释"]},
        }
        patches = [
            {"version": 2, "parts": {"0": {"delete": False}}, "comments": {}, "description": ""},
            {"version": 2, "parts": {"0": {"diff": "=2"}}, "comments": {}, "description": ""},
            {"version": 2, "parts": {"0": {"type": "paragraph"}}, "comments": {}, "description": ""},
            {"version": 2, "parts": {"0": {"insertAfter": []}}, "comments": {}, "description": ""},
            {"version": 2, "parts": {}, "comments": {"1": {"diff": "=2"}}, "description": ""},
            {"version": 2, "parts": {}, "comments": {}, "description": "=0"},
        ]
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            responses = [self.client.post("/api/proofread", json={"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "patch": value}) for value in patches]
        self.assertTrue(all(response.status_code == 400 for response in responses))
        dispatch.assert_not_awaited()

    def test_unchanged_metadata_is_rejected_before_dispatch(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "a", "publication_id": "p",
            "title": "标题", "article": {"title": "标题", "parts": [], "comments": []},
        }
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={
                "doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "metadata": {"article": {"title": "标题"}},
            })
        self.assertEqual(response.status_code, 400)
        dispatch.assert_not_awaited()

    def test_metadata_requires_exact_string_types(self):
        stored = {"doc_id": "doc-1", "archive_id": 3, "article_id": "a", "publication_id": "p", "article": {}}
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), patch.object(source_files, "get_source_doc", return_value=stored), patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "metadata": {"article": {"title": []}}})
        self.assertEqual(response.status_code, 400)
        dispatch.assert_not_awaited()

    def test_proofread_patch_boundaries_and_merge_limits(self):
        article = {"parts": [{"text": "甲😀乙", "type": "paragraph"}], "comments": ["注释"]}
        patch_doc = {
            "version": 2,
            "parts": {"0": {"diff": "=1\t-2\t+校\t=1", "insertAfter": [{"text": "插入😀", "type": "paragraph"}]}},
            "comments": {"1": {"diff": "=1\t+新\t=1"}},
        }
        result = proofread_core.validate_proofread_patch(patch_doc, article, proofread_api.PART_TYPES)
        self.assertEqual(result["parts"]["0"]["diff"], "=1\t-2\t+校\t=1")
        self.assertEqual(proofread_core.proofread_change_preview(result, article, None, {})[0]["edited"], "甲校乙")
        # 2 deleted UTF-16 units + 1 inserted + 4 inserted paragraph units + 1 comment unit.
        self.assertTrue(proofread_core.patch_is_safe_to_merge(result, article, 8, 1))
        self.assertFalse(proofread_core.patch_is_safe_to_merge(result, article, 7, 1))
        self.assertFalse(proofread_core.patch_is_safe_to_merge(result, article, 8, 0))
        self.assertFalse(proofread_core.patch_is_safe_to_merge({"version": 2, "parts": {"0": {"delete": False}}, "comments": {}}, article, 8, 1))
        invalid = [
            {"version": 2, "parts": {"00": {"diff": "=4"}}, "comments": {}},
            {"version": 2, "parts": {"0": {"delete": True, "diff": "=4"}}, "comments": {}},
            {"version": 2, "parts": {"0": {"insertAfter": [{"text": "x", "type": []}]}}, "comments": {}},
            {"version": 2, "parts": {"0": {"diff": "=3"}}, "comments": {}},
            {"version": 2, "parts": {}, "comments": {"1": {"insertAfter": [{"text": 1}]}}},
            {"version": 2, "parts": {}, "comments": {}, "newComments": ["ok", None]},
        ]
        for value in invalid:
            with self.subTest(patch=value), self.assertRaises((TypeError, ValueError)):
                proofread_core.validate_proofread_patch(value, article, proofread_api.PART_TYPES)

    def test_proofread_metadata_boundaries_never_dispatch_invalid_values(self):
        stored = {"doc_id": "doc-1", "archive_id": 3, "article_id": "a", "publication_id": "p", "article": {"parts": [], "comments": []}}
        invalid = [
            {"article": {"dates": [{"year": True}]}},
            {"article": {"dates": [{"year": 1967, "extra": 1}]}},
            {"article": {"tags": [{"name": "x", "type": "未定义"}]}},
            {"article": {"authors": ["合格", 7]}},
            {"source": {"files": ["http://127.0.0.1/private"]}},
            {"article": {"title": " "}},
        ]
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            for metadata in invalid:
                with self.subTest(metadata=metadata):
                    response = self.client.post("/api/proofread", json={"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(stored), "metadata": metadata})
                    self.assertEqual(response.status_code, 400, response.text)
        dispatch.assert_not_awaited()

    def test_proofread_proxy_configuration_fails_closed(self):
        with patch.dict("os.environ", {"BHA_TRUST_PROXY_CIDRS": "invalid", "BHA_TRUST_PROXY_HOPS": "bad"}):
            self.assertEqual(proofread_api.proxy_networks(), ())
            self.assertEqual(proofread_api.proxy_hops(), 1)
        with patch.dict("os.environ", {"BHA_TRUST_PROXY_CIDRS": "10.0.0.0/8,2001:db8::/32", "BHA_TRUST_PROXY_HOPS": "2"}):
            self.assertEqual(len(proofread_api.proxy_networks()), 2)
            self.assertEqual(proofread_api.proxy_hops(), 2)

    def test_tag_correction_requires_manual_review(self):
        stored = {
            "doc_id": "doc-1", "archive_id": 3, "article_id": "article-1",
            "publication_id": "publication-1", "title": "标题",
            "article": {"title": "标题", "page_start": 1, "page_end": 1, "tags": []},
        }
        dispatch = AsyncMock()
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=stored), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            response = self.client.post("/api/proofread", json={
                "doc_id": "doc-1",
                "base_content_hash": document_identity.content_hash(stored),
                "metadata": {"article": {"tags": [{"name": "标签", "type": "主题/事件"}]}},
            })
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["auto_merge"])
        self.assertNotIn("auto_merge", dispatch.await_args.args[0])

    def test_query_length_is_limited(self):
        response = self.client.post("/api/search", json={"q": "甲" * 257})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("server-timing", response.headers)

    def test_search_rejects_invalid_sort_and_filter_lists_over_500(self):
        invalid_sort = self.client.post("/api/search", json={"q": "手机", "sort": "dat_desc"})
        too_many_filters = self.client.post("/api/search", json={"q": "手机", "sources": [str(index) for index in range(501)]})
        self.assertEqual(invalid_sort.status_code, 422)
        self.assertEqual(too_many_filters.status_code, 422)

    def test_search_request_mutable_defaults_are_isolated(self):
        first = search_logic.SearchRequest()
        second = search_logic.SearchRequest()
        first.sources.append("刊物")
        first.date_terms.append({"year": 1966})
        self.assertEqual(second.sources, [])
        self.assertEqual(second.date_terms, [])

    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    @patch.object(runtime, "has_index_data", return_value=False)
    def test_search_without_index_returns_stable_shape(self, _has_index, _status):
        response = self.client.post("/api/search", json={"q": "手机", "page": -2, "page_size": 500})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])
        self.assertEqual(response.json()["page"], 1)

    @patch.object(search_api, "get_search_docs")
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_search_calls_es_and_trims_results(self, _status, _has_index, get_search_docs):
        stored = {
            "doc-1": {
                "doc_id": "doc-1",
                "title": "手机资料",
                "publication_name": "测试刊物",
                "content": "这是手机资料",
            }
        }
        get_search_docs.return_value = stored
        fake_es = MagicMock()
        fake_es.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [{"_id": "doc-1", "_score": 2.0, "highlight": {"content": ["这是<mark>手机</mark>资料"]}}],
            }
        }
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": "手机", "exact": False, "page_size": 500})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["page_size"], 100)
        self.assertEqual(payload["results"][0]["doc_id"], "doc-1")
        self.assertEqual(payload["results"][0]["title_highlight"], "<mark>手机</mark>资料")
        for field in ("article_id", "publication_id", "archive_id", "path", "score"):
            self.assertNotIn(field, payload["results"][0])
        kwargs = fake_es.search.call_args.kwargs
        self.assertEqual(kwargs["size"], 100)
        self.assertIs(kwargs["source"], False)
        self.assertNotIn("_source", kwargs)
        self.assertNotIn("highlight", kwargs)

    @patch.object(search_api, "get_search_docs", return_value={})
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_empty_relevance_search_uses_stable_doc_id_sort(self, _status, _has_index, _get_docs):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            fake_es.search.call_args.kwargs["sort"],
            [{"doc_id": {"order": "asc"}}],
        )

    @patch.object(search_api, "get_search_docs", return_value={
        "doc-1": {"doc_id": "doc-1", "title": "无关标题", "content": "正文手机内容"},
    })
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_search_omits_redundant_title_highlight(self, _status, _has_index, _get_docs):
        fake_es = MagicMock()
        fake_es.search.return_value = {
            "hits": {"total": {"value": 1}, "hits": [{"_id": "doc-1", "_score": 1.0}]},
        }
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": "手机"})
        item = response.json()["results"][0]
        self.assertEqual(item["title"], "无关标题")
        self.assertNotIn("title_highlight", item)

    @patch.object(search_api, "get_search_docs", return_value={})
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_identical_search_requests_use_bounded_response_cache(self, _status, _has_index, _get_docs):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        with patch.object(runtime, "es", fake_es):
            first = self.client.post("/api/search", json={"q": "手机", "page": 1, "page_size": 20})
            second = self.client.post("/api/search", json={"q": "手机", "page": 1, "page_size": 20})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(fake_es.search.call_count, 1)

    @patch.object(search_api, "get_search_docs", return_value={})
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_search_cache_key_separates_page_and_filter_options(self, _status, _has_index, _get_docs):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        with patch.object(runtime, "es", fake_es):
            self.client.post("/api/search", json={"q": "手机", "page": 1, "page_size": 20})
            self.client.post("/api/search", json={"q": "手机", "page": 2, "page_size": 20})
            self.client.post("/api/search", json={"q": "手机", "page": 1, "page_size": 20, "archive_id": 1})
        self.assertEqual(fake_es.search.call_count, 3)

    def test_search_cache_key_covers_every_result_option(self):
        base = search_logic.SearchRequest(q="手机")
        variants = [
            search_logic.SearchRequest(q="手机", page=2), search_logic.SearchRequest(q="手机", page_size=50),
            search_logic.SearchRequest(q="手机", exact=False), search_logic.SearchRequest(q="手机", fulltext=False),
            search_logic.SearchRequest(q="手机", sort="title"), search_logic.SearchRequest(q="手机", sources=["刊物甲"]),
            search_logic.SearchRequest(q="手机", exclude_sources=["刊物乙"]), search_logic.SearchRequest(q="手机", authors=["作者甲"]),
            search_logic.SearchRequest(q="手机", exclude_authors=["作者乙"]), search_logic.SearchRequest(q="手机", tags=["标签甲"]),
            search_logic.SearchRequest(q="手机", exclude_tags=["标签乙"]),
            search_logic.SearchRequest(q="手机", archive_id=3), search_logic.SearchRequest(q="手机", publication_type="期刊"),
            search_logic.SearchRequest(q="手机", date_from=19660101), search_logic.SearchRequest(q="手机", date_to=19661231),
            search_logic.SearchRequest(q="手机", date_terms=[{"year": 1966, "month": 5}]),
        ]
        keys = {runtime.search_cache_key(item, max(1, item.page), min(max(1, item.page_size), 100)) for item in [base, *variants]}
        self.assertEqual(len(keys), 1 + len(variants))
        base_key = runtime.search_cache_key(base, 1, 20)
        runtime.store_search_response(base_key, {"total": 20000, "index_status": "ready"})
        for number, variant in enumerate(variants):
            key = runtime.search_cache_key(variant, max(1, variant.page), min(max(1, variant.page_size), 100))
            self.assertEqual(runtime.cached_search_total(key), 20000 if number < 2 else None)

    @patch.object(search_api, "get_search_docs", return_value={})
    @patch.object(runtime, "has_index_data", return_value=True)
    def test_ordinary_pages_reuse_exact_total_from_complete_response(self, _has_index, _get_docs):
        fake_es = MagicMock()
        fake_es.search.side_effect = [
            {"hits": {"total": {"value": 20000, "relation": "eq"}, "hits": []}},
            {"hits": {"hits": []}},
            {"hits": {"total": {"value": 100, "relation": "eq"}, "hits": []}},
        ]
        with patch.object(runtime, "es", fake_es):
            first = self.client.post("/api/search", json={"q": "革命", "exact": False, "page": 1})
            second = self.client.post("/api/search", json={"q": "革命", "exact": False, "page": 2, "page_size": 50})
            filtered = self.client.post("/api/search", json={"q": "革命", "exact": False, "page": 2, "archive_id": 1})
        self.assertEqual(first.json()["total"], 20000)
        self.assertEqual(second.json()["total"], 20000)
        self.assertEqual(filtered.json()["total"], 100)
        self.assertEqual([call.kwargs["track_total_hits"] for call in fake_es.search.call_args_list], [True, False, True])

    def test_exact_total_reuse_obeys_expiry_generation_and_ready_status(self):
        body = search_logic.SearchRequest(q="革命", exact=False)
        key = runtime.search_cache_key(body, 1, 100)
        other_page = runtime.search_cache_key(body, 2, 50)
        with patch.object(runtime.time, "monotonic", return_value=100):
            runtime.store_search_response(key, {"total": 20000, "index_status": "ready"})
        with patch.object(runtime.time, "monotonic", return_value=129):
            self.assertEqual(runtime.cached_search_total(other_page), 20000)
            self.assertIsNone(runtime.cached_search_total(("different-generation", *other_page[1:])))
        with patch.object(runtime.time, "monotonic", return_value=130):
            self.assertIsNone(runtime.cached_search_total(other_page))
            for invalid in ({"total": 20000, "index_status": "indexing"},
                            {"total": -1, "index_status": "ready"}, {"total": True, "index_status": "ready"}):
                runtime.store_search_response(key, invalid)
                self.assertIsNone(runtime.cached_search_total(other_page))
        runtime.clear_response_caches()
        self.assertIsNone(runtime.cached_search_total(other_page))

    @patch.object(search_api, "get_search_docs", return_value={})
    @patch.object(runtime, "has_index_data", return_value=True)
    def test_generation_change_before_lock_forces_recount_without_old_cache_fill(self, _has_index, _get_docs):
        body = search_logic.SearchRequest(q="革命", exact=False)
        old_generation, new_generation = ((1, 2, 3, 4),), ((1, 9, 3, 10),)
        with patch.object(runtime, "serving_generation_token", return_value=old_generation):
            key = runtime.search_cache_key(body, 1, 100)
        runtime.store_search_response(key, {"total": 20000, "index_status": "ready"})
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 30000, "relation": "eq"}, "hits": []}}
        with patch.object(runtime, "es", fake_es), \
             patch.object(runtime, "serving_generation_token", side_effect=[old_generation, new_generation]):
            response = self.client.post("/api/search", json={"q": "革命", "exact": False, "page": 2})
        self.assertEqual(response.json()["total"], 30000)
        self.assertTrue(fake_es.search.call_args.kwargs["track_total_hits"])
        self.assertEqual(list(runtime.search_response_cache), [key])

    @patch.object(search_api, "get_search_docs", return_value={})
    @patch.object(runtime, "has_index_data", return_value=True)
    def test_reused_total_does_not_allow_partial_page_results(self, _has_index, _get_docs):
        body = search_logic.SearchRequest(q="革命", exact=False)
        key = runtime.search_cache_key(body, 1, 100)
        runtime.store_search_response(key, {"total": 20000, "index_status": "ready"})
        fake_es = MagicMock()
        fake_es.search.side_effect = [
            {"timed_out": True, "hits": {"hits": []}},
            {"_shards": {"failed": 1}, "hits": {"hits": []}},
            {"terminated_early": True, "hits": {"hits": []}},
        ]
        with patch.object(runtime, "es", fake_es):
            for _ in range(3):
                response = self.client.post("/api/search", json={"q": "革命", "exact": False, "page": 2})
                self.assertEqual(response.status_code, 503)
        self.assertEqual(list(runtime.search_response_cache), [key])
        self.assertTrue(all(not call.kwargs["track_total_hits"] for call in fake_es.search.call_args_list))

    def test_search_cache_key_canonicalizes_equivalent_filters(self):
        first = search_logic.SearchRequest(q=" 手机 ", sources=["刊物甲"], authors=["乙", "甲", "乙"])
        second = search_logic.SearchRequest(q="手机", sources=["刊物甲"], authors=["甲", "乙"])
        with patch.object(runtime, "serving_generation_token", return_value=((1, 2, 3, 4), None)):
            self.assertEqual(runtime.search_cache_key(first, 1, 20), runtime.search_cache_key(second, 1, 20))

    def test_search_accepts_singular_filter_fields_for_compatibility(self):
        for field in ("source", "author", "tag"):
            with self.subTest(field=field):
                request = search_logic.SearchRequest(**{field: "旧调用者"})
                self.assertEqual(getattr(request, field), "旧调用者")

    def test_search_response_cache_ttl_covers_navigation_revisits(self):
        self.assertEqual(runtime.SEARCH_CACHE_TTL_SECONDS, 30)

    def test_punctuation_request_runs_as_normal_search_without_changing_the_contract(self):
        # The URL/UI still say exact=true; only the execution mode changes.
        self.assertTrue(search_logic.SearchRequest(q="手。机").exact)
        self.assertFalse(search_logic.effective_exact("手。机", True))
        self.assertFalse(search_logic.effective_exact("毛泽东，", True))
        self.assertTrue(search_logic.effective_exact("文化革命", True))
        self.assertTrue(search_logic.effective_exact("手*机", True))
        self.assertTrue(search_logic.effective_exact("手?机", True))
        self.assertFalse(search_logic.effective_exact("文化革命", False))
        self.assertFalse(search_logic.has_query_punctuation("文化 革命"))
        self.assertFalse(search_logic.has_query_punctuation("ABC123"))
        # `*`/`?` stay literal exact characters, not punctuation.
        self.assertFalse(search_logic.has_query_punctuation("手*机"))
        self.assertFalse(search_logic.has_query_punctuation("手?机"))

    @patch.object(search_api, "get_search_docs", return_value={
        "doc-1": {"doc_id": "doc-1", "title": "手。机"},
    })
    @patch.object(runtime, "has_index_data", return_value=True)
    def test_punctuation_exact_request_uses_the_normal_query_and_cache_key(self, _has_index, _docs):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 1}, "hits": [{"_id": "doc-1"}]}}
        with patch.object(runtime, "es", fake_es):
            response = self.client.post(
                "/api/search", json={"q": "手。机", "exact": True, "fulltext": True},
            )
        self.assertEqual(response.status_code, 200)
        query = fake_es.search.call_args.kwargs["query"]
        self.assertNotIn("dis_max", str(query))
        self.assertIn("multi_match", str(query))
        self.assertEqual(runtime.literal_search_cache, {})
        body = search_logic.SearchRequest(q="手。机", exact=True)
        effective = body.model_copy(update={"exact": False})
        self.assertEqual(list(runtime.search_response_cache), [runtime.search_cache_key(effective, 1, 20)])

    def test_plain_exact_request_keeps_the_literal_query(self):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": "文化革命", "exact": True})
        self.assertEqual(response.status_code, 200)
        self.assertIn("dis_max", str(fake_es.search.call_args.kwargs["query"]))

    def test_elasticsearch_client_is_configured_for_search_bursts(self):
        self.assertEqual(runtime.es._request_timeout, 120)

    def test_literal_cache_is_generation_scoped_and_bounded(self):
        body = search_logic.SearchRequest(q="手。机", exact=True)
        with patch.object(runtime, "serving_generation_token", return_value=((1, 2, 3, 4), None)):
            old_key = runtime.literal_cache_key(body)
        with patch.object(runtime, "serving_generation_token", return_value=((5, 6, 7, 8), None)):
            new_key = runtime.literal_cache_key(body)
        runtime.store_literal_ids(old_key, ["doc-1"])
        self.assertEqual(runtime.cached_literal_ids(old_key), ("doc-1",))
        self.assertIsNone(runtime.cached_literal_ids(new_key))
        with patch.object(runtime, "LITERAL_CACHE_MAX_TOTAL_IDS", 1):
            runtime.store_literal_ids(new_key, ["doc-2", "doc-3"])
        self.assertIsNone(runtime.cached_literal_ids(new_key))

    def test_concurrent_identical_searches_share_one_backend_request(self):
        started = threading.Event()
        release = threading.Event()
        fake_es = MagicMock()

        def execute(**_kwargs):
            started.set()
            release.wait(2)
            return {"hits": {"total": {"value": 0}, "hits": []}}

        fake_es.search.side_effect = execute
        with patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(runtime, "has_index_data", return_value=True), \
                patch.object(runtime, "serving_generation_token", return_value=((1, 2, 3, 4), None)), \
                patch.object(search_api, "serving_lock", return_value=nullcontext()), \
                patch.object(runtime, "es", fake_es):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(search_api.search, search_logic.SearchRequest(q="手机"))
                self.assertTrue(started.wait(1))
                second = executor.submit(search_api.search, search_logic.SearchRequest(q="手机"))
                time.sleep(0.05)
                self.assertEqual(fake_es.search.call_count, 1)
                release.set()
                self.assertEqual(json.loads(first.result().body)["total"], 0)
                self.assertEqual(json.loads(second.result().body)["total"], 0)
        self.assertEqual(fake_es.search.call_count, 1)
        self.assertEqual(runtime.search_inflight, {})

    def test_concurrent_literal_pages_share_one_complete_scan(self):
        started = threading.Event()
        joined = threading.Event()
        release = threading.Event()
        original_begin = runtime.begin_search
        fake_es = MagicMock()

        def begin(key):
            result = original_begin(key)
            if key[0] == "literal" and not result[0]:
                joined.set()
            return result

        def execute(**_kwargs):
            started.set()
            release.wait(5)
            return {"hits": {"total": {"value": 2, "relation": "eq"}, "hits": [
                {"_id": "doc-1", "sort": [2.0, "doc-1"]},
                {"_id": "doc-2", "sort": [1.0, "doc-2"]},
            ]}}

        fake_es.search.side_effect = execute
        docs = {doc_id: {"doc_id": doc_id, "title": "手。机"} for doc_id in ("doc-1", "doc-2")}
        with patch.object(runtime, "has_index_data", return_value=True), \
                patch.object(runtime, "begin_search", side_effect=begin), \
                patch.object(runtime, "es", fake_es), \
                patch.object(search_api, "literal_match_ids", return_value=set(docs)) as literal, \
                patch.object(search_api, "get_search_docs", side_effect=lambda ids, *_: {doc_id: docs[doc_id] for doc_id in ids}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(search_api.search, search_logic.SearchRequest(q="手。机", page=1, page_size=1), normalize_exact=False)
                try:
                    self.assertTrue(started.wait(5))
                    second = executor.submit(search_api.search, search_logic.SearchRequest(q="手。机", page=2, page_size=1), normalize_exact=False)
                    self.assertTrue(joined.wait(5))
                finally:
                    release.set()
                first_payload = json.loads(first.result(timeout=5).body)
                second_payload = json.loads(second.result(timeout=5).body)
        self.assertEqual(first_payload["total"], 2)
        self.assertEqual(second_payload["total"], 2)
        self.assertEqual(first_payload["results"][0]["doc_id"], "doc-1")
        self.assertEqual(second_payload["results"][0]["doc_id"], "doc-2")
        self.assertEqual(fake_es.search.call_count, 1)
        self.assertEqual(literal.call_count, 1)
        self.assertEqual(runtime.search_inflight, {})

    def test_failed_literal_leader_releases_waiter_without_caching_partial_ids(self):
        started = threading.Event()
        joined = threading.Event()
        release = threading.Event()
        original_begin = runtime.begin_search
        body = search_logic.SearchRequest(q="手。机")

        def begin(key):
            result = original_begin(key)
            if not result[0]:
                joined.set()
            return result

        def fail(*_args):
            if started.is_set():
                return []
            started.set()
            release.wait(5)
            raise search_api.IncompleteSearchError("interrupted scan")

        with patch.object(runtime, "begin_search", side_effect=begin), \
                patch.object(search_api, "verified_literal_ids", side_effect=fail) as scan:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(search_api.request_literal_ids, body, {}, True)
                try:
                    self.assertTrue(started.wait(5))
                    second = executor.submit(search_api.request_literal_ids, body, {}, True)
                    self.assertTrue(joined.wait(5))
                    self.assertEqual(runtime.literal_search_cache, {})
                finally:
                    release.set()
                with self.assertRaises(search_api.IncompleteSearchError):
                    first.result(timeout=5)
                self.assertEqual(second.result(timeout=5), ())
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(runtime.cached_literal_ids(runtime.literal_cache_key(body)), ())
        self.assertEqual(runtime.search_inflight, {})

    def test_literal_singleflight_does_not_retain_oversized_sets(self):
        body = search_logic.SearchRequest(q="手。机")
        with patch.object(runtime, "LITERAL_CACHE_MAX_TOTAL_IDS", 1), \
                patch.object(search_api, "verified_literal_ids", return_value=["doc-1", "doc-2"]) as scan:
            for _ in range(2):
                self.assertEqual(search_api.request_literal_ids(body, {}, True), ("doc-1", "doc-2"))
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(runtime.literal_search_cache, {})
        self.assertEqual(runtime.search_inflight, {})

    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_invalid_es_query_returns_400(self, _status, _has_index):
        fake_es = MagicMock()
        fake_es.search.side_effect = BadRequestError("bad query", meta=MagicMock(status=400), body={"error": "bad query"})
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": "手机"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid search query")

    @patch.object(search_api, "get_search_docs", return_value={})
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_fulltext_off_does_not_request_highlights(self, _status, _has_index, _get_docs):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": "手机", "fulltext": False})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("highlight", fake_es.search.call_args.kwargs)

    @patch.object(search_api, "get_search_docs", side_effect=search_api.SearchStoreError("sidecar failed"))
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_search_storage_failure_returns_503_and_is_not_cached(self, _status, _has_index, _get_docs):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 1}, "hits": [{"_id": "doc-1"}]}}
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": "手机"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "search storage unavailable")
        self.assertEqual(runtime.search_response_cache, {})

    @patch.object(search_api, "get_search_docs", return_value={})
    @patch.object(runtime, "has_index_data", return_value=True)
    def test_incomplete_es_results_are_rejected_without_poisoning_cache(self, _has_index, _get_docs):
        complete = {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
        partials = [
            {**complete, "timed_out": True},
            {**complete, "_shards": {"total": 2, "successful": 1, "failed": 1}},
            {**complete, "terminated_early": True},
            {"hits": {"total": {"value": 0, "relation": "gte"}, "hits": []}},
        ]
        for query in ("手机", "手。机"):
            for partial in partials:
                with self.subTest(query=query, partial=partial):
                    runtime.search_response_cache.clear()
                    runtime.literal_search_cache.clear()
                    fake_es = MagicMock()
                    fake_es.search.side_effect = [partial, complete]
                    with patch.object(runtime, "es", fake_es):
                        failed = self.client.post("/api/search", json={"q": query})
                        self.assertEqual(failed.status_code, 503)
                        self.assertEqual(failed.json()["error"], "search results incomplete")
                        self.assertEqual(runtime.search_response_cache, {})
                        self.assertEqual(runtime.literal_search_cache, {})
                        self.assertEqual(runtime.search_inflight, {})
                        recovered = self.client.post("/api/search", json={"q": query})
                    self.assertEqual(recovered.status_code, 200)
                    self.assertEqual(recovered.json()["total"], 0)
                    self.assertEqual(fake_es.search.call_count, 2)

    @patch.object(search_api, "get_search_docs", return_value={
        "doc-1": {"doc_id": "doc-1", "title": "手。机"},
    })
    @patch.object(search_api, "literal_match_ids", return_value={"doc-1"})
    @patch.object(runtime, "has_index_data", return_value=True)
    def test_incomplete_literal_candidate_batches_are_not_cached(self, _has_index, _literal, _docs):
        first = {"hits": {"total": {"value": 2, "relation": "eq"}, "hits": [
            {"_id": "doc-1", "sort": [1.0, "doc-1"]},
        ]}}
        for batches in (
            [first, {"hits": {"hits": []}}],
            [{"hits": {"total": {"value": 2, "relation": "eq"}, "hits": [{"_id": "doc-1"}]}}],
            [first, {"timed_out": True, "hits": {"hits": []}}],
        ):
            with self.subTest(batches=batches):
                runtime.search_response_cache.clear()
                runtime.literal_search_cache.clear()
                fake_es = MagicMock()
                fake_es.search.side_effect = batches
                with patch.object(runtime, "es", fake_es):
                    response = search_api.search(search_logic.SearchRequest(q="手。机"), normalize_exact=False)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(runtime.search_response_cache, {})
                self.assertEqual(runtime.literal_search_cache, {})
                self.assertEqual(runtime.search_inflight, {})

    @patch.object(search_api, "get_search_docs", return_value={
        "doc-3": {"doc_id": "doc-3", "title": "手。机资料", "content": "正文手。机内容"},
    })
    @patch.object(search_api, "literal_match_ids", return_value={"doc-3"})
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "index_status", return_value="ready")
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_literal_exact_verification_recomputes_total_across_all_candidates(
        self, _status, _index_status, _has_index, _literal_ids, _get_docs,
    ):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 3}, "hits": [
            {"_id": "doc-1", "_score": 3.0, "sort": [3.0, "doc-1"]},
            {"_id": "doc-2", "_score": 2.0, "sort": [2.0, "doc-2"]},
            {"_id": "doc-3", "_score": 1.0, "sort": [1.0, "doc-3"]},
        ]}}
        with patch.object(runtime, "es", fake_es), patch.object(search_api, "serving_lock", return_value=nullcontext()):
            response = search_api.search(
                search_logic.SearchRequest(q="手。机", exact=True, fulltext=True, page_size=1),
                normalize_exact=False,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["total"], 1)
        self.assertEqual([item["doc_id"] for item in json.loads(response.body)["results"]], ["doc-3"])
        self.assertEqual(fake_es.search.call_count, 1)
        self.assertEqual(fake_es.search.call_args.kwargs["size"], search_api.LITERAL_CANDIDATE_BATCH_SIZE)
        self.assertEqual(fake_es.search.call_args.kwargs["sort"][-1], {"doc_id": {"order": "asc"}})

    @patch.object(search_api, "get_search_docs", return_value={
        "doc-2": {"doc_id": "doc-2", "title": "手。机二", "content": "手。机"},
    })
    @patch.object(search_api, "literal_match_ids", return_value={"doc-1", "doc-2"})
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "index_status", return_value="ready")
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_literal_verification_cache_is_shared_across_pages(
        self, _status, _index_status, _has_index, literal_match_ids, _get_docs,
    ):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 2}, "hits": [
            {"_id": "doc-1", "sort": [2.0, "doc-1"]},
            {"_id": "doc-2", "sort": [1.0, "doc-2"]},
        ]}}
        with patch.object(runtime, "es", fake_es), patch.object(search_api, "serving_lock", return_value=nullcontext()):
            first = search_api.search(
                search_logic.SearchRequest(q="手。机", exact=True, page=1, page_size=1), normalize_exact=False,
            )
            second = search_api.search(
                search_logic.SearchRequest(q="手。机", exact=True, page=2, page_size=1), normalize_exact=False,
            )
        self.assertEqual(json.loads(first.body)["total"], 2)
        self.assertEqual(json.loads(second.body)["results"][0]["doc_id"], "doc-2")
        self.assertEqual(fake_es.search.call_count, 1)
        self.assertEqual(literal_match_ids.call_count, 1)

    def test_literal_read_plan_uses_exact_candidate_total_without_changing_order(self):
        fake_es = MagicMock()
        fake_es.search.return_value = {"hits": {"total": {"value": 2, "relation": "eq"}, "hits": [
            {"_id": "doc-2", "sort": [2.0, "doc-2"]}, {"_id": "doc-1", "sort": [1.0, "doc-1"]},
        ]}}
        kwargs = {"index": "test", "query": {"match_all": {}}, "track_total_hits": True}
        for threshold in (2, 3):
            with self.subTest(threshold=threshold), patch.object(runtime, "es", fake_es), \
                    patch.object(search_api, "LITERAL_ROW_ORDER_MIN_CANDIDATES", threshold), \
                    patch.object(search_api, "literal_match_ids", return_value={"doc-1", "doc-2"}) as verify:
                self.assertEqual(search_api.verified_literal_ids(kwargs, "query"), ["doc-2", "doc-1"])
                if threshold == 2:
                    verify.assert_called_once_with(["doc-2", "doc-1"], "query", row_order=True)
                else:
                    verify.assert_called_once_with(["doc-2", "doc-1"], "query")

    def test_global_literal_buffer_collects_every_batch_and_streams_above_budget(self):
        kwargs = {"index": "test", "query": {"match_all": {}}, "track_total_hits": True}
        complete = [
            {"hits": {"total": {"value": 3, "relation": "eq"}, "hits": [
                {"_id": "doc-3", "sort": [3.0, "doc-3"]}, {"_id": "doc-1", "sort": [2.0, "doc-1"]},
            ]}},
            {"hits": {"hits": [{"_id": "doc-2", "sort": [1.0, "doc-2"]}]}},
        ]
        for budget in (3, 2):
            fake_es = MagicMock()
            fake_es.search.side_effect = complete
            with self.subTest(budget=budget), patch.object(runtime, "es", fake_es), \
                    patch.object(search_api, "LITERAL_ROW_ORDER_MIN_CANDIDATES", 2), \
                    patch.object(search_api, "LITERAL_ROW_BUFFER_MAX_IDS", budget), \
                    patch.object(search_api, "literal_match_ids", return_value={"doc-2", "doc-3"}) as verify:
                self.assertEqual(search_api.verified_literal_ids(kwargs, "query"), ["doc-3", "doc-2"])
                if budget == 3:
                    verify.assert_called_once_with(["doc-3", "doc-1", "doc-2"], "query", row_order=True)
                else:
                    self.assertEqual(verify.call_count, 2)
                    self.assertTrue(all(not call.kwargs for call in verify.call_args_list))

    def test_global_literal_buffer_abandons_changed_total_without_dropping_candidates(self):
        fake_es = MagicMock()
        fake_es.search.side_effect = [
            {"hits": {"total": {"value": 3, "relation": "eq"}, "hits": [
                {"_id": "doc-1", "sort": [4.0, "doc-1"]}, {"_id": "doc-2", "sort": [3.0, "doc-2"]},
            ]}},
            {"hits": {"hits": [
                {"_id": "doc-3", "sort": [2.0, "doc-3"]}, {"_id": "doc-4", "sort": [1.0, "doc-4"]},
            ]}},
        ]
        with patch.object(runtime, "es", fake_es), \
                patch.object(search_api, "LITERAL_ROW_ORDER_MIN_CANDIDATES", 2), \
                patch.object(search_api, "LITERAL_ROW_BUFFER_MAX_IDS", 3), \
                patch.object(search_api, "literal_match_ids", side_effect=lambda ids, _: set(ids)):
            self.assertEqual(search_api.verified_literal_ids({"track_total_hits": True}, "query"),
                             ["doc-1", "doc-2", "doc-3", "doc-4"])

    def test_global_literal_buffer_rejects_partial_scan_before_verification(self):
        fake_es = MagicMock()
        fake_es.search.side_effect = [
            {"hits": {"total": {"value": 2, "relation": "eq"}, "hits": [
                {"_id": "doc-1", "sort": [2.0, "doc-1"]},
            ]}},
            {"timed_out": True, "hits": {"hits": []}},
        ]
        with patch.object(runtime, "es", fake_es), \
                patch.object(search_api, "LITERAL_ROW_ORDER_MIN_CANDIDATES", 1), \
                patch.object(search_api, "literal_match_ids") as verify:
            with self.assertRaises(search_api.IncompleteSearchError):
                search_api.verified_literal_ids({"track_total_hits": True}, "query")
        verify.assert_not_called()

    @patch.object(search_api, "literal_match_ids", side_effect=[{"doc-2"}, {"doc-3"}])
    def test_literal_candidates_use_search_after_batches(self, literal_match_ids):
        fake_es = MagicMock()
        fake_es.search.side_effect = [
            {"hits": {"total": {"value": 3}, "hits": [
                {"_id": "doc-1", "sort": [3.0, "doc-1"]},
                {"_id": "doc-2", "sort": [2.0, "doc-2"]},
            ]}},
            {"hits": {"hits": [
                {"_id": "doc-3", "sort": [1.0, "doc-3"]},
            ]}},
        ]
        search_kwargs = {
            "index": "test", "from_": 0, "size": 1, "query": {"match_all": {}},
            "sort": None, "track_total_hits": True, "_source": False,
        }
        with patch.object(runtime, "es", fake_es), patch.object(search_api, "LITERAL_CANDIDATE_BATCH_SIZE", 2):
            result = search_api.verified_literal_ids(search_kwargs, "手。机")
        self.assertEqual(result, ["doc-2", "doc-3"])
        self.assertEqual(fake_es.search.call_count, 2)
        second = fake_es.search.call_args_list[1].kwargs
        self.assertEqual(second["search_after"], [2.0, "doc-2"])
        self.assertFalse(second["track_total_hits"])
        self.assertEqual(literal_match_ids.call_count, 2)

    @patch.object(search_api, "get_search_docs")
    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_missing_or_oversized_highlight_fetches_full_fallback_content(self, _status, _has_index, get_search_docs):
        stored = {"doc-1": {"doc_id": "doc-1", "title": "标题", "content": "后文手机正文"}}
        get_search_docs.return_value = stored
        fake_es = MagicMock()
        fake_es.search.return_value = {
            "hits": {"total": {"value": 1, "relation": "eq"}, "hits": [
            {"_id": "doc-1", "highlight": {"content": ["甲" * (search_logic.MAX_HIGHLIGHT_FRAGMENT_CHARS + 1)]}}
            ]}
        }
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": "手机", "fulltext": True})
        self.assertEqual(response.status_code, 200)
        self.assertIn("<mark>手机</mark>", response.json()["results"][0]["snippet"][0])
        self.assertEqual(get_search_docs.call_count, 1)

    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"})
    def test_result_window_limit(self, _status, _has_index):
        response = self.client.post("/api/search", json={"q": "手机", "page": 5001, "page_size": 100})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "result window exceeded")

    def test_unknown_facet_returns_404(self):
        response = self.client.get("/api/facet/not-a-facet")
        self.assertEqual(response.status_code, 404)

    def test_facet_storage_failure_returns_stable_empty_shape(self):
        runtime.facet_response_cache.clear()
        with patch.object(runtime, "FACET_DB") as facet_db, \
                patch.object(runtime, "cached_status_payload", return_value={"index_ready": True, "index_status": "ready"}), \
                patch.object(runtime, "serving_lock", return_value=nullcontext()), \
                patch.object(runtime, "list_facets", return_value={"items": [], "total": 0, "page": 1, "page_size": 200}):
            facet_db.exists.return_value = True
            response = self.client.get("/api/facet/source")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])

    @patch.object(runtime, "FACET_DB")
    @patch.object(runtime, "cached_status_payload", return_value={"index_ready": False, "index_status": "indexing"})
    def test_sources_and_facets_have_stable_building_shapes_without_sidecar(self, _status, facet_db):
        facet_db.exists.return_value = False
        sources = self.client.get("/api/sources").json()
        facet = self.client.get("/api/facet/source?page=-2&page_size=999").json()
        self.assertEqual(sources["sources"], [])
        self.assertTrue(sources["indexing"])
        self.assertEqual(facet["items"], [])
        self.assertEqual(facet["page"], 1)

    @patch.object(preview_api, "variants_for_title", return_value=[{"doc_id": "doc-2", "publication_name": "另一刊物"}])
    @patch.object(preview_api, "get_doc")
    def test_preview_random_and_spa_routes(self, get_doc, _variants):
        get_doc.return_value = {
            "doc_id": "doc-1", "title": "标题", "publication_name": "刊物", "publication_type": "期刊",
            "authors": ["作者"], "tag_names": ["标签"], "date_display": ["1966/5/16"],
            "source_files": [], "source_file_count": 0, "content": "正文",
        }
        with patch.object(preview_api, "serving_lock", return_value=nullcontext()):
            preview = self.client.get("/api/preview/doc-1")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.json()["variants"][0]["doc_id"], "doc-2")
            with patch.object(preview_api, "random_doc", return_value=get_doc.return_value):
                self.assertEqual(self.client.get("/api/random").json()["doc_id"], "doc-1")
            with patch.object(preview_api, "get_doc", return_value=None), patch.object(preview_api, "random_doc", return_value=None):
                self.assertEqual(self.client.get("/api/preview/missing").status_code, 404)
                self.assertEqual(self.client.get("/api/random").status_code, 404)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/route/that/does/not/exist").status_code, 200)
        self.assertIn("BHA Search", self.client.get("/route/that/does/not/exist").text)

    def test_preview_and_random_storage_failures_return_404(self):
        with patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(preview_api, "serving_lock", return_value=nullcontext()), \
                patch.object(preview_api, "get_doc", return_value=None):
            self.assertEqual(self.client.get("/api/preview/doc-1").status_code, 404)
        with patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(preview_api, "serving_lock", return_value=nullcontext()), \
                patch.object(preview_api, "random_doc", return_value=None):
            self.assertEqual(self.client.get("/api/random").status_code, 404)

    def test_cors_contract(self):
        response = self.client.options(
            "/api/search",
            headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertNotIn("server-timing", response.headers)

    def test_large_responses_prefer_brotli_and_fall_back_to_gzip(self):
        brotli = self.client.get("/", headers={"Accept-Encoding": "br, gzip"})
        gzip = self.client.get("/", headers={"Accept-Encoding": "gzip"})
        identity = self.client.get("/", headers={"Accept-Encoding": "identity"})
        self.assertEqual(brotli.status_code, 200)
        self.assertEqual(brotli.headers["content-encoding"], "br")
        self.assertEqual(gzip.headers["content-encoding"], "gzip")
        self.assertNotIn("content-encoding", identity.headers)
        self.assertEqual(brotli.content, identity.content)
        self.assertEqual(gzip.content, identity.content)

    def test_versioned_static_assets_are_immutable(self):
        root = self.client.get("/", headers={"Accept-Encoding": "identity"})
        urls = re.findall(r'/static/(?:app\.js|style\.css)\?v=[0-9a-f]{16}', root.text)
        self.assertEqual(len(urls), 2)
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(self.client.get("/static/app.js").headers["cache-control"], "no-cache")
        worker = self.client.get("/static/sw.js")
        self.assertEqual(worker.headers["cache-control"], "no-cache")
        self.assertEqual(worker.headers["service-worker-allowed"], "/")
        self.assertNotIn("service-worker-allowed", self.client.get("/static/app.js").headers)
        unchanged = self.client.get("/", headers={"If-None-Match": root.headers["etag"]})
        self.assertEqual(unchanged.status_code, 304)

    @patch.object(runtime, "index_status", return_value="failed")
    @patch.object(runtime, "index_ready", return_value=False)
    @patch.object(runtime, "document_count", return_value=0)
    def test_health_ok_reflects_readiness(self, _count, _ready, _status):
        runtime.status_response_cache = None
        with patch.object(runtime, "bucket_snapshot_status", return_value={"state": "unavailable", "stage": "validate_sidecars"}):
            payload = runtime.cached_status_payload()
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["index_ready"])
        self.assertEqual(payload["bucket_snapshot"]["stage"], "validate_sidecars")

    def test_health_exposes_unavailable_document_count_without_caching_zero(self):
        runtime.status_response_cache = None
        with patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(runtime, "index_ready", return_value=True), \
                patch.object(runtime, "FACET_DB") as facet_db, \
                patch.object(runtime.es, "count", side_effect=OSError("unavailable")), \
                patch.object(runtime, "bucket_snapshot_status", return_value=None):
            facet_db.exists.return_value = True
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["document_count"], 0)
        self.assertNotIn("document_count_available", response.json())

    def test_external_generation_switch_changes_response_cache_keys(self):
        old_generation = ((1, 2, 3, 4), (5, 6, 7, 8))
        new_generation = ((1, 9, 3, 10), (5, 11, 7, 12))
        request = search_logic.SearchRequest(q="test")
        with patch.object(runtime, "serving_generation_token", return_value=old_generation):
            old_key = runtime.search_cache_key(request, 1, 20)
        with patch.object(runtime, "serving_generation_token", return_value=new_generation):
            new_key = runtime.search_cache_key(request, 1, 20)
        self.assertNotEqual(old_key, new_key)

    def test_serving_generation_is_not_ready_until_shell_status_is_ready(self):
        with patch.object(runtime, "index_ready", return_value=True), patch.object(runtime, "index_status", return_value="restoring"):
            self.assertFalse(runtime.serving_generation_ready())
        with patch.object(runtime, "index_ready", return_value=True), patch.object(runtime, "index_status", return_value="ready"):
            self.assertTrue(runtime.serving_generation_ready())

    @patch.object(runtime, "cached_status_payload", return_value={
        "ok": False,
        "index_ready": False,
        "document_count": 0,
        "index_status": "indexing",
        "progress": {"status": "indexing", "indexed": 42},
        "es": True,
    })
    def test_health_returns_progress_json_with_not_ready_status(self, _status):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["progress"]["indexed"], 42)
        self.assertNotIn("server-timing", response.headers)

    def test_ping_is_empty_and_not_cached(self):
        response = self.client.get("/api/ping")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_bootstrap_is_cached_until_serving_generation_changes(self):
        generation = ((1, 2, 3, 4), (5, 6, 7, 8))
        search = bootstrap_api.JSONResponse({
            "results": [{"doc_id": "doc-1"}], "total": 1, "page": 1, "page_size": 20,
            "indexing": False, "index_status": "ready",
        })
        facet_payloads = {
            kind: {"items": [{"name": kind}], "total": 1, "page": 1, "page_size": 200, "has_more": False}
            for kind in ("source", "author", "tag")
        }
        with patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(runtime, "serving_generation_ready", return_value=True), \
                patch.object(runtime, "serving_generation_token", return_value=generation) as token, \
                patch.object(runtime, "cached_sources_payload", return_value={"archives": [], "types": [], "years": []}), \
                patch.object(runtime, "cached_facet_payload", side_effect=lambda kind, *_args: facet_payloads[kind]), \
                patch.object(bootstrap_api.search_api, "search", return_value=search) as default_search, \
                patch.object(bootstrap_api, "serving_lock", return_value=nullcontext()):
            bootstrap_api.bootstrap_cache = None
            bootstrap_api.facet_cache = None
            first = bootstrap_api.bootstrap()
            second = bootstrap_api.bootstrap()
            token.return_value = ((9, 10, 11, 12), generation[1])
            third = bootstrap_api.bootstrap()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.body, first.body)
        self.assertNotEqual(json.loads(third.body)["generation"], json.loads(first.body)["generation"])
        self.assertEqual(default_search.call_count, 2)
        self.assertEqual(first.headers["cache-control"], "no-store")

    def test_bootstrap_rejects_unready_and_incomplete_search_without_caching(self):
        generation = ((1, 2, 3, 4), None)
        bootstrap_api.bootstrap_cache = None
        with patch.object(runtime, "serving_generation_token", return_value=generation), \
                patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(runtime, "serving_generation_ready", return_value=False):
            response = bootstrap_api.bootstrap()
            self.assertEqual(response.status_code, 503)
            self.assertIsNone(bootstrap_api.bootstrap_cache)
        with patch.object(runtime, "serving_generation_token", return_value=generation), \
                patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(runtime, "serving_generation_ready", return_value=True), \
                patch.object(bootstrap_api.search_api, "search", return_value=bootstrap_api.JSONResponse({"error": "down"}, status_code=503)):
            response = bootstrap_api.bootstrap()
            self.assertEqual(response.status_code, 503)
            self.assertIsNone(bootstrap_api.bootstrap_cache)

    def test_bootstrap_rejects_generation_that_keeps_changing(self):
        first = ((1, 2, 3, 4), None)
        second = ((9, 8, 7, 6), None)
        bootstrap_api.bootstrap_cache = None
        with patch.object(runtime, "serving_generation_token", side_effect=[first, first, second, first, second]), \
                patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(runtime, "serving_generation_ready", return_value=True), \
                patch.object(runtime, "cached_sources_payload", return_value={}), \
                patch.object(bootstrap_api, "serving_lock", return_value=nullcontext()), \
                patch.object(bootstrap_api.search_api, "search", return_value=bootstrap_api.JSONResponse({"results": []})):
            response = bootstrap_api.bootstrap()
            self.assertEqual(response.status_code, 503)
            self.assertIsNone(bootstrap_api.bootstrap_cache)

    def test_bootstrap_payload_excludes_facets_that_are_fetched_separately(self):
        generation = ((1, 2, 3, 4), (5, 6, 7, 8))
        search = bootstrap_api.JSONResponse({
            "results": [{"doc_id": "doc-1"}], "total": 1, "page": 1, "page_size": 20,
            "indexing": False, "index_status": "ready",
        })
        facet_payloads = {
            kind: {"items": [{"name": kind}], "total": 1, "page": 1, "page_size": 200, "has_more": False}
            for kind in ("source", "author", "tag")
        }
        with patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(runtime, "serving_generation_ready", return_value=True), \
                patch.object(runtime, "serving_generation_token", return_value=generation), \
                patch.object(runtime, "cached_sources_payload", return_value={"archives": [], "types": [], "years": []}), \
                patch.object(runtime, "cached_facet_payload", side_effect=lambda kind, *_args: facet_payloads[kind]) as facet_calls, \
                patch.object(bootstrap_api.search_api, "search", return_value=search), \
                patch.object(bootstrap_api, "serving_lock", return_value=nullcontext()):
            bootstrap_api.bootstrap_cache = None
            bootstrap_api.facet_cache = None
            core = json.loads(bootstrap_api.bootstrap().body)
            facet_calls.assert_not_called()
            facets = json.loads(bootstrap_api.bootstrap_facets().body)
        self.assertNotIn("facets", core)
        self.assertEqual(set(core), {"generation", "search", "sources"})
        self.assertEqual(set(facets), {"generation", "facets"})
        self.assertEqual(set(facets["facets"]), {"source", "author", "tag"})
        self.assertEqual(facet_calls.call_count, 3)


        generation = ((1, 2, 3, 4), (5, 6, 7, 8))
        search = bootstrap_api.JSONResponse({
            "results": [{"doc_id": "doc-1"}], "total": 1, "page": 1, "page_size": 20,
            "indexing": False, "index_status": "ready",
        })
        facet_payloads = {
            kind: {"items": [{"name": kind}], "total": 1, "page": 1, "page_size": 200, "has_more": False}
            for kind in ("source", "author", "tag")
        }
        request = Request({"type": "http", "method": "GET", "path": "/api/bootstrap",
                           "headers": [(b"accept-encoding", b"br")]})

        def call():
            return bootstrap_api.bootstrap(request)

        with patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(runtime, "serving_generation_ready", return_value=True), \
                patch.object(runtime, "serving_generation_token", return_value=generation), \
                patch.object(runtime, "cached_sources_payload", return_value={"archives": [], "types": [], "years": []}), \
                patch.object(runtime, "cached_facet_payload", side_effect=lambda kind, *_args: facet_payloads[kind]), \
                patch.object(bootstrap_api.search_api, "search", return_value=search), \
                patch.object(bootstrap_api, "serving_lock", return_value=nullcontext()), \
                patch.object(bootstrap_api.brotli, "compress", wraps=bootstrap_api.brotli.compress) as compress:
            bootstrap_api.bootstrap_cache = None
            bootstrap_api.facet_cache = None
            first = call()
            second = call()
        self.assertEqual(first.headers["content-encoding"], "br")
        self.assertEqual(second.headers["content-encoding"], "br")
        self.assertEqual(first.body, second.body)
        self.assertEqual(compress.call_count, 1)
        self.assertEqual(json.loads(brotli.decompress(first.body))["generation"],
                         bootstrap_api.generation_id(generation))

    def test_reindex_requires_configured_bearer_token(self):
        with patch.object(reindex_api, "REINDEX_TOKEN", ""), patch.object(reindex_api, "acquire_index_lock") as acquire_lock:
            response = self.client.post("/api/reindex")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["cache-control"], "no-store")
        acquire_lock.assert_not_called()

        with patch.object(reindex_api, "REINDEX_TOKEN", "secret"), patch.object(reindex_api, "acquire_index_lock") as acquire_lock:
            response = self.client.post("/api/reindex", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        acquire_lock.assert_not_called()

    def test_authenticated_reindex_starts_once_with_short_cooldown(self):
        lock_file = MagicMock()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(reindex_api, "REINDEX_TOKEN", "secret"), \
                patch.object(reindex_api, "REINDEX_LAST_START", Path(directory) / "last-start"), \
                patch.object(reindex_api, "acquire_index_lock", return_value=lock_file), \
                patch.object(reindex_api, "start_reindex_worker") as start_worker:
            response = self.client.post("/api/reindex", headers={"Authorization": "Bearer secret"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["cooldown_seconds"], 600)
        start_worker.assert_called_once_with(lock_file, reset=False, job_id=response.json()["job_id"])
        self.assertEqual(response.json()["mode"], "incremental")
        lock_file.close.assert_not_called()

    def test_authenticated_reindex_can_request_explicit_full_rebuild(self):
        lock_file = MagicMock()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(reindex_api, "REINDEX_TOKEN", "secret"), \
                patch.object(reindex_api, "REINDEX_LAST_START", Path(directory) / "last-start"), \
                patch.object(reindex_api, "acquire_index_lock", return_value=lock_file), \
                patch.object(reindex_api, "start_reindex_worker") as start_worker:
            response = self.client.post(
                "/api/reindex?full=true", headers={"Authorization": "Bearer secret"}
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["mode"], "full")
        start_worker.assert_called_once_with(lock_file, reset=True, job_id=response.json()["job_id"])

    def test_reindex_passes_exact_archive_identities_to_worker(self):
        lock_file = MagicMock()
        commits = {str(value): "a" * 40 for value in range(32)}
        revisions = {str(value): "b" * 40 for value in range(32)}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(reindex_api, "REINDEX_TOKEN", "secret"), \
                patch.object(reindex_api, "REINDEX_LAST_START", Path(directory) / "last-start"), \
                patch.object(reindex_api, "acquire_index_lock", return_value=lock_file), \
                patch.object(reindex_api, "start_reindex_worker") as start_worker:
            response = self.client.post("/api/reindex", headers={"Authorization": "Bearer secret"}, json={
                "archive_commits": commits,
                "archive_revisions": revisions,
                "changed_archives": [0, 4],
            })
        self.assertEqual(response.status_code, 202)
        start_worker.assert_called_once_with(
            lock_file, reset=False,
            expected_commits={value: "a" * 40 for value in range(32)},
            expected_revisions={value: "b" * 40 for value in range(32)},
            changed_archives=[0, 4], job_id=response.json()["job_id"],
        )

    def test_reindex_status_includes_commits_and_is_not_cacheable(self):
        lock_file = MagicMock()
        commits = {str(value): "a" * 40 for value in range(32)}
        revisions = {str(value): "b" * 40 for value in range(32)}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(reindex_api, "REINDEX_TOKEN", "secret"), \
                patch.object(reindex_api, "REINDEX_LAST_START", Path(directory) / "last-start"), \
                patch.object(reindex_api, "REINDEX_JOB_PATH", Path(directory) / "job.json"), \
                patch.object(reindex_api, "acquire_index_lock", return_value=lock_file), \
                patch.object(reindex_api, "start_reindex_worker"), \
                patch.object(runtime, "index_status", return_value="ready"), \
                patch.object(runtime, "index_progress", return_value=None), \
                patch.object(runtime, "INDEX_SWITCH_STATE_PATH", Path(directory) / "switch"):
            accepted = self.client.post("/api/reindex", headers={"Authorization": "Bearer secret"}, json={
                "archive_commits": commits, "archive_revisions": revisions, "changed_archives": [0, 4],
            })
            status = self.client.get("/api/reindex/status", headers={"Authorization": "Bearer secret"})
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.headers["cache-control"], "no-store")
        self.assertEqual(status.json()["job"]["job_id"], accepted.json()["job_id"])
        self.assertEqual(status.json()["job"]["target_archive_commits"], commits)
        self.assertEqual(status.json()["job"]["target_archive_revisions"], revisions)

    def test_reindex_marks_ready_before_starting_snapshot_publish(self):
        lock_file = MagicMock()
        thread = MagicMock()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(runtime, "INDEX_STATUS_PATH", Path(directory) / "status"), \
                patch.object(reindex_api, "BUCKET_DIR", Path(directory)), \
                patch.object(reindex_api, "read_reindex_job", return_value={"job_id": "synthetic"}), \
                patch.object(reindex_api, "update_reindex_job", return_value=True), \
                patch.object(reindex_api, "ensure_index", return_value=True), \
                patch.object(reindex_api.threading, "Thread", return_value=thread):
            reindex_api.run_reindex(lock_file, job_id="synthetic")
            self.assertEqual(runtime.INDEX_STATUS_PATH.read_text(encoding="utf-8"), "ready")
        thread.start.assert_called_once_with()
        lock_file.close.assert_called_once_with()

    def test_reindex_noop_does_not_publish_snapshot(self):
        lock_file = MagicMock()
        thread = MagicMock()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(runtime, "INDEX_STATUS_PATH", Path(directory) / "status"), \
                patch.object(reindex_api, "BUCKET_DIR", Path(directory)), \
                patch.object(reindex_api, "read_reindex_job", return_value={"job_id": "synthetic"}), \
                patch.object(reindex_api, "update_reindex_job", return_value=True), \
                patch.object(reindex_api, "ensure_index", return_value=False), \
                patch.object(reindex_api.threading, "Thread", return_value=thread):
            reindex_api.run_reindex(lock_file, job_id="synthetic")
        thread.assert_not_called()
        lock_file.close.assert_called_once_with()

    def test_reindex_rejects_incomplete_revision_pair_without_writing_plan(self):
        lock_file = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(runtime, "INDEX_STATUS_PATH", root / "index-status"), \
                    patch.object(runtime, "INDEX_SWITCH_STATE_PATH", root / "pending"), \
                    patch.object(runtime, "serving_generation_valid", return_value=True), \
                    patch.object(reindex_api, "read_reindex_job", return_value={"job_id": "synthetic"}), \
                    patch.object(reindex_api, "update_reindex_job", return_value=True), \
                    patch.object(reindex_api, "write_convergence_plan") as plan, \
                    patch.object(reindex_api, "ensure_index") as index:
                reindex_api.run_reindex(lock_file, expected_commits={1: "a" * 40}, job_id="synthetic")
                self.assertEqual(runtime.INDEX_STATUS_PATH.read_text(), "ready")
                plan.assert_not_called()
                index.assert_not_called()
        lock_file.close.assert_called_once_with()

    def test_reindex_failure_with_pending_switch_is_not_marked_ready(self):
        lock_file = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            switch_state = root / "index-switch.json"
            switch_state.write_text("{}", encoding="utf-8")
            status = root / "index-status"
            with patch.object(runtime, "INDEX_SWITCH_STATE_PATH", switch_state), \
                    patch.object(runtime, "INDEX_STATUS_PATH", status), \
                    patch.object(reindex_api, "BUCKET_DIR", None), \
                    patch.object(reindex_api, "read_reindex_job", return_value={"job_id": "synthetic"}), \
                    patch.object(reindex_api, "update_reindex_job", return_value=True), \
                    patch.object(reindex_api, "ensure_index", side_effect=RuntimeError("uncertain switch")), \
                    patch.object(runtime, "index_ready", return_value=True):
                reindex_api.run_reindex(lock_file, job_id="synthetic")
            self.assertEqual(status.read_text(encoding="utf-8"), "failed")
            lock_file.close.assert_called_once_with()

    def test_reindex_failure_keeps_valid_serving_generation_ready(self):
        lock_file = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "index-status"
            with patch.object(runtime, "INDEX_SWITCH_STATE_PATH", Path(directory) / "missing-switch.json"), \
                    patch.object(runtime, "INDEX_STATUS_PATH", status), \
                    patch.object(reindex_api, "BUCKET_DIR", None), \
                    patch.object(reindex_api, "read_reindex_job", return_value={"job_id": "synthetic"}), \
                    patch.object(reindex_api, "update_reindex_job", return_value=True), \
                    patch.object(reindex_api, "ensure_index", side_effect=RuntimeError("database is locked")), \
                    patch.object(runtime, "serving_generation_valid", return_value=True):
                reindex_api.run_reindex(lock_file, job_id="synthetic")
            self.assertEqual(status.read_text(encoding="utf-8"), "ready")
            lock_file.close.assert_called_once_with()

    def test_serving_generation_validation_retries_transient_sidecar_lock(self):
        fake_es = MagicMock()
        fake_es.get.return_value = {"_source": {"version": "1"}}
        with patch.object(runtime, "es", fake_es), \
                patch.object(runtime, "sidecars_ready", side_effect=[False, False, True]) as sidecars_ready, \
                patch.object(runtime.time, "sleep") as sleep:
            self.assertTrue(runtime.serving_generation_valid())
        self.assertEqual(sidecars_ready.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "index_status", return_value="restoring")
    def test_search_does_not_read_restoring_generation(self, _status, _has_data):
        fake_es = MagicMock()
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": "文化革命"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])
        self.assertTrue(response.json()["indexing"])
        fake_es.search.assert_not_called()

    @patch.object(runtime, "has_index_data", return_value=True)
    @patch.object(runtime, "index_status", return_value="failed")
    def test_search_does_not_serve_failed_generation(self, _status, _has_data):
        fake_es = MagicMock()
        with patch.object(runtime, "es", fake_es):
            response = self.client.post("/api/search", json={"q": "文化革命"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])
        self.assertTrue(response.json()["indexing"])
        self.assertNotIn("error", response.json())
        self.assertEqual(response.json()["index_status"], "failed")
        fake_es.search.assert_not_called()

    @patch.object(runtime, "index_status", return_value="restoring")
    @patch.object(preview_api, "get_doc")
    def test_preview_is_blocked_during_restore(self, get_doc, _status):
        response = self.client.get("/api/preview/doc-1")
        self.assertEqual(response.status_code, 503)
        get_doc.assert_not_called()

    def test_preview_payload_exposes_mirrored_pdf_page_previews(self):
        stored = {
            "doc_id": "doc-1", "title": "资料",
            "source_files": ["https://raw.githubusercontent.com/o/r/main/a.pdf"],
        }
        entry = {"page_previews": {"paths": ["archives0/aa/one.webp", "archives0/bb/two.webp"]}}
        with patch.object(source_files, "source_mirror_entry", AsyncMock(return_value=entry)), \
                patch.object(preview_api, "variants_for_title", return_value=[]), \
                patch.object(preview_api, "serving_lock", return_value=nullcontext()):
            payload = asyncio.run(preview_api.preview_payload(stored))
        self.assertEqual(
            payload["source_files"][0]["pages"],
            [
                "https://huggingface.co/datasets/vomebook/BHA-Source-Files/resolve/main/archives0/aa/one.webp",
                "https://huggingface.co/datasets/vomebook/BHA-Source-Files/resolve/main/archives0/bb/two.webp",
            ],
        )

    def test_preview_ignores_invalid_or_escaping_mirror_paths(self):
        stored = {"doc_id": "doc-1", "title": "资料", "article": {"page_start": 1},
                  "source_files": ["https://raw.githubusercontent.com/o/r/main/a.pdf"]}
        entry = {"page_previews": {"paths": ["archives0/../private.webp", 123, "archives0/safe.webp"]}}
        with patch.object(source_files, "source_mirror_entry", AsyncMock(return_value=entry)), \
                patch.object(preview_api, "variants_for_title", return_value=[]), \
                patch.object(preview_api, "serving_lock", return_value=nullcontext()):
            payload = asyncio.run(preview_api.preview_payload(stored))
        self.assertEqual(payload["source_files"][0]["pages"], [])
        self.assertNotIn("initial_page", payload["source_files"][0])

    def test_source_and_facet_cache_ttl_is_ten_minutes(self):
        self.assertEqual(runtime.FACET_CACHE_TTL_SECONDS, 600)

    @patch.object(runtime, "warm_static_files", return_value=True)
    @patch.object(runtime, "clear_response_caches")
    @patch.object(runtime, "cached_facet_payload", return_value={"items": []})
    @patch.object(runtime, "available_years", return_value=[1966])
    @patch.object(runtime, "cached_sources_payload", return_value={})
    @patch.object(runtime, "warmup_db", return_value=True)
    @patch.object(runtime, "warmup_elasticsearch", return_value=True)
    def test_full_auxiliary_warmup_clears_caches_and_loads_sidecars(
        self,
        _elasticsearch,
        _warmup_db,
        _sources,
        _years,
        facet,
        clear_caches,
        _static,
    ):
        result = runtime.run_auxiliary_warmup(full=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["elasticsearch_warmed"])
        self.assertEqual(facet.call_count, 3)
        clear_caches.assert_called_once()

    @patch.object(runtime, "warm_static_files", return_value=True)
    @patch.object(runtime, "clear_response_caches")
    @patch.object(runtime, "cached_facet_payload", return_value={"items": []})
    @patch.object(runtime, "available_years", return_value=[])
    @patch.object(runtime, "cached_sources_payload", return_value={})
    @patch.object(runtime, "warmup_db", return_value=True)
    @patch.object(runtime, "warmup_elasticsearch", return_value=True)
    def test_periodic_auxiliary_warmup_preserves_response_caches(
        self,
        _elasticsearch,
        _warmup_db,
        _sources,
        _years,
        facet,
        clear_caches,
        _static,
    ):
        result = runtime.run_auxiliary_warmup(full=False)
        self.assertTrue(result["ok"])
        self.assertEqual(facet.call_count, 3)
        clear_caches.assert_not_called()

    @patch.object(runtime, "es")
    def test_elasticsearch_warmup_is_a_result_free_match_all_query(self, fake_es):
        self.assertTrue(runtime.warmup_elasticsearch())
        fake_es.search.assert_called_once_with(
            index=runtime.INDEX_NAME,
            size=0,
            track_total_hits=False,
            query={"match_all": {}},
            source=False,
        )


if __name__ == "__main__":
    unittest.main()
