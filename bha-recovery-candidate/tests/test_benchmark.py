from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
import unittest
from datetime import datetime, timezone
from urllib.request import Request, urlopen

RESULTS_PATH = os.path.join(os.path.dirname(__file__), "benchmark-results.json")
LIVE_URL = os.environ.get("BHA_LIVE_BASE_URL", "").rstrip("/")


def _git_sha():
    try:
        return subprocess.run(
            ["git", "log", "-1", "--format=%H"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _save_results(project, queries):
    existing = {"results": []}
    if os.path.isfile(RESULTS_PATH):
        try:
            with open(RESULTS_PATH) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    existing.setdefault("results", []).append({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": project,
        "git_sha": _git_sha(),
        "queries": queries,
    })
    existing["results"] = existing["results"][-50:]
    with open(RESULTS_PATH, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


# ---- local-only imports ----
os.environ.setdefault("APP_ROOT", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from app.search_logic import build_query, SearchRequest
    from app.data_loader import search_words
except ModuleNotFoundError:
    build_query = None
    SearchRequest = None
    search_words = None


# ====== Local benchmarks (tokenize + query DSL) ======

@unittest.skipIf(build_query is None or search_words is None,
                 "BHA app dependencies unavailable")
class BhaLocalBenchmarkTests(unittest.TestCase):
    _results: list = []

    def _bench(self, label, fn, iterations=10):
        times = []
        for _ in range(iterations):
            start = time.perf_counter()
            fn()
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)
        median = statistics.median(times)
        mn = min(times)
        mx = max(times)
        print(f"  {label:<46s} median={median:>8.3f}ms  min={mn:>8.3f}ms  max={mx:>8.3f}ms  (n={iterations})")
        self._results.append({
            "label": label.strip(),
            "iterations": iterations,
            "median_ms": round(median, 3),
            "min_ms": round(mn, 3),
            "max_ms": round(mx, 3),
        })

    def test_tokenize_benchmarks(self):
        queries = ["手机", "文化革命", "文化 革命", "手。机", "手*机", "手?机", "文", "ABC"]
        for q in queries:
            self._bench(f'"{q}" tokenize', lambda q=q: search_words(q))

    def test_query_build_benchmarks(self):
        cases = [
            ("normal", SearchRequest(q="手机", exact=False, fulltext=True)),
            ("exact", SearchRequest(q="手机", exact=True, fulltext=True)),
            ("exact wildcard", SearchRequest(q="手*机", exact=True, fulltext=True)),
            ("normal no fulltext", SearchRequest(q="手机", exact=False, fulltext=False)),
            ("with date sort", SearchRequest(q="文化革命", exact=False, fulltext=True, sort="date_desc")),
            ("with source filter", SearchRequest(q="手机", exact=False, fulltext=True, sources=["source-1"])),
        ]
        for label, req in cases:
            self._bench(f'"{req.q}" {label} build_query', lambda req=req: build_query(req))

    @classmethod
    def tearDownClass(cls):
        if cls._results:
            _save_results("bha-search-lite-local", cls._results)


# ====== Live HTTP benchmarks (requires BHA_LIVE_BASE_URL) ======

@unittest.skipUnless(LIVE_URL, "set BHA_LIVE_BASE_URL to benchmark real HTTP search")
class BhaLiveBenchmarkTests(unittest.TestCase):
    _results: list = []

    def _request_json(self, path, payload=None, timeout=60):
        body = json.dumps(payload).encode() if payload else None
        req = Request(LIVE_URL + path, data=body,
                      headers={"Content-Type": "application/json"} if body else {})
        start = time.perf_counter()
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        elapsed = (time.perf_counter() - start) * 1000
        return data, elapsed

    def _bench(self, label, path, payload=None, iterations=5):
        times = []
        for i in range(iterations + 1):
            if i == 0:
                self._request_json(path, payload)
                continue
            _data, elapsed = self._request_json(path, payload)
            times.append(elapsed)
        median = statistics.median(times)
        mn = min(times)
        mx = max(times)
        print(f"  {label:<46s} median={median:>8.3f}ms  min={mn:>8.3f}ms  max={mx:>8.3f}ms  (n={iterations})")
        self._results.append({
            "label": label.strip(),
            "iterations": iterations,
            "median_ms": round(median, 3),
            "min_ms": round(mn, 3),
            "max_ms": round(mx, 3),
        })

    def test_live_search_benchmarks(self):
        self._bench('"手机" normal', "/api/search", {"q": "手机"})
        self._bench('"手机" exact', "/api/search", {"q": "手机", "exact": True})
        self._bench('"文化革命" exact', "/api/search", {"q": "文化革命", "exact": True})
        self._bench('"文化 革命" normal', "/api/search", {"q": "文化 革命"})
        self._bench('"手。机" exact', "/api/search", {"q": "手。机", "exact": True})
        self._bench('"手*机" exact', "/api/search", {"q": "手*机", "exact": True})
        self._bench('"手?机" exact', "/api/search", {"q": "手?机", "exact": True})
        self._bench('"文" exact', "/api/search", {"q": "文", "exact": True})
        self._bench('"ABC" normal', "/api/search", {"q": "ABC"})
        self._bench('health', "/api/health")
        self._bench('sources', "/api/sources")

    @classmethod
    def tearDownClass(cls):
        if cls._results:
            _save_results("bha-search-lite-live", cls._results)


# ===== Browser benchmarks (Playwright, production URL) =====

BHA_LIVE_URL = os.environ.get("BHA_LIVE_BASE_URL", "").rstrip("/")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright and BHA_LIVE_URL,
                     "set BHA_LIVE_BASE_URL and install requirements-test.txt, playwright install chromium")
class BhaBrowserBenchmarkTests(unittest.TestCase):
    _results: list = []

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])

    @classmethod
    def tearDownClass(cls):
        if cls._results:
            _save_results("bha-search-lite-browser", cls._results)
        cls.browser.close()
        cls.playwright.stop()

    def _bench_search(self, label, query, iterations=3):
        ctx = self.browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        times = []
        for i in range(iterations + 1):
            page.goto(BHA_LIVE_URL + "/")
            page.wait_for_selector("#results-list .result-item", timeout=30000)
            inp = page.locator("#search-input")
            start = time.perf_counter()
            inp.fill(query)
            page.wait_for_function(f"""
                location.hash.includes('q=' + encodeURIComponent({json.dumps(query)}))
            """, timeout=60000)
            elapsed = (time.perf_counter() - start) * 1000
            if i > 0:
                times.append(elapsed)
        ctx.close()
        _print_and_collect(self._results, label, iterations, times)

    def test_browser_search_benchmarks(self):
        self._bench_search('"手机" normal', "手机")
        self._bench_search('"文化革命" exact', "文化革命")
        self._bench_search('"手*机" exact', "手*机")
