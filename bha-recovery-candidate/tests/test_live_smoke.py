import json
import os
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BASE_URL = os.environ.get("BHA_LIVE_BASE_URL", "").rstrip("/")


@unittest.skipUnless(BASE_URL, "set BHA_LIVE_BASE_URL to run deployment smoke tests")
class LiveSmokeTests(unittest.TestCase):
    def request(self, path, payload=None, timeout=45):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{BASE_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "bha-smoke-test"},
        )
        with urlopen(request, timeout=timeout) as response:
            self.assertEqual(response.status, 200)
            return response.status, dict(response.headers), response.read()

    def request_json(self, path, payload=None):
        _status, _headers, body = self.request(path, payload)
        return json.loads(body)

    def test_health_ready(self):
        payload = self.request_json("/api/health")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["es"])
        self.assertTrue(payload["index_ready"])
        self.assertGreater(payload["document_count"], 0)

    def test_sources_shape(self):
        payload = self.request_json("/api/sources")
        for key in ("sources", "authors", "tags", "archives", "types", "years"):
            self.assertIn(key, payload)
        self.assertTrue(payload["archives"])
        self.assertTrue(payload["types"])

    def test_bootstrap_contains_one_ready_generation(self):
        _status, headers, body = self.request("/api/bootstrap")
        payload = json.loads(body)
        headers = {name.lower(): value for name, value in headers.items()}
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertRegex(payload["generation"], r"^[0-9a-f]{16}$")
        self.assertEqual(payload["search"]["index_status"], "ready")
        self.assertFalse(payload["search"]["indexing"])
        self.assertTrue(payload["search"]["results"])
        self.assertTrue(payload["sources"]["archives"])
        # Facets are intentionally fetched after the first result paint. Keep
        # comparing generation IDs, but don't require both requests to land on
        # the same generation while a legitimate index switch is in progress.
        facet_payload = self.request_json("/api/bootstrap/facets")
        for _ in range(2):
            if facet_payload["generation"] == payload["generation"]:
                break
            payload = self.request_json("/api/bootstrap")
            facet_payload = self.request_json("/api/bootstrap/facets")
        self.assertEqual(payload["generation"], facet_payload["generation"])
        for kind in ("source", "author", "tag"):
            self.assertGreater(facet_payload["facets"][kind]["total"], 0)
            self.assertTrue(facet_payload["facets"][kind]["items"])

    def test_lazy_facets_have_data(self):
        for kind in ("source", "author", "tag"):
            payload = self.request_json(f"/api/facet/{kind}?page=1&page_size=3")
            self.assertGreater(payload["total"], 0)
            self.assertTrue(payload["items"])
            self.assertLessEqual(len(payload["items"]), 3)

    def test_search_modes_return_stable_shape(self):
        for exact in (False, True):
            payload = self.request_json("/api/search", {
                "q": "手机",
                "exact": exact,
                "fulltext": True,
                "page": 1,
                "page_size": 10,
            })
            for key in ("results", "total", "page", "page_size", "index_status"):
                self.assertIn(key, payload)
            self.assertEqual(payload["page"], 1)
            self.assertLessEqual(len(payload["results"]), 10)
            for result in payload["results"]:
                self.assertTrue(all(len(snippet) <= 1000 for snippet in result.get("snippet", [])))

    def test_search_matrix_has_exact_totals_and_stable_pagination(self):
        matrix = [
            {"q": "手机", "exact": False, "fulltext": True},
            {"q": "手机", "exact": True, "fulltext": True},
            {"q": "手。机", "exact": True, "fulltext": True},
            {"q": "文化 革命", "exact": False, "fulltext": True},
            {"q": "文化革命", "exact": True, "fulltext": False},
            {"q": "手*机", "exact": True, "fulltext": True},
            {"q": "手?机", "exact": True, "fulltext": True},
        ]
        for body in matrix:
            with self.subTest(body=body):
                first = self.request_json("/api/search", {**body, "page": 1, "page_size": 10})
                repeated = self.request_json("/api/search", {**body, "page": 1, "page_size": 10})
                self.assertEqual(first["total"], repeated["total"])
                first_ids = [item["doc_id"] for item in first["results"]]
                self.assertEqual(first_ids, [item["doc_id"] for item in repeated["results"]])
                self.assertEqual(len(first_ids), len(set(first_ids)))
                if first["total"] > len(first_ids):
                    second = self.request_json("/api/search", {**body, "page": 2, "page_size": 10})
                    second_ids = [item["doc_id"] for item in second["results"]]
                    self.assertFalse(set(first_ids) & set(second_ids))

    def test_facets_compose_with_search_and_preview_download_contracts(self):
        sources = self.request_json("/api/facet/source?page=1&page_size=1")
        self.assertTrue(sources["items"])
        source = sources["items"][0]["value"]
        result = self.request_json("/api/search", {
            "q": "", "sources": [source], "page": 1, "page_size": 5, "sort": "title",
        })
        self.assertGreater(result["total"], 0)
        self.assertTrue(result["results"])
        self.assertTrue(all(item["publication_name"] == source for item in result["results"]))
        doc_id = result["results"][0]["doc_id"]
        preview = self.request_json(f"/api/preview/{doc_id}")
        self.assertEqual(preview["doc_id"], doc_id)
        self.assertIn("content", preview)
        _status, headers, body = self.request(f"/api/download/txt/{doc_id}")
        headers = {name.lower(): value for name, value in headers.items()}
        self.assertTrue(headers.get("content-type", "").startswith("text/plain"))
        self.assertIn("attachment", headers.get("content-disposition", ""))
        self.assertGreater(len(body), 0)

    def test_invalid_routes_remain_explicit(self):
        for path in ("/api/facet/not-a-facet", "/api/preview/definitely-missing-document"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as raised:
                    self.request(path)
                self.assertEqual(raised.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
