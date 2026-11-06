"""Offline parse API boundaries; no OCR or pipeline calls leave the process."""
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app import document_identity, main, parse_api, source_files


class ParseBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def setUp(self):
        submissions = patch.dict('os.environ', {'BHA_PIPELINE_SUBMISSIONS_PAUSED': '0'})
        submissions.start()
        self.addCleanup(submissions.stop)
        parse_api.parse_requests.clear()
        parse_api.parse_dispatches.clear()
        self.source = {
            "doc_id": "parse-boundary", "archive_id": 25,
            "publication_id": "052417de-42fb-4781-9885-af4fb006e9b6",
            "title": "【文章待拆分】小报", "article": {"page_start": 2, "page_end": 3},
        }

    def test_ocr_invalid_identity_range_payload_and_upstream_failures(self):
        endpoint = "/api/parse/parse-boundary/ocr"
        valid = MagicMock()
        valid.content = b'{"ocr_results":[]}'
        valid.json.return_value = {"ocr_results": [{"text": "识别正文"}]}
        upstream = AsyncMock(return_value=valid)
        with patch.object(source_files, "get_source_doc", return_value=self.source), \
                patch.object(source_files.source_client, "get", upstream):
            ok = self.client.get(endpoint + "?page=2")
            outside = self.client.get(endpoint + "?page=1")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["ocr_results"], [{"text": "识别正文"}])
        self.assertEqual(ok.headers["cache-control"], "public, max-age=300")
        self.assertEqual(outside.status_code, 404)
        self.assertEqual(upstream.await_count, 1)
        self.assertTrue(upstream.await_args.args[0].endswith("/052417de-42fb-4781-9885-af4fb006e9b6/2.json"))

        cases = [
            (None, None, 404, "not found"),
            ({**self.source, "publication_id": "not-a-uuid"}, valid, 500, "publication"),
        ]
        for source, response, status, message in cases:
            with self.subTest(status=status), patch.object(source_files, "get_source_doc", return_value=source), \
                    patch.object(source_files.source_client, "get", AsyncMock(return_value=response)):
                result = self.client.get(endpoint + "?page=2")
                self.assertEqual(result.status_code, status)
                self.assertIn(message, result.json()["error"])

        for mode in ("large", "bad-shape", "network", "http"):
            response = MagicMock()
            response.content = b"x" * 11 if mode == "large" else b"{}"
            response.json.return_value = {"ocr_results": {}} if mode == "bad-shape" else {"ocr_results": []}
            if mode == "http":
                response.raise_for_status.side_effect = RuntimeError("HTTP failure")
            get = AsyncMock(side_effect=RuntimeError("network offline")) if mode == "network" else AsyncMock(return_value=response)
            with self.subTest(mode=mode), patch.object(parse_api, "PARSE_OCR_MAX_BYTES", 10), \
                    patch.object(source_files, "get_source_doc", return_value=self.source), \
                    patch.object(source_files.source_client, "get", get):
                result = self.client.get(endpoint + "?page=2")
                self.assertEqual(result.status_code, 502)
                self.assertIn("error", result.json())

    def test_invalid_pagination_never_queries_store(self):
        with patch.object(parse_api, "parse_queue_docs") as query:
            for suffix in ("?page=0", "?page_size=0", "?page_size=101"):
                with self.subTest(suffix=suffix):
                    self.assertEqual(self.client.get("/api/parse/queue" + suffix).status_code, 400)
        query.assert_not_called()

    def test_submit_rejects_missing_config_bad_hash_and_oversized_payload_without_dispatch(self):
        body = {"doc_id": "parse-boundary", "articles": [{"title": "文章", "page_start": 2,
                "page_end": 3, "content": "正文", "base_part_count": 1}]}
        dispatch = AsyncMock()
        with patch.object(parse_api, "dispatch_parse", dispatch), patch.object(source_files, "get_source_doc", return_value=self.source):
            with patch.object(parse_api, "GITHUB_TOKEN", ""):
                self.assertEqual(self.client.post("/api/parse", json=body).status_code, 503)
            with patch.object(parse_api, "GITHUB_TOKEN", "secret"), patch.object(parse_api, "PARSE_MAX_PAYLOAD_CHARACTERS", 10):
                self.assertEqual(self.client.post("/api/parse", json=body).status_code, 413)
            with patch.object(parse_api, "GITHUB_TOKEN", "secret"):
                stale = self.client.post("/api/parse", json={**body, "base_content_hash": "f" * 64})
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(len(stale.json()["content_hash"]), 64)
                with patch.object(source_files, "get_source_doc", return_value=None):
                    self.assertEqual(self.client.post("/api/parse", json={**body, "base_content_hash": document_identity.content_hash(self.source)}).status_code, 404)
        dispatch.assert_not_awaited()

    def test_trusted_proxy_chain_and_expired_dispatches(self):
        from ipaddress import ip_network
        from starlette.requests import Request
        def request(peer, forwarded):
            return Request({"type": "http", "method": "POST", "path": "/", "headers": [(b"x-forwarded-for", forwarded.encode())],
                            "client": (peer, 1), "server": ("test", 80), "scheme": "http", "query_string": b""})
        with patch.object(parse_api, "TRUST_PROXY", True), patch.object(parse_api, "PROXY_NETWORKS", (ip_network("10.0.0.0/8"),)), \
                patch.object(parse_api, "PROXY_HOPS", 1):
            self.assertEqual(parse_api.parse_client_key(request("10.0.0.5", "203.0.113.5, 10.0.0.2")), "203.0.113.5")
            self.assertEqual(parse_api.parse_client_key(request("8.8.8.8", "203.0.113.5, 10.0.0.2")), "8.8.8.8")
            self.assertEqual(parse_api.parse_client_key(request("10.0.0.5", "invalid, 10.0.0.2")), "10.0.0.5")
        parse_api.parse_dispatches["old"] = {"created_at": 10, "status": "accepted"}
        with patch.object(parse_api.time, "time", return_value=100000):
            self.assertIsNone(parse_api.duplicate_parse_response("new"))
            self.assertNotIn("old", parse_api.parse_dispatches)
            duplicate = parse_api.duplicate_parse_response("new")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(json.loads(duplicate.body)["status"], "processing")

    def test_uncertain_parse_is_not_expired_or_evicted(self):
        for i in range(1000):
            parse_api.parse_dispatches[str(i)] = {"created_at": 0, "status": "uncertain"}
        with patch.object(parse_api.time, "time", return_value=100000):
            self.assertEqual(parse_api.duplicate_parse_response("0").status_code, 409)
            self.assertEqual(parse_api.duplicate_parse_response("new").status_code, 503)
        self.assertEqual(len(parse_api.parse_dispatches), 1000)


if __name__ == "__main__":
    unittest.main()
