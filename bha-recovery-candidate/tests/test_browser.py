import json
import os
import re
import base64
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


BASE_DIR = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def minimal_pdf():
    stream = b"BT /F1 18 Tf 20 100 Td (BHA PDF) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(payload)


def range_pdf():
    """Three distinct pages with enough padding to require follow-up ranges."""
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [4 0 R 7 0 R 10 0 R] /Count 3 >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, number in enumerate((4, 7, 10), 1):
        stream = f"BT /F1 18 Tf 20 100 Td (BHA page {index}) Tj ET".encode()
        objects[number] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
                           f"/Resources << /Font << /F1 3 0 R >> >> /Contents {number + 1} 0 R >>").encode()
        objects[number + 1] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        padding = b"x" * 600000
        objects[number + 2] = b"<< /Length " + str(len(padding)).encode() + b" >>\nstream\n" + padding + b"\nendstream"
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in sorted(objects.items()):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(payload)


@unittest.skipUnless(sync_playwright, "install requirements-test.txt and run: playwright install chromium")
class BrowserInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        env = {**os.environ, "APP_ROOT": str(BASE_DIR), "DATA_ROOT": str(BASE_DIR / "data")}
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(cls.port), "--log-level", "warning"],
            cwd=BASE_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(80):
            try:
                with urlopen(cls.base_url + "/", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        else:
            cls.server.terminate()
            raise RuntimeError("BHA browser test server did not start")
        cls.playwright = sync_playwright().start()
        browser = getattr(cls.playwright, os.environ.get("BHA_TEST_BROWSER", "chromium"))
        cls.browser = browser.launch(headless=True, args=["--no-sandbox"] if browser == cls.playwright.chromium else [])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()

    def open_page(self, viewport=None, preview_parts=None, track_empty=False, bootstrap_total=1, preview_text=None, speech_content=None,
                  pdf_bytes=None, track_workers=False):
        context = self.browser.new_context(viewport=viewport or {"width": 1440, "height": 900}, accept_downloads=True)
        page = context.new_page()
        errors = []
        if track_workers:
            page.add_init_script("""(() => {
              const NativeWorker = window.Worker;
              window.__pdfWorkers = [];
              window.Worker = class extends NativeWorker {
                constructor(url, options) {
                  super(url, options);
                  const worker = {url:String(url), errors:[], terminated:0};
                  window.__pdfWorkers.push(worker);
                  this.addEventListener('error', event => worker.errors.push(event.message));
                  const terminate = this.terminate.bind(this);
                  this.terminate = () => { worker.terminated++; return terminate(); };
                }
              };
            })();""")
        if track_empty:
            page.add_init_script("""
              window.__emptyStateWasVisible = false;
              document.addEventListener("DOMContentLoaded", () => {
                const empty = document.querySelector("#empty-state");
                new MutationObserver(() => {
                  if (getComputedStyle(empty).display !== "none") window.__emptyStateWasVisible = true;
                }).observe(empty, { attributes: true, attributeFilter: ["style", "hidden"] });
              });
            """)
        page.on("pageerror", lambda error: errors.append(str(error)))
        results = [{
            "doc_id": "doc-1", "title": "手机资料", "title_highlight": "<mark>手机</mark>资料",
            "publication_name": "测试刊物", "publication_type": "期刊", "authors": ["作者甲"],
            "tag_names": ["标签甲"], "date_display": ["1966/5/16"], "source_file_count": 0,
            "snippet": ["这是<mark>手机</mark>正文"], "score": 1,
        }]
        preview = {
            "doc_id": "doc-1", "title": "手机资料", "publication_name": "测试刊物",
            "content_hash": "a" * 64,
            "publication_author": "出版者", "publication_type": "期刊", "authors": ["作者甲"], "tag_names": ["标签甲"],
            "date_display": ["1966/5/16"], "source_file_count": 1, "content": preview_text if preview_text is not None else "这是手机正文", "variants": [],
            "article_id": "article-1", "publication_id": "publication-1", "archive_id": 1,
            "source_files": [{"index": 0, "name": "原稿.pdf", "url": "https://example.com/source.pdf"}],
            "article": {"title": "手机资料", "authors": ["作者甲"], "dates": [{"year": 1966, "month": 5, "day": 16}], "tags": [{"type": "主题/事件", "name": "标签甲"}, {"name": "标签乙"}], "page_start": 2, "page_end": 2, "description": "", "parts": preview_parts or [{"type": "paragraph", "text": "这是手机正文"}], "comments": [], "comment_pivots": []},
        }
        if speech_content is not None:
            preview["content"] = speech_content
        pdf = minimal_pdf() if pdf_bytes is None else pdf_bytes

        def route_api(route, request):
            path = request.url.split(self.base_url, 1)[-1]
            if path.startswith("/api/bootstrap"):
                facet = lambda name: {"items": [{"name": name, "value": name, "count": 1}], "total": 1, "page": 1, "page_size": 200, "has_more": False}
                payload = {
                    "generation": "test-generation",
                    "search": {"results": results, "total": bootstrap_total, "page": 1, "page_size": 20, "indexing": False, "index_status": "ready"},
                    "sources": {"archives": [{"name": "3", "value": 3, "count": 1}], "types": [{"name": "期刊", "value": "期刊", "count": 1}], "years": [{"name": "1966", "value": 1966, "count": 0}]},
                    "facets": {"source": facet("测试刊物"), "author": facet("作者甲"), "tag": facet("标签甲")},
                }
            elif path.startswith("/api/search"):
                body = request.post_data_json or {}
                payload = {"results": results, "total": bootstrap_total, "page": body.get("page", 1), "page_size": body.get("page_size", 20), "indexing": False, "index_status": "ready"}
            elif path.startswith("/api/sources"):
                payload = {"sources": [], "authors": [], "tags": [], "archives": [{"name": "3", "value": 3, "count": 1}], "types": [{"name": "期刊", "value": "期刊", "count": 1}], "years": [{"name": "1966", "value": 1966, "count": 0}], "indexing": False, "index_status": "ready"}
            elif path.startswith("/api/facet/"):
                kind = path.split("/api/facet/", 1)[1].split("?", 1)[0]
                name = {"source": "测试刊物", "author": "作者甲", "tag": "标签甲"}[kind]
                has_query = bool(parse_qs(urlparse(path).query).get("q"))
                show_item = has_query or kind != "tag"
                payload = {"items": [{"name": name, "value": name, "count": 1}] if show_item else [], "total": 1 if show_item else 0, "page": 1, "page_size": 500 if has_query else 200, "has_more": False, "indexing": False, "index_status": "ready"}
            elif path.startswith("/api/preview/source/"):
                match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("range", ""))
                if not match:
                    route.fulfill(status=416, body=b"")
                    return
                begin = int(match.group(1))
                end = min(int(match.group(2)), len(pdf) - 1)
                chunk = pdf[begin:end + 1]
                route.fulfill(status=206, body=chunk, headers={
                    "Content-Type": "application/pdf",
                    "Content-Range": f"bytes {begin}-{end}/{len(pdf)}",
                    "Content-Length": str(len(chunk)),
                    "Accept-Ranges": "bytes",
                })
                return
            elif path.startswith("/api/preview/") or path.startswith("/api/random"):
                payload = preview
            elif path.startswith("/api/health"):
                payload = {"ok": True, "es": True, "index_ready": True, "index_status": "ready", "document_count": 1, "progress": None}
            elif path.startswith("/api/download/txt/"):
                route.fulfill(status=200, body="标题：手机资料\n\n这是手机正文", headers={"Content-Type": "text/plain; charset=utf-8", "Content-Disposition": "attachment; filename*=UTF-8''test.txt"})
                return
            else:
                payload = {}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

        page.route("**/api/**", route_api)
        # Most cases mock APIs and inspect a single isolated page; worker
        # lifecycle is covered explicitly by the root-scope test below.
        context.add_init_script("delete Navigator.prototype.serviceWorker")
        page.goto(self.base_url, wait_until="domcontentloaded")
        page.locator(".result-item").wait_for(timeout=15000)
        return context, page, errors

    def test_selection_ignores_malformed_article_parts_and_pivots(self):
        for invalid in ({"parts": [None]}, {"parts": [{"text": "这是手机正文"}], "comment_pivots": [None]},
                        {"parts": [{"text": "这是手机正文"}], "comment_pivots": [{"part_idx": {}, "offset": 1, "index": 1}]}):
            with self.subTest(article=invalid):
                context, page, errors = self.open_page()
                try:
                    page.locator("[data-preview]").click()
                    page.locator("[data-proofread-select]").wait_for()
                    page.evaluate("article => { getCachedPreview(STATE.preview).article = article; }", invalid)
                    page.locator("[data-proofread-select]").evaluate("node => { const range = document.createRange(); range.selectNodeContents(node); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); node.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })); }")
                    self.assertEqual(page.locator("[data-proofread-form]").count(), 0)
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_delegated_handlers_ignore_non_element_targets_and_malformed_rows(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""() => {
                let calls = 0;
                for (const root of [DOM.results,DOM.previewPanel]) {
                  for (const type of ['click','keydown','pointerdown','change','input','submit','mouseup']) {
                    const text = document.createTextNode('文本目标'); root.append(text);
                    const event = type === 'keydown' ? new KeyboardEvent(type,{bubbles:true,key:'Enter'}) :
                      type === 'pointerdown' ? new PointerEvent(type,{bubbles:true}) : new Event(type,{bubbles:true});
                    text.dispatchEvent(event); text.remove(); calls += 1;
                  }
                }
                const prior = STATE.sources.slice();
                const malformed = document.createElement('span'); malformed.className='path-folder';
                malformed.dataset.source=''; DOM.results.append(malformed); malformed.click(); malformed.remove();
                return {calls,unchanged:JSON.stringify(STATE.sources) === JSON.stringify(prior)};
            }""")
            self.assertEqual(result, {"calls": 14, "unchanged": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_required_dom_bindings_validate_actual_controls(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""() => {
                const valid = requireDom('search-input', HTMLInputElement) === DOM.searchInput;
                let missing = false, wrongType = false;
                try { requireDom('missing-required-control', HTMLElement); } catch (_) { missing = true; }
                try { requireDom('search-input', HTMLButtonElement); } catch (_) { wrongType = true; }
                return {valid, missing, wrongType};
            }""")
            self.assertEqual(result, {"valid": True, "missing": True, "wrongType": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_client_generated_utf16_delta_matches_backend(self):
        from app.proofread_core import apply_text_delta, delta_edit_cost

        context, page, errors = self.open_page()
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            page.locator("[data-proofread-form]").wait_for(state="visible")
            page.wait_for_function("typeof window.diff_match_patch === 'function'")
            cases = [("甲😀乙", "甲𠮷%\t乙"), ("旧文。\n新文", "新文\n旧文。"),
                     ("繁體中文", "繁體+中文"), ("短句", ""), ("", "新😀文")]
            generated = page.evaluate("""cases => cases.map(([oldText, newText]) => ({
                delta: textDelta(oldText, newText),
                engine: typeof window.diff_match_patch === 'function'}))""", cases)
            for (old_text, new_text), entry in zip(cases, generated):
                with self.subTest(old=old_text, new=new_text):
                    self.assertTrue(entry["engine"])
                    self.assertIsNotNone(delta_edit_cost(entry["delta"], old_text))
                    self.assertEqual(apply_text_delta(old_text, entry["delta"]), new_text)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_preview_highlight_escapes_markup_and_matches_literal_query(self):
        for viewport in ({"width": 1440, "height": 900}, {"width": 390, "height": 844}):
            with self.subTest(viewport=viewport):
                context, page, errors = self.open_page(viewport, preview_text='<img src=x onerror="alert(1)"> 手机正文')
                try:
                    page.evaluate("STATE.q='手机';STATE.exact=true;STATE.previewHighlight=true")
                    if page.locator("body").evaluate("node => node.classList.contains('mobile')") and page.locator("#left-sidebar").evaluate("node => node.classList.contains('open')"):
                        page.locator("#left-toggle").click()
                    page.locator("[data-preview]").first.click()
                    body = page.locator(".preview-body")
                    body.wait_for()
                    self.assertEqual(body.locator("img").count(), 0)
                    self.assertEqual(body.locator("mark").first.text_content(), "手机")
                    self.assertIn("<img", body.text_content())
                    self.assertEqual(errors, [])
                finally:
                    context.close()

    def test_native_reader_plays_bha_preview_and_survives_mode_changes(self):
        speech = "https://vomebook-hitokoto.hf.space"
        frame = bytes.fromhex("fffb9064") + bytes(413)
        mp3 = frame * 58  # ~1.5 s of silent MPEG-1 Layer III; no model/network.
        text = "".join(f"第{index}段，这是一篇用于朗读测试的文章。手机和文化都要读出来。\n" for index in range(40))
        posts, deleted = [], []
        for mobile in (False, True):
            with self.subTest(mobile=mobile):
                context, page, errors = self.open_page({"width": 390, "height": 844} if mobile else None, speech_content=text)
                page.add_init_script("new MutationObserver(()=>{const a=document.querySelector('#speech-audio');if(a)a.muted=true;}).observe(document,{childList:true,subtree:true});")
                page.evaluate("new MutationObserver(()=>{const a=document.querySelector('#speech-audio');if(a)a.muted=true;}).observe(document,{childList:true,subtree:true});")
                headers = {"Access-Control-Allow-Origin": self.base_url, "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS"}
                jobs = {}

                def route_speech(route, request):
                    path = urlparse(request.url).path
                    if request.method == "OPTIONS":
                        route.fulfill(status=200, headers=headers); return
                    if path == "/tts/jobs" and request.method == "POST":
                        body = request.post_data_json
                        posts.append(body)
                        job_id = f"{len(posts):032d}"
                        spans = list(zip([0] + body["segment_ends"][:-1], body["segment_ends"]))
                        jobs[job_id] = {"id": job_id, "state": "ready", "result": {"pitch_semitones": body["pitch_semitones"]},
                                        "progress": {"total_parts": len(spans), "parts": [
                                            {"index": index, "start": begin, "end": end, "bytes": len(mp3), "pitch_semitones": body["pitch_semitones"],
                                             "audio_url": f"/tts/audio/{job_id}/parts/{index}.mp3"} for index, (begin, end) in enumerate(spans)]}}
                        route.fulfill(status=202, content_type="application/json", body=json.dumps(jobs[job_id]), headers=headers); return
                    if path.startswith("/tts/jobs/"):
                        job_id = path.rsplit("/", 1)[-1]
                        if request.method == "DELETE": deleted.append(job_id)
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(jobs.get(job_id, {})), headers=headers); return
                    if path.startswith("/tts/audio/"):
                        route.fulfill(status=200, content_type="audio/mpeg", body=mp3, headers=headers); return
                    route.fulfill(status=404, body="{}", headers=headers)

                page.route(speech + "/**", route_speech)
                if page.locator("body").evaluate("node => node.classList.contains('mobile')") and page.locator("#left-sidebar").evaluate("node => node.classList.contains('open')"):
                    page.locator("#left-toggle").click()
                page.locator("[data-preview]").first.click()
                page.locator(".preview-body").wait_for()
                button = page.locator("[data-speech-toggle]")
                button.wait_for()
                before = len(posts)
                button.click()
                page.wait_for_function("document.querySelector('#speech-audio')?.currentTime>0.05&&!document.querySelector('#speech-audio').paused", polling=100, timeout=15000)
                self.assertGreater(len(posts), before)
                first = posts[before]
                self.assertEqual(first["voice"], "zf_001")
                self.assertEqual(first["unsupported_policy"], "pause")
                self.assertTrue(text.startswith(first["text"]))
                # First batch: a short first clip, then a ramp of growing clips.
                self.assertLessEqual(len(first["text"]), 260)
                self.assertLessEqual(first["segment_ends"][0], 32)
                self.assertTrue(page.locator("#speech-panel").is_visible())
                self.assertTrue(page.evaluate("document.querySelector('#preview-panel').contains(document.querySelector('#speech-panel'))"))
                # Next sentence plays from memory: no resynthesis, only the
                # speculative following batch (context = this batch's end).
                count = len(posts)
                page.locator("#speech-next").click()
                page.wait_for_function("!document.querySelector('#speech-audio').paused", polling=100)
                for later in posts[count:]:
                    self.assertTrue(later["context_before"] and first["text"].endswith(later["context_before"]), later["text"][:40])
                # A double-click reads that sentence; it must not open the proofreading editor.
                box = page.locator(".preview-body").evaluate("n=>{const w=document.createTreeWalker(n,NodeFilter.SHOW_TEXT);const t=w.nextNode();const r=document.createRange();r.setStart(t,2);r.setEnd(t,3);const b=r.getBoundingClientRect();return {x:b.x+b.width/2,y:b.y+b.height/2};}")
                page.mouse.dblclick(box["x"], box["y"])
                page.wait_for_timeout(400)
                self.assertFalse(page.evaluate("STATE.proofread"))
                self.assertEqual(page.locator("form[data-proofread-form]").count(), 0)
                # Closing the preview keeps playback and keeps the panel out of the cleared DOM.
                page.locator("[data-close-preview]").first.click()
                page.wait_for_function("!STATE.preview")
                self.assertTrue(page.evaluate("document.querySelector('#speech-panel')?.isConnected===true"))
                self.assertFalse(page.locator("#speech-audio").evaluate("n=>n.paused"))
                page.locator("#search-input").fill("文化")
                page.wait_for_function("STATE.q==='文化'")
                page.evaluate("document.querySelector('#speech-panel').hidden=false")
                page.locator("#speech-stop").click()
                self.assertTrue(page.locator("#speech-audio").evaluate("n=>n.paused"))
                self.assertFalse(errors, errors)
                context.close()
        self.assertTrue(deleted, "stopping must release owned TTS jobs")

    def test_default_screen_hydrates_then_prefetches_the_next_page(self):
        context, page, errors = self.open_page(track_empty=True, bootstrap_total=21)
        try:
            page.wait_for_function("""performance.getEntriesByType('resource')
                .some((entry) => new URL(entry.name).pathname === '/api/search')""")
            page.wait_for_function("document.querySelectorAll('#source-filter [data-source]').length > 0")
            api_paths = page.evaluate("""performance.getEntriesByType('resource')
                .map((entry) => new URL(entry.name).pathname).filter((path) => path.startsWith('/api/'))""")
            # The first screen only needs the core bootstrap; facets arrive in a
            # separate request once the results are rendered.
            self.assertEqual(api_paths[0], "/api/ping")
            self.assertEqual(api_paths[1], "/api/bootstrap")
            self.assertIn("/api/bootstrap/facets", api_paths)
            self.assertIn("/api/sources", api_paths)
            self.assertNotIn("/api/facet/source", api_paths)
            self.assertEqual(page.locator("#source-filter [data-source]").count(), 1)
            self.assertFalse(page.evaluate("window.__emptyStateWasVisible"))
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_late_bootstrap_does_not_replace_or_cache_over_user_search(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                let release;
                window.__bhaBootstrapPromise = new Promise(resolve => { release = resolve; });
                const bootstrap = loadInitialBootstrap();
                STATE.q = 'new-query';
                await doSearch();
                release({search: {results: [{doc_id: 'stale', title: '旧首页'}], total: 999,
                    page_size: 20, index_status: 'ready', indexing: false}, sources: {}, facets: {}});
                await bootstrap;
                const cached = getCachedSearch(stableStringify(searchPayload()));
                return {id: STATE.results[0].doc_id, total: STATE.total,
                    cachedId: cached.results[0].doc_id, pending: bootstrapPending};
            }""")
            self.assertEqual(result, {"id": "doc-1", "total": 1, "cachedId": "doc-1", "pending": False})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_failed_bootstrap_does_not_restart_newer_search(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                let fail;
                window.__bhaBootstrapPromise = new Promise((resolve, reject) => { fail = reject; });
                const bootstrap = loadInitialBootstrap();
                STATE.q = 'new-query';
                await doSearch();
                const sequence = searchRequestSeq;
                fail(new Error('bootstrap failed'));
                await bootstrap;
                return {unchanged: sequence === searchRequestSeq, pending: bootstrapPending};
            }""")
            self.assertEqual(result, {"unchanged": True, "pending": False})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_malformed_search_does_not_replace_or_cache_valid_results(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const previousId = STATE.results[0].doc_id;
                const original = window.fetch;
                STATE.q = 'malformed-search';
                const key = stableStringify(searchPayload());
                window.fetch = async () => ({ok: true, json: async () => ({
                    results: [{doc_id: 42}], total: 1, page: 1, page_size: 20,
                    index_status: 'ready', indexing: false})});
                try { await doSearch(); } finally { window.fetch = original; }
                return {preserved: STATE.results[0].doc_id === previousId,
                    cached: getCachedSearch(key) !== null, status: DOM.statusText.textContent};
            }""")
            self.assertEqual(result, {"preserved": True, "cached": False, "status": "搜索失败"})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_malformed_optional_facet_bootstrap_preserves_source_options(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                apiWithTimeout = async path => path === '/api/sources'
                    ? {sources: [], authors: [], tags: [], archives: [{name:'5',value:5,count:1}],
                       types: [{name:'书',value:'书',count:1}], years: [], indexing:false,index_status:'ready'}
                    : {generation:'bad', facets:{source: {items:'invalid'}}};
                try { await loadFacetsNow(); } finally { apiWithTimeout = original; }
                return {archive: STATE.facets.archives[0]?.name,
                    rendered: DOM.archiveFilter.textContent.includes('5'),
                    failure: DOM.sourceFilter.textContent.includes('加载失败')};
            }""")
            self.assertEqual(result, {"archive": "5", "rendered": True, "failure": False})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_legacy_source_list_is_accepted_with_optional_facets_missing(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                apiWithTimeout = async path => path === '/api/sources'
                    ? [{name:'旧来源', value:'旧来源', count:1}] : null;
                try { await loadFacetsNow(); } finally { apiWithTimeout = original; }
                return {name:STATE.facets.sources[0].name,
                    rendered:DOM.sourceFilter.textContent.includes('旧来源')};
            }""")
            self.assertEqual(result, {"name": "旧来源", "rendered": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_search_body_timeout_shows_failure_and_allows_retry(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const originalFetch = window.fetch;
                const originalTimeout = window.setTimeout;
                window.setTimeout = (fn, ms, ...args) => originalTimeout(fn, ms === 25000 ? 30 : ms, ...args);
                window.fetch = (url, options) => Promise.resolve({ok: true,
                    json: () => new Promise((resolve, reject) => {
                        options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), {once: true});
                    })});
                STATE.q = 'timeout-query';
                await doSearch();
                const status = DOM.statusText.textContent;
                const released = searchAbortController === null;
                window.fetch = originalFetch;
                window.setTimeout = originalTimeout;
                await doSearch();
                return {status, released, retried: DOM.statusText.textContent === '' && STATE.results[0].doc_id === 'doc-1'};
            }""")
            self.assertEqual(result, {"status": "搜索超时，请重试", "released": True, "retried": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_superseded_search_cancellation_is_silent(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const originalFetch = window.fetch;
                const toasts = [];
                showToast = message => toasts.push(message);
                window.fetch = (url, options) => Promise.resolve({ok: true,
                    json: () => new Promise((resolve, reject) => {
                        if (options.signal.aborted) reject(new DOMException('aborted', 'AbortError'));
                        else options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), {once: true});
                    })});
                STATE.q = 'old-query';
                const oldSearch = doSearch();
                window.fetch = originalFetch;
                STATE.q = 'new-query';
                await doSearch();
                await oldSearch;
                return {status: DOM.statusText.textContent, toasts, released: searchAbortController === null};
            }""")
            self.assertEqual(result, {"status": "", "toasts": [], "released": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_facet_keyword_round_trip_does_not_reuse_cancelled_request(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const requests = [];
                apiWithTimeout = (url, options) => new Promise(resolve => requests.push({url, resolve, signal: options.signal}));
                STATE.filterSearch.source = 'A';
                const first = loadFacetPage('source');
                STATE.filterSearch.source = 'B';
                const second = loadFacetPage('source');
                STATE.filterSearch.source = 'A';
                const third = loadFacetPage('source');
                const atRoundTrip = requests.length;
                requests[0].resolve({items: [{name: 'old A', count: 1}], total: 1, page: 1, page_size: 200, has_more: false});
                await first;
                const sameCurrentRequest = loadFacetPage('source');
                for (const request of requests.slice(1)) {
                    request.resolve({items: [{name: request.url.endsWith('q=A') ? 'new A' : 'B', count: 1}], total: 1, page: 1, page_size: 200, has_more: false});
                }
                await Promise.all([second, third, sameCurrentRequest]);
                return {atRoundTrip, requests: requests.length, name: STATE.facets.sources[0].name,
                    pending: facetPending.size, released: facetAbortControllers.source === null};
            }""")
            self.assertEqual(result, {"atRoundTrip": 3, "requests": 3, "name": "new A", "pending": 0, "released": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_malformed_random_response_does_not_open_preview(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout; const messages = [];
                showToast = text => messages.push(text);
                apiWithTimeout = async () => ({doc_id:{toString(){return 'doc-1'}}});
                try { await openRandomPreview(); } finally { apiWithTimeout = original; }
                return {preview:STATE.preview, messages};
            }""")
            self.assertEqual(result, {"preview": "", "messages": ["随机文章加载失败"]})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_malformed_preview_is_not_cached_or_rendered(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                const messages = [];
                showToast = message => messages.push(message);
                apiWithTimeout = async () => ({doc_id: 'doc-1', content: '<img src=x onerror=alert(1)>', article: []});
                try { await openPreview('doc-1'); } finally { apiWithTimeout = original; }
                return {cached: getCachedPreview('doc-1') !== null, visible: !DOM.previewPanel.hidden,
                    injected: DOM.previewPanel.querySelectorAll('img').length, messages};
            }""")
            self.assertEqual(result, {"cached": False, "visible": False, "injected": 0, "messages": ["预览失败"]})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_malformed_comparison_keeps_original_preview(self):
        context, page, errors = self.open_page()
        try:
            page.locator('[data-preview="doc-1"]').click()
            page.locator('.preview-body').wait_for()
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                const messages = [];
                const text = DOM.previewPanel.querySelector('.preview-body').textContent;
                showToast = message => messages.push(message);
                apiWithTimeout = async () => ({...getCachedPreview('doc-1'), doc_id: 'unexpected'});
                try { await comparePreview('doc-2'); } finally { apiWithTimeout = original; }
                return {preserved: DOM.previewPanel.querySelector('.preview-body').textContent === text,
                    cached: getCachedPreview('doc-2') !== null, messages};
            }""")
            self.assertEqual(result, {"preserved": True, "cached": False, "messages": ["对比加载失败"]})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_motion_handlers_clean_up_and_honor_reduced_motion(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""() => {
                const original = window.matchMedia;
                const row = document.createElement('div');
                window.matchMedia = () => ({matches:false});
                animateResultRows([row]);
                const resultStarted = row.classList.contains('result-enter') && !!row.style.getPropertyValue('--result-enter-delay');
                row.dispatchEvent(new Event('animationend'));
                const resultClean = !row.classList.contains('result-enter') && !row.style.getPropertyValue('--result-enter-delay');
                animatePreviewPanel(); const previewStarted = DOM.previewPanel.classList.contains('preview-enter');
                DOM.previewPanel.dispatchEvent(new Event('animationend'));
                const previewClean = !DOM.previewPanel.classList.contains('preview-enter') && previewAnimationCleanup === null;
                window.matchMedia = () => ({matches:true});
                animateResultRows([row]); animatePreviewPanel();
                const reducedMotion = !row.classList.contains('result-enter') && !DOM.previewPanel.classList.contains('preview-enter');
                window.matchMedia = original;
                return {resultStarted,resultClean,previewStarted,previewClean,reducedMotion};
            }""")
            self.assertEqual(result, {"resultStarted": True, "resultClean": True, "previewStarted": True,
                                      "previewClean": True, "reducedMotion": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_preview_tag_pager_escapes_and_navigates(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""() => {
                const item = {publication_name: '<刊物>', authors:['A&B'],
                  tag_names: Array.from({length:17}, (_,i) => i === 0 ? '<img src=x>' : `标签${i}`)};
                DOM.previewPanel.innerHTML = previewMetaHTML(item);
                const container = DOM.previewPanel.querySelector('[data-preview-tags]');
                const next = container.querySelector('[data-preview-tag-page-change=next]');
                const previous = container.querySelector('[data-preview-tag-page-change=prev]');
                const visible = () => Array.from(container.querySelectorAll('[data-preview-tag-page]')).filter(x=>!x.hidden).length;
                const before = {visible:visible(),current:container.querySelector('[data-preview-tag-current]').textContent,
                    previousDisabled:previous.disabled, imageCount:container.querySelectorAll('img').length};
                changePreviewTagPage(next); const middle = {visible:visible(),current:container.querySelector('[data-preview-tag-current]').textContent};
                changePreviewTagPage(next); const end = {visible:visible(),current:container.querySelector('[data-preview-tag-current]').textContent,nextDisabled:next.disabled};
                changePreviewTagPage(previous); const back = {visible:visible(),current:container.querySelector('[data-preview-tag-current]').textContent};
                return {before,middle,end,back};
            }""")
            self.assertEqual(result, {"before": {"visible": 8, "current": "1", "previousDisabled": True, "imageCount": 0},
                                      "middle": {"visible": 8, "current": "2"},
                                      "end": {"visible": 1, "current": "3", "nextDisabled": True},
                                      "back": {"visible": 8, "current": "2"}})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_late_comparison_cannot_replace_proofread_draft(self):
        context, page, errors = self.open_page()
        try:
            page.locator('[data-preview="doc-1"]').click()
            page.locator(".preview-body").wait_for()
            result = page.evaluate("""async () => {
                const item = getCachedPreview('doc-1');
                let release;
                apiWithTimeout = () => new Promise(resolve => { release = resolve; });
                const comparing = comparePreview('doc-2');
                openProofread({...item, source_files: [], source_file_count: 0});
                DOM.previewPanel.querySelector('[data-fulltext-input]').value = '尚未保存的修改';
                release({...item, doc_id: 'doc-2'});
                await comparing;
                return {proofread: DOM.previewPanel.classList.contains('proofread-mode'),
                    text: DOM.previewPanel.querySelector('[data-fulltext-input]')?.value || null};
            }""")
            self.assertEqual(result, {"proofread": True, "text": "尚未保存的修改"})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_malformed_parse_queue_does_not_replace_existing_results(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                STATE.parseMode = true;
                STATE.parseItems = [{doc_id:'old',title:'旧条目',archive_id:25,publication_name:null,page_start:1,page_end:1,date_display:[],authors:[],source_files:[]}];
                STATE.parseTotal = 1; const original = apiWithTimeout; const toasts = [];
                showToast = text => toasts.push(text);
                apiWithTimeout = async () => ({page:1,page_size:100,total:1,items:[{doc_id:'bad',source_files:['ok',3]}]});
                try { await loadParseQueue(); } finally { apiWithTimeout = original; }
                return {id:STATE.parseItems[0].doc_id,total:STATE.parseTotal,status:DOM.statusText.textContent,toasts};
            }""")
            self.assertEqual(result, {"id": "old", "total": 1, "status": "待解析队列加载失败",
                                      "toasts": ["待解析队列加载失败：待解析队列响应格式无效"]})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_parse_queue_uses_latest_page_response(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                STATE.parseMode = true;
                const requests = [];
                apiWithTimeout = () => new Promise(resolve => requests.push(resolve));
                STATE.parsePage = 1;
                const first = loadParseQueue();
                STATE.parsePage = 2;
                const second = loadParseQueue();
                const item = (doc_id,title) => ({doc_id,title,archive_id:25,publication_name:null,page_start:1,page_end:1,date_display:[],authors:[],source_files:[]});
                requests[1]({total: 200,page:2,page_size:100,items: [item('second','第二页')]});
                await second;
                requests[0]({total: 200,page:1,page_size:100,items: [item('first','第一页')]});
                await first;
                return {id: STATE.parseItems[0].doc_id, page: DOM.pageInput.value,
                    visible: DOM.results.textContent.includes('第二页')};
            }""")
            self.assertEqual(result, {"id": "second", "page": "2", "visible": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_old_parse_queue_failure_does_not_affect_search_mode(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const originalApi = apiWithTimeout;
                const toasts = [];
                showToast = message => toasts.push(message);
                let fail;
                apiWithTimeout = () => new Promise((resolve, reject) => { fail = reject; });
                STATE.parseMode = true;
                const pending = loadParseQueue();
                STATE.parseMode = false;
                apiWithTimeout = originalApi;
                STATE.q = 'new-search';
                await doSearch();
                fail(new Error('late queue failure'));
                await pending;
                return {status: DOM.statusText.textContent, toasts};
            }""")
            self.assertEqual(result, {"status": "", "toasts": []})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_old_search_cannot_replace_parse_queue(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const requests = [];
                apiWithTimeout = () => new Promise(resolve => requests.push(resolve));
                STATE.q = 'old-search';
                const search = doSearch();
                STATE.parseMode = true;
                const queue = loadParseQueue();
                requests[1]({total:1,page:1,page_size:100,items:[{doc_id:'queue',title:'待解析刊物',archive_id:25,publication_name:null,page_start:1,page_end:1,date_display:[],authors:[],source_files:[]}]});
                await queue;
                const html = DOM.results.innerHTML;
                requests[0]({results: [], total: 0, index_status: 'ready', indexing: false, page_size: 20});
                await search;
                return {preserved: DOM.results.innerHTML === html, status: DOM.statusText.textContent};
            }""")
            self.assertEqual(result, {"preserved": True, "status": "Archive 25"})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_malformed_ocr_page_is_not_cached(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                apiWithTimeout = async () => ({ocr_results:[{text:'bad',box:[null]}]});
                let rejected = false;
                try { await parseOCRPage({doc_id:'bad-ocr'},1); } catch (_) { rejected = true; }
                finally { apiWithTimeout = original; }
                return {rejected, cached:parseOCRCache.has('bad-ocr:1')};
            }""")
            self.assertEqual(result, {"rejected": True, "cached": False})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_parse_ocr_races_cannot_overwrite_newer_page_or_editor(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                const requests = [];
                apiWithTimeout = () => new Promise((resolve, reject) => requests.push({resolve, reject}));
                STATE.parseMode = true;
                const item = {doc_id:'ocr-race',article:{page_end:3},source_files:[]};
                STATE.parseItem = item;
                DOM.previewPanel.innerHTML = '<div data-parse-crop-toolbar hidden></div><img data-parse-image><div data-parse-ocr></div><input data-parse-page><span data-parse-page-status></span><button data-parse-page-prev></button><button data-parse-page-next></button>';
                const text = DOM.previewPanel.querySelector('[data-parse-ocr]');
                try {
                    const first = loadParsePage(1);
                    const second = loadParsePage(2);
                    requests[1].resolve({ocr_results:[{text:'第二页'}]});
                    await second;
                    requests[0].reject(new Error('旧页面失败'));
                    await first;
                    const afterOldFailure = text.textContent;

                    const oldSame = loadParsePage(3);
                    const newSame = updateParsePageOCR();
                    requests[3].resolve({ocr_results:[{text:'最新裁剪'}]});
                    await newSame;
                    requests[2].resolve({ocr_results:[{text:'过期裁剪'}]});
                    await oldSame;
                    const afterOldSuccess = text.textContent;
                    const cachedAfterOldSuccess = parseOCRCache.get('ocr-race:3').ocr_results[0].text;

                    parseOCRCache.clear();
                    const oldEditor = loadParsePage(1);
                    STATE.parseItem = {...item};
                    text.textContent = '新编辑器';
                    requests[4].reject('网络异常');
                    await oldEditor;
                    const afterEditorReplacement = text.textContent;

                    const currentFailure = loadParsePage(1);
                    requests[5].reject('网络异常');
                    await currentFailure;
                    return {afterOldFailure,afterOldSuccess,cachedAfterOldSuccess,afterEditorReplacement,currentFailure:text.textContent};
                } finally { apiWithTimeout = original; }
            }""")
            self.assertEqual(result, {"afterOldFailure": "第二页", "afterOldSuccess": "最新裁剪", "cachedAfterOldSuccess": "最新裁剪",
                                      "afterEditorReplacement": "新编辑器", "currentFailure": "OCR 加载失败：未知错误"})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_parse_ocr_cache_retains_newest_success_and_cleans_failed_requests(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                const requests = [];
                apiWithTimeout = () => new Promise((resolve,reject) => requests.push({resolve,reject}));
                try {
                    const old = parseOCRPage({doc_id:'cache-race'},1);
                    const newer = parseOCRPage({doc_id:'cache-race'},1);
                    requests[1].resolve({ocr_results:[{text:'新'}]}); await newer;
                    requests[0].resolve({ocr_results:[{text:'旧'}]}); await old;
                    const latest = parseOCRCache.get('cache-race:1').ocr_results[0].text;

                    const pending = parseOCRPage({doc_id:'cache-fail'},1);
                    const failed = parseOCRPage({doc_id:'cache-fail'},1).catch(() => null);
                    requests[3].reject(new Error('失败')); await failed;
                    requests[2].resolve({ocr_results:[{text:'旧'}]}); await pending;
                    return {latest,failedCached:parseOCRCache.has('cache-fail:1'),inFlight:parseOCRRequests.size};
                } finally { apiWithTimeout = original; }
            }""")
            self.assertEqual(result, {"latest": "新", "failedCached": False, "inFlight": 0})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_parse_article_ocr_refresh_ignores_old_range_and_old_editor(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                const requests = [];
                apiWithTimeout = () => new Promise((resolve,reject) => requests.push({resolve,reject}));
                const item = {doc_id:'article-race',article:{page_end:2},source_files:[]};
                STATE.parseMode = true; STATE.parseItem = item;
                DOM.previewPanel.innerHTML = '<form data-parse-form data-max-page="2"><div data-parse-article><input data-parse-start value="1"><input data-parse-end value="1"><textarea data-parse-content></textarea><span data-parse-body-status></span></div></form>';
                const row = DOM.previewPanel.querySelector('[data-parse-article]');
                const start = row.querySelector('[data-parse-start]');
                const end = row.querySelector('[data-parse-end]');
                const text = row.querySelector('[data-parse-content]');
                const status = row.querySelector('[data-parse-body-status]');
                try {
                    const first = refreshParseArticleBody(row,true);
                    start.value='2'; end.value='2';
                    const second = refreshParseArticleBody(row,true);
                    requests[1].resolve({ocr_results:[{text:'第二页'}]}); await second;
                    requests[0].reject(new Error('旧页面失败')); await first;
                    const afterOldFailure = {text:text.value,status:status.textContent,count:row.dataset.basePartCount};

                    parseOCRCache.clear();
                    const previous = refreshParseArticleBody(row,true);
                    const latest = refreshParseArticleBody(row,true);
                    requests[3].resolve({ocr_results:[{text:'最新结果'}]}); await latest;
                    requests[2].resolve({ocr_results:[{text:'旧结果'}]}); await previous;
                    const afterOldSuccess = text.value;

                    parseOCRCache.clear();
                    const pending = refreshParseArticleBody(row,true);
                    STATE.parseItem = {...item};
                    text.value = '新编辑器'; status.textContent = '保持';
                    requests[4].reject(new Error('旧编辑器失败')); await pending;
                    return {afterOldFailure,afterOldSuccess,afterEditor:{text:text.value,status:status.textContent}};
                } finally { apiWithTimeout = original; }
            }""")
            self.assertEqual(result, {"afterOldFailure": {"text": "第二页", "status": "3 字", "count": "1"},
                                      "afterOldSuccess": "最新结果", "afterEditor": {"text": "新编辑器", "status": "保持"}})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_parse_article_rows_escape_draft_and_remote_fields(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""() => {
                const article = {title:'<标题>',authors:['<作者>'],dates:[{year:1967}],content:'<正文>',
                    page_start:'1" autofocus onfocus="alert(1)', page_end:'2" data-injected="yes'};
                DOM.previewPanel.innerHTML = parseArticleRowHTML(article,0,2);
                const start = DOM.previewPanel.querySelector('[data-parse-start]');
                const end = DOM.previewPanel.querySelector('[data-parse-end]');
                return {title:DOM.previewPanel.querySelector('[data-parse-title]').value,
                        authors:DOM.previewPanel.querySelector('[data-parse-authors]').value,
                        content:DOM.previewPanel.querySelector('[data-parse-content]').value,
                        injected:DOM.previewPanel.querySelectorAll('[autofocus],[onfocus],[data-injected]').length,
                        startType:start.type,endType:end.type};
            }""")
            self.assertEqual(result, {"title": "<标题>", "authors": "<作者>", "content": "<正文>",
                                      "injected": 0, "startType": "number", "endType": "number"})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_parse_submit_ignores_ocr_completion_after_editor_changes(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                let release;
                apiWithTimeout = () => new Promise(resolve => { release = resolve; });
                const item = {doc_id:'submit-race',article:{page_end:1},source_files:[]};
                STATE.parseMode = true; STATE.parseItem = item;
                DOM.previewPanel.innerHTML = '<form data-parse-form data-max-page="1"><div data-parse-article><input data-parse-title value="标题"><input data-parse-authors><input data-parse-year><input data-parse-month><input data-parse-day><input data-parse-start value="1"><input data-parse-end value="1"><textarea data-parse-content>正文</textarea><span data-parse-body-status></span></div><textarea data-parse-description></textarea><div data-parse-error></div><button type="submit">提交</button></form>';
                const form = DOM.previewPanel.querySelector('[data-parse-form]');
                try {
                    const pending = submitParseForm(form);
                    STATE.parseItem = {...item};
                    release({ocr_results:[{text:'旧 OCR'}]});
                    await pending;
                    return {confirm:!!document.querySelector('[data-parse-confirm]'),button:form.querySelector('button').disabled};
                } finally { apiWithTimeout = original; }
            }""")
            self.assertEqual(result, {"confirm": False, "button": False})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_parse_crop_handles_corrupt_state_and_preserves_valid_settings(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""() => {
                DOM.previewPanel.innerHTML = '<div data-parse-crop-toolbar data-scope="page"><span data-parse-crop-values></span></div><div data-parse-crop-box></div><div data-parse-article class="is-crop-target"><input data-parse-start value="1"><input data-parse-end value="2"></div>';
                STATE.parseCurrentPage = 1;
                const row = DOM.previewPanel.querySelector('[data-parse-article]');
                row.dataset.parseOcr = JSON.stringify({auto_vsplit:true,content_thresholds:[0.1,0.2,0.3,0.4]});
                row.dataset.parseOcrExceptions = JSON.stringify({'1':{auto_vsplit:false,content_thresholds:[0.5,0.1,0.2,0.1]}});
                const override = parseOCRThresholds(row,1);
                setParseCropThresholds(row,'page',1,[0.2,0.1,0.1,0.2]);
                const updated = JSON.parse(row.dataset.parseOcrExceptions)['1'];
                setParseCropThresholds(row,'page',1,[0,0,0,0],true);
                const fallback = parseOCRThresholds(row,1);
                row.dataset.parseOcr = 'null'; row.dataset.parseOcrExceptions = '[null]';
                const corrupt = parseOCRThresholds(row,1);
                setParseCropThresholds(row,'page',1,[0.1,0.1,0.1,0.1]);
                const repaired = JSON.parse(row.dataset.parseOcrExceptions)['1'].content_thresholds;
                row.dataset.parseOcr = JSON.stringify({content_thresholds:[0.5]});
                row.dataset.parseOcrExceptions = '{}';
                const wrongLength = parseOCRThresholds(row,1);
                row.dataset.parseOcr = JSON.stringify({content_thresholds:[-5,'0.25',1.5,'bad']});
                const badValue = parseOCRThresholds(row,1);
                row.dataset.parseOcr = JSON.stringify({content_thresholds:[-5,'0.25',1.5,0]});
                const clamped = parseOCRThresholds(row,1);
                row.dataset.parseOcrExceptions = JSON.stringify({'1':{content_thresholds:[0.9,0.9,0,0]}});
                const invalidOverride = parseOCRThresholds(row,1);
                updateParseCropUI();
                const box = DOM.previewPanel.querySelector('[data-parse-crop-box]');
                return {override,updated,fallback,corrupt,repaired,wrongLength,badValue,clamped,invalidOverride,
                        styles:[box.style.top,box.style.bottom,box.style.left,box.style.right]};
            }""")
            self.assertEqual(result, {"override": [0.5, 0.1, 0.2, 0.1],
                                      "updated": {"auto_vsplit": False, "content_thresholds": [0.2, 0.1, 0.1, 0.2]},
                                      "fallback": [0.1, 0.2, 0.3, 0.4], "corrupt": [0, 0, 0, 0],
                                      "repaired": [0.1, 0.1, 0.1, 0.1], "wrongLength": [0, 0, 0, 0],
                                      "badValue": [0, 0, 0, 0], "clamped": [0, 0.25, 0.98, 0],
                                      "invalidOverride": [0, 0.25, 0.98, 0],
                                      "styles": ["0%", "25%", "98%", "0%"]})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_parse_page_tolerates_missing_or_wrong_typed_optional_nodes(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout;
                STATE.parseMode = true;
                STATE.parseItem = {doc_id:'partial-dom',article:{page_end:2},source_files:[]};
                DOM.previewPanel.innerHTML = '<div data-parse-crop-toolbar></div><div data-parse-article class="is-crop-target"></div><span data-parse-image></span><div data-parse-ocr></div><div data-parse-page></div><div data-parse-page-prev></div><div data-parse-page-next></div>';
                apiWithTimeout = async () => ({ocr_results:[{text:'没有坐标',box:[]}]});
                try { await loadParsePage(2); } finally { apiWithTimeout = original; }
                return {page:STATE.parseCurrentPage,text:DOM.previewPanel.querySelector('[data-parse-ocr]').textContent};
            }""")
            self.assertEqual(result, {"page": 2, "text": "没有坐标"})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_malformed_parse_preview_never_opens_editor(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const original = apiWithTimeout; const toasts = [];
                showToast = text => toasts.push(text);
                STATE.parseMode = true;
                STATE.parseItems = [{doc_id:'parse-bad',title:'待拆分',archive_id:25,publication_name:'小报',page_start:1,page_end:1,date_display:[],authors:[],source_files:[]}];
                apiWithTimeout = async () => ({doc_id:'parse-bad',content:'<img src=x>',article:[]});
                try { await openParseEditor('parse-bad'); } finally { apiWithTimeout = original; }
                return {item:STATE.parseItem,visible:!DOM.previewPanel.hidden,toasts};
            }""")
            self.assertIsNone(result["item"])
            self.assertFalse(result["visible"])
            self.assertTrue(any("预览响应格式无效" in toast for toast in result["toasts"]))
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_late_parse_editor_cannot_reopen_after_leaving_parse_mode(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const originalApi = apiWithTimeout;
                let release;
                apiWithTimeout = () => new Promise(resolve => { release = resolve; });
                STATE.parseMode = true;
                STATE.parseItems = [{doc_id: 'old-editor'}];
                const editor = openParseEditor('old-editor');
                STATE.parseMode = false;
                apiWithTimeout = originalApi;
                await doSearch();
                release({doc_id: 'old-editor', article: {parts: [], page_start: 1, page_end: 1}, source_files: []});
                await editor;
                return {item: STATE.parseItem, preview: STATE.preview, hidden: DOM.previewPanel.hidden};
            }""")
            self.assertEqual(result, {"item": None, "preview": "", "hidden": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_pending_random_preview_does_not_replace_manual_selection(self):
        context, page, errors = self.open_page()
        try:
            page.locator('[data-preview="doc-1"]').click()
            page.locator(".preview-body").wait_for()
            result = page.evaluate("""async () => {
                let release;
                apiWithTimeout = () => new Promise(resolve => { release = resolve; });
                const random = openRandomPreview();
                openPreview('doc-1');
                release({doc_id: 'random-doc'});
                await random;
                return {preview: STATE.preview, title: DOM.previewPanel.querySelector('.preview-title').textContent};
            }""")
            self.assertEqual(result, {"preview": "doc-1", "title": "手机资料"})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_parse_crop_filters_ocr_and_submits_page_thresholds(self):
        context = self.browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        errors = []
        submitted = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        queue_item = {
            "doc_id": "parse-doc", "archive_id": 25, "publication_id": "052417de-42fb-4781-9885-af4fb006e9b6",
            "publication_name": "待拆小报", "title": "【文章待拆分】待拆小报", "publication_type": "img",
            "authors": [], "date_display": ["1967/5/12"], "page_start": 1, "page_end": 1, "source_files": ["one"],
        }
        preview = {
            **queue_item, "content_hash": "a" * 64, "content": "页眉\n正文", "variants": [],
            "tag_names": [], "source_file_count": 1,
            "source_files": [{"index": 0, "name": "one.jpg", "url": "https://example.com/one.jpg"}],
            "article": {"title": queue_item["title"], "authors": [], "dates": [{"year": 1967, "month": 5, "day": 12}], "page_start": 1, "page_end": 1},
        }

        def route_api(route, request):
            path = request.url.split(self.base_url, 1)[-1]
            if path.startswith("/api/parse/queue"):
                payload = {"total": 1, "page": 1, "page_size": 100, "items": [queue_item]}
            elif path.startswith("/api/parse/parse-doc/ocr"):
                payload = {"page": 1, "dimensions": {"width": 1000, "height": 1000}, "ocr_results": [
                    {"text": "页眉", "box": [[400, 20], [600, 20], [600, 80], [400, 80]]},
                    {"text": "正文", "box": [[400, 450], [600, 450], [600, 550], [400, 550]]},
                ]}
            elif path.startswith("/api/preview/source/parse-doc"):
                route.fulfill(status=200, body=png, headers={"Content-Type": "image/png"})
                return
            elif path.startswith("/api/preview/parse-doc"):
                payload = preview
            elif path == "/api/parse" and request.method == "POST":
                submitted.append(request.post_data_json)
                route.fulfill(status=202, content_type="application/json", body='{"ok":true}')
                return
            elif path.startswith("/api/health"):
                payload = {"ok": True, "es": True, "index_status": "ready", "progress": None}
            elif path.startswith("/api/sources"):
                payload = {"sources": [], "authors": [], "tags": [], "archives": [], "types": [], "years": [], "indexing": False, "index_status": "ready"}
            elif path.startswith("/api/facet/"):
                payload = {"items": [], "total": 0, "page": 1, "page_size": 200, "has_more": False}
            else:
                payload = {}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

        page.route("**/api/**", route_api)
        try:
            page.goto(self.base_url + "/?parse=1", wait_until="domcontentloaded")
            page.locator("[data-parse-open]").click()
            page.locator("[data-parse-content]").wait_for()
            self.assertIn("/api/preview/source-page/pdf-doc?file=0&page=2", page.evaluate("parseSourceImageURL({ doc_id: 'pdf-doc', source_files: [{ index: 0, page_count: 2 }] }, 2)"))
            self.assertFalse(page.locator("#search-form").is_visible())
            self.assertFalse(page.locator("#random-book-btn").is_visible())
            page.wait_for_function("document.querySelector('[data-parse-content]').value.includes('页眉')")
            self.assertIn("/api/download/source/parse-doc", page.locator(".parse-source-download").get_attribute("href"))
            page.locator("[data-parse-crop-toggle]").click()
            page.locator("[data-parse-crop-scope='page']").click()
            overlay = page.locator("[data-parse-crop-overlay]").bounding_box()
            page.mouse.move(overlay["x"] + overlay["width"] * 0.1, overlay["y"] + overlay["height"] * 0.2)
            page.mouse.down()
            page.mouse.move(overlay["x"] + overlay["width"] * 0.9, overlay["y"] + overlay["height"] * 0.9, steps=8)
            page.mouse.up()
            page.wait_for_function("document.querySelector('[data-parse-content]').value === '正文'")
            southeast = page.locator('[data-parse-crop-handle="se"]').bounding_box()
            page.mouse.move(southeast["x"] + southeast["width"] / 2, southeast["y"] + southeast["height"] / 2)
            page.mouse.down()
            page.mouse.move(overlay["x"] + overlay["width"] * 0.8, overlay["y"] + overlay["height"] * 0.8, steps=4)
            page.mouse.up()
            crop_box = page.locator("[data-parse-crop-box]").bounding_box()
            page.mouse.move(crop_box["x"] + crop_box["width"] / 2, crop_box["y"] + crop_box["height"] / 2)
            page.mouse.down()
            page.mouse.move(crop_box["x"] + crop_box["width"] / 2 + overlay["width"] * 0.05,
                            crop_box["y"] + crop_box["height"] / 2 + overlay["height"] * 0.05, steps=4)
            page.mouse.up()
            page.evaluate("sessionStorage.removeItem('bha-parse-draft:parse-doc')")
            page.locator("[data-parse-title]").fill("第一篇")
            page.locator("[data-parse-content]").fill("正文修订")
            page.locator("[data-parse-description]").fill("拆分草稿说明")
            self.assertIsNone(page.evaluate("sessionStorage.getItem('bha-parse-draft:parse-doc')"))
            page.wait_for_timeout(500)
            draft = json.loads(page.evaluate("sessionStorage.getItem('bha-parse-draft:parse-doc')"))
            self.assertEqual(draft["content_hash"], "a" * 64)
            self.assertEqual(draft["articles"][0]["content"], "正文修订")
            self.assertTrue(draft["articles"][0]["content_edited"])
            page.evaluate("""draft => {
                draft.articles[0].base_part_count = 0;
                sessionStorage.setItem('bha-parse-draft:parse-doc', JSON.stringify(draft));
            }""", draft)
            page.locator("[data-parse-back]").click()
            page.locator("[data-parse-open]").click()
            page.wait_for_function("document.querySelector('[data-parse-title]')?.value === '第一篇'")
            page.wait_for_function("Number(document.querySelector('[data-parse-article]')?.dataset.basePartCount) > 0")
            self.assertEqual(page.locator("[data-parse-content]").input_value(), "正文修订")
            self.assertEqual(page.locator("[data-parse-description]").input_value(), "拆分草稿说明")
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#mobile-toggle-btn").click()
            self.assertTrue(page.locator("body").evaluate("node => node.classList.contains('mobile')"))
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
            page.locator("[data-parse-form] button[type='submit']").click()
            page.locator("[data-parse-confirm]").wait_for()
            self.assertEqual(submitted, [])
            self.assertIn("第一篇", page.locator("[data-parse-confirm]").inner_text())
            self.assertIn("正文修订", page.locator("[data-parse-confirm]").inner_text())
            self.assertIn("拆分草稿说明", page.locator("[data-parse-confirm]").inner_text())
            self.assertEqual(page.locator("[data-parse-review-crop]").count(), 1)
            page.wait_for_function("document.querySelector('[data-parse-review-crop]').width > 0")
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
            page.locator("[data-cancel-parse]").last.click()
            self.assertEqual(submitted, [])
            self.assertEqual(page.locator("[data-parse-content]").input_value(), "正文修订")
            page.locator("[data-parse-form] button[type='submit']").click()
            page.locator("[data-confirm-parse]").click()
            page.wait_for_function("document.querySelector('[data-confirm-parse]')?.disabled === true && document.querySelector('#toast').textContent.includes('勿重复提交')")
            saved = json.loads(page.evaluate("sessionStorage.getItem('bha-parse-draft:parse-doc')"))
            self.assertEqual(saved["articles"][0]["content"], "正文修订")
            thresholds = submitted[0]["articles"][0]["ocr_exceptions"]["1"]["content_thresholds"]
            self.assertGreater(thresholds[0], 0.24)
            self.assertGreater(thresholds[1], 0.14)
            self.assertGreater(thresholds[2], 0.14)
            self.assertGreater(thresholds[3], 0.14)
            self.assertEqual(submitted[0]["articles"][0]["content"], "正文修订")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_desktop_search_filters_preview_theme_and_download(self):
        context, page, errors = self.open_page()
        try:
            page.locator("#search-input").fill("文化革命")
            page.wait_for_timeout(250)
            self.assertIn("q=", page.url)
            page.locator("label:has(#exact-toggle)").click()
            page.locator("label:has(#fulltext-toggle)").click()
            page.locator("#sort-filter").select_option("title")
            page.locator("#source-filter [data-source]").click()
            self.assertIn("source=", page.url)
            original = page.locator("body").get_attribute("class")
            page.locator("#theme-btn").click()
            self.assertNotEqual(original, page.locator("body").get_attribute("class"))
            page.locator("[data-preview]").click()
            page.locator("#preview-panel").wait_for(state="visible")
            self.assertIn("这是", page.locator(".preview-body").text_content())
            page.locator("[data-close-preview]").click()
            with page.expect_download(timeout=10000) as info:
                page.locator('a[href^="/api/download/txt/"]').click()
            self.assertTrue(info.value.suggested_filename.endswith(".txt"))
            page.locator("#random-book-btn").click()
            page.locator("#preview-panel").wait_for(state="visible")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_each_filter_cycles_inclusion_exclusion_and_clear(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""() => {
                const result = {};
                for (const [kind, filter, include, exclude] of [
                  ['source',DOM.sourceFilter,'sources','excludeSources'],
                  ['author',DOM.authorFilter,'authors','excludeAuthors'],
                  ['tag',DOM.tagFilter,'tags','excludeTags']]) {
                    const name = '选项-' + kind;
                    STATE.facets[include] = [{name,value:name,count:1}];
                    STATE[include] = []; STATE[exclude] = [];
                    renderFacetFilter(kind);
                    const clickValue = () => filter.querySelector(`[data-${kind}="${name}"]`).click();
                    clickValue(); const selected = STATE[include].includes(name) && !STATE[exclude].includes(name);
                    clickValue(); const excluded = !STATE[include].includes(name) && STATE[exclude].includes(name);
                    clickValue(); const cleared = !STATE[include].includes(name) && !STATE[exclude].includes(name);
                    clickValue(); filter.querySelector(`[data-clear-${kind}]`).click();
                    const clearedButton = !STATE[include].length && !STATE[exclude].length;
                    result[kind] = {selected,excluded,cleared,clearedButton};
                }
                return result;
            }""")
            expected = {"selected": True, "excluded": True, "cleared": True, "clearedButton": True}
            self.assertEqual(result, {"source": expected, "author": expected, "tag": expected})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_each_filter_pagination_buttons_and_inputs(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""() => {
                const original = loadFacetPage; const calls = [];
                loadFacetPage = (kind) => { calls.push(kind); return Promise.resolve(); };
                const result = {};
                try {
                  for (const [kind, filter] of [['source',DOM.sourceFilter],['author',DOM.authorFilter],['tag',DOM.tagFilter]]) {
                    STATE.facetTotals[kind]=1000; STATE.filterPage[kind]=2;
                    filter.innerHTML = choiceListHTML([{name:'测试'}],[],[],kind,200,2,1000,true);
                    filter.querySelector(`[data-${kind}-page=next]`).click(); const next=STATE.filterPage[kind];
                    const input=filter.querySelector(`[data-${kind}-page-input]`); input.value='4';
                    input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true})); const entered=STATE.filterPage[kind];
                    input.value='999'; input.dispatchEvent(new FocusEvent('focusout',{bubbles:true}));
                    result[kind]={next,entered,restored:input.value};
                  }
                } finally { loadFacetPage=original; }
                return {result,calls};
            }""")
            expected = {"next": 3, "entered": 4, "restored": "4"}
            self.assertEqual(result, {"result": {"source": expected, "author": expected, "tag": expected},
                                      "calls": ["source", "source", "author", "author", "tag", "tag"]})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_result_date_click_syncs_date_terms_ui(self):
        context, page, errors = self.open_page()
        try:
            page.locator("#left-toggle").click()
            page.locator("[data-date-term-label='1966/5/16']").click()
            term = page.locator("[data-date-term-index='0']")
            self.assertEqual(term.locator("[data-date-term-field='year']").input_value(), "1966")
            self.assertEqual(term.locator("[data-date-term-field='month']").input_value(), "5")
            self.assertEqual(term.locator("[data-date-term-field='day']").input_value(), "16")
            self.assertIn("date_term=year%3A1966-month%3A5-day%3A16", page.url)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_proofread_compare_mode_is_usable_on_desktop_and_mobile(self):
        for viewport in ({"width": 1440, "height": 900}, {"width": 390, "height": 844}):
            context, page, errors = self.open_page(viewport)
            try:
                if page.locator("body").evaluate("node => node.classList.contains('mobile')") and page.locator("#left-sidebar").evaluate("node => node.classList.contains('open')"):
                    page.locator("#left-toggle").click()
                page.locator("[data-preview]").click()
                if viewport["width"] > 600:
                    page.locator("[data-proofread-select]").evaluate("node => { const range = document.createRange(); range.selectNodeContents(node); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); node.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })); }")
                else:
                    page.locator("[data-compare-select]").select_option("__proofread__")
                page.locator("[data-proofread-form]").wait_for(state="visible")
                page.wait_for_function("typeof diff_match_patch !== 'undefined'")
                self.assertTrue(page.locator("[data-proofread-back]").is_visible())
                self.assertTrue(page.locator("[data-preview-mobile]").is_visible() if viewport["width"] <= 600 else True)
                self.assertEqual(page.locator("[data-meta-title]").input_value(), "手机资料")
                self.assertEqual(page.locator("[data-proofread-part-input='0']").input_value(), "这是手机正文")
                delta = page.evaluate("textDelta('旧〔1〕文', '新〔1〕文')")
                self.assertIn("新", delta)
                self.assertNotIn("%E6%96%B0", delta)
                applied = page.evaluate("([original, delta]) => applyTextDelta(original, delta)", ["旧〔1〕文", delta])
                self.assertEqual(applied, "新〔1〕文")
                moved = page.evaluate("() => { const original = '甲乙共同段丙丁'; const edited = '丙丁甲乙共同段'; const delta = textDelta(original, edited); return { delta, applied: applyTextDelta(original, delta) }; }")
                self.assertEqual(moved["applied"], "丙丁甲乙共同段")
                self.assertIn("=", moved["delta"], (viewport, moved))
                self.assertGreaterEqual(len(moved["delta"].split("\t")), 3)
                escaped = page.evaluate("() => { const original = '甲乙'; const edited = '甲%\t校乙'; const delta = textDelta(original, edited); return { delta, applied: applyTextDelta(original, delta) }; }")
                self.assertEqual(escaped["applied"], "甲%\t校乙")
                self.assertIn("%25", escaped["delta"])
                self.assertIn("%09", escaped["delta"])
                source_box = page.locator(".proofread-source").bounding_box()
                editor_box = page.locator(".proofread-editor").bounding_box()
                self.assertIsNotNone(source_box)
                self.assertIsNotNone(editor_box)
                if viewport["width"] > 600:
                    self.assertLessEqual(source_box["x"] + source_box["width"], editor_box["x"] + 1)
                else:
                    self.assertLessEqual(source_box["y"] + source_box["height"], editor_box["y"] + 1)
                self.assertEqual(errors, [])
            finally:
                context.close()

    def test_page_image_controls_recover_from_malformed_datasets(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                DOM.previewPanel.innerHTML = '<div data-page-image-viewer data-page-count="2" data-page-doc-id="doc-1" data-page-file-index="0"><img data-proofread-source-image><input data-page-image-input><button data-page-image-prev></button><button data-page-image-next></button></div>';
                const viewer = DOM.previewPanel.querySelector('[data-page-image-viewer]');
                viewer.dataset.pageImages = '[null]'; viewer.dataset.pageNumbers = '{"broken":true}';
                await setPageImage(viewer,2);
                const image = viewer.querySelector('img');
                return {page:viewer.dataset.pageIndex,src:new URL(image.src).pathname,
                        query:new URL(image.src).search,input:viewer.querySelector('input').value,
                        nextDisabled:viewer.querySelector('[data-page-image-next]').disabled};
            }""")
            self.assertEqual(result, {"page": "1", "src": "/api/preview/source-page/doc-1",
                                      "query": "?file=0&page=2", "input": "2", "nextDisabled": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_proofread_page_images_refit_and_recover_after_error(self):
        context, page, errors = self.open_page()
        first = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='80'%3E%3Crect width='120' height='80' fill='white'/%3E%3C/svg%3E"
        broken = "data:image/webp;base64,AAAA"
        third = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='800' height='100'%3E%3Crect width='800' height='100' fill='white'/%3E%3C/svg%3E"
        try:
            page.locator("[data-preview]").click()
            page.locator("#preview-panel").wait_for(state="visible")
            page.evaluate("""([first, broken, third]) => {
              const item = getCachedPreview(STATE.preview);
              item.source_files = [{ index: 0, name: "原稿.pdf", url: "https://example.com/source.pdf", pages: [first, broken, third] }];
              STATE.proofreadFile = "0";
              STATE.proofreadPage = "1";
              openProofread(item, null, true);
            }""", [first, broken, third])
            image = page.locator("[data-proofread-source-image]")
            page.wait_for_function("document.querySelector('[data-proofread-source-image]').naturalWidth === 120")
            first_width = image.get_attribute("data-image-base-width")
            page.locator("[data-page-image-next]").click()
            page.locator("[data-image-error]").wait_for(state="visible")
            self.assertTrue(image.is_hidden())
            page.locator("[data-page-image-next]").click()
            page.wait_for_function("document.querySelector('[data-proofread-source-image]').naturalWidth === 800 && !document.querySelector('[data-proofread-source-image]').hidden")
            self.assertNotEqual(first_width, image.get_attribute("data-image-base-width"))
            self.assertEqual(page.locator("[data-page-image-input]").input_value(), "3")
            self.assertIn("proofread_page=3", page.url)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_pdf_range_transport_rejects_invalid_followup_ranges(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
              class Transport {
                constructor(length, initialData) { this.length = length; this.initialData = initialData; }
                onDataRange() {}
              }
              const originalFetch = window.fetch;
              let request = 0;
              window.fetch = async () => {
                request += 1;
                if (request === 1) {
                  return new Response(new Uint8Array(8), { status: 206, headers: { "Content-Range": "bytes 0-7/8" } });
                }
                return new Response(new Uint8Array(4), { status: 206, headers: { "Content-Range": "bytes 0-3/8" } });
              };
              try {
                const range = await createPDFRangeTransport({ PDFDataRangeTransport: Transport }, "https://example.test/file.pdf");
                range.requestDataRange(4, 8);
                try { await range.failurePromise; return false; } catch (_error) { return true; }
              } finally {
                window.fetch = originalFetch;
              }
            }""")
            self.assertTrue(result)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_service_worker_controls_root_scope(self):
        context = self.browser.new_context(service_workers="allow")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            # A pre-existing cache owned by another reader/app must survive
            # activation of this page-image worker.
            context.add_init_script("""(() => {
                const register = ServiceWorkerContainer.prototype.register;
                ServiceWorkerContainer.prototype.register = function(...args) {
                  return caches.open('reader-downloads').then(() => register.apply(this,args));
                };
            })();""")
            page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            result = page.evaluate("""async () => {
                const response = await fetch('/static/sw.js', {cache:'no-store'});
                const registration = await navigator.serviceWorker.register('/static/sw.js', {scope:'/'});
                await navigator.serviceWorker.ready;
                if (!navigator.serviceWorker.controller) await new Promise((resolve, reject) => {
                  const timeout = setTimeout(() => reject(new Error('service worker did not control root')), 10000);
                  navigator.serviceWorker.addEventListener('controllerchange', () => { clearTimeout(timeout); resolve(); }, {once:true});
                });
                return {status:response.status,allowed:response.headers.get('Service-Worker-Allowed'),
                        scope:new URL(registration.scope).pathname,controlled:!!navigator.serviceWorker.controller,
                        unrelatedCachePreserved:(await caches.keys()).includes('reader-downloads')};
            }""")
            self.assertEqual(result, {"status": 200, "allowed": "/", "scope": "/", "controlled": True,
                                      "unrelatedCachePreserved": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_pdf_cache_teardown_uses_loading_task_not_document_proxy(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                let destroyed = 0;
                const url = 'test://pdf-cleanup';
                const entry = {promise:Promise.resolve({numPages:1}),initialController:new AbortController(),
                  range:null,loadingTask:{destroy:async () => { destroyed += 1; }},
                  document:{numPages:1,getPage:async () => ({})},destroyed:false,lastUsed:Date.now()};
                pdfDocumentCache.set(url, entry);
                destroyPDFCacheEntry(url, entry);
                destroyPDFCacheEntry(url, entry);
                await Promise.resolve();
                return {destroyed,removed:!pdfDocumentCache.has(url),aborted:entry.initialController.signal.aborted};
            }""")
            self.assertEqual(result, {"destroyed": 1, "removed": True, "aborted": True})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_pdf_range_rejects_malformed_headers_lengths_and_status(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""async () => {
                const make = (body, status, contentRange) => new Response(new Uint8Array(body),
                    {status, headers: contentRange ? {'Content-Range': contentRange} : {}});
                const good = await readPDFRange(make(4, 206, 'bytes 4-7/8'), 4, 8, 8);
                const inputs = [
                  () => readPDFRange(make(4, 200, ''), 4, 8, 8),
                  () => readPDFRange(make(4, 206, ''), 4, 8, 8),
                  () => readPDFRange(make(4, 206, 'bytes 0-3/8'), 4, 8, 8),
                  () => readPDFRange(make(4, 206, 'bytes 4-7/9'), 4, 8, 8),
                  () => readPDFRange(make(3, 206, 'bytes 4-7/8'), 4, 8, 8),
                  () => readPDFRange(make(4, 206, 'bytes 4-7/9007199254740992'), 4, 8)
                ];
                const rejected = [];
                for (const run of inputs) {
                    try { await run(); rejected.push(false); }
                    catch (_) { rejected.push(true); }
                }
                return {length: good.length, bytes: good.data.length, rejected};
            }""")
            self.assertEqual(result, {"length": 8, "bytes": 4, "rejected": [True] * 6})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_real_pdf_worker_renders_and_download_stays_in_toolbar(self):
        context, page, errors = self.open_page()
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            page.wait_for_function("document.querySelector('[data-pdf-status]')?.textContent === '1 / 1'", timeout=20000)
            self.assertGreater(page.locator("[data-pdf-canvas]").evaluate("canvas => canvas.width"), 0)
            download = page.locator(".proofread-pdf-tools [data-pdf-download-link]")
            self.assertTrue(download.is_visible())
            page.locator('[data-pdf-action="zoom-in"]').evaluate("button => { button.click(); button.click(); button.click(); }")
            page.wait_for_timeout(300)
            self.assertEqual(page.locator("[data-pdf-status]").text_content(), "1 / 1")
            self.assertEqual(page.evaluate("pdfDocumentCache.size"), 1)
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
            page.wait_for_function("pdfDocumentCache.size === 0")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_real_cross_origin_worker_range_pages_cache_and_disposal(self):
        context, page, errors = self.open_page(pdf_bytes=range_pdf(), track_workers=True)
        ranges, cdn_responses = [], []
        page.on("request", lambda request: ranges.append(request.headers.get("range", ""))
                if "/api/preview/source/" in request.url else None)
        page.on("response", lambda response: cdn_responses.append((response.url, response.status))
                if "cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/" in response.url else None)
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            page.wait_for_function("document.querySelector('[data-pdf-status]')?.textContent === '2 / 3'", timeout=30000)
            self.assertEqual(page.evaluate("async () => (await loadPDFJS()).version"), "6.5.438")
            self.assertEqual(page.evaluate("""async () => {
              const doc = await getCachedPDFDocument(activePDFViewer.url);
              return (await (await doc.getPage(2)).getTextContent()).items.map(item => item.str).join(' ');
            }"""), "BHA page 2")
            self.assertTrue(page.evaluate("""async () => {
              const url = activePDFViewer.url;
              return (await getCachedPDFDocument(url)) === (await getCachedPDFDocument(url));
            }"""))
            page.locator('[data-pdf-action="next"]').click()
            page.wait_for_function("document.querySelector('[data-pdf-status]')?.textContent === '3 / 3'")
            self.assertGreater(page.locator("[data-pdf-canvas]").evaluate("canvas => canvas.width"), 0)
            self.assertTrue(any(value == "bytes=0-524287" for value in ranges), ranges)
            self.assertTrue(any(value and not value.startswith("bytes=0-") for value in ranges), ranges)
            workers = page.evaluate("window.__pdfWorkers")
            self.assertTrue(any(worker["url"].startswith("blob:") and not worker["errors"] for worker in workers), workers)
            self.assertTrue(any("/build/pdf.min.mjs" in url and status == 200 for url, status in cdn_responses), cdn_responses)
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
            page.wait_for_function("pdfDocumentCache.size === 0")
            page.wait_for_function("window.__pdfWorkers.some(worker => worker.terminated > 0)")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_real_pdf_initial_range_failure_recovers_on_retry(self):
        context, page, errors = self.open_page(pdf_bytes=range_pdf(), track_workers=True)
        attempts = []

        def fail_once(route):
            attempts.append(route.request.headers.get("range", ""))
            if len(attempts) == 1:
                route.fulfill(status=503, body="temporary failure")
            else:
                route.fallback()

        page.route("**/api/preview/source/**", fail_once)
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            page.wait_for_function("document.querySelector('[data-pdf-status]')?.textContent === 'PDF 无法在线预览'", timeout=30000)
            self.assertTrue(page.locator("[data-pdf-download-link]").is_visible())
            page.wait_for_function("pdfDocumentCache.size === 0")
            page.evaluate("""() => {
              const viewer = document.querySelector('[data-pdf-viewer]');
              clearActiveMediaViewer();
              viewer.querySelector('.proofread-pdf-stage').classList.remove('is-error');
              renderPDFViewer(viewer);
            }""")
            page.wait_for_function("document.querySelector('[data-pdf-status]')?.textContent === '2 / 3'", timeout=30000)
            self.assertGreaterEqual(len(attempts), 2)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_real_pdf_close_aborts_a_pending_initial_range(self):
        context, page, errors = self.open_page(pdf_bytes=range_pdf(), track_workers=True)
        try:
            page.evaluate("""() => {
              const original = window.fetch;
              window.__pdfAbort = {started:false, aborted:false};
              window.fetch = (input, init) => {
                if (String(input).includes('/api/preview/source/')) {
                  window.__pdfAbort.started = true;
                  return new Promise((_resolve, reject) => {
                    init.signal.addEventListener('abort', () => {
                      window.__pdfAbort.aborted = true;
                      reject(new DOMException('closed', 'AbortError'));
                    }, {once:true});
                  });
                }
                return original(input, init);
              };
            }""")
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            page.wait_for_function("window.__pdfAbort.started && pdfDocumentCache.size === 1")
            page.evaluate("clearActiveMediaViewer()")
            page.wait_for_function("window.__pdfAbort.aborted && pdfDocumentCache.size === 0")
            self.assertEqual(page.locator("[data-pdf-fallback]").count(), 0)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_proofread_draft_writes_are_debounced(self):
        context, page, errors = self.open_page()
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            page.evaluate("sessionStorage.removeItem(`bha-proofread-draft:${STATE.preview}`)")
            page.locator('[data-proofread-part-input="0"]').fill("防抖后的正文")
            self.assertIsNone(page.evaluate("sessionStorage.getItem(`bha-proofread-draft:${STATE.preview}`)"))
            page.wait_for_timeout(500)
            draft = json.loads(page.evaluate("sessionStorage.getItem(`bha-proofread-draft:${STATE.preview}`)"))
            self.assertEqual(draft["parts"]["0"], "防抖后的正文")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_proofread_draft_round_trip_restores_all_edits(self):
        context, page, errors = self.open_page(preview_parts=[{"type": "paragraph", "text": "原始正文"},
                                                               {"type": "paragraph", "text": "待删除正文"}])
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            result = page.evaluate("""() => {
                const item = STATE.proofreadItem;
                const form = DOM.previewPanel.querySelector('[data-proofread-form]');
                form.querySelector('[data-proofread-part-input="0"]').value = '校订正文';
                form.querySelector('[data-meta-title]').value = '校订标题';
                form.querySelector('[data-source-name]').value = '校订来源';
                form.querySelector('[data-list="authors"] [data-list-field="value"]').value = '校订作者';
                form.querySelector('[data-proofread-note]').value = '备注';
                form.querySelector('[data-proofread-description]').value = '说明';
                form.querySelector('[data-add-part-before="0"]').click();
                const added = form.querySelector('[data-part-insert-before] [data-proofread-new-part]');
                added.querySelector('textarea').value = '新增正文';
                form.querySelector('[data-delete-part="1"]').click();
                const type = form.querySelector('[data-proofread-part-type="0"]');
                type.value = Array.from(type.options).find(option => option.value !== 'paragraph').value;
                flushProofreadDraft();
                const saved = JSON.parse(sessionStorage.getItem(proofreadDraftKey(item.doc_id)));
                openProofread(item);
                const restored = DOM.previewPanel.querySelector('[data-proofread-form]');
                return {saved, restored: collectProofreadDraft(restored),
                  fulltext: fullTextFromParts(restored),
                  deletedClass: restored.querySelector('[data-proofread-part-group="1"]').classList.contains('proofread-deleted')};
            }""")
            self.assertEqual(result["restored"], result["saved"])
            self.assertEqual(result["saved"]["parts"]["0"], "校订正文")
            self.assertEqual(result["saved"]["newParts"][0]["text"], "新增正文")
            self.assertEqual(result["saved"]["lists"]["authors"], ["校订作者"])
            self.assertEqual(result["fulltext"], "新增正文\n校订正文")
            self.assertTrue(result["deletedClass"])
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_proofread_restore_tolerates_corrupt_storage_and_partial_dom(self):
        context, page, errors = self.open_page()
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            result = page.evaluate("""() => {
                const item = STATE.proofreadItem;
                const form = DOM.previewPanel.querySelector('[data-proofread-form]');
                const key = proofreadDraftKey(item.doc_id);
                const malformed = [null, [], 'bad', {parts:null, comments:[], source:[], types:3,
                    lists:{authors:[null,{},1]}, deleted:[], newParts:[null,{}, {group:'0',text:{},type:[]}]},
                    {lists:{authors:'not an array'},newParts:'not an array'}];
                for (const draft of malformed) {
                  sessionStorage.setItem(key, JSON.stringify(draft));
                  restoreProofreadDraft(item,form);
                }
                sessionStorage.setItem(key, '{bad json'); restoreProofreadDraft(item,form);
                const partial = document.createElement('form');
                partial.innerHTML = '<div data-list="authors"><button type="button" data-add-list="authors"></button></div>';
                sessionStorage.setItem(key, JSON.stringify({lists:{authors:['x']}}));
                restoreProofreadDraft(item,partial);
                return {title:form.querySelector('[data-meta-title]').value,partialRows:partial.querySelectorAll('[data-list-row]').length};
            }""")
            self.assertEqual(result, {"title": "手机资料", "partialRows": 0})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_fulltext_apply_is_idempotent_and_syncs_with_parts(self):
        context, page, errors = self.open_page()
        try:
            page.locator("[data-preview]").click()
            page.locator("#preview-panel").wait_for(state="visible")
            page.locator("[data-proofread-select]").evaluate("node => { const range = document.createRange(); range.selectNodeContents(node); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); node.dispatchEvent(new MouseEvent('mouseup', { bubbles: true })); }")
            page.locator("[data-proofread-form]").wait_for(state="visible")
            page.locator("[data-fulltext-input]").fill("第一段\n第二段\n第三段")
            page.locator("[data-fulltext-apply]").click()
            page.wait_for_timeout(150)
            self.assertEqual(page.locator("[data-proofread-new-part]").count(), 2)
            self.assertEqual(page.locator("[data-proofread-new-part]").last.locator("textarea").input_value(), "第三段")
            page.locator("[data-fulltext-apply]").click()
            page.wait_for_timeout(150)
            self.assertEqual(page.locator("[data-proofread-new-part]").count(), 2)
            page.locator("[data-fulltext-toggle]").click()
            page.locator("[data-part-editor-toggle]").click()
            page.locator("[data-add-part-after='0']").click()
            page.locator("[data-proofread-part-group='0'] [data-part-insert-after] [data-proofread-new-part]").last.locator("textarea").fill("中间段")
            page.locator("[data-fulltext-toggle]").click()
            self.assertIn("中间段", page.locator("[data-fulltext-input]").input_value())
            page.locator("[data-fulltext-input]").fill("临时修改未应用")
            page.locator("[data-fulltext-toggle]").click()
            page.locator("[data-fulltext-toggle]").click()
            self.assertEqual(page.locator("[data-fulltext-input]").input_value(), "临时修改未应用")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_result_path_clicks_sync_filters_and_url(self):
        context, page, errors = self.open_page()
        try:
            page.locator(".result-item [data-source]").click()
            self.assertIn("source=%E6%B5%8B%E8%AF%95%E5%88%8A%E7%89%A9", page.url)
            page.locator(".result-item [data-author]").click()
            self.assertIn("author=%E4%BD%9C%E8%80%85%E7%94%B2", page.url)
            page.locator(".result-item [data-tag]").click()
            self.assertIn("tag=%E6%A0%87%E7%AD%BE%E7%94%B2", page.url)
            page.locator("#tag-filter [data-tag='标签甲'] .checkbox-count").wait_for(state="visible")
            self.assertEqual(page.locator("#tag-filter [data-tag='标签甲'] .checkbox-count").text_content(), "1")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_date_term_handler_ignores_malformed_field_and_index(self):
        context, page, errors = self.open_page()
        try:
            result = page.evaluate("""() => {
                STATE.dateTerms = [{year:'1967',month:'8',day:'29'}];
                const node = document.createElement('input');
                node.dataset.dateTermField = '__proto__';
                const row = document.createElement('div'); row.dataset.dateTermIndex = '0';
                row.append(node); DOM.dateTerms.append(row); node.value = 'unexpected';
                node.dispatchEvent(new Event('change',{bubbles:true}));
                const afterBadField = JSON.stringify(STATE.dateTerms);
                node.dataset.dateTermField = 'year'; row.dataset.dateTermIndex = 'invalid';
                node.dispatchEvent(new Event('change',{bubbles:true}));
                return {unchanged: afterBadField === JSON.stringify(STATE.dateTerms),year:STATE.dateTerms[0].year};
            }""")
            self.assertEqual(result, {"unchanged": True, "year": "1967"})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_mobile_sidebar_date_and_pagination_controls(self):
        context, page, errors = self.open_page({"width": 390, "height": 844})
        try:
            self.assertTrue(page.locator("body").evaluate("node => node.classList.contains('mobile')"))
            if page.locator("#left-sidebar").evaluate("node => node.classList.contains('open')"):
                page.locator("#left-toggle").click()
            page.locator("#left-toggle").click()
            self.assertTrue(page.locator("#left-sidebar").evaluate("node => node.classList.contains('open')"))
            page.locator("#date-from-year").fill("1966")
            page.locator("#date-from-year").press("Enter")
            page.locator("#date-from-month").select_option("5")
            page.locator("#date-from-day").select_option("16")
            self.assertIn("date_from=19660516", page.url)
            page.locator("#date-clear").click()
            self.assertNotIn("date_from=", page.url)
            if page.locator("#left-sidebar").evaluate("node => node.classList.contains('open')"):
                page.locator("#left-toggle").click()
            page.locator("[data-date-term-label='1966/5/16']").click()
            term = page.locator("[data-date-term-index='0']")
            self.assertEqual(term.locator("[data-date-term-field='year']").input_value(), "1966")
            self.assertEqual(term.locator("[data-date-term-field='month']").input_value(), "5")
            self.assertEqual(term.locator("[data-date-term-field='day']").input_value(), "16")
            self.assertIn("date_term=year%3A1966-month%3A5-day%3A16", page.url)
            page.locator("#mobile-toggle-btn").click()
            self.assertFalse(page.locator("body").evaluate("node => node.classList.contains('mobile')"))
            page.keyboard.press("/")
            self.assertTrue(page.locator("#search-input").evaluate("node => document.activeElement === node"))
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_accessibility_keyboard_filters_paths_and_preview_focus(self):
        context, page, errors = self.open_page()
        try:
            self.assertEqual(page.locator("#search-input").get_attribute("aria-label"), "搜索档案全文")
            self.assertEqual(page.locator("#results").get_attribute("role"), "list")
            self.assertEqual(page.locator("#page-input").get_attribute("aria-label"), "页码")
            filter_toggle = page.locator('[data-filter-toggle="source"]')
            filter_body = page.locator('[data-filter-body="source"]')
            filter_toggle.press("Enter")
            self.assertEqual(filter_toggle.get_attribute("aria-expanded"), "false")
            self.assertTrue(filter_body.evaluate("node => node.classList.contains('collapsed')"))
            self.assertEqual(filter_body.get_attribute("aria-hidden"), "true")
            self.assertTrue(filter_body.evaluate("node => node.inert"))
            filter_toggle.press("Enter")
            self.assertEqual(filter_toggle.get_attribute("aria-expanded"), "true")
            self.assertEqual(filter_body.get_attribute("aria-hidden"), "false")
            choice = page.locator("#source-filter .filter-choice-item").nth(1)
            self.assertEqual(choice.get_attribute("type"), "button")
            self.assertTrue(choice.get_attribute("aria-label"))
            path = page.locator(".path-folder[role='button']").first
            self.assertEqual(path.get_attribute("tabindex"), "0")

            page.locator('[data-preview="doc-1"]').click()
            page.locator("#preview-panel").wait_for(state="visible")
            self.assertEqual(page.locator("#preview-panel").get_attribute("role"), "dialog")
            self.assertEqual(page.locator("#preview-panel").get_attribute("aria-modal"), "false")
            self.assertFalse(page.locator("#header").evaluate("node => node.inert"))
            self.assertEqual(page.evaluate("document.activeElement.getAttribute('data-close-preview')"), "")
            page.locator("#search-input").click()
            self.assertTrue(page.locator("#preview-panel").is_visible())
            self.assertTrue(page.locator("#search-input").evaluate("node => document.activeElement === node"))
            page.keyboard.press("Escape")
            self.assertTrue(page.locator("#preview-panel").is_visible())
            page.locator("[data-close-preview]").press("Escape")
            page.locator("#preview-panel").wait_for(state="hidden")
            self.assertFalse(page.locator("#header").evaluate("node => node.inert"))
            self.assertEqual(page.evaluate("document.activeElement.getAttribute('data-preview')"), "doc-1")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_desktop_preview_and_background_search_remain_independent(self):
        context, page, errors = self.open_page(preview_text="预览正文保持原样。\n" * 1000)
        try:
            requests = []
            page.on("request", lambda request: requests.append(request.url) if "/api/preview/" in request.url else None)
            page.locator('[data-preview="doc-1"]').click()
            panel = page.locator("#preview-panel")
            panel.wait_for(state="visible")
            page.wait_for_function("document.activeElement.matches('[data-close-preview]')")
            page.evaluate("window.__independentBody = document.querySelector('.preview-body'); __independentBody.scrollTop = 200")
            scroll = page.locator(".preview-body").evaluate("node => node.scrollTop")
            page.evaluate("previewFocusableElements().slice(-1)[0].focus()")
            page.keyboard.press("Tab")
            if panel.evaluate("node => node.contains(document.activeElement)"):
                self.assertTrue(page.locator(".preview-body").evaluate("node => node === document.activeElement"))
                page.keyboard.press("Tab")
            self.assertFalse(panel.evaluate("node => node.contains(document.activeElement)"))

            with page.expect_request(lambda request: "/api/search" in request.url and request.post_data_json.get("q") == "手机"):
                page.locator("#search-input").fill("手机")
            page.wait_for_function("searchAbortController === null")
            with page.expect_request(lambda request: "/api/search" in request.url and request.post_data_json.get("fulltext") is False):
                page.locator("#fulltext-toggle-section label").click()
            page.wait_for_function("searchAbortController === null")
            self.assertTrue(panel.is_visible())
            self.assertTrue(page.evaluate("__independentBody === document.querySelector('.preview-body')"))
            self.assertAlmostEqual(page.locator(".preview-body").evaluate("node => node.scrollTop"), scroll, delta=1)
            self.assertEqual(len(requests), 1)
            self.assertFalse(page.locator("#results").evaluate("node => node.inert"))
            self.assertFalse(page.locator("#left-sidebar").evaluate("node => node.inert"))
            panel.locator("[data-close-preview]").click()
            panel.wait_for(state="hidden")
            self.assertEqual(page.locator("#search-input").input_value(), "手机")
            self.assertFalse(page.locator("#fulltext-toggle").is_checked())
            self.assertIn("q=", page.url)
            self.assertIn("fulltext=0", page.url)
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_desktop_preview_fullscreen_preserves_content_scroll_and_focus(self):
        context, page, errors = self.open_page(preview_text="正文内容不应因全屏切换而重新加载。\n" * 1000)
        try:
            page.locator('[data-preview="doc-1"]').click()
            panel = page.locator("#preview-panel")
            panel.wait_for(state="visible")
            button = panel.locator("[data-preview-fullscreen]")
            self.assertEqual(button.get_attribute("aria-label"), "全屏预览")
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
            self.assertEqual(panel.locator("[data-preview-tts], .preview-tts-controls").count(), 0)
            self.assertTrue(button.evaluate("node => node.nextElementSibling.matches('[data-close-preview]')"))
            self.assertLess(button.bounding_box()["x"], panel.locator("[data-close-preview]").bounding_box()["x"])
            self.assertLess(panel.bounding_box()["width"], page.viewport_size["width"])
            page.evaluate("window.__fullscreenBody = document.querySelector('.preview-body'); __fullscreenBody.scrollTop = 320")
            before_scroll = page.locator(".preview-body").evaluate("node => node.scrollTop")
            before_url = page.url

            button.press("Enter")
            self.assertEqual(button.get_attribute("aria-label"), "还原预览")
            self.assertEqual(button.get_attribute("aria-pressed"), "true")
            self.assertEqual(panel.get_attribute("aria-modal"), "true")
            self.assertTrue(page.locator("#header").evaluate("node => node.inert"))
            self.assertIn("fullscreen=1", page.url)
            share_url = page.url
            bounds = panel.bounding_box()
            for key, expected in {"x": 0, "y": 0, **page.viewport_size}.items():
                self.assertAlmostEqual(bounds[key], expected, delta=1)
            self.assertGreater(page.locator(".preview-body").bounding_box()["height"], 700)
            self.assertTrue(page.evaluate("__fullscreenBody === document.querySelector('.preview-body')"))
            self.assertAlmostEqual(page.locator(".preview-body").evaluate("node => node.scrollTop"), before_scroll, delta=1)
            self.assertFalse(page.locator("body").evaluate("node => node.classList.contains('mobile')"))
            page.keyboard.press("Tab")
            self.assertEqual(page.evaluate("document.activeElement.getAttribute('data-close-preview')"), "")
            page.keyboard.press("Tab")
            self.assertTrue(page.evaluate("document.activeElement === previewFocusableElements()[0]"))

            button.click()
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
            self.assertEqual(panel.get_attribute("aria-modal"), "false")
            self.assertFalse(page.locator("#header").evaluate("node => node.inert"))
            self.assertAlmostEqual(page.locator(".preview-body").evaluate("node => node.scrollTop"), before_scroll, delta=1)
            button.press("Space")
            page.keyboard.press("Escape")
            self.assertTrue(panel.is_visible())
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
            self.assertTrue(button.evaluate("node => document.activeElement === node"))
            self.assertEqual(page.url, before_url)

            button.press("Enter")
            panel.locator("[data-close-preview]").click()
            panel.wait_for(state="hidden")
            self.assertFalse(panel.evaluate("node => node.classList.contains('preview-fullscreen')"))
            page.locator('[data-preview="doc-1"]').click()
            panel.wait_for(state="visible")
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
            button.click()
            page.go_back()
            panel.wait_for(state="hidden")
            self.assertFalse(panel.evaluate("node => node.classList.contains('preview-fullscreen')"))
            self.assertFalse(page.locator("#header").evaluate("node => node.inert"))
            page.goto("about:blank")
            page.goto(share_url, wait_until="domcontentloaded")
            panel.wait_for(state="visible", timeout=30000)
            self.assertEqual(button.get_attribute("aria-pressed"), "true")
            self.assertEqual(panel.locator("[data-preview-tts], .preview-tts-controls").count(), 0)
            page.keyboard.press("Escape")
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
            self.assertNotIn("fullscreen=1", page.url)
            page.keyboard.press("Escape")
            panel.wait_for(state="hidden")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_preview_fullscreen_is_desktop_only_and_preserves_proofread_drafts(self):
        context, page, errors = self.open_page(viewport={"width": 390, "height": 844})
        try:
            if page.locator("#left-sidebar").evaluate("node => node.classList.contains('open')"):
                page.locator("#left-toggle").click()
            page.locator('[data-preview="doc-1"]').click()
            panel = page.locator("#preview-panel")
            panel.wait_for(state="visible")
            button = panel.locator("[data-preview-fullscreen]")
            self.assertFalse(button.is_visible())
            page.evaluate("toggleMobileMode()")
            self.assertTrue(button.is_visible())
            button.click()
            self.assertEqual(button.get_attribute("aria-pressed"), "true")
            bounds = panel.bounding_box()
            self.assertAlmostEqual(bounds["width"], 390, delta=1)
            self.assertAlmostEqual(bounds["height"], 844, delta=1)

            page.evaluate("""async () => {
                const item = getCachedPreview(STATE.preview);
                setCachedPreview('doc-2', {...item, doc_id: 'doc-2', content: '对照正文'});
                await comparePreview('doc-2');
            }""")
            self.assertEqual(page.locator(".compare-body").count(), 2)
            self.assertEqual(button.get_attribute("aria-label"), "还原预览")
            self.assertEqual(button.get_attribute("aria-pressed"), "true")

            page.evaluate("""() => {
                const item = getCachedPreview(STATE.preview);
                item.source_files = [];
                openProofread(item);
                DOM.previewPanel.querySelector('[data-fulltext-input]').value = '尚未保存的校对正文';
            }""")
            self.assertTrue(button.evaluate("node => node.nextElementSibling.matches('[data-close-preview]')"))
            self.assertEqual(button.get_attribute("aria-pressed"), "true")
            button.click()
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
            self.assertEqual(page.locator("[data-fulltext-input]").input_value(), "尚未保存的校对正文")
            button.click()
            page.evaluate("toggleMobileMode()")
            self.assertFalse(button.is_visible())
            self.assertFalse(panel.evaluate("node => node.classList.contains('preview-fullscreen')"))
            self.assertEqual(page.locator("[data-fulltext-input]").input_value(), "尚未保存的校对正文")
            page.locator("[data-preview-mobile]").click()
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
            self.assertEqual(page.locator("[data-fulltext-input]").input_value(), "尚未保存的校对正文")
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_year_picker_lists_and_selects_every_available_year(self):
        context, page, errors = self.open_page()
        try:
            page.evaluate("STATE.facets.years = [...Array.from({ length: 140 }, (_, index) => ({ value: 1880 + index })), { value: '66' }, { value: '未知' }, { value: '12345' }]")
            page.locator("#date-from-year").click()
            options = page.locator("#year-picker .year-option")
            self.assertEqual(options.count(), 140)
            self.assertEqual(page.locator("#year-picker [data-year-value='66']").count(), 0)
            self.assertEqual(page.locator("#year-picker [data-year-value='12345']").count(), 0)
            page.locator("#year-picker").evaluate("node => { node.scrollTop = node.scrollHeight; }")
            options.last.click()
            self.assertEqual(page.locator("#date-from-year").input_value(), "2019")
            page.evaluate("STATE.facets.years = []")
            page.locator("#date-to-year").click()
            fallback_options = page.locator("#year-picker .year-option")
            self.assertEqual(fallback_options.count(), 183)
            self.assertEqual(page.locator("#year-picker [data-year-value='2019']").count(), 1)
            self.assertEqual(errors, [])
        finally:
            context.close()


    def test_proofread_submit_rejects_invalid_editor_without_posting(self):
        context, page, errors = self.open_page()
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            result = page.evaluate("""async () => {
                const item = STATE.proofreadItem;
                const form = DOM.previewPanel.querySelector('[data-proofread-form]');
                const original = apiWithTimeout;
                let posts = 0;
                apiWithTimeout = async () => { posts += 1; return {}; };
                const errors = [];
                try {
                  const title = form.querySelector('[data-meta-title]');
                  title.remove();
                  await submitProofreadForm(form);
                  errors.push(DOM.toast.textContent);
                  form.append(title);
                  const part = form.querySelector('[data-proofread-part-input]');
                  part.dataset.proofreadPartInput = '999';
                  await submitProofreadForm(form);
                  errors.push(DOM.toast.textContent);
                  part.dataset.proofreadPartInput = '0';
                  item.article.parts = [null];
                  await submitProofreadForm(form);
                  errors.push(DOM.toast.textContent);
                  return {errors,posts,confirm:!!document.querySelector('[data-proofread-confirm]')};
                } finally { apiWithTimeout = original; }
            }""")
            self.assertEqual(result, {"errors": ["校对字段缺失：[data-meta-title]", "段落索引超出范围", "文章段落无效，请重新打开校对页"],
                                      "posts": 0, "confirm": False})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_proofread_refetch_rejects_wrong_document_and_stale_response(self):
        context, page, errors = self.open_page()
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            page.locator("[data-proofread-form]").wait_for(state="visible")
            result = page.evaluate("""async () => {
                const valid = getCachedPreview('doc-1');
                const originalApi = apiWithTimeout;
                const messages = []; showToast = text => messages.push(text);
                STATE.proofreadItem = null; previewCache.clear(); previewCacheChars = 0;
                apiWithTimeout = async () => ({...valid, doc_id:'other'});
                await submitProofreadForm(DOM.previewPanel.querySelector('[data-proofread-form]'));
                const badMessage = messages.slice();
                let release; apiWithTimeout = () => new Promise(resolve => { release = resolve; });
                const waiting = submitProofreadForm(DOM.previewPanel.querySelector('[data-proofread-form]'));
                STATE.preview = 'other'; release(valid); await waiting;
                apiWithTimeout = originalApi;
                return {badMessage, messages, confirm:!!document.querySelector('[data-proofread-confirm]')};
            }""")
            self.assertEqual(result, {"badMessage": ["校订内容已过期，请重新打开校对页"],
                                      "messages": ["校订内容已过期，请重新打开校对页"], "confirm": False})
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_proofread_submit_requires_confirm_and_posts_payload(self):
        context, page, errors = self.open_page()
        try:
            submitted = []

            def route_proofread(route, request):
                if request.method == "POST":
                    submitted.append(request.post_data_json)
                route.fulfill(status=202, content_type="application/json", body=json.dumps({"ok": True, "status": "accepted", "tasks": 1, "auto_merge": True}))

            page.route("**/api/proofread", route_proofread)
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            page.locator("[data-proofread-form]").wait_for(state="visible")
            page.locator("[data-proofread-part-input]").fill("这是手机正文（已校订）")
            page.locator('button[type="submit"]').click()
            confirm = page.locator("[data-proofread-confirm]")
            confirm.wait_for(state="visible")
            self.assertIn("段落 1", confirm.text_content())
            self.assertIn("diff-del", confirm.inner_html())
            page.locator("[data-cancel-proofread]").first.click()
            confirm.wait_for(state="hidden")
            self.assertEqual(submitted, [])
            page.locator('button[type="submit"]').click()
            page.locator("[data-confirm-proofread]").click()
            page.wait_for_function("document.querySelector('[data-confirm-proofread]')?.disabled === true && document.querySelector('#toast').textContent.includes('勿重复提交')")
            self.assertIsNotNone(page.evaluate("sessionStorage.getItem(proofreadDraftKey('doc-1'))"))
            self.assertEqual(len(submitted), 1)
            body = submitted[0]
            self.assertEqual(body["doc_id"], "doc-1")
            self.assertIn("0", body["patch"]["parts"])
            self.assertIn("diff", body["patch"]["parts"]["0"])
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_paused_submission_errors_keep_both_drafts(self):
        context, page, errors = self.open_page()
        try:
            page.route('**/api/proofread', lambda route: route.fulfill(
                status=503, content_type='application/json',
                body=json.dumps({'error': '校订提交暂不可用，请保留草稿并稍后重试。'}, ensure_ascii=False)))
            page.route('**/api/parse', lambda route: route.fulfill(
                status=503, content_type='application/json',
                body=json.dumps({'error': '文章拆分提交暂不可用，请保留草稿并稍后重试。'}, ensure_ascii=False)))
            page.locator('[data-preview]').click()
            page.locator('[data-compare-select]').select_option('__proofread__')
            page.locator('[data-proofread-form]').wait_for(state='visible')
            page.locator('[data-proofread-part-input="0"]').fill('暂停期间的校订正文')
            page.locator('button[type="submit"]').click()
            page.locator('[data-confirm-proofread]').click()
            page.wait_for_function('''() => document.querySelector('[data-confirm-proofread]')?.disabled === true
              && !!sessionStorage.getItem(proofreadDraftKey('doc-1'))''')
            saved = json.loads(page.evaluate("() => sessionStorage.getItem(proofreadDraftKey('doc-1'))"))
            self.assertEqual(saved['parts']['0'], '暂停期间的校订正文')
            self.assertTrue(page.locator('[data-proofread-confirm]').is_visible())
            result = page.evaluate('''async () => {
              const messages=[];showToast=text=>messages.push(text);
              const doc='fixture';sessionStorage.setItem(proofreadDraftKey(doc),'proof-draft');
              sessionStorage.setItem(parseDraftKey(doc),'parse-draft');
              await postProofread({doc_id:doc});
              const form=document.createElement('form');
              const ok=await postParse(form,{doc_id:doc,content_hash:'hash'},{articles:[]});
              const parseForm=document.createElement('form');parseForm.dataset.parseForm='';parseForm.dataset.maxPage='1';
              parseForm.innerHTML='<div data-parse-article data-base-part-count="1"><input data-parse-title value="第一篇">'
                +'<input data-parse-year><input data-parse-month><input data-parse-day><input data-parse-authors>'
                +'<input data-parse-start value="1"><input data-parse-end value="1">'
                +'<textarea data-parse-content>本地未提交正文</textarea></div><textarea data-parse-description>说明</textarea>';
              DOM.previewPanel.append(parseForm);
              STATE.parseItem={doc_id:'parse-draft-real',content_hash:'hash'};STATE.parseCurrentPage=1;
              const parseResult=await postParse(parseForm,STATE.parseItem,{articles:[]});
              return {ok, proof:sessionStorage.getItem(proofreadDraftKey(doc)),
                parse:sessionStorage.getItem(parseDraftKey(doc)),submitted:form.dataset.parseSubmitted||null,messages,
                parseResult,flushed:sessionStorage.getItem(parseDraftKey('parse-draft-real'))};
            }''')
            self.assertFalse(result['ok'])
            self.assertEqual(result['proof'], 'proof-draft')
            self.assertEqual(result['parse'], 'parse-draft')
            self.assertIsNone(result['submitted'])
            self.assertFalse(result['parseResult'])
            self.assertEqual(json.loads(result['flushed'])['articles'][0]['content'], '本地未提交正文')
            self.assertEqual(len(result['messages']), 3)
            self.assertTrue(all('草稿' in message for message in result['messages']))
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_ambiguous_network_failure_keeps_proofread_draft_and_editor(self):
        context, page, errors = self.open_page()
        try:
            page.route('**/api/proofread', lambda route: route.abort('failed'))
            page.locator('[data-preview]').click()
            page.locator('[data-compare-select]').select_option('__proofread__')
            page.locator('[data-proofread-form]').wait_for(state='visible')
            page.locator('[data-proofread-part-input="0"]').fill('网络失败仍需保留的正文')
            page.locator('button[type="submit"]').click()
            page.locator('[data-confirm-proofread]').click()
            page.wait_for_function('''() => document.querySelector('[data-confirm-proofread]')?.disabled === true
              && !!sessionStorage.getItem(proofreadDraftKey('doc-1'))''')
            saved = json.loads(page.evaluate("() => sessionStorage.getItem(proofreadDraftKey('doc-1'))"))
            self.assertEqual(saved['parts']['0'], '网络失败仍需保留的正文')
            self.assertTrue(page.locator('[data-proofread-confirm]').is_visible())
            self.assertIn('勿重复提交', page.locator('#toast').text_content())
            self.assertEqual(errors, [])
        finally:
            context.close()

    def test_fulltext_split_preserves_following_part_indices(self):
        parts = [
            {"type": "paragraph", "text": "第一段完整内容"},
            {"type": "paragraph", "text": "第二段保持不变"},
            {"type": "paragraph", "text": "第三段保持不变"},
        ]
        context, page, errors = self.open_page(preview_parts=parts)
        try:
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            form = page.locator("[data-proofread-form]")
            form.wait_for(state="visible")
            form.locator("[data-fulltext-input]").fill("第一段\n完整内容\n第二段保持不变\n第三段保持不变")
            form.locator("[data-fulltext-apply]").click()
            payload = page.evaluate("() => proofreadPayload(STATE.proofreadItem, document.querySelector('[data-proofread-form]'))")
            self.assertEqual(set(payload["patch"]["parts"]), {"0"})
            change = payload["patch"]["parts"]["0"]
            self.assertIn("diff", change)
            self.assertEqual(change["insertAfter"], [{"text": "完整内容", "type": "paragraph"}])
            self.assertEqual(form.locator("[data-proofread-part-input='1']").input_value(), "第二段保持不变")
            self.assertEqual(form.locator("[data-proofread-part-input='2']").input_value(), "第三段保持不变")
            self.assertEqual(errors, [])
        finally:
            context.close()


    def test_proofread_review_ignores_untouched_tags_with_varying_key_order(self):
        context, page, errors = self.open_page()
        try:
            submitted = []

            def route_proofread(route, request):
                if request.method == "POST":
                    submitted.append(request.post_data_json)
                route.fulfill(status=202, content_type="application/json", body=json.dumps({"ok": True, "status": "accepted", "tasks": 1, "auto_merge": True}))

            page.route("**/api/proofread", route_proofread)
            page.locator("[data-preview]").click()
            page.locator("[data-compare-select]").select_option("__proofread__")
            page.locator("[data-proofread-form]").wait_for(state="visible")
            page.locator('button[type="submit"]').click()
            self.assertEqual(page.locator("[data-proofread-confirm]").count(), 0)
            self.assertEqual(page.evaluate("document.querySelector('#toast').textContent"), "没有需要提交的修改")
            page.locator("[data-proofread-part-input]").fill("这是手机正文（已校订）")
            page.locator('button[type="submit"]').click()
            confirm = page.locator("[data-proofread-confirm]")
            confirm.wait_for(state="visible")
            self.assertNotIn("标签", confirm.text_content())
            page.locator("[data-confirm-proofread]").click()
            page.wait_for_function("document.querySelector('[data-confirm-proofread]')?.disabled === true && document.querySelector('#toast').textContent.includes('勿重复提交')")
            self.assertEqual(len(submitted), 1)
            self.assertIsNone(submitted[0].get("metadata"))
            self.assertIn("0", submitted[0]["patch"]["parts"])
            self.assertEqual(errors, [])
        finally:
            context.close()


if __name__ == "__main__":
    unittest.main()
