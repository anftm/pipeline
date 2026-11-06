"""Exercise malformed proofreading metadata and retry boundaries offline."""
import json
import unittest
from ipaddress import ip_network
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from app import document_identity, main, proofread_api, source_files


class ProofreadBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def setUp(self):
        submissions = patch.dict('os.environ', {'BHA_PIPELINE_SUBMISSIONS_PAUSED': '0'})
        submissions.start()
        self.addCleanup(submissions.stop)
        proofread_api.proofread_requests.clear()
        proofread_api.proofread_dispatches.clear()

    def test_string_list_limits_and_metadata_shapes(self):
        self.assertEqual(proofread_api.clean_string_list([" 甲 ", "乙"], "authors", 2), ["甲", "乙"])
        for value in (None, "甲", ["甲", "乙", "丙"], [123], [" "], ["甲" * 301]):
            with self.subTest(value=str(value)[:30]), self.assertRaises(ValueError):
                proofread_api.clean_string_list(value, "authors", 2)
        valid = {"article": {"title": " 标题 ", "authors": [" 作者 "],
                             "dates": [{"year": 1967, "month": 5, "day": 1}],
                             "tags": [{"name": " 标签 ", "type": "主题/事件"}]},
                 "source": {"name": "刊物", "author": "作者", "type": "期刊", "files": ["https://example.com/a.pdf"]}}
        result = proofread_api.clean_metadata(valid)
        self.assertEqual(result["article"]["title"], "标题")
        self.assertEqual(result["article"]["authors"], ["作者"])
        self.assertEqual(result["source"]["files"], ["https://example.com/a.pdf"])
        invalid = [
            {}, {"unexpected": {}}, {"article": []}, {"article": {"unknown": "x"}},
            {"article": {"dates": "bad"}}, {"article": {"dates": [{}]}},
            {"article": {"dates": [{"month": 13}]}}, {"article": {"tags": "bad"}},
            {"article": {"tags": [{"name": "标签"}]}}, {"article": {"tags": [{"name": 1, "type": "主题/事件"}]}},
            {"article": {"tags": [{"name": " ", "type": "主题/事件"}]}},
            {"source": []}, {"source": {"name": 123}}, {"source": {"name": " "}},
            {"source": {"files": ["http://127.0.0.1/private"]}},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                proofread_api.clean_metadata(value)

    def test_uncertain_dispatch_blocks_same_process_retry(self):
        source = {"doc_id": "doc-1", "archive_id": 1, "article_id": "a", "publication_id": "p",
                  "article": {"parts": [{"text": "旧", "type": "paragraph"}], "comments": []}}
        body = {"doc_id": "doc-1", "base_content_hash": document_identity.content_hash(source),
                "patch": {"version": 2, "parts": {"0": {"diff": "-1\t+新"}}, "comments": {}}}
        dispatch = AsyncMock(side_effect=[RuntimeError("offline"), None])
        with patch.object(proofread_api, "GITHUB_TOKEN", "secret"), \
                patch.object(source_files, "get_source_doc", return_value=source), \
                patch.object(proofread_api, "dispatch_proofread", dispatch):
            first = self.client.post("/api/proofread", json=body)
            second = self.client.post("/api/proofread", json=body)
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["status"], "uncertain")
        self.assertEqual(dispatch.await_count, 1)
        self.assertEqual(sum(map(len, proofread_api.proofread_requests.values())), 1)

    def test_uncertain_or_processing_duplicate_never_reports_success(self):
        for status in ("uncertain", "processing"):
            with self.subTest(status=status):
                proofread_api.proofread_dispatches["synthetic"] = {"created_at": 0, "status": status}
                with patch.object(proofread_api.time, "time", return_value=100000):
                    duplicate = proofread_api.duplicate_proofread_response("synthetic")
                self.assertEqual(duplicate.status_code, 409)
                self.assertEqual(json.loads(duplicate.body)["status"], status)

    def test_full_cache_does_not_evict_uncertain_entries(self):
        for i in range(1000):
            proofread_api.proofread_dispatches[str(i)] = {"created_at": 0, "status": "uncertain"}
        with patch.object(proofread_api.time, "time", return_value=100000):
            self.assertEqual(proofread_api.duplicate_proofread_response("new").status_code, 503)
        self.assertEqual(len(proofread_api.proofread_dispatches), 1000)

    def test_trusted_proxy_rate_and_release(self):
        def request(peer, forwarded):
            return Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"x-forwarded-for", forwarded.encode())],
                            "client": (peer, 1), "server": ("test", 80), "scheme": "http", "query_string": b""})
        with patch.object(proofread_api, "TRUST_PROXY", True), \
                patch.object(proofread_api, "PROXY_NETWORKS", (ip_network("10.0.0.0/8"),)), \
                patch.object(proofread_api, "PROXY_HOPS", 1), patch.object(proofread_api, "RATE_LIMIT", 1), \
                patch.object(proofread_api.time, "monotonic", return_value=100):
            first = request("10.0.0.5", "203.0.113.5, 10.0.0.2")
            self.assertIsNone(proofread_api.proofread_rate_error(first))
            self.assertEqual(first.state.proofread_rate_key, "203.0.113.5")
            self.assertEqual(proofread_api.proofread_rate_error(request("10.0.0.5", "203.0.113.5, 10.0.0.2")).status_code, 429)
            proofread_api.release_proofread_rate(first)
            self.assertNotIn("203.0.113.5", proofread_api.proofread_requests)
            invalid = request("10.0.0.5", "bad, 10.0.0.2")
            self.assertIsNone(proofread_api.proofread_rate_error(invalid))
            self.assertEqual(invalid.state.proofread_rate_key, "10.0.0.5")


if __name__ == "__main__":
    unittest.main()
