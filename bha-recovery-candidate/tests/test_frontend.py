import unittest
from html.parser import HTMLParser
from pathlib import Path

from app.config import APP_ROOT


class InteractiveIdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"button", "input", "select", "form"} and values.get("id"):
            self.ids.add(values["id"])


class FrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.static = Path(APP_ROOT) / "static"
        cls.html = (cls.static / "index.html").read_text(encoding="utf-8")
        cls.source = (cls.static / "app.js").read_text(encoding="utf-8")
        cls.css = (cls.static / "style.css").read_text(encoding="utf-8")

    def test_every_static_control_is_cached_and_bound(self):
        parser = InteractiveIdParser()
        parser.feed(self.html)
        expected = {
            "search-form", "search-input", "left-toggle", "mobile-toggle-btn", "theme-btn",
            "parse-btn",
            "sidebar-expand", "exact-toggle", "preview-highlight-toggle", "fulltext-toggle",
            "page-size", "sort-filter", "source-search", "author-search", "tag-search",
            "archive-filter", "type-filter", "date-from-year", "date-from-month",
            "date-from-day", "date-to-year", "date-to-month", "date-to-day", "date-clear",
            "date-term-add", "clear-filters", "empty-random-btn", "random-book-btn",
            "prev-page", "next-page", "page-input",
        }
        self.assertTrue(expected.issubset(parser.ids), expected - parser.ids)
        for control_id in expected - {"search-form"}:
            self.assertIn(f'requireDom("{control_id}",', self.source, control_id)
        self.assertIn("DOM.searchForm.addEventListener", self.source)

    def test_native_reader_is_lazy_loaded_and_detached_before_preview_rewrites(self):
        reader = (self.static / "tts-client.js").read_text(encoding="utf-8")
        parser = InteractiveIdParser()
        parser.feed(self.html)
        controls = {"speech-collapse", "speech-stop", "speech-play", "speech-previous", "speech-next",
                    "speech-pitch", "speech-speed", "speech-timer", "speech-visible", "speech-locate"}
        self.assertTrue(controls.issubset(parser.ids), controls - parser.ids)
        self.assertIn('<section id="speech-panel" class="speech-panel"', self.html)
        # The Reader/TTS client is not on the default screen; the preview injects
        # it with the content hash exposed by main.py.
        self.assertNotIn('<script src="/static/tts-client.js"', self.html)
        self.assertIn('<meta name="bha-tts-client-version" content="__BHA_TTS_CLIENT_VERSION__">', self.html)
        main = (Path(APP_ROOT) / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('"tts-client.js"', main)
        self.assertIn('versioned_assets["tts-client.js"]', main)
        self.assertIn("esbuild static/tts-client.js", (Path(APP_ROOT) / "Dockerfile").read_text(encoding="utf-8"))
        self.assertIn('const ORIGIN = "https://vomebook-hitokoto.hf.space"', reader)
        # Every preview DOM replacement first moves the reader panel out of it.
        lines = self.source.splitlines()
        rewrites = [index for index, line in enumerate(lines) if "DOM.previewPanel.innerHTML =" in line]
        self.assertGreaterEqual(len(rewrites), 5)
        for index in rewrites:
            self.assertIn("speechPreviewClosed();", "\n".join(lines[max(0, index - 3):index]), lines[index])
        for contract in ("function ensureSpeechClient()", 'script.src = "/static/tts-client.js"',
                         "const speechReady = STATE.proofread || STATE.compare ? null : ensureSpeechClient();",
                         "await speechReady;", "speechPreviewLoaded(docId, item)",
                         "speechHost.CCRDSpeech?.previewPending?.()", "return restorePreview(scroll);",
                         "if (event.detail >= 2 && (speechHost.CCRDSpeech || speechClientPromise)) return;",
                         "text: body.textContent"):
            self.assertIn(contract, self.source)
        self.assertIn(".speech-panel {", self.css)
        self.assertIn(".preview-body::highlight(ccrd-speech)", self.css)

    def test_parse_queue_editor_uses_structured_same_origin_contracts(self):
        for contract in (
            'apiWithTimeout(`/api/parse/queue?page=${STATE.parsePage}&page_size=${STATE.parsePageSize}`, { signal: controller.signal })',
            'apiWithTimeout(`/api/parse/${encodeURIComponent(item.doc_id)}/ocr?page=${page}`',
            'apiWithTimeout("/api/parse", { method: "POST"',
            "function collectParseArticles(form)",
            "function parseCoverageError(articles, maxPage)",
            "function parseReviewHTML(item, payload)",
            "function parseSourceImageURL(item, page)",
            "function showParseConfirm(form, item, payload)",
            "function hydrateParseReviewCrops(holder, item)",
            "data-confirm-parse",
            "data-cancel-parse",
            "function refreshParseArticleBody(row, force = false, persist = false, preserveEdited = false)",
            "refreshParseArticleBody(row, true, false, true)",
            'button.textContent = "正在读取 OCR 基线..."',
            'textarea.dataset.edited === "1" && textarea.value !== sourceValue',
            "base_part_count: Number(row.dataset.basePartCount) || 0",
            "data-parse-content",
            'data-parse-page-prev',
            'data-parse-add',
            'data-parse-crop-toggle',
            'data-parse-crop-scope="page"',
            "function beginParseCropDrag(event)",
            "const startX = Math.min(1, Math.max(0, (drag.startX - drag.rect.left) / drag.rect.width))",
            'data-parse-crop-handle="nw"',
            'drag.mode === "move"',
            'drag.mode === "select"',
            "content_thresholds: thresholds",
            "article.ocr_exceptions = ocrExceptions",
            "function collectParseDraft(form)",
            "function restoreParseDraft(item, form)",
            "draft.content_hash !== item.content_hash",
            "setTimeout(flushParseDraft, 400)",
            "function clearParseDraft(docId)",
            "parseOCRText(data, parseOCRThresholds(row, start + index))",
            "renderParseQueue({ animate: true })",
            "parse-source-download",
            "/api/download/source-zip/",
        ):
            self.assertIn(contract, self.source)
        self.assertNotIn("clearParseDraft(item.doc_id)", self.source)
        for contract in (
            "body.parse-mode #search-form",
            "body.parse-mode #random-book-btn",
            "body.parse-mode #empty-random-btn",
        ):
            self.assertIn(contract, self.css)
        self.assertNotIn("replace_articles", self.source)
        self.assertNotIn("window.confirm(`确认将占位文章替换为", self.source)

    def test_search_race_timeout_cache_and_ime_guards_are_present(self):
        for contract in (
            "const REQUEST_TIMEOUT_MS = 12000", "const SEARCH_CACHE_LIMIT = 60",
            "const SEARCH_CACHE_TTL = 8 * 60 * 1000", "searchRequestSeq",
            "searchAbortController.abort()", 'addEventListener("compositionstart"',
            'addEventListener("compositionend"', "requestSeq !== searchRequestSeq",
            'data.index_status === "ready" && !data.indexing',
        ):
            self.assertIn(contract, self.source)

    def test_default_screen_uses_one_generation_scoped_bootstrap(self):
        for contract in (
            'window.__bhaBootstrapPromise = fetch("/api/bootstrap"',
            "function canUseInitialBootstrap()",
            "async function loadInitialBootstrap()",
            "earlyBootstrap ? await earlyBootstrap : await apiWithTimeout(\"/api/bootstrap\", {}, 25000)",
            "setCachedSearch(cacheKey, search)",
            "scheduleNextSearchPagePrefetch(payload)",
            "if (!searchAbortController && !bootstrapPending) renderResults()",
            "canUseInitialBootstrap() ? loadInitialBootstrap() : Promise.all([doSearch(), loadFacets()])",
        ):
            self.assertIn(contract, self.html + self.source)

    def test_preview_cache_has_ttl_and_bounded_size(self):
        for contract in (
            "const PREVIEW_CACHE_TTL = 10 * 60 * 1000",
            "Date.now() - entry.time > PREVIEW_CACHE_TTL",
            "previewCache.delete(docId)",
            "PREVIEW_CACHE_MAX_CHARS",
        ):
            self.assertIn(contract, self.source)
        self.assertIn("function getCachedPreview(docId)", self.source)
        self.assertIn("function setCachedPreview(docId, value)", self.source)

    def test_visible_page_keeps_the_search_connection_warm(self):
        self.assertIn('fetch("/api/ping", { cache: "no-store" }).catch(() => {})', self.html)
        self.assertLess(self.html.index('fetch("/api/ping"'), self.html.index('<link rel="stylesheet"'))
        for contract in (
            "const KEEPALIVE_INTERVAL_MS = 45 * 1000",
            'fetch("/api/ping"',
            "if (document.hidden || !navigator.onLine || keepalivePending)",
            'window.addEventListener("online", () => warmConnection(true))',
            'document.addEventListener("visibilitychange"',
            "window.setInterval(() => warmConnection(), KEEPALIVE_INTERVAL_MS)",
        ):
            self.assertIn(contract, self.source)

    def test_dynamic_output_is_escaped_and_download_routes_are_explicit(self):
        self.assertIn("function escapeHTML(value)", self.source)
        self.assertIn("highlightPreviewText(item.content || \"\")", self.source)
        self.assertIn("/api/download/txt/", self.source)
        self.assertIn("/api/download/source-zip/", self.source)
        self.assertIn("/api/download/source/", self.source)
        self.assertIn("/api/preview/source/", self.source)
        self.assertIn("encodeURIComponent(item.doc_id)", self.source)

    def test_result_tags_keep_compact_metadata_text(self):
        self.assertIn('class="result-size"', self.source)
        self.assertRegex(self.css, r"\.result-size\s*\{[^}]*font-size:\s*11px;")

    def test_proofread_delta_keeps_unicode_and_escapes_only_delimiters(self):
        self.assertNotIn("diff_match_patch/20121119/diff_match_patch.js", self.html)
        self.assertIn("diff_match_patch/20121119/diff_match_patch.js", self.source)
        self.assertIn("sha512-5YZxUisfaneUbwv58nPp10qwt6DefHuJ", self.source)
        self.assertIn("function loadDiffMatchPatch()", self.source)
        self.assertIn("loadDiffMatchPatch();", self.source)
        self.assertIn("differ.diff_main(original, edited, true)", self.source)
        self.assertIn('inserted.replace(/%/g, "%25").replace(/\\t/g, "%09")', self.source)
        self.assertNotIn('encodeURI(inserted)', self.source)
        self.assertIn('if (cursor !== source.length) throw new Error("delta length mismatch")', self.source)

    def test_preview_filters_dates_pagination_and_mobile_have_handlers(self):
        for contract in (
            'DOM.results.addEventListener("click"', 'DOM.previewPanel.addEventListener("click"',
            'window.addEventListener("popstate"', "toggleResultFilter", "toggleResultDateTerm",
            "refreshSelectedFacetItem", "commitResultPage", "commitFilterPage", "validateDateRange", "localStorage.setItem(\"mobileMode\"",
            'showToast("随机文章加载失败")',
        ):
            self.assertIn(contract, self.source)

    def test_result_and_preview_motion_is_bounded_and_reduced_motion_safe(self):
        for contract in (
            'window.matchMedia("(prefers-reduced-motion: reduce)")',
            "renderResults({ animate: true })",
            "children).slice(0, 20)",
            "let animateNextPreview = false",
            "if (animateNextPreview) animatePreviewPanel()",
            "function stopPreviewAnimation()",
            "function openProofread(item, focusPart = null, restoring = false) {\n  if (!item) return;\n  stopPreviewAnimation();",
        ):
            self.assertIn(contract, self.source)
        for contract in (
            ".result-item.result-enter",
            "animation: result-enter 320ms",
            ".preview-panel.preview-enter",
            "animation: preview-enter 360ms",
        ):
            self.assertIn(contract, self.css)

    def test_year_picker_uses_available_years_with_a_stable_fallback(self):
        self.assertIn('const isFourDigitYear = (value) => /^\\d{4}$/.test(String(value))', self.source)
        self.assertIn("Array.from({ length: 183 }, (_, index) => String(1843 + index))", self.source)
        self.assertIn("[...new Set([...values, ...selectedYears])]", self.source)
        self.assertIn("const years = yearItems().filter", self.source)
        self.assertNotIn(".filter((year) => !keyword || String(year).includes(keyword)).slice", self.source)
        self.assertIn("const openAbove = spaceBelow", self.source)
        self.assertIn('DOM.yearPicker.style.bottom = openAbove ?', self.source)

    def test_sources_accept_current_and_legacy_response_shapes(self):
        self.assertIn("if (isFacetList(rawSources))", self.source)
        self.assertIn("archives: sortFacetItems(data.archives || [])", self.source)

    def test_proofreading_uses_compare_menu_selection_and_source_proxy(self):
        for contract in (
            "对比原文件并校对", 'value="__proofread__"', "data-proofread-select",
            'addEventListener("mouseup"', "/api/download/source/", "/api/preview/source/", "inline=true",
            'apiWithTimeout("/api/proofread"', "data-meta-title", "data-list=",
        ):
            self.assertIn(contract, self.source)

    def test_proofreading_persists_state_and_supports_structural_editing(self):
        for contract in ("sessionStorage", "STATE.compare", "STATE.proofread", "STATE.proofreadItem", "STATE.proofreadPage", "data-add-part-before", "data-add-part-after", "data-delete-part", "data-fulltext-input", "flushProofreadDraft", "setTimeout(flushProofreadDraft, 400)", 'addEventListener("pagehide"', "校订内容已过期，请重新打开校对页"):
            self.assertIn(contract, self.source)
        self.assertIn('data-fulltext-toggle>收起全文编辑</button>', self.source)
        self.assertIn('<div data-fulltext-box><textarea data-fulltext-input>', self.source)
        self.assertNotIn('<div data-fulltext-box hidden>', self.source)

    def test_proofreading_shows_change_preview_before_submitting(self):
        for contract in (
            "校订提交预览", "data-confirm-proofread", "data-cancel-proofread",
            "确认提交", "返回继续编辑", "function proofreadReviewHTML",
            "function applyTextDelta", "function postProofread",
            "元数据对照", "proofread-metadata-table", "原元数据", "修改后元数据",
            "function metadataItemText", "function metadataCellHTML",
        ):
            self.assertIn(contract, self.source)
        self.assertIn("function matchingLineAnchors(current, desired)", self.source)
        self.assertIn("const anchors = [...matchingLineAnchors(current, lines)", self.source)
        self.assertIn('apiWithTimeout("/api/proofread"', self.source)
        self.assertNotIn("JSON.stringify(oldValues[key])", self.source)

    def test_proofreading_mobile_places_fulltext_editor_at_top(self):
        for contract in (
            "data-fulltext-section", "function relocateFulltextEditor",
            "STATE.isMobile ? fulltextSection :", "STATE.isMobile ? partToggle :",
            "relocateFulltextEditor();",
        ):
            self.assertIn(contract, self.source)

    def test_proofreading_image_fallback_and_compact_body_editor(self):
        for contract in (
            "data-proofread-source-image", "data-image-zoom", "loading=\"eager\"", "fetchpriority=\"high\"", "data-proofread-back", "data-preview-mobile", "图片无法在线显示",
            "没有原文件", "仍可编辑正文并提交校对", "data-part-editor-toggle", "收起段落编辑",
            "在上新建段落", "在下新建段落", "删除当前段落", "proofread-structure-closed",
            "proofread-description-heading", "proofread-grid-no-source", '"校对"',
            "isPDFSource", "pageImageViewerHTML", "initial_page", "initial_page_number", "page_numbers", "data-page-image-viewer", "data-page-count", "source-page", "data-page-image-prev", "data-page-image-next", "此原文件暂不支持在线预览", "proofread-document-viewer",
            "loadPDFJS", "data-pdf-canvas", "proofread-pdf-stage is-loading", "is-loading", "is-error", "pdf.worker.min.mjs", "getCachedPDFDocument", "PDF_DOCUMENT_CACHE_LIMIT = 8", "createPDFRangeTransport", "readPDFRange", "failurePromise", "destroyPDFCacheEntry", "PDFDataRangeTransport", "Range: \"bytes=0-524287\"", "requestDataRange(begin, end)", "disableAutoFetch", "disableStream: true", "disableRange: false", "rangeChunkSize: 512 * 1024", "RenderingCancelledException", "ResizeObserver", "data-image-error", "data-pdf-download-link", "decoding=\"async\"", "PDF 无法在线预览",
        ):
            self.assertIn(contract, self.source)

    def test_preview_open_limits_text_selection_to_the_preview_panel(self):
        self.assertIn("body.preview-open {\n  user-select: none;", self.css)
        self.assertIn("-webkit-user-select: none;", self.css)
        self.assertIn("body.preview-open #preview-panel {\n  user-select: text;", self.css)
        self.assertIn("body.preview-open #preview-panel .preview-header {\n  user-select: none;", self.css)

    def test_pdfjs_is_pinned_and_preconnected(self):
        self.assertIn('cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/pdf.min.mjs', self.source)
        self.assertIn('cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/pdf.worker.min.mjs', self.source)
        for resource in ('cmaps/', 'standard_fonts/', 'wasm/'):
            self.assertIn('cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/' + resource, self.source)
        self.assertIn('rel="preconnect" href="https://cdnjs.cloudflare.com"', self.html)
        self.assertIn('rel="preconnect" href="https://cdn.jsdelivr.net"', self.html)
        self.assertIn('rel="dns-prefetch" href="//cdn.jsdelivr.net"', self.html)
        self.assertNotIn('cdnjs.cloudflare.com/ajax/libs/pdf.js/', self.source)
        self.assertNotIn('pdfjs-dist@6.4.299/', self.source)
        self.assertIn('rel="preconnect" href="https://huggingface.co"', self.html)
        self.assertIn("fetchInitialPDFRange", self.source)
        self.assertIn("Promise.all([pdfjsLoad, initialLoad])", self.source)
        self.assertIn('navigator.serviceWorker.register("/static/sw.js"', self.source)
        service_worker = (self.static / "sw.js").read_text(encoding="utf-8")
        self.assertIn('bha-page-webp-v1.0.0', service_worker)
        self.assertIn('"/api/preview/source-page/"', service_worker)
        self.assertIn("MAX_ENTRIES = 50", service_worker)
        self.assertIn("event.waitUntil(response.then(() => cacheWrite)", service_worker)
        self.assertNotIn("await cache.put", service_worker)


if __name__ == "__main__":
    unittest.main()
