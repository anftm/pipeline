// BEGIN STRICT FACET TYPES
/** @typedef {"source" | "author" | "tag"} FacetKind */
/** @typedef {{name?: string | null, value?: string | number | null, count?: number | null}} FacetItem */
/** @typedef {"sources" | "authors" | "tags"} IncludedFacetKey */
/** @typedef {"excludeSources" | "excludeAuthors" | "excludeTags"} ExcludedFacetKey */
// END STRICT FACET TYPES
// BEGIN STRICT SEARCH RESPONSE TYPES
/** @typedef {{doc_id: string, title?: string | null, title_highlight?: string | null, publication_name?: string | null,
 * publication_type?: string | null, publication_author?: string | null, source_file_count: number,
 * date_display: string[], authors: string[], tag_names: string[], snippet: string[]}} SearchResult */
/** @typedef {{results: SearchResult[], total: number, page: number, page_size: number, indexing: boolean, index_status: string}} SearchResponse */
/** @typedef {{archives?: FacetItem[], types?: FacetItem[], years?: FacetItem[]}} BootstrapSources */
/** @typedef {{generation: string, search: SearchResponse, sources: BootstrapSources}} BootstrapResponse */
/** @typedef {{items: FacetItem[], total: number, page: number, page_size: number, has_more: boolean, indexing?: boolean, index_status?: string}} FacetPage */
/** @typedef {{sources: FacetItem[], authors: FacetItem[], tags: FacetItem[], archives: FacetItem[], types: FacetItem[], years: FacetItem[], indexing?: boolean, index_status?: string}} FacetSourcesResponse */
/** @typedef {{generation: string, facets: Record<FacetKind, FacetPage>}} BootstrapFacetsResponse */
/** @typedef {{content: string, [key: string]: unknown}} CachedPreview */
/** @typedef {{index: number, name: string, url: string, preview_url?: string, initial_page?: string,
 * page_count?: number, initial_page_number?: number, pages?: string[], page_numbers?: number[]}} PreviewSourceFile */
/** @typedef {{doc_id: string, publication_name: string}} PreviewVariant */
/** @typedef {CachedPreview & {doc_id: string, title?: string | null, publication_name?: string | null,
 * publication_type?: string | null, publication_author?: string | null, source_file_count: number, authors: string[], tag_names: string[], date_display: string[],
 * source_files: PreviewSourceFile[], variants: PreviewVariant[], article: Record<string, unknown>}} ValidatedPreview */
/** @typedef {{doc_id: string, title: string | null, publication_name: string | null, archive_id: number,
 * page_start: number | null, page_end: number | null, date_display: string[], authors: string[], source_files: string[]}} ParseQueueItem */
/** @typedef {{total: number, page: number, page_size: number, items: ParseQueueItem[]}} ParseQueueResponse */
/** @typedef {{title: string, authors: string[], dates: {year?: number, month?: number, day?: number}[],
 * is_range_date: boolean, page_start: number, page_end: number, content: string, base_part_count: number,
 * ocr?: Record<string, unknown>, ocr_exceptions?: Record<string, unknown>}} CollectedParseArticle */
/** @typedef {{articles: CollectedParseArticle[], description: string}} ParseSubmitPayload */
/** @typedef {{text: string, type: string}} ProofreadInsertedPart */
/** @typedef {{delete?: boolean, diff?: string, type?: string, insertBefore?: ProofreadInsertedPart[], insertAfter?: ProofreadInsertedPart[]}} ProofreadChange */
/** @typedef {{version: 2, parts: Record<string, ProofreadChange>, comments: Record<string, ProofreadChange>, description: string}} ProofreadPatch */
/** @typedef {{article?: Record<string, unknown>, source?: Record<string, unknown>}} ProofreadMetadata */
/** @typedef {{doc_id: string, base_content_hash: string | null, patch: ProofreadPatch | null, metadata: ProofreadMetadata | null, description: string}} ProofreadPayload */
/** @typedef {{length: number, onDataRange(begin: number, data: Uint8Array): void, abort(): void}} PDFRangeBase */
/** @typedef {{failurePromise: Promise<never>, requestDataRange: (begin: number, end: number) => void, abort(error?: unknown): void} & PDFRangeBase} PDFRangeTransport */
/** @typedef {{promise: Promise<void>, cancel: () => void}} PDFRenderTask */
/** @typedef {{width: number, height: number, [key: string]: unknown}} PDFViewport */
/** @typedef {{getViewport: (options: {scale: number}) => PDFViewport, render: (options: {canvasContext: CanvasRenderingContext2D, viewport: PDFViewport}) => PDFRenderTask}} PDFPage */
/** @typedef {{numPages: number, getPage: (page: number) => Promise<PDFPage>}} PDFDocument */
/** @typedef {{promise: Promise<PDFDocument>, destroy: () => Promise<void>}} PDFLoadingTask */
/** @typedef {{GlobalWorkerOptions: {workerSrc: string}, PDFDataRangeTransport: new (length: number, data: Uint8Array, progressiveDone: boolean) => PDFRangeBase,
 * getDocument: (options: {range: PDFRangeTransport, disableStream: boolean, disableAutoFetch: boolean, disableRange: boolean, rangeChunkSize: number,
 * cMapUrl: string, cMapPacked: boolean, standardFontDataUrl: string, wasmUrl: string}) => PDFLoadingTask}} PDFJSLibrary */
/** @typedef {{promise: Promise<PDFDocument> | null, initialController: AbortController, range: PDFRangeTransport | null,
 * loadingTask: PDFLoadingTask | null, document: PDFDocument | null, destroyed: boolean, lastUsed: number}} PDFCacheEntry */
/** @typedef {{viewer: HTMLElement, url: string, generation: number, renderTask: PDFRenderTask | null,
 * resizeObserver: ResizeObserver | null, resizeFrame: number}} PDFViewerLifecycle */
// END STRICT SEARCH RESPONSE TYPES
const STATE = {
  q: "",
  page: 1,
  pageSize: 20,
  exact: true,
  previewHighlight: true,
  fulltext: true,
  sort: "relevance",
  sources: /** @type {string[]} */ ([]),
  authors: /** @type {string[]} */ ([]),
  tags: /** @type {string[]} */ ([]),
  excludeSources: /** @type {string[]} */ ([]),
  excludeAuthors: /** @type {string[]} */ ([]),
  excludeTags: /** @type {string[]} */ ([]),
  archiveId: "",
  publicationType: "",
  dateFromYear: "",
  dateFromMonth: "",
  dateFromDay: "",
  dateToYear: "",
  dateToMonth: "",
  dateToDay: "",
  dateTerms: /** @type {DateTerm[]} */ ([{ year: "", month: "", day: "" }]),
  total: 0,
  results: /** @type {SearchResult[]} */ ([]),
  facets: /** @type {Record<IncludedFacetKey | "archives" | "types" | "years", FacetItem[]>} */
    ({ sources: [], authors: [], tags: [], archives: [], types: [], years: [] }),
  facetTotals: { source: 0, author: 0, tag: 0 },
  facetHasMore: { source: false, author: false, tag: false },
  leftOpen: true,
  preview: "",
  previewFullscreen: false,
  compare: "",
  proofread: false,
  proofreadItem: /** @type {ValidatedPreview | null} */ (null),
  proofreadFile: "",
  proofreadPage: "",
  parseMode: false,
  parsePage: 1,
  parsePageSize: 100,
  parseTotal: 0,
  parseItems: /** @type {ParseQueueItem[]} */ ([]),
  parseItem: /** @type {ValidatedPreview | null} */ (null),
  parseCurrentPage: 1,
  isDark: true,
  isMobile: false,
  filterSearch: { source: "", author: "", tag: "" },
  filterPage: { source: 1, author: 1, tag: 1 },
  filterCollapsed: /** @type {Record<string, boolean>} */ ({}),
  sidebarExpanded: false,
};

const PERIOD_PRESETS = [
  { label: "新民主主义革命时期", from: "19190504", to: "19490930" },
  { label: "社会主义革命时期", from: "19491001", to: "19561231" },
  { label: "全面建设社会主义时期", from: "19560915", to: "19660515" },
  { label: "无产阶级文化大革命时期", from: "19660516", to: "19761006" },
  { label: "改开-南巡", from: "19781218", to: "19920117" },
  { label: "南巡-2012", from: "19920118", to: "20121114" },
  { label: "2012-", from: "20121115", to: "" },
];
const TAG_TYPES = ["文稿大类", "文稿类型", "地点", "人物", "出版方/发行方", "主题/事件", "记录", "审核", "翻译", "翻印/传抄"];
const PART_TYPES = ["appellation", "title", "authors", "place", "subtitle", "subtitle2", "subtitle3", "subtitle4", "subtitle5", "subdate", "paragraph", "quotation", "signature", "image", "image_description"];

// BEGIN STRICT DOM TYPES
/** @typedef {{header: HTMLElement, leftSidebar: HTMLElement, sidebarTitle: HTMLElement, overlay: HTMLElement,
 * sourceSearch: HTMLInputElement, sourceFilter: HTMLElement, searchForm: HTMLFormElement, searchInput: HTMLInputElement,
 * leftToggle: HTMLButtonElement, mobileToggleBtn: HTMLButtonElement, mobileToggleIcon: HTMLElement, themeBtn: HTMLButtonElement,
 * parseBtn: HTMLButtonElement, sidebarExpand: HTMLButtonElement, exactToggle: HTMLInputElement,
 * previewHighlightToggle: HTMLInputElement, fulltextToggle: HTMLInputElement, pageSize: HTMLSelectElement,
 * parsePageSizeOption: HTMLOptionElement, sortFilter: HTMLSelectElement, authorSearch: HTMLInputElement,
 * authorFilter: HTMLElement, tagSearch: HTMLInputElement, tagFilter: HTMLElement, archiveFilter: HTMLSelectElement,
 * typeFilter: HTMLSelectElement, periodPresets: HTMLElement, dateFromYear: HTMLInputElement, dateFromMonth: HTMLSelectElement,
 * dateFromDay: HTMLSelectElement, dateToYear: HTMLInputElement, dateToMonth: HTMLSelectElement, dateToDay: HTMLSelectElement,
 * dateClear: HTMLButtonElement, dateError: HTMLElement, yearPicker: HTMLElement, dateTermAdd: HTMLButtonElement,
 * dateTerms: HTMLElement, clearFilters: HTMLButtonElement, statusBar: HTMLElement, resultCount: HTMLElement,
 * statusText: HTMLElement, indexProgress: HTMLElement, results: HTMLElement, resultsContainer: HTMLElement,
 * emptyState: HTMLElement, randomBookBtn: HTMLButtonElement, emptyRandomBtn: HTMLButtonElement, previewPanel: HTMLElement,
 * prevPage: HTMLButtonElement, nextPage: HTMLButtonElement, pageInput: HTMLInputElement, pageTotal: HTMLElement,
 * loadInfo: HTMLElement, toast: HTMLElement}} SearchDOM */
/** Filled and validated by cacheDom before event handlers run. @type {SearchDOM} */
const DOM = /** @type {SearchDOM} */ (/** @type {unknown} */ ({}));
// END STRICT DOM TYPES

/** @param {string} selector @returns {Element | null} */
const $ = (selector) => document.querySelector(selector);

const THEME_ICON = {
  dark: '<svg class="theme-icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 15.5A8 8 0 1 1 8.5 4 6.5 6.5 0 0 0 20 15.5Z"/></svg>',
  light: '<svg class="theme-icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/></svg>',
};
/** @type {number | undefined} */ let searchTimer;
/** @type {number | undefined} */ let facetRetryTimer;
/** @type {number | undefined} */ let progressTimer;
let searchRequestSeq = 0;
/** @type {AbortController | null} */ let searchAbortController = null;
/** @type {AbortController | null} */ let searchPrefetchAbortController = null;
let searchPrefetchCacheKey = "";
let bootstrapPending = false;
/** @type {Promise<PDFJSLibrary> | null} */ let pdfjsPromise = null;
/** @type {Promise<void> | null} */ let diffMatchPatchPromise = null;
const DIFF_MATCH_PATCH_URL = "https://cdnjs.cloudflare.com/ajax/libs/diff_match_patch/20121119/diff_match_patch.js";
const DIFF_MATCH_PATCH_INTEGRITY = "sha512-5YZxUisfaneUbwv58nPp10qwt6DefHuJ+iAfvPoABZAYwLg4WGJHITaLpyyxJdrljfc0ggUoWc87Z0lfoDS84Q==";
/** @type {Map<string, PDFCacheEntry>} */ const pdfDocumentCache = new Map();
const PDF_DOCUMENT_CACHE_LIMIT = 8;
const PDF_DOCUMENT_MOBILE_CACHE_LIMIT = 3;
const PDF_DOCUMENT_CACHE_IDLE_MS = 15 * 60 * 1000;
let pdfCacheCleanupTimer = 0;
/** @type {PDFViewerLifecycle | null} */ let activePDFViewer = null;
/** @type {ResizeObserver | null} */ let activeImageResizeObserver = null;
let activeImageFitFrame = 0;
let parseDraftTimer = 0;
let parseRequestSeq = 0;
let parsePageRequestSeq = 0;
/** @type {AbortController | null} */ let parseAbortController = null;
let proofreadDraftTimer = 0;
const prefetchedPageImages = new Set();
/** @type {WeakMap<HTMLElement, AbortController>} */ const pageImageRequests = new WeakMap();
/** @type {(() => void) | null} */ let cancelPageImagePrefetch = null;
/** @type {AbortController | null} */ let pageImagePrefetchController = null;
/** @type {Map<string, {value: SearchResponse, time: number}>} */ const searchCache = new Map();
const SEARCH_CACHE_LIMIT = 60;
const SEARCH_CACHE_TTL = 8 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 12000;
const KEEPALIVE_INTERVAL_MS = 45 * 1000;
const KEEPALIVE_MIN_GAP_MS = 30 * 1000;
const facetPending = new Map();
const facetRetryCounts = new Map();
/** @type {Partial<Record<FacetKind, number>>} */
const facetInputTimers = {};
const facetRequestSeq = { source: 0, author: 0, tag: 0 };
const facetRequestKeys = { source: "", author: "", tag: "" };
/** @type {Record<"source" | "author" | "tag", AbortController | null>} */
const facetAbortControllers = { source: null, author: null, tag: null };
/** @type {AbortController | null} */ let sourcesAbortController = null;
/** @type {Map<string, {value: ValidatedPreview, time: number}>} */ const previewCache = new Map();
/** @type {Map<string, ParseOCRPage>} */ const parseOCRCache = new Map();
/** @type {Map<string, object>} */ const parseOCRRequests = new Map();
/** @type {WeakMap<HTMLElement, object>} */ const parseArticleRequests = new WeakMap();
const PARSE_OCR_CACHE_LIMIT = 64;
const PREVIEW_CACHE_LIMIT = 20;
const PREVIEW_CACHE_MAX_CHARS = 2 * 1024 * 1024;
const PREVIEW_CACHE_TTL = 10 * 60 * 1000;
let previewCacheChars = 0;
/** @type {number | undefined} */ let pageRecoveryTimer;
let previewRequestSeq = 0;
/** @type {AbortController | null} */ let previewAbortController = null;
/** @type {HTMLElement | null} */ let previewReturnFocus = null;
let previewReturnFocusSelector = "";
let compareRequestSeq = 0;
/** @type {AbortController | null} */ let compareAbortController = null;
let randomRequestSeq = 0;
/** @type {AbortController | null} */ let randomAbortController = null;
let previewCloseUrl = "/";
let previewResultsScrollTop = 0;
let animateNextPreview = false;
/** @type {((event: Event) => void) | null} */ let previewAnimationCleanup = null;
/** @type {{row: HTMLElement, scope: "page" | "article", page: number, thresholds: number[],
 * mode: "resize" | "move" | "select", corner: string, startX: number, startY: number,
 * rect: DOMRect, changed: boolean} | null} */ let parseCropDrag = null;
/** @type {HTMLInputElement | null} */ let activeYearInput = null;
/** @type {{option: HTMLElement, x: number, y: number} | null} */ let yearPickerPointer = null;
let searchComposing = false;
/** @type {Promise<Response | null> | null} */ let keepalivePending = null;
let lastKeepaliveAt = Date.now();

// BEGIN STRICT HTML ESCAPE
/** @param {unknown} value @returns {string} */
function escapeHTML(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[/** @type {"&" | "<" | ">" | '"' | "'"} */ (ch)]);
}
// END STRICT HTML ESCAPE

/** @type {number | undefined} */ let toastTimer;
/** @param {string} message */
function showToast(message) {
  DOM.toast.textContent = message;
  DOM.toast.hidden = false;
  DOM.toast.style.display = "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    DOM.toast.hidden = true;
    DOM.toast.style.display = "none";
  }, 2400);
}

function motionAllowed() {
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function parseUrl() {
  const params = new URLSearchParams(location.search);
  STATE.q = params.get("q") || "";
  STATE.page = Math.max(1, parseInt(params.get("page") || "1", 10) || 1);
  const requestedPageSize = parseInt(params.get("page_size") || "20", 10);
  STATE.pageSize = [10, 20, 50].includes(requestedPageSize) ? requestedPageSize : 20;
  STATE.exact = params.get("exact") !== "0";
  STATE.previewHighlight = params.get("preview_highlight") !== "0";
  STATE.fulltext = params.get("fulltext") !== "0";
  STATE.sort = params.get("sort") || "relevance";
  STATE.sources = params.getAll("source");
  STATE.authors = params.getAll("author");
  STATE.tags = params.getAll("tag");
  STATE.excludeSources = params.getAll("exclude_source");
  STATE.excludeAuthors = params.getAll("exclude_author");
  STATE.excludeTags = params.getAll("exclude_tag");
  STATE.archiveId = params.get("archive") || "";
  STATE.publicationType = params.get("type") || "";
  setDateParts("from", params.get("date_from") || "");
  setDateParts("to", params.get("date_to") || "");
  if (!params.has("date_from")) setDateRangeParts("from", params.get("date_range_from") || "");
  if (!params.has("date_to")) setDateRangeParts("to", params.get("date_range_to") || "");
  const dateTerms = params.getAll("date_term").map(parseDateTermParam).filter(Boolean);
  STATE.dateTerms = uniqDateTerms(dateTerms);
  if (!STATE.dateTerms.length) STATE.dateTerms = [{ year: "", month: "", day: "" }];
  STATE.leftOpen = params.get("sidebar") !== "0";
  STATE.sidebarExpanded = params.get("sidebar_wide") === "1";
  if (!STATE.leftOpen) STATE.sidebarExpanded = false;
  // On a phone the sidebar is a fixed 320px overlay: opening it by default
  // covers the results that the first screen is supposed to show. Only an
  // explicit `sidebar=1` may open it.
  if (STATE.isMobile && !params.has("sidebar")) {
    STATE.leftOpen = false;
    STATE.sidebarExpanded = false;
  }
  STATE.filterPage.source = Math.max(1, parseInt(params.get("source_page") || "1", 10) || 1);
  STATE.filterPage.author = Math.max(1, parseInt(params.get("author_page") || "1", 10) || 1);
  STATE.filterPage.tag = Math.max(1, parseInt(params.get("tag_page") || "1", 10) || 1);
  STATE.preview = params.get("preview") || "";
  STATE.previewFullscreen = Boolean(STATE.preview && !STATE.isMobile && params.get("fullscreen") === "1");
  STATE.compare = params.get("compare") || "";
  STATE.proofread = params.get("proofread") === "1";
  STATE.proofreadFile = params.get("source_file") || "";
  STATE.proofreadPage = params.get("proofread_page") || "";
  STATE.parseMode = params.get("parse") === "1";
  STATE.parsePage = Math.max(1, parseInt(params.get("parse_page") || "1", 10) || 1);
  const requestedParsePageSize = parseInt(params.get("parse_page_size") || "100", 10);
  STATE.parsePageSize = [10, 20, 50, 100].includes(requestedParsePageSize) ? requestedParsePageSize : 100;
  if (STATE.parseMode) {
    STATE.leftOpen = !STATE.isMobile;
    STATE.sidebarExpanded = false;
  }
}

function syncUrl(replace = true) {
  if (STATE.parseMode) {
    const params = new URLSearchParams();
    params.set("parse", "1");
    if (STATE.parsePage > 1) params.set("parse_page", String(STATE.parsePage));
    if (STATE.parsePageSize !== 100) params.set("parse_page_size", String(STATE.parsePageSize));
    history[replace ? "replaceState" : "pushState"]({}, "", `/?${params.toString()}`);
    return;
  }
  const params = new URLSearchParams();
  if (STATE.q) params.set("q", STATE.q);
  if (STATE.page > 1) params.set("page", String(STATE.page));
  if (STATE.pageSize !== 20) params.set("page_size", String(STATE.pageSize));
  if (!STATE.exact) params.set("exact", "0");
  if (!STATE.previewHighlight) params.set("preview_highlight", "0");
  if (!STATE.fulltext) params.set("fulltext", "0");
  if (STATE.sort !== "relevance") params.set("sort", STATE.sort);
  STATE.sources.forEach((source) => params.append("source", source));
  STATE.authors.forEach((author) => params.append("author", author));
  STATE.tags.forEach((tag) => params.append("tag", tag));
  STATE.excludeSources.forEach((source) => params.append("exclude_source", source));
  STATE.excludeAuthors.forEach((author) => params.append("exclude_author", author));
  STATE.excludeTags.forEach((tag) => params.append("exclude_tag", tag));
  if (STATE.archiveId) params.set("archive", STATE.archiveId);
  if (STATE.publicationType) params.set("type", STATE.publicationType);
  const dateFrom = dateValue("from");
  const dateTo = dateValue("to");
  if (dateFrom) params.set("date_from", String(dateFrom));
  if (dateTo) params.set("date_to", String(dateTo));
  if (!dateFrom && dateRangeParam("from")) params.set("date_range_from", dateRangeParam("from"));
  if (!dateTo && dateRangeParam("to")) params.set("date_range_to", dateRangeParam("to"));
  uniqDateTerms(STATE.dateTerms).map(dateTermParam).filter(Boolean).forEach((term) => params.append("date_term", term));
  if (!STATE.leftOpen) params.set("sidebar", "0");
  if (STATE.sidebarExpanded) params.set("sidebar_wide", "1");
  if (STATE.filterPage.source > 1) params.set("source_page", String(STATE.filterPage.source));
  if (STATE.filterPage.author > 1) params.set("author_page", String(STATE.filterPage.author));
  if (STATE.filterPage.tag > 1) params.set("tag_page", String(STATE.filterPage.tag));
  if (STATE.preview) params.set("preview", STATE.preview);
  if (STATE.preview && STATE.previewFullscreen) params.set("fullscreen", "1");
  if (STATE.compare) params.set("compare", STATE.compare);
  if (STATE.proofread) params.set("proofread", "1");
  if (STATE.proofreadFile) params.set("source_file", STATE.proofreadFile);
  if (STATE.proofreadPage) params.set("proofread_page", STATE.proofreadPage);
  const url = params.toString() ? `/?${params.toString()}` : "/";
  if (STATE.preview) {
    const closeParams = new URLSearchParams(params);
    closeParams.delete("preview");
    closeParams.delete("fullscreen");
    previewCloseUrl = closeParams.toString() ? `/?${closeParams.toString()}` : "/";
  }
  const previewEntry = Boolean(STATE.preview && (!replace || (history.state && history.state.previewEntry === true)));
  history[replace ? "replaceState" : "pushState"]({ previewEntry }, "", url);
}

function applyStateToDom() {
  document.body.classList.toggle("light", !STATE.isDark);
  document.body.classList.toggle("mobile", STATE.isMobile);
  document.body.classList.toggle("force-desktop", !STATE.isMobile);
  setPreviewFullscreen(STATE.previewFullscreen);
  document.body.classList.toggle("preview-open", Boolean(STATE.preview));
  document.body.classList.toggle("parse-mode", STATE.parseMode);
  if (DOM.parseBtn) DOM.parseBtn.classList.toggle("primary", STATE.parseMode);
  if (DOM.parseBtn) DOM.parseBtn.setAttribute("aria-pressed", String(STATE.parseMode));
  if (DOM.sidebarTitle) DOM.sidebarTitle.textContent = STATE.parseMode ? "解析队列" : "搜索过滤";
  if (DOM.parsePageSizeOption) DOM.parsePageSizeOption.hidden = !STATE.parseMode;
  DOM.themeBtn.innerHTML = THEME_ICON[STATE.isDark ? "dark" : "light"];
  DOM.themeBtn.setAttribute("aria-label", STATE.isDark ? "切换到白天模式" : "切换到黑夜模式");
  DOM.themeBtn.title = STATE.isDark ? "白天模式" : "黑夜模式";
  if (DOM.mobileToggleIcon) DOM.mobileToggleIcon.className = STATE.isMobile ? "ui-icon ui-icon-phone" : "ui-icon ui-icon-desktop";
  DOM.mobileToggleBtn.setAttribute("aria-pressed", String(STATE.isMobile));
  DOM.searchInput.value = STATE.q;
  DOM.exactToggle.checked = STATE.exact;
  DOM.previewHighlightToggle.checked = STATE.previewHighlight;
  DOM.fulltextToggle.checked = STATE.fulltext;
  DOM.sortFilter.value = STATE.sort;
  DOM.pageSize.value = String(STATE.parseMode ? STATE.parsePageSize : STATE.pageSize);
  DOM.archiveFilter.value = STATE.archiveId;
  DOM.typeFilter.value = STATE.publicationType;
  applyDateStateToDom();
  DOM.clearFilters.hidden = !hasActiveFilters();
  DOM.leftSidebar.classList.toggle("collapsed", !STATE.leftOpen);
  DOM.leftSidebar.classList.toggle("open", STATE.leftOpen);
  DOM.leftSidebar.classList.toggle("expanded-wide", STATE.leftOpen && STATE.sidebarExpanded);
  const sidebarOverlayOpen = STATE.isMobile && STATE.leftOpen;
  DOM.overlay.style.display = "";
  DOM.overlay.classList.toggle("open", sidebarOverlayOpen);
  DOM.leftToggle.setAttribute("aria-expanded", String(STATE.leftOpen));
  DOM.leftToggle.setAttribute("aria-label", STATE.leftOpen ? "关闭搜索过滤" : "打开搜索过滤");
  const previewModal = previewIsModal();
  DOM.leftSidebar.setAttribute("aria-hidden", String(!STATE.leftOpen || previewModal));
  DOM.leftSidebar.inert = !STATE.leftOpen || previewModal;
  if (DOM.sidebarExpand) {
    const expanded = STATE.sidebarExpanded && STATE.leftOpen;
    DOM.sidebarExpand.setAttribute("aria-expanded", String(expanded));
    DOM.sidebarExpand.setAttribute("aria-label", expanded ? "收起过滤栏" : "展开过滤栏");
  }
  for (const key of ["source", "author", "tag"]) {
    const collapsed = Boolean(STATE.filterCollapsed[key]);
    const body = document.querySelector(`[data-filter-body="${key}"]`);
    const toggle = document.querySelector(`[data-filter-toggle="${key}"]`);
    if (body instanceof HTMLElement) {
      body.classList.toggle("collapsed", collapsed);
      body.inert = collapsed;
      body.setAttribute("aria-hidden", String(collapsed));
    }
    if (toggle) {
      toggle.classList.toggle("collapsed", collapsed);
      toggle.setAttribute("aria-expanded", String(!collapsed));
    }
  }
  const previewThemeButton = DOM.previewPanel ? DOM.previewPanel.querySelector("[data-preview-theme]") : null;
  if (previewThemeButton instanceof HTMLElement) {
    previewThemeButton.setAttribute("aria-label", STATE.isDark ? "切换到白天模式" : "切换到黑夜模式");
    previewThemeButton.title = STATE.isDark ? "白天模式" : "黑夜模式";
    previewThemeButton.innerHTML = THEME_ICON[STATE.isDark ? "dark" : "light"];
  }
  const previewMobileButton = DOM.previewPanel ? DOM.previewPanel.querySelector("[data-preview-mobile]") : null;
  if (previewMobileButton instanceof HTMLElement) {
    const mobileLabel = STATE.isMobile ? "切换到电脑模式" : "切换到手机模式";
    previewMobileButton.setAttribute("aria-label", mobileLabel);
    previewMobileButton.title = mobileLabel;
    previewMobileButton.innerHTML = `<span class="ui-icon ${STATE.isMobile ? "ui-icon-phone" : "ui-icon-desktop"}" aria-hidden="true"></span>`;
  }
}

function toggleTheme() {
  STATE.isDark = !STATE.isDark;
  localStorage.setItem("theme", STATE.isDark ? "dark" : "light");
  applyStateToDom();
}

function toggleMobileMode() {
  STATE.isMobile = !STATE.isMobile;
  localStorage.setItem("mobileMode", STATE.isMobile ? "mobile" : "desktop");
  if (STATE.isMobile) {
    STATE.leftOpen = false;
    STATE.sidebarExpanded = false;
  }
  applyStateToDom();
  relocateFulltextEditor();
  trimPDFDocumentCache();
  syncUrl();
}

function relocateFulltextEditor() {
  if (!STATE.proofread) return;
  const form = DOM.previewPanel ? DOM.previewPanel.querySelector("[data-proofread-form]") : null;
  if (!form) return;
  const box = form.querySelector("[data-fulltext-box]");
  const toggle = form.querySelector("[data-fulltext-toggle]");
  if (!box || !toggle) return;
  const header = form.querySelector(".proofread-pane-header");
  if (STATE.isMobile) {
    if (box.closest("[data-fulltext-section]")) return;
    const section = document.createElement("div");
    section.className = "proofread-fulltext-section";
    section.dataset.fulltextSection = "true";
    const actions = document.createElement("div");
    actions.className = "proofread-form-actions";
    actions.append(toggle);
    section.append(actions, box);
    if (header && header.nextSibling) header.after(section); else form.prepend(section);
  } else {
    const section = box.closest("[data-fulltext-section]");
    if (!section) return;
    const fieldset = Array.from(form.querySelectorAll("fieldset")).find((f) => f.querySelector("[data-part-editor]")) || form.querySelector("fieldset");
    if (!fieldset) return;
    section.remove();
    const actions = fieldset.querySelector(".proofread-form-actions");
    const partEditor = fieldset.querySelector("[data-part-editor]");
    if (actions) {
      const partToggle = fieldset.querySelector("[data-part-editor-toggle]");
      actions.insertBefore(toggle, partToggle || null);
      if (partEditor) fieldset.insertBefore(box, partEditor);
    } else {
      fieldset.prepend(toggle);
      fieldset.insertBefore(box, toggle.nextSibling);
    }
  }
}

function hasActiveFilters() {
  return Boolean(
    STATE.sources.length ||
    STATE.authors.length ||
    STATE.tags.length ||
    STATE.excludeSources.length ||
    STATE.excludeAuthors.length ||
    STATE.excludeTags.length ||
    STATE.archiveId ||
    STATE.publicationType ||
    hasDateFilter()
  );
}

// BEGIN STRICT DATE RANGES
/** @param {"from" | "to"} side @param {string} value */
function setDateParts(side, value) {
  const text = String(value || "").replace(/\D/g, "");
  const prefix = side === "from" ? "dateFrom" : "dateTo";
  STATE[`${prefix}Year`] = text.length >= 4 ? cleanYear(text.slice(0, 4)) : "";
  STATE[`${prefix}Month`] = text.length >= 6 ? String(parseInt(text.slice(4, 6), 10)) : "";
  STATE[`${prefix}Day`] = text.length >= 8 ? String(parseInt(text.slice(6, 8), 10)) : "";
}

/** @param {"from" | "to"} side @param {string} value */
function setDateRangeParts(side, value) {
  const term = parseDateTermParam(value);
  const prefix = side === "from" ? "dateFrom" : "dateTo";
  STATE[`${prefix}Year`] = term ? (term.year || "") : "";
  STATE[`${prefix}Month`] = term ? (term.month || "") : "";
  STATE[`${prefix}Day`] = term ? (term.day || "") : "";
}

function hasDateFilter() {
  return Boolean(
    STATE.dateFromYear ||
    STATE.dateFromMonth ||
    STATE.dateFromDay ||
    STATE.dateToYear ||
    STATE.dateToMonth ||
    STATE.dateToDay ||
    STATE.dateTerms.some((term) => term.year || term.month || term.day)
  );
}

/** @param {string} year @param {string} month */
function daysInMonth(year, month) {
  return new Date(Number(year), Number(month), 0).getDate();
}

/** @param {string} value */
function padded(value) {
  return String(value).padStart(2, "0");
}

/** @param {"from" | "to"} side @returns {number | null} */
function dateValue(side) {
  const prefix = side === "from" ? "dateFrom" : "dateTo";
  const year = cleanYear(STATE[`${prefix}Year`]);
  if (!year || !STATE[`${prefix}Month`] || !STATE[`${prefix}Day`]) return null;
  const month = STATE[`${prefix}Month`];
  const day = STATE[`${prefix}Day`];
  return parseInt(`${year}${padded(month)}${padded(day)}`, 10);
}

/** @param {"from" | "to"} side @returns {number | null} */
function rangeDateValue(side) {
  const prefix = side === "from" ? "dateFrom" : "dateTo";
  const year = cleanYear(STATE[`${prefix}Year`]);
  if (!year) return null;
  const month = STATE[`${prefix}Month`] || (side === "from" ? "1" : "12");
  const day = STATE[`${prefix}Day`] || (side === "from" ? "1" : String(daysInMonth(year, month)));
  return parseInt(`${year}${padded(month)}${padded(day)}`, 10);
}

/** @param {"from" | "to"} side */
function dateKey(side) {
  const value = dateValue(side);
  return value ? String(value) : "";
}

/** @param {{show?: boolean}} [options] */
function validateDateRange({ show = true } = {}) {
  const from = rangeDateValue("from");
  const to = rangeDateValue("to");
  const invalid = Boolean(from && to && from > to);
  if (DOM.dateError) {
    DOM.dateError.hidden = !invalid;
    DOM.dateError.textContent = invalid ? "开始年份不能晚于结束年份" : "";
  }
  if (invalid && show) showToast("开始年份不能晚于结束年份");
  return !invalid;
}

function clearDateFilter() {
  setDateParts("from", "");
  setDateParts("to", "");
  STATE.dateTerms = [{ year: "", month: "", day: "" }];
}
// END STRICT DATE RANGES

// BEGIN STRICT DATE INPUT UPDATE
/** @typedef {"dateFromYear" | "dateFromMonth" | "dateFromDay" | "dateToYear" | "dateToMonth" | "dateToDay"} DateInputKey */
/** @param {DateInputKey} key @param {string} value */
function updateDateInputState(key, value) {
  STATE[key] = value;
  if (key.endsWith("Year")) STATE[key] = cleanYear(value);
  const prefix = key.startsWith("dateFrom") ? "dateFrom" : "dateTo";
  if (key.endsWith("Year") && !STATE[key]) {
    STATE[`${prefix}Month`] = "";
    STATE[`${prefix}Day`] = "";
  }
  if (key.endsWith("Month")) {
    const day = STATE[`${prefix}Day`];
    const year = STATE[`${prefix}Year`];
    if (!value) STATE[`${prefix}Day`] = "";
    if (day && year && value && Number(day) > daysInMonth(year, value)) STATE[`${prefix}Day`] = "";
  }
}
// END STRICT DATE INPUT UPDATE

function yearItems() {
  /** @param {unknown} value */ const isFourDigitYear = (value) => /^\d{4}$/.test(String(value));
  const years = (STATE.facets.years || []).map((item) => String(item.value == null ? item.name : item.value)).filter(isFourDigitYear);
  const selectedYears = [STATE.dateFromYear, STATE.dateToYear, ...STATE.dateTerms.map((term) => term.year)].filter(isFourDigitYear);
  const values = years.length ? years : Array.from({ length: 183 }, (_, index) => String(1843 + index));
  return [...new Set([...values, ...selectedYears])].sort((a, b) => Number(a) - Number(b));
}

/** @param {(string | number)[]} values @param {string | number | null | undefined} selected @param {string} emptyLabel */
function selectOptions(values, selected, emptyLabel) {
  const options = [`<option value="">${emptyLabel}</option>`];
  for (const value of values) {
    const text = String(value);
    options.push(`<option value="${escapeHTML(text)}" ${String(selected) === text ? "selected" : ""}>${escapeHTML(text)}</option>`);
  }
  return options.join("");
}

/** @param {string} year @param {string} month @param {string} selected */
function dayOptions(year, month, selected) {
  const max = year && month ? daysInMonth(year, month) : 31;
  return selectOptions(Array.from({ length: max }, (_, index) => index + 1), selected, "日");
}

function renderPeriodPresets() {
  const currentFrom = dateKey("from");
  const currentTo = dateKey("to");
  DOM.periodPresets.innerHTML = PERIOD_PRESETS.map((period) => {
    const active = currentFrom === period.from && currentTo === period.to;
    return `<button type="button" class="period-btn ${active ? "active" : ""}" data-period-from="${period.from}" data-period-to="${period.to}">${escapeHTML(period.label)}</button>`;
  }).join("");
}

function renderDateControls() {
  DOM.dateFromYear.value = STATE.dateFromYear;
  DOM.dateToYear.value = STATE.dateToYear;
  DOM.dateFromMonth.innerHTML = selectOptions(Array.from({ length: 12 }, (_, index) => index + 1), STATE.dateFromMonth, "月");
  DOM.dateToMonth.innerHTML = selectOptions(Array.from({ length: 12 }, (_, index) => index + 1), STATE.dateToMonth, "月");
  DOM.dateFromDay.innerHTML = dayOptions(STATE.dateFromYear, STATE.dateFromMonth, STATE.dateFromDay);
  DOM.dateToDay.innerHTML = dayOptions(STATE.dateToYear, STATE.dateToMonth, STATE.dateToDay);
  DOM.dateFromMonth.disabled = !STATE.dateFromYear;
  DOM.dateToMonth.disabled = !STATE.dateToYear;
  DOM.dateFromDay.disabled = !STATE.dateFromYear || !STATE.dateFromMonth;
  DOM.dateToDay.disabled = !STATE.dateToYear || !STATE.dateToMonth;
  DOM.dateClear.hidden = !hasDateFilter();
  renderPeriodPresets();
  renderDateTerms();
  validateDateRange({ show: false });
}

// BEGIN STRICT DATE TERMS
/** @typedef {{year?: string | null, month?: string | null, day?: string | null, display?: string | null}} DateTerm */
/** @typedef {{year: string, month: string, day: string, display: string}} NormalizedDateTerm */

/** @param {unknown} value @returns {string} */
function cleanYear(value) {
  const text = String(value || "").replace(/\D/g, "").slice(0, 4);
  const number = parseInt(text, 10);
  return Number.isFinite(number) && number >= 1 && number <= 9999 ? String(number) : "";
}

/** @param {unknown} value @returns {NormalizedDateTerm | null} */
function parseDateTermParam(value) {
  const parts = String(value || "").split("-");
  const term = { year: "", month: "", day: "", display: "" };
  for (const part of parts) {
    const [kind, rawValue] = part.split(":");
    if (kind === "display") {
      term.display = decodeURIComponent(rawValue || "");
      continue;
    }
    if (!["year", "month", "day"].includes(kind)) continue;
    const number = parseInt(rawValue, 10);
    if (!Number.isFinite(number)) continue;
    if (kind === "year" && number >= 1 && number <= 9999) term.year = String(number);
    if (kind === "month" && number >= 1 && number <= 12) term.month = String(number);
    if (kind === "day" && number >= 1 && number <= 31) term.day = String(number);
  }
  return term.year || term.month || term.day || term.display ? term : null;
}

/** @param {DateTerm} term @returns {string} */
function dateTermParam(term) {
  const parts = [];
  const year = cleanYear(term.year);
  if (year) parts.push(`year:${year}`);
  if (term.month) parts.push(`month:${term.month}`);
  if (term.day) parts.push(`day:${term.day}`);
  if (term.display) parts.push(`display:${encodeURIComponent(term.display)}`);
  return parts.join("-");
}

/** @param {DateTerm | null | undefined} term @returns {NormalizedDateTerm} */
function normalizedDateTerm(term) {
  const monthNumber = term && term.month ? parseInt(term.month, 10) : null;
  const dayNumber = term && term.day ? parseInt(term.day, 10) : null;
  return {
    year: cleanYear(term && term.year) || "",
    month: monthNumber && monthNumber >= 1 && monthNumber <= 12 ? String(monthNumber) : "",
    day: dayNumber && dayNumber >= 1 && dayNumber <= 31 ? String(dayNumber) : "",
    display: term && typeof term.display === "string" ? term.display : "",
  };
}

/** @param {DateTerm | null | undefined} term @returns {string} */
function dateTermKey(term) {
  const normalized = normalizedDateTerm(term);
  return normalized.display ? `display|${normalized.display}` : `${normalized.year}|${normalized.month}|${normalized.day}`;
}

/** @param {(DateTerm | null)[] | null | undefined} terms @returns {NormalizedDateTerm[]} */
function uniqDateTerms(terms) {
  const seen = new Set();
  const result = [];
  for (const term of terms || []) {
    const normalized = normalizedDateTerm(term);
    if (!(normalized.year || normalized.month || normalized.day || normalized.display)) continue;
    const key = dateTermKey(normalized);
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(normalized);
  }
  return result;
}

/** @param {unknown} value @returns {{year: string, month: string, day: string} | null} */
function parseDisplayDateTerm(value) {
  const parts = String(value || "").split("/").map((part) => part.trim());
  let year = "";
  let month = "";
  let day = "";
  if (parts.length >= 3) {
    year = parts[0] && parts[0] !== "----" ? cleanYear(parts[0]) : "";
    const monthNumber = parts[1] && parts[1] !== "--" ? parseInt(parts[1], 10) : null;
    const dayNumber = parts[2] && parts[2] !== "--" ? parseInt(parts[2], 10) : null;
    month = monthNumber && monthNumber >= 1 && monthNumber <= 12 ? String(monthNumber) : "";
    day = dayNumber && dayNumber >= 1 && dayNumber <= 31 ? String(dayNumber) : "";
  } else {
    const numbers = String(value || "").match(/\d{1,4}/g) || [];
    if (numbers.length >= 2) {
      const first = parseInt(numbers[0] || "", 10);
      const second = parseInt(numbers[1] || "", 10);
      if (first >= 1 && first <= 12 && second >= 1 && second <= 31) {
        month = String(first);
        day = String(second);
      } else {
        year = cleanYear(numbers[0]);
        month = second >= 1 && second <= 12 ? String(second) : "";
      }
    } else if (numbers.length === 1) {
      year = cleanYear(numbers[0]);
    }
  }
  return year || month || day ? { year, month, day } : null;
}

/** @param {unknown} value @returns {string} */
function resultDateTermKey(value) {
  const text = String(value || "").trim();
  const parsed = parseDisplayDateTerm(text);
  return parsed ? dateTermKey(parsed) : (text ? `display|${text}` : "");
}

/** @param {unknown} value @returns {DateTerm | null} */
function resultDateTermPayload(value) {
  const display = String(value || "").trim();
  if (!display) return null;
  return parseDisplayDateTerm(display) || { year: null, month: null, day: null, display };
}
// END STRICT DATE TERMS

/** @param {"from" | "to"} side */
function dateRangeParam(side) {
  const prefix = side === "from" ? "dateFrom" : "dateTo";
  return dateTermParam({
    year: STATE[`${prefix}Year`],
    month: STATE[`${prefix}Month`],
    day: STATE[`${prefix}Day`],
  });
}

function renderDateTerms() {
  if (!DOM.dateTerms) return;
  DOM.dateTerms.innerHTML = STATE.dateTerms.map((term, index) => {
    const monthOptions = selectOptions(Array.from({ length: 12 }, (_, item) => item + 1), term.month || "", "月");
    const dayOptionsHtml = selectOptions(Array.from({ length: 31 }, (_, item) => item + 1), term.day || "", "日");
    return `<div class="date-term-row" data-date-term-index="${index}"><input class="date-select year-input" aria-label="包含日期第 ${index + 1} 项年份" data-date-term-field="year" inputmode="numeric" placeholder="年" autocomplete="off" value="${escapeHTML(term.year || "")}"><select class="date-select" aria-label="包含日期第 ${index + 1} 项月份" data-date-term-field="month">${monthOptions}</select><select class="date-select" aria-label="包含日期第 ${index + 1} 项日期" data-date-term-field="day">${dayOptionsHtml}</select><button type="button" class="text-btn-sm" aria-label="删除包含日期第 ${index + 1} 项" data-date-term-remove="${index}">删除</button></div>`;
  }).join("");
}

/** @param {EventTarget | null} target */
function yearInputFromTarget(target) {
  const input = target instanceof Element ? target.closest(".year-input") : null;
  return input instanceof HTMLInputElement ? input : null;
}

/** @param {HTMLInputElement} input */
function showYearPicker(input) {
  activeYearInput = input;
  const keyword = input.value.trim();
  const years = yearItems().filter((year) => !keyword || String(year).includes(keyword));
  DOM.yearPicker.innerHTML = years.length
    ? years.map((year) => `<button type="button" class="year-option ${String(year) === input.value ? "active" : ""}" data-year-value="${escapeHTML(year)}">${escapeHTML(year)}</button>`).join("")
    : '<div class="date-empty">无匹配年份</div>';
  const rect = input.getBoundingClientRect();
  const gap = 4;
  const viewportPadding = 8;
  const width = Math.max(rect.width, 92);
  const preferredHeight = Math.min(360, Math.floor(window.innerHeight * 0.52));
  const spaceBelow = window.innerHeight - rect.bottom - gap - viewportPadding;
  const spaceAbove = rect.top - gap - viewportPadding;
  const openAbove = spaceBelow < Math.min(180, preferredHeight) && spaceAbove > spaceBelow;
  const availableHeight = Math.max(80, Math.min(preferredHeight, openAbove ? spaceAbove : spaceBelow));
  const left = Math.max(viewportPadding, Math.min(rect.left, window.innerWidth - width - viewportPadding));
  DOM.yearPicker.style.left = `${left}px`;
  DOM.yearPicker.style.width = `${width}px`;
  DOM.yearPicker.style.maxHeight = `${availableHeight}px`;
  DOM.yearPicker.style.top = openAbove ? "auto" : `${rect.bottom + gap}px`;
  DOM.yearPicker.style.bottom = openAbove ? `${window.innerHeight - rect.top + gap}px` : "auto";
  DOM.yearPicker.hidden = false;
}

function repositionYearPicker() {
  if (activeYearInput && !DOM.yearPicker.hidden) showYearPicker(activeYearInput);
}

function hideYearPicker() {
  DOM.yearPicker.hidden = true;
  activeYearInput = null;
}

/** @param {Event} event */
function handleYearPickerScroll(event) {
  if (event.target instanceof Node && DOM.yearPicker.contains(event.target)) return;
  if (activeYearInput && document.activeElement === activeYearInput) {
    repositionYearPicker();
    return;
  }
  hideYearPicker();
}

/** @param {HTMLInputElement} input @param {string} year */
function chooseYear(input, year) {
  input.value = year;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  hideYearPicker();
}

function applyDateStateToDom() {
  if (DOM.dateFromYear) renderDateControls();
}

// BEGIN STRICT PREVIEW HIGHLIGHT
/** @param {unknown} value @returns {string} */
function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function queryTerms() {
  const q = STATE.q.trim();
  return q.split(/\s+/).map((item) => item.trim()).filter(Boolean);
}

/** @param {unknown} text @returns {string} */
function highlightPreviewText(text) {
  let html = escapeHTML(text);
  if (!STATE.previewHighlight || !STATE.q.trim()) return html;
  if (STATE.exact) {
    const pattern = new RegExp(escapeRegExp(escapeHTML(STATE.q.trim())), "g");
    return html.replace(pattern, (match) => match ? `<mark>${match}</mark>` : match);
  }
  const terms = [...new Set(queryTerms().map(escapeHTML).filter(Boolean))].sort((a, b) => b.length - a.length);
  if (!terms.length) return html;
  const pattern = new RegExp(terms.map(escapeRegExp).join("|"), "g");
  return html.replace(pattern, (match) => `<mark>${match}</mark>`);
}
// END STRICT PREVIEW HIGHLIGHT

function cancelPreviewRequests() {
  previewRequestSeq += 1;
  compareRequestSeq += 1;
  randomRequestSeq += 1;
  if (previewAbortController) previewAbortController.abort();
  if (compareAbortController) compareAbortController.abort();
  if (randomAbortController) randomAbortController.abort();
  previewAbortController = null;
  compareAbortController = null;
  randomAbortController = null;
}

/** @param {boolean} fullscreen */
function previewFullscreenIcon(fullscreen) {
  const path = fullscreen
    ? "M8 3v5H3m18 0h-5V3M3 16h5v5m8 0v-5h5"
    : "M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5";
  return `<svg class="preview-fullscreen-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="${path}"/></svg>`;
}

function previewFullscreenButtonHTML() {
  return `<button type="button" class="icon-btn-sm preview-fullscreen-btn" data-preview-fullscreen aria-label="全屏预览" title="全屏预览" aria-pressed="false">${previewFullscreenIcon(false)}</button>`;
}

/** @param {boolean} fullscreen @param {boolean} [sync] */
function setPreviewFullscreen(fullscreen, sync = false) {
  const expanded = Boolean(fullscreen && !STATE.isMobile);
  STATE.previewFullscreen = expanded;
  if (expanded) stopPreviewAnimation();
  DOM.previewPanel.classList.toggle("preview-fullscreen", expanded);
  if (sync && STATE.preview) syncUrl(true);
  if (STATE.preview && DOM.previewPanel && !DOM.previewPanel.hidden) setPreviewModalState(true);
  const button = DOM.previewPanel.querySelector("[data-preview-fullscreen]");
  if (!(button instanceof HTMLElement)) return;
  const label = expanded ? "还原预览" : "全屏预览";
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-pressed", String(expanded));
  button.title = label;
  button.innerHTML = previewFullscreenIcon(expanded);
}

/** @returns {HTMLElement[]} */
function previewFocusableElements() {
  if (!DOM.previewPanel || DOM.previewPanel.hidden) return [];
  return Array.from(DOM.previewPanel.querySelectorAll("button, a[href], input, select, textarea, [tabindex]:not([tabindex='-1'])"))
    .filter((node) => node instanceof HTMLElement)
    .filter((node) => !("disabled" in node && node.disabled) && !node.hidden && node.getClientRects().length);
}

function previewIsModal() {
  return Boolean(STATE.preview && (STATE.isMobile || STATE.previewFullscreen));
}

/** @param {boolean} open */
function setPreviewModalState(open) {
  const modal = Boolean(open && previewIsModal());
  const background = [DOM.header, DOM.leftSidebar, DOM.statusBar, DOM.results, DOM.emptyState, DOM.loadInfo];
  for (const node of background) {
    if (!node) continue;
    const inert = modal || (node === DOM.leftSidebar && !STATE.leftOpen);
    node.inert = inert;
    if (inert) node.setAttribute("aria-hidden", "true");
    else node.removeAttribute("aria-hidden");
  }
  if (DOM.previewPanel) DOM.previewPanel.setAttribute("aria-modal", String(modal));
}

function labelPreviewPanel() {
  setPreviewFullscreen(STATE.previewFullscreen);
  const title = DOM.previewPanel && DOM.previewPanel.querySelector(".preview-title");
  if (!title) return;
  title.id = "preview-title";
  DOM.previewPanel.setAttribute("aria-labelledby", "preview-title");
}

function restorePreviewFocus() {
  const node = previewReturnFocus;
  const selector = previewReturnFocusSelector;
  previewReturnFocus = null;
  previewReturnFocusSelector = "";
  requestAnimationFrame(() => {
    const target = node && node.isConnected ? node : selector ? document.querySelector(selector) : null;
    if (!(target instanceof HTMLElement) || target.inert) return;
    target.focus({ preventScroll: true });
  });
}

// Native reader hooks (shared static/tts-client.js). The client is injected on
// the first preview so the default screen does not pay for its parse/download.
// Reading continues across preview changes; the panel is moved out before any
// preview DOM replacement.
/** @typedef {{previewClosed: () => void, previewPending?: () => void, previewLoaded: (value: {id: string, title: string, text: string}) => void}} BhaSpeechClient */
const speechHost = /** @type {Window & {CCRDSpeech?: BhaSpeechClient | null}} */ (window);
/** @type {Promise<BhaSpeechClient | null> | null} */ let speechClientPromise = null;

function ensureSpeechClient() {
  if (speechHost.CCRDSpeech) return Promise.resolve(speechHost.CCRDSpeech);
  if (speechClientPromise) return speechClientPromise;
  speechClientPromise = new Promise((resolve) => {
    const script = document.createElement("script");
    const versionNode = document.querySelector('meta[name="bha-tts-client-version"]');
    const version = versionNode instanceof HTMLMetaElement ? String(versionNode.content || "") : "";
    script.src = "/static/tts-client.js" + (version ? `?v=${encodeURIComponent(version)}` : "");
    script.async = true;
    script.onload = () => {
      if (!speechHost.CCRDSpeech) speechClientPromise = null;
      resolve(speechHost.CCRDSpeech || null);
    };
    script.onerror = () => {
      speechClientPromise = null;
      resolve(null);
    };
    document.head.appendChild(script);
  });
  return speechClientPromise;
}

function speechPreviewClosed() {
  speechHost.CCRDSpeech?.previewClosed();
}

/** @param {string} docId @param {{title?: string | null, publication_name?: string | null}} item */
function speechPreviewLoaded(docId, item) {
  const body = DOM.previewPanel.querySelector(".preview-body");
  if (!speechHost.CCRDSpeech || !body) return;
  // Speak exactly the rendered text so source offsets match the DOM.
  speechHost.CCRDSpeech.previewLoaded({ id: docId, title: item.title || item.publication_name || "朗读", text: body.textContent || "" });
}

function clearPreviewDom() {
  flushProofreadDraft();
  clearActiveMediaViewer();
  stopPreviewAnimation();
  setPreviewFullscreen(false);
  document.body.classList.remove("preview-open");
  DOM.previewPanel.hidden = true;
  DOM.previewPanel.style.display = "none";
  speechPreviewClosed();
  DOM.previewPanel.innerHTML = "";
  DOM.previewPanel.classList.remove("proofread-mode");
  DOM.previewPanel.removeAttribute("aria-labelledby");
  setPreviewModalState(false);
  restorePreviewFocus();
  animateNextPreview = false;
}

function closePreview(useHistory = false) {
  flushProofreadDraft();
  cancelPreviewRequests();
  if (useHistory && history.state && history.state.previewEntry === true) {
    history.back();
    return;
  }
  STATE.preview = "";
  STATE.compare = "";
  STATE.proofread = false;
  STATE.proofreadItem = null;
  STATE.proofreadFile = "";
  STATE.proofreadPage = "";
  clearPreviewDom();
  renderResults();
  syncUrl();
}

/** @param {number} top */
function restoreResultsScroll(top) {
  requestAnimationFrame(() => {
    DOM.results.scrollTop = top;
    requestAnimationFrame(() => { DOM.results.scrollTop = top; });
  });
}

/** @param {string} path @param {RequestInit} [options] @returns {Promise<unknown>} */
async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = "";
    try {
      const body = /** @type {unknown} */ (await response.json());
      if (isJsonObject(body) && typeof body.error === "string") detail = `：${body.error}`;
    } catch (_error) {
      // keep the status-only message
    }
    throw new Error(`HTTP ${response.status}${detail}`);
  }
  return response.json();
}

// BEGIN STRICT SEARCH RESPONSE CONTRACTS
/** @param {unknown} value @returns {value is Record<string, unknown>} */
function isJsonObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
/** @param {unknown} value @returns {value is string[]} */
function isStringList(value) { return Array.isArray(value) && value.every((item) => typeof item === "string"); }
/** @param {unknown} value @returns {value is SearchResult} */
function isSearchResult(value) {
  return isJsonObject(value) && typeof value.doc_id === "string" && value.doc_id.length > 0 &&
    (value.title == null || typeof value.title === "string") && (value.title_highlight == null || typeof value.title_highlight === "string") &&
    (value.publication_name == null || typeof value.publication_name === "string") &&
    (value.publication_type == null || typeof value.publication_type === "string") &&
    (value.publication_author == null || typeof value.publication_author === "string") &&
    Number.isSafeInteger(value.source_file_count) && Number(value.source_file_count) >= 0 &&
    isStringList(value.date_display) && isStringList(value.authors) && isStringList(value.tag_names) && isStringList(value.snippet);
}
/** @param {unknown} value @returns {value is SearchResponse} */
function isSearchResponse(value) {
  return isJsonObject(value) && Array.isArray(value.results) && value.results.every(isSearchResult) &&
    Number.isSafeInteger(value.total) && Number(value.total) >= 0 && Number.isSafeInteger(value.page) && Number(value.page) >= 1 &&
    Number.isSafeInteger(value.page_size) && Number(value.page_size) >= 1 &&
    typeof value.indexing === "boolean" && typeof value.index_status === "string";
}
/** @param {unknown} value @returns {value is FacetItem} */
function isFacetItem(value) {
  return isJsonObject(value) && (value.name == null || typeof value.name === "string") &&
    (value.value == null || typeof value.value === "string" || typeof value.value === "number" && Number.isFinite(value.value)) &&
    (value.count == null || typeof value.count === "number" && Number.isSafeInteger(value.count) && value.count >= 0);
}
/** @param {unknown} value @returns {value is BootstrapResponse} */
function isBootstrapResponse(value) {
  if (!isJsonObject(value) || typeof value.generation !== "string" || !isSearchResponse(value.search) || !isJsonObject(value.sources)) return false;
  const sources = value.sources;
  return ["archives", "types", "years"].every((key) => sources[key] === undefined ||
    Array.isArray(sources[key]) && sources[key].every(isFacetItem));
}
/** @param {unknown} value @returns {value is FacetItem[]} */
function isFacetList(value) { return Array.isArray(value) && value.every(isFacetItem); }
/** @param {unknown} value @returns {value is FacetPage} */
function isFacetPage(value) {
  return isJsonObject(value) && isFacetList(value.items) && Number.isSafeInteger(value.total) && Number(value.total) >= 0 &&
    Number.isSafeInteger(value.page) && Number(value.page) >= 1 && Number.isSafeInteger(value.page_size) && Number(value.page_size) >= 1 &&
    typeof value.has_more === "boolean" && (value.indexing === undefined || typeof value.indexing === "boolean") &&
    (value.index_status === undefined || typeof value.index_status === "string");
}
/** @param {unknown} value @returns {value is FacetSourcesResponse} */
function isFacetSourcesResponse(value) {
  return isJsonObject(value) && isFacetList(value.sources) && isFacetList(value.authors) && isFacetList(value.tags) &&
    isFacetList(value.archives) && isFacetList(value.types) && isFacetList(value.years) &&
    (value.indexing === undefined || typeof value.indexing === "boolean") &&
    (value.index_status === undefined || typeof value.index_status === "string");
}
/** @param {unknown} value @returns {value is BootstrapFacetsResponse} */
function isBootstrapFacetsResponse(value) {
  return isJsonObject(value) && typeof value.generation === "string" && isJsonObject(value.facets) &&
    isFacetPage(value.facets.source) && isFacetPage(value.facets.author) && isFacetPage(value.facets.tag);
}
/** @template T @param {unknown} value @param {(value: unknown) => value is T} check @param {string} label @returns {T} */
function parseBhaPayload(value, check, label) {
  if (!check(value)) throw new Error(`${label}响应格式无效`);
  return value;
}
/** @param {unknown} value @returns {SearchResponse} */
function parseSearchResponse(value) {
  if (!isSearchResponse(value)) throw new Error("搜索响应格式无效");
  return value;
}
/** @param {unknown} value @returns {value is ParseQueueResponse} */
function isParseQueueResponse(value) {
  const optionalNumber = (/** @type {unknown} */ number) => number === null || typeof number === "number" && Number.isSafeInteger(number);
  const optionalString = (/** @type {unknown} */ text) => text === null || typeof text === "string";
  return isJsonObject(value) && Number.isSafeInteger(value.total) && Number(value.total) >= 0 &&
    Number.isSafeInteger(value.page) && Number(value.page) >= 1 && Number.isSafeInteger(value.page_size) && Number(value.page_size) >= 1 &&
    Array.isArray(value.items) && value.items.every((item) => isJsonObject(item) &&
      typeof item.doc_id === "string" && item.doc_id.length > 0 && Number.isSafeInteger(item.archive_id) &&
      optionalString(item.title) && optionalString(item.publication_name) && optionalNumber(item.page_start) && optionalNumber(item.page_end) &&
      isStringList(item.date_display) && isStringList(item.authors) && isStringList(item.source_files));
}
// END STRICT SEARCH RESPONSE CONTRACTS

// BEGIN STRICT PREVIEW RESPONSE CONTRACTS
/** @param {unknown} value @returns {boolean} */
function isPreviewSourceFile(value) {
  if (!isJsonObject(value) || !Number.isSafeInteger(value.index) || Number(value.index) < 0 ||
      typeof value.name !== "string" || typeof value.url !== "string") return false;
  if (value.preview_url !== undefined && typeof value.preview_url !== "string") return false;
  if (value.initial_page !== undefined && typeof value.initial_page !== "string") return false;
  if (value.pages !== undefined && !isStringList(value.pages)) return false;
  for (const key of ["page_count", "initial_page_number"]) {
    if (value[key] !== undefined && (!Number.isSafeInteger(value[key]) || Number(value[key]) < 1)) return false;
  }
  return value.page_numbers === undefined || Array.isArray(value.page_numbers) &&
    value.page_numbers.every((number) => Number.isSafeInteger(number) && number > 0);
}
/** @param {unknown} value @returns {value is ValidatedPreview} */
function isPreviewResponse(value) {
  return isJsonObject(value) && typeof value.doc_id === "string" && value.doc_id.length > 0 &&
    typeof value.content === "string" && (value.title == null || typeof value.title === "string") &&
    (value.publication_name == null || typeof value.publication_name === "string") &&
    (value.publication_author == null || typeof value.publication_author === "string") &&
    (value.publication_type == null || typeof value.publication_type === "string") &&
    Number.isSafeInteger(value.source_file_count) && Number(value.source_file_count) >= 0 &&
    isStringList(value.authors) && isStringList(value.tag_names) && isStringList(value.date_display) &&
    Array.isArray(value.source_files) && value.source_files.every(isPreviewSourceFile) &&
    Array.isArray(value.variants) && value.variants.every((variant) => isJsonObject(variant) &&
      typeof variant.doc_id === "string" && typeof variant.publication_name === "string") && isJsonObject(value.article);
}
/** @param {unknown} value @param {string} expectedId @returns {ValidatedPreview} */
function parsePreviewResponse(value, expectedId) {
  if (!isPreviewResponse(value) || value.doc_id !== expectedId) throw new Error("预览响应格式无效");
  return value;
}
// END STRICT PREVIEW RESPONSE CONTRACTS

/** @param {string} path @param {RequestInit} [options] @param {number} [timeoutMs] @returns {Promise<unknown>} */
async function apiWithTimeout(path, options = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const externalSignal = options.signal;
  const abortFromExternal = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener("abort", abortFromExternal, { once: true });
  }
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const nextOptions = Object.assign({}, options, { signal: controller.signal });
  try {
    return await api(path, nextOptions);
  } catch (error) {
    if (timedOut && !(externalSignal && externalSignal.aborted)) {
      throw new DOMException("Request timed out", "TimeoutError");
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
    if (externalSignal) externalSignal.removeEventListener("abort", abortFromExternal);
  }
}

async function healthWithTimeout(timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch("/api/health", { signal: controller.signal });
    return await response.json();
  } finally {
    clearTimeout(timeoutId);
  }
}

function warmConnection(force = false) {
  if (document.hidden || !navigator.onLine || keepalivePending) return keepalivePending;
  const now = Date.now();
  if (!force && now - lastKeepaliveAt < KEEPALIVE_MIN_GAP_MS) return null;
  lastKeepaliveAt = now;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  keepalivePending = fetch("/api/ping", {
    cache: "no-store",
    signal: controller.signal,
  }).catch(() => null).finally(() => {
    clearTimeout(timeoutId);
    keepalivePending = null;
  });
  return keepalivePending;
}

// BEGIN STRICT STABLE STRINGIFY
/** @param {unknown} value @returns {string} */
function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    const fields = /** @type {Record<string, unknown>} */ (value);
    return `{${Object.keys(fields).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(fields[key])}`).join(",")}}`;
  }
  return /** @type {string} */ (JSON.stringify(value));
}
// END STRICT STABLE STRINGIFY

/** @param {string} key @returns {SearchResponse | null} */
function getCachedSearch(key) {
  if (!searchCache.has(key)) return null;
  const entry = searchCache.get(key);
  if (!entry || Date.now() - entry.time > SEARCH_CACHE_TTL) {
    searchCache.delete(key);
    return null;
  }
  searchCache.delete(key);
  searchCache.set(key, entry);
  return entry.value;
}

/** @param {string} key @param {SearchResponse} value */
function setCachedSearch(key, value) {
  searchCache.set(key, { value, time: Date.now() });
  while (searchCache.size > SEARCH_CACHE_LIMIT) {
    const oldest = searchCache.keys().next().value;
    if (oldest === undefined) break;
    searchCache.delete(oldest);
  }
}

// BEGIN STRICT PREVIEW CACHE
/** @param {string} docId @returns {ValidatedPreview | null} */
function getCachedPreview(docId) {
  if (!previewCache.has(docId)) return null;
  const entry = previewCache.get(docId);
  if (!entry || Date.now() - entry.time > PREVIEW_CACHE_TTL) {
    if (entry) previewCacheChars -= entry.value.content.length;
    previewCache.delete(docId);
    return null;
  }
  previewCache.delete(docId);
  previewCache.set(docId, entry);
  return entry.value;
}

/** @param {string} docId @param {ValidatedPreview} value */
function setCachedPreview(docId, value) {
  const size = value.content.length;
  if (size > 500000) return;
  const previous = previewCache.get(docId);
  if (previous) previewCacheChars -= previous.value.content.length;
  previewCache.set(docId, { value, time: Date.now() });
  previewCacheChars += size;
  while (previewCache.size > PREVIEW_CACHE_LIMIT || previewCacheChars > PREVIEW_CACHE_MAX_CHARS) {
    const firstKey = previewCache.keys().next().value;
    if (firstKey === undefined) break;
    const firstValue = previewCache.get(firstKey);
    if (firstValue) previewCacheChars -= firstValue.value.content.length;
    previewCache.delete(firstKey);
  }
}
// END STRICT PREVIEW CACHE

function searchPayload() {
  const dateFrom = rangeDateValue("from");
  const dateTo = rangeDateValue("to");
  return {
    q: STATE.q,
    page: STATE.page,
    page_size: STATE.pageSize,
    exact: STATE.exact,
    fulltext: STATE.fulltext,
    sort: STATE.sort,
    sources: STATE.sources,
    authors: STATE.authors,
    tags: STATE.tags,
    exclude_sources: STATE.excludeSources,
    exclude_authors: STATE.excludeAuthors,
    exclude_tags: STATE.excludeTags,
    archive_id: STATE.archiveId ? parseInt(STATE.archiveId, 10) : null,
    publication_type: STATE.publicationType || null,
    date_from: dateFrom,
    date_to: dateTo,
    date_terms: STATE.dateTerms
      .filter((term) => term.year || term.month || term.day || term.display)
      .map((term) => ({
        year: term.year ? parseInt(term.year, 10) : null,
        month: term.month ? parseInt(term.month, 10) : null,
        day: term.day ? parseInt(term.day, 10) : null,
        display: term.display || null,
      })),
  };
}

function abortSearchPrefetch() {
  if (searchPrefetchAbortController) searchPrefetchAbortController.abort();
  searchPrefetchAbortController = null;
  searchPrefetchCacheKey = "";
}

/** @param {ReturnType<typeof searchPayload>} [basePayload] */
function prefetchNextSearchPage(basePayload) {
  if (!STATE.total || STATE.page >= Math.ceil(STATE.total / STATE.pageSize)) return;
  const payload = Object.assign({}, basePayload || searchPayload(), { page: STATE.page + 1 });
  const cacheKey = stableStringify(payload);
  if (getCachedSearch(cacheKey)) return;
  if (searchPrefetchAbortController && searchPrefetchCacheKey === cacheKey) return;
  abortSearchPrefetch();
  const controller = new AbortController();
  searchPrefetchAbortController = controller;
  searchPrefetchCacheKey = cacheKey;
  apiWithTimeout("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: controller.signal,
  }, 25000)
    .then((raw) => {
      const data = parseSearchResponse(raw);
      if (!controller.signal.aborted && data.index_status === "ready" && !data.indexing) setCachedSearch(cacheKey, data);
    })
    .catch((error) => {
      if (error.name !== "AbortError") console.warn("搜索预取失败", error);
    })
    .finally(() => {
      if (searchPrefetchAbortController === controller) {
        searchPrefetchAbortController = null;
        searchPrefetchCacheKey = "";
      }
    });
}

/** @param {ReturnType<typeof searchPayload>} basePayload */
function scheduleNextSearchPagePrefetch(basePayload) {
  const cacheKey = stableStringify(basePayload);
  const run = () => {
    if (cacheKey === stableStringify(searchPayload())) prefetchNextSearchPage(basePayload);
  };
  const idleWindow = /** @type {Window & {requestIdleCallback?: (callback: () => void, options: {timeout: number}) => number}} */ (window);
  if (typeof idleWindow.requestIdleCallback === "function") idleWindow.requestIdleCallback(run, { timeout: 1500 });
  else setTimeout(run, 0);
}

function canUseInitialBootstrap() {
  const payload = searchPayload();
  return payload.q === "" && payload.page === 1 && payload.page_size === 20
    && payload.exact === true && payload.fulltext === true && payload.sort === "relevance"
    && !payload.archive_id
    && !payload.publication_type && !payload.date_from && !payload.date_to
    && !payload.sources.length && !payload.authors.length && !payload.tags.length
    && !payload.exclude_sources.length && !payload.exclude_authors.length && !payload.exclude_tags.length
    && !payload.date_terms.length;
}

/** @param {Record<FacetKind, FacetPage>} facets */
function applyBootstrapFacets(facets) {
  STATE.facets = {
    sources: sortFacetItems((facets.source || {}).items || []),
    authors: sortFacetItems((facets.author || {}).items || []),
    tags: sortFacetItems((facets.tag || {}).items || []),
    archives: STATE.facets.archives,
    types: STATE.facets.types,
    years: STATE.facets.years,
  };
  for (const kind of /** @type {const} */ (["source", "author", "tag"])) {
    STATE.facetTotals[kind] = Number((facets[kind] || {}).total || 0);
    STATE.facetHasMore[kind] = Boolean((facets[kind] || {}).has_more);
  }
}

async function loadInitialBootstrap() {
  const requestSeq = searchRequestSeq;
  const payload = searchPayload();
  const cacheKey = stableStringify(payload);
  const isCurrent = () => requestSeq === searchRequestSeq && !STATE.parseMode
    && cacheKey === stableStringify(searchPayload());
  bootstrapPending = true;
  if (DOM.resultsContainer) DOM.resultsContainer.setAttribute("aria-busy", "true");
  DOM.statusText.textContent = "搜索中...";
  DOM.emptyState.style.display = "none";
  try {
    const bootstrapHost = /** @type {Window & {__bhaBootstrapPromise?: Promise<unknown>}} */ (window);
    const earlyBootstrap = bootstrapHost.__bhaBootstrapPromise;
    delete bootstrapHost.__bhaBootstrapPromise;
    const data = earlyBootstrap ? await earlyBootstrap : await apiWithTimeout("/api/bootstrap", {}, 25000);
    if (!isCurrent()) {
      await loadFacets();
      return;
    }
    if (!isBootstrapResponse(data)) throw new Error("bootstrap is unavailable or invalid");
    const search = data.search;
    if (search.index_status !== "ready" || search.indexing) throw new Error("bootstrap is not ready");
    STATE.results = search.results || [];
    STATE.total = Number(search.total || 0);
    STATE.pageSize = Number(search.page_size || 20);
    const sources = data.sources || {};
    STATE.facets = {
      ...STATE.facets,
      archives: sortFacetItems(sources.archives || []),
      types: sortFacetItems(sources.types || []),
      years: sortFacetItems(sources.years || []),
    };
    setCachedSearch(cacheKey, search);
    // Paint results first: the facet lists are a separate request now, so the
    // first screen never waits for ~600 filter buttons.
    renderResults({ animate: true });
    DOM.results.scrollTop = 0;
    DOM.statusText.textContent = "";
    DOM.indexProgress.textContent = "";
    scheduleNextSearchPagePrefetch(payload);
    requestAnimationFrame(() => { if (isCurrent()) loadFacets(); });
  } catch (_error) {
    await Promise.all([isCurrent() ? doSearch() : Promise.resolve(), loadFacets()]);
  } finally {
    bootstrapPending = false;
    if (DOM.resultsContainer) DOM.resultsContainer.setAttribute("aria-busy", "false");
  }
}

async function loadFacets() {
  const pendingKey = "sources";
  if (facetPending.has(pendingKey)) return facetPending.get(pendingKey);
  const pending = loadFacetsNow().finally(() => facetPending.delete(pendingKey));
  facetPending.set(pendingKey, pending);
  return pending;
}

async function loadFacetsNow() {
  if (sourcesAbortController) sourcesAbortController.abort();
  sourcesAbortController = new AbortController();
  const controller = sourcesAbortController;
  try {
    const [rawSources, rawFacets] = await Promise.all([
      apiWithTimeout("/api/sources", { signal: controller.signal }),
      apiWithTimeout("/api/bootstrap/facets", { signal: controller.signal }).catch(() => null),
    ]);
    if (isFacetList(rawSources)) {
      const data = rawSources;
      STATE.facets.sources = sortFacetItems(data);
    } else {
      const data = parseBhaPayload(rawSources, isFacetSourcesResponse, "来源筛选");
      STATE.facets = {
        sources: STATE.facets.sources,
        authors: STATE.facets.authors,
        tags: STATE.facets.tags,
        archives: sortFacetItems(data.archives || []),
        types: sortFacetItems(data.types || []),
        years: sortFacetItems(data.years || []),
      };
    }
    // Facets are an optional second response. A malformed/old cached copy
    // must not hide the independently valid source and archive selectors.
    if (isBootstrapFacetsResponse(rawFacets)) applyBootstrapFacets(rawFacets.facets);
    renderFilterOptions({ preserveScroll: false });
    facetRetryCounts.delete("sources");
    clearTimeout(facetRetryTimer);
    if (!Array.isArray(rawSources) && isFacetSourcesResponse(rawSources) && rawSources.index_status === "failed") {
      DOM.statusText.textContent = "索引失败，请看日志";
      return;
    }
    if (!Array.isArray(rawSources) && isFacetSourcesResponse(rawSources) && rawSources.indexing) {
      if (!STATE.facets.sources.length && !STATE.facets.authors.length && !STATE.facets.tags.length) {
        DOM.sourceFilter.innerHTML = '<div class="sidebar-loading">索引构建中...</div>';
      }
      clearTimeout(facetRetryTimer);
      facetRetryTimer = setTimeout(loadFacets, 5000);
    }
  } catch (error) {
    const tries = facetRetryCounts.get("sources") || 0;
    if (tries < 2) {
      facetRetryCounts.set("sources", tries + 1);
      DOM.sourceFilter.innerHTML = '<div class="sidebar-loading">来源加载失败，正在重试...</div>';
      setTimeout(loadFacets, 1200);
    } else {
      DOM.sourceFilter.innerHTML = '<div class="sidebar-loading">来源加载失败</div>';
    }
  } finally {
    if (sourcesAbortController === controller) sourcesAbortController = null;
  }
}

/** @param {FacetKind} kind @param {{preserveScroll?: boolean}} [options] */
async function loadFacetPage(kind, { preserveScroll = false } = {}) {
  const page = STATE.filterPage[kind];
  const keyword = STATE.filterSearch[kind] || "";
  const key = kind === "source" ? "sources" : kind === "author" ? "authors" : "tags";
  const pendingKey = `${kind}|${page}|${keyword}`;
  if (facetRequestKeys[kind] === pendingKey && facetPending.has(pendingKey)) return facetPending.get(pendingKey);
  const requestSeq = (facetRequestSeq[kind] || 0) + 1;
  facetRequestSeq[kind] = requestSeq;
  facetRequestKeys[kind] = pendingKey;
  /** @type {FacetPage} */ let data;
  /** @type {AbortController | null} */ let controller = null;
  const pending = (async () => {
    try {
      if (facetAbortControllers[kind]) facetAbortControllers[kind].abort();
      controller = new AbortController();
      facetAbortControllers[kind] = controller;
      const raw = await apiWithTimeout(`/api/facet/${kind}?page=${encodeURIComponent(page)}&page_size=200&q=${encodeURIComponent(keyword)}`, { signal: controller.signal });
      data = parseBhaPayload(raw, isFacetPage, "筛选");
    } catch (error) {
      if (requestSeq !== facetRequestSeq[kind]) return;
      const tries = facetRetryCounts.get(pendingKey) || 0;
      if (tries < 2) {
        facetRetryCounts.set(pendingKey, tries + 1);
        const filter = kind === "source" ? DOM.sourceFilter : kind === "author" ? DOM.authorFilter : DOM.tagFilter;
        filter.innerHTML = '<div class="sidebar-loading">筛选加载失败，正在重试...</div>';
        setTimeout(() => loadFacetPage(kind, { preserveScroll }), 1200);
      } else if (!STATE.facets[key].length) {
        const filter = kind === "source" ? DOM.sourceFilter : kind === "author" ? DOM.authorFilter : DOM.tagFilter;
        filter.innerHTML = '<div class="sidebar-loading">筛选加载失败</div>';
      }
      return;
    }
    if (requestSeq !== facetRequestSeq[kind]) return;
    facetRetryCounts.delete(pendingKey);
    STATE.facets[key] = sortFacetItems(data.items || []);
    STATE.facetTotals[kind] = Number(data.total || 0);
    STATE.facetHasMore[kind] = Boolean(data.has_more);
    renderFacetFilter(kind, { preserveScroll });
  })().finally(() => {
    if (facetPending.get(pendingKey) === pending) facetPending.delete(pendingKey);
    if (requestSeq === facetRequestSeq[kind]) facetRequestKeys[kind] = "";
    if (facetAbortControllers[kind] === controller) facetAbortControllers[kind] = null;
  });
  facetPending.set(pendingKey, pending);
  return pending;
}

/** @param {FacetItem[]} items @returns {FacetItem[]} */
function sortFacetItems(items) {
  return [...items].sort((a, b) => String(a.name == null ? (a.value == null ? "" : a.value) : a.name).localeCompare(String(b.name == null ? (b.value == null ? "" : b.value) : b.name), "zh-Hans-CN", { numeric: true, sensitivity: "base" }));
}

/** @param {FacetItem[]} items @param {string} selected @param {boolean} [includeEmpty] */
function optionHTML(items, selected, includeEmpty = true) {
  const options = includeEmpty ? ['<option value="">全部</option>'] : [];
  for (const item of items) {
    const value = String(item.value == null ? item.name : item.value);
    options.push(`<option value="${escapeHTML(value)}" ${selected === value ? "selected" : ""}>${escapeHTML(item.name)} (${Number(item.count || 0).toLocaleString()})</option>`);
  }
  return options.join("");
}

function filterScrollPositions() {
  return {
    source: (DOM.sourceFilter.querySelector(".filter-choice-options") || { scrollTop: 0 }).scrollTop || 0,
    author: (DOM.authorFilter.querySelector(".filter-choice-options") || { scrollTop: 0 }).scrollTop || 0,
    tag: (DOM.tagFilter.querySelector(".filter-choice-options") || { scrollTop: 0 }).scrollTop || 0,
  };
}

/** @param {Record<FacetKind, number>} positions */
function restoreFilterScrollPositions(positions) {
  for (const [kind, top] of Object.entries(positions)) {
    const filter = kind === "source" ? DOM.sourceFilter : kind === "author" ? DOM.authorFilter : DOM.tagFilter;
    const options = filter.querySelector(".filter-choice-options");
    if (options) options.scrollTop = top;
  }
}

/** @param {{preserveScroll?: boolean}} [options] */
function renderFilterOptions({ preserveScroll = true } = {}) {
  const scrollPositions = preserveScroll ? filterScrollPositions() : null;
  DOM.sourceFilter.innerHTML = choiceListHTML(STATE.facets.sources, STATE.sources, STATE.excludeSources, "source", 200, STATE.filterPage.source, STATE.facetTotals.source, STATE.facetHasMore.source);
  DOM.authorFilter.innerHTML = choiceListHTML(STATE.facets.authors, STATE.authors, STATE.excludeAuthors, "author", 200, STATE.filterPage.author, STATE.facetTotals.author, STATE.facetHasMore.author);
  DOM.tagFilter.innerHTML = choiceListHTML(STATE.facets.tags, STATE.tags, STATE.excludeTags, "tag", 200, STATE.filterPage.tag, STATE.facetTotals.tag, STATE.facetHasMore.tag);
  DOM.archiveFilter.innerHTML = optionHTML(STATE.facets.archives, STATE.archiveId);
  DOM.typeFilter.innerHTML = optionHTML(STATE.facets.types, STATE.publicationType);
  renderDateControls();
  if (scrollPositions) restoreFilterScrollPositions(scrollPositions);
}

/** @param {FacetKind} kind @param {{preserveScroll?: boolean}} [options] */
function renderFacetFilter(kind, { preserveScroll = true } = {}) {
  const dataKey = kind === "source" ? "sources" : kind === "author" ? "authors" : "tags";
  const includeKey = dataKey;
  const excludeKey = kind === "source" ? "excludeSources" : kind === "author" ? "excludeAuthors" : "excludeTags";
  const filter = kind === "source" ? DOM.sourceFilter : kind === "author" ? DOM.authorFilter : DOM.tagFilter;
  const scrollTopNode = filter.querySelector(".filter-choice-options");
  const scrollTop = preserveScroll ? (scrollTopNode ? scrollTopNode.scrollTop : 0) : 0;
  filter.innerHTML = choiceListHTML(STATE.facets[dataKey], STATE[includeKey], STATE[excludeKey], kind, 200, STATE.filterPage[kind], STATE.facetTotals[kind], STATE.facetHasMore[kind]);
  if (preserveScroll) {
    const options = filter.querySelector(".filter-choice-options");
    if (options) options.scrollTop = scrollTop;
  }
}

/** @param {FacetItem[]} items @param {string[]} selectedValues @param {string[]} excludedValues @param {FacetKind} kind
 * @param {number} [limit] @param {number} [page] @param {number | null} [remoteTotal] @param {boolean} [hasMore] */
function choiceListHTML(items, selectedValues, excludedValues, kind, limit = 80, page = 1, remoteTotal = null, hasMore = false) {
  const selectedSet = new Set(selectedValues);
  const excludedSet = new Set(excludedValues);
  const usableItems = items.filter((item) => String(item.name == null ? "" : item.name).trim());
  const itemByName = new Map(usableItems.map((item) => [String(item.name == null ? "" : item.name), item]));
  const pinnedNames = [...new Set([...selectedValues, ...excludedValues].map((value) => String(value || "")).filter(Boolean))];
  const pinnedNameSet = new Set(pinnedNames);
  const pinnedItems = pinnedNames.map((name) => itemByName.get(name) || { name });
  const visibleItems = usableItems.filter((item) => !pinnedNameSet.has(String(item.name == null ? "" : item.name)));
  const total = remoteTotal == null ? usableItems.length : remoteTotal;
  const totalPages = remoteTotal === null ? null : (limit ? Math.max(1, Math.ceil(total / limit)) : 1);
  const safePage = totalPages ? Math.min(Math.max(1, page), totalPages) : Math.max(1, page);
  if (kind in STATE.filterPage && STATE.filterPage[kind] !== safePage) STATE.filterPage[kind] = safePage;
  const pageNames = usableItems.map((item) => String(item.name == null ? "" : item.name)).filter(Boolean);
  const allPageSelected = Boolean(pageNames.length) && pageNames.every((name) => selectedSet.has(name));
  const allPageExcluded = Boolean(pageNames.length) && pageNames.every((name) => excludedSet.has(name));
  const hasState = Boolean(selectedValues.length || excludedValues.length);
  let optionsHTML = `<div class="filter-choice-all"><button type="button" class="filter-choice-item filter-choice-page-action ${allPageSelected ? "active" : ""} ${allPageExcluded ? "excluded" : ""}" data-cycle-${kind}-page="" aria-label="全选本页" aria-pressed="${allPageSelected}"><span>全选本页</span></button>${hasState ? `<button type="button" class="text-btn-sm" data-clear-${kind}="">取消</button>` : ""}</div>`;
  for (const item of [...pinnedItems, ...visibleItems]) {
    const name = String(item.name);
    const checked = selectedSet.has(name);
    const excluded = excludedSet.has(name);
    const countHTML = item.count == null ? '<span class="checkbox-count"></span>' : `<span class="checkbox-count">${Number(item.count || 0).toLocaleString()}</span>`;
    const stateLabel = checked ? "（已选）" : excluded ? "（已排除）" : "";
    optionsHTML += `<button type="button" class="filter-choice-item ${checked ? "active" : ""} ${excluded ? "excluded" : ""}" data-${kind}="${escapeHTML(name)}" aria-label="${escapeHTML(name)}${stateLabel}" aria-pressed="${checked}"><span class="filter-choice-left"><span class="tri-state-box" aria-hidden="true"></span><span>${escapeHTML(name)}</span></span>${countHTML}</button>`;
  }
  let html = `<div class="filter-choice-options">${optionsHTML}</div>`;
  if (limit && (totalPages === null || total > limit || safePage > 1)) {
    html += `<div class="filter-choice-pager"><button type="button" class="text-btn-sm" data-${kind}-page="prev" aria-label="上一页" ${safePage <= 1 ? "disabled" : ""}>上一页</button><span class="page-jump">第 <input class="page-input filter-page-input" type="number" aria-label="筛选页码" min="1" value="${safePage}" data-${kind}-page-input="" ${totalPages ? `max="${totalPages}"` : ""}> / <span>${totalPages == null ? "?" : totalPages}</span> 页</span><button type="button" class="text-btn-sm" data-${kind}-page="next" aria-label="下一页" ${totalPages ? (safePage >= totalPages ? "disabled" : "") : (!hasMore ? "disabled" : "")}>下一页</button></div>`;
  }
  return html;
}

/** @param {HTMLInputElement} input @param {number} fallback @param {number | null} [maxPage] */
function pageFromInput(input, fallback, maxPage = null) {
  const raw = parseInt(input.value, 10);
  let page = Number.isFinite(raw) ? raw : fallback;
  page = Math.max(1, page);
  if (maxPage) page = Math.min(page, maxPage);
  input.value = String(page);
  return page;
}

function commitResultPage() {
  const maxPage = Math.max(1, Math.ceil(STATE.total / STATE.pageSize));
  const nextPage = pageFromInput(DOM.pageInput, STATE.page, maxPage);
  if (nextPage === STATE.page) {
    DOM.pageInput.value = String(STATE.page);
    return;
  }
  STATE.page = nextPage;
  doSearch();
}

/** @param {FacetKind} kind @param {HTMLInputElement} input */
function commitFilterPage(kind, input) {
  const total = STATE.facetTotals[kind];
  const maxPage = total ? Math.max(1, Math.ceil(total / 200)) : null;
  const nextPage = pageFromInput(input, STATE.filterPage[kind], maxPage);
  if (nextPage === STATE.filterPage[kind]) {
    input.value = String(STATE.filterPage[kind]);
    return;
  }
  STATE.filterPage[kind] = nextPage;
  loadFacetPage(kind);
  syncUrl();
}

/** @param {FacetKind} kind @param {HTMLInputElement} input */
function restoreFilterPageInput(kind, input) {
  input.value = String(STATE.filterPage[kind]);
}

/** @param {FacetKind} kind @param {HTMLElement} container @param {HTMLButtonElement} pageButton */
function handleFilterPageButton(kind, container, pageButton) {
  if (pageButton.disabled) return;
  const input = container.querySelector(`[data-${kind}-page-input]`);
  if (input instanceof HTMLInputElement) restoreFilterPageInput(kind, input);
  STATE.filterPage[kind] += pageButton.dataset[`${kind}Page`] === "next" ? 1 : -1;
  loadFacetPage(kind);
  syncUrl();
}

/** @param {FacetKind} kind */
function scheduleFacetSearch(kind) {
  clearTimeout(facetInputTimers[kind]);
  facetInputTimers[kind] = setTimeout(() => {
    loadFacetPage(kind, { preserveScroll: false });
  }, 150);
}

function recoverPageState() {
  if (bootstrapPending) return;
  if (DOM.statusText.textContent === "搜索中..." && !searchAbortController) DOM.statusText.textContent = "";
  // Facets arrive from /api/bootstrap/facets right after the first paint. Do
  // not start a second facet request while that one is still in flight.
  if (facetPending.has("sources")) return;
  let shouldLoadSources = false;
  for (const kind of /** @type {const} */ (["source", "author", "tag"])) {
    const filter = kind === "source" ? DOM.sourceFilter : kind === "author" ? DOM.authorFilter : DOM.tagFilter;
    if (!filter.querySelector(".sidebar-loading")) continue;
    if (kind === "source" && !STATE.facets.archives.length && !STATE.facets.types.length) shouldLoadSources = true;
    else loadFacetPage(kind);
  }
  if (shouldLoadSources) loadFacets();
  prefetchNextSearchPage();
}

function schedulePageRecovery(delay = 0) {
  clearTimeout(pageRecoveryTimer);
  pageRecoveryTimer = setTimeout(recoverPageState, delay);
}

/** @param {string[]} list @param {string} value */
function toggleValue(list, value) {
  if (!value) return [];
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

// BEGIN STRICT FACET KEYS
/** @param {FacetKind} kind @returns {{include: IncludedFacetKey, exclude: ExcludedFacetKey}} */
function filterKeys(kind) {
  return {
    include: kind === "source" ? "sources" : kind === "author" ? "authors" : "tags",
    exclude: kind === "source" ? "excludeSources" : kind === "author" ? "excludeAuthors" : "excludeTags",
  };
}
// END STRICT FACET KEYS

/** @param {FacetKind} kind @param {string} value */
function cycleFilterValue(kind, value) {
  if (!value) return;
  const keys = filterKeys(kind);
  const included = STATE[keys.include].includes(value);
  const excluded = STATE[keys.exclude].includes(value);
  if (included) {
    STATE[keys.include] = STATE[keys.include].filter((item) => item !== value);
    STATE[keys.exclude] = [...new Set([...STATE[keys.exclude], value])];
  } else if (excluded) {
    STATE[keys.exclude] = STATE[keys.exclude].filter((item) => item !== value);
  } else {
    STATE[keys.exclude] = STATE[keys.exclude].filter((item) => item !== value);
    STATE[keys.include] = [...new Set([...STATE[keys.include], value])];
  }
  STATE.page = 1;
  renderFilterOptions({ preserveScroll: true });
  doSearch();
}

/** @param {FacetKind} kind @param {string} value */
function toggleResultFilter(kind, value) {
  if (!value) return;
  const keys = filterKeys(kind);
  STATE[keys.exclude] = STATE[keys.exclude].filter((item) => item !== value);
  STATE[keys.include] = toggleValue(STATE[keys.include], value);
  STATE.page = 1;
  renderFilterOptions({ preserveScroll: true });
  doSearch();
  refreshSelectedFacetItem(kind, value);
}

/** @param {FacetKind} kind @param {string} value */
async function refreshSelectedFacetItem(kind, value) {
  const dataKey = kind === "source" ? "sources" : kind === "author" ? "authors" : "tags";
  const keys = filterKeys(kind);
  try {
    const raw = await apiWithTimeout(`/api/facet/${kind}?page=1&page_size=500&q=${encodeURIComponent(value)}`);
    const data = parseBhaPayload(raw, isFacetPage, "筛选");
    if (!STATE[keys.include].includes(value) && !STATE[keys.exclude].includes(value)) return;
    const item = (data.items || []).find((candidate) => String(candidate.name) === value);
    if (!item) return;
    STATE.facets[dataKey] = sortFacetItems([
      ...STATE.facets[dataKey].filter((candidate) => String(candidate.name) !== value),
      item,
    ]);
    renderFacetFilter(kind);
  } catch (error) {
    if (!(error instanceof Error) || error.name !== "AbortError") console.warn("筛选数量加载失败", error);
  }
}

/** @param {unknown} value */
function toggleResultDateTerm(value) {
  const payload = resultDateTermPayload(value);
  if (!payload) return;
  const key = dateTermKey(payload);
  const activeTerms = uniqDateTerms(STATE.dateTerms);
  const exists = activeTerms.some((item) => dateTermKey(item) === key);
  STATE.dateTerms = exists
    ? activeTerms.filter((item) => dateTermKey(item) !== key)
    : uniqDateTerms([...activeTerms, payload]);
  if (!STATE.dateTerms.length) STATE.dateTerms = [{ year: "", month: "", day: "" }];
  STATE.page = 1;
  renderDateControls();
  doSearch();
}

/** @param {FacetKind} kind */
function cycleFilterPage(kind) {
  const key = kind === "source" ? "sources" : kind === "author" ? "authors" : "tags";
  const keys = filterKeys(kind);
  const values = (STATE.facets[key] || []).map((item) => String(item.name == null ? "" : item.name)).filter(Boolean);
  const includedSet = new Set(STATE[keys.include]);
  const excludedSet = new Set(STATE[keys.exclude]);
  const allIncluded = Boolean(values.length) && values.every((value) => includedSet.has(value));
  const allExcluded = Boolean(values.length) && values.every((value) => excludedSet.has(value));
  if (allIncluded) {
    STATE[keys.include] = STATE[keys.include].filter((value) => !values.includes(value));
  } else if (allExcluded) {
    STATE[keys.exclude] = STATE[keys.exclude].filter((value) => !values.includes(value));
  } else {
    STATE[keys.exclude] = STATE[keys.exclude].filter((value) => !values.includes(value));
    STATE[keys.include] = [...new Set([...STATE[keys.include], ...values])];
  }
  STATE.page = 1;
  renderFilterOptions({ preserveScroll: true });
  doSearch();
}

/** @param {FacetKind} kind */
function clearFilterKind(kind) {
  const keys = filterKeys(kind);
  STATE[keys.include] = [];
  STATE[keys.exclude] = [];
  STATE.page = 1;
  renderFilterOptions({ preserveScroll: true });
  doSearch();
}

async function doSearch() {
  if (STATE.parseMode) return;
  cancelParseRequest();
  if (!validateDateRange()) return;
  const requestSeq = searchRequestSeq + 1;
  searchRequestSeq = requestSeq;
  if (searchAbortController) searchAbortController.abort();
  abortSearchPrefetch();
  searchAbortController = new AbortController();
  if (DOM.resultsContainer) DOM.resultsContainer.setAttribute("aria-busy", "true");
  applyStateToDom();
  syncUrl();
  DOM.statusText.textContent = "搜索中...";
  const payload = searchPayload();
  const cacheKey = stableStringify(payload);
  try {
    const cached = getCachedSearch(cacheKey);
    const raw = cached || await apiWithTimeout("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: searchAbortController.signal,
    }, 25000);
    if (requestSeq !== searchRequestSeq) return;
    const data = parseSearchResponse(raw);
    if (data.index_status === "failed") {
      STATE.results = [];
      STATE.total = 0;
      renderResults();
      DOM.statusText.textContent = "索引失败，请看日志";
      return;
    }
    if (data.indexing && !(data.results || []).length) {
      STATE.results = [];
      STATE.total = 0;
      renderResults();
      DOM.statusText.textContent = "索引构建中...";
      updateProgress();
      return;
    }
    STATE.results = data.results || [];
    STATE.total = data.total || 0;
    STATE.pageSize = [10, 20, 50].includes(Number(data.page_size)) ? Number(data.page_size) : STATE.pageSize;
    if (!cached && data.index_status === "ready" && !data.indexing) setCachedSearch(cacheKey, data);
    renderResults({ animate: true });
    DOM.results.scrollTop = 0;
    if (STATE.preview) previewResultsScrollTop = 0;
    DOM.statusText.textContent = "";
    if (data.index_status === "ready" && !data.indexing) DOM.indexProgress.textContent = "";
    else updateProgress();
    scheduleNextSearchPagePrefetch(payload);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError" || requestSeq !== searchRequestSeq) return;
    const message = error instanceof Error && error.name === "TimeoutError" ? "搜索超时，请重试" : "搜索失败";
    DOM.statusText.textContent = message;
    showToast(message);
  } finally {
    if (requestSeq === searchRequestSeq) {
      searchAbortController = null;
      if (DOM.resultsContainer) DOM.resultsContainer.setAttribute("aria-busy", "false");
    }
  }
}

async function updateProgress() {
  try {
    const data = await healthWithTimeout();
    const progress = data.progress;
    if (data.es === false) {
      DOM.indexProgress.textContent = "Elasticsearch 不可用，正在重试...";
      clearTimeout(progressTimer);
      progressTimer = setTimeout(updateProgress, 5000);
      return;
    }
    if (!progress || (data.index_status === "ready" && data.ok !== false)) {
      DOM.indexProgress.textContent = "";
      clearTimeout(progressTimer);
      return;
    }
    const indexed = Number(progress.indexed || data.document_count || 0).toLocaleString();
    const elapsed = progress.elapsed_seconds ? ` / ${Math.floor(progress.elapsed_seconds / 60)}分${progress.elapsed_seconds % 60}秒` : "";
    const syncing = progress.status === "syncing" && progress.archive_total;
    const detail = syncing
      ? `同步数据仓库：${progress.archive_current} / ${progress.archive_total}`
      : `索引${data.index_status === "failed" ? "失败" : "中"}：${indexed} 条`;
    DOM.indexProgress.textContent = `${detail}${elapsed}`;
    clearTimeout(progressTimer);
    if (data.index_status !== "failed") progressTimer = setTimeout(updateProgress, 5000);
  } catch (error) {
    DOM.indexProgress.textContent = "";
  }
}

function renderResults({ animate = false } = {}) {
  const totalPages = Math.max(1, Math.ceil(STATE.total / STATE.pageSize));
  DOM.resultCount.textContent = STATE.total ? `共 ${STATE.total.toLocaleString()} 条结果` : "";
  DOM.pageInput.value = String(STATE.page);
  DOM.pageInput.max = String(totalPages);
  DOM.pageTotal.textContent = String(totalPages);
  DOM.prevPage.disabled = STATE.page <= 1;
  DOM.nextPage.disabled = STATE.page >= totalPages;
  DOM.results.hidden = !STATE.results.length;
  DOM.emptyState.style.display = STATE.results.length ? "none" : "flex";
  DOM.results.innerHTML = STATE.results.map((item) => resultHTML(item)).join("");
  if (animate) animateResultRows(Array.from(DOM.results.children).slice(0, 20));
}

// BEGIN STRICT MOTION
/** @param {Element[]} rows */
function animateResultRows(rows) {
  if (!motionAllowed()) return;
  rows.forEach((row, index) => {
    if (!(row instanceof HTMLElement)) return;
    row.style.setProperty("--result-enter-delay", `${Math.round(24 * Math.sqrt(index))}ms`);
    row.classList.add("result-enter");
    row.addEventListener("animationend", () => {
      row.classList.remove("result-enter");
      row.style.removeProperty("--result-enter-delay");
    }, { once: true });
  });
}

function animatePreviewPanel() {
  if (!motionAllowed()) return;
  stopPreviewAnimation();
  void DOM.previewPanel.offsetWidth;
  DOM.previewPanel.classList.add("preview-enter");
  /** @param {Event} event */
  const finish = (event) => {
    if (event.target !== DOM.previewPanel) return;
    DOM.previewPanel.classList.remove("preview-enter");
    DOM.previewPanel.removeEventListener("animationend", finish);
    if (previewAnimationCleanup === finish) previewAnimationCleanup = null;
  };
  previewAnimationCleanup = finish;
  DOM.previewPanel.addEventListener("animationend", finish);
}

function stopPreviewAnimation() {
  DOM.previewPanel.classList.remove("preview-enter");
  if (!previewAnimationCleanup) return;
  DOM.previewPanel.removeEventListener("animationend", previewAnimationCleanup);
  previewAnimationCleanup = null;
}
// END STRICT MOTION

/** @param {SearchResult} item */
function resultHTML(item) {
  const type = item.publication_type ? `<span class="path-sep">/</span><span>${escapeHTML(item.publication_type)}</span>` : "";
  const dates = item.date_display && item.date_display.length
    ? `<span class="path-sep">/</span>${item.date_display.map((date) => `<span class="path-folder" role="button" tabindex="0" data-date-term-key="${escapeHTML(resultDateTermKey(date))}" data-date-term-label="${escapeHTML(date)}" aria-label="按日期筛选：${escapeHTML(date)}">${escapeHTML(date)}</span>`).join('<span class="path-sep">,</span>')}`
    : "";
  const authorLinks = item.authors && item.authors.length ? `<span class="path-sep">/</span>${item.authors.map((author) => `<span class="path-folder" role="button" tabindex="0" data-author="${escapeHTML(author)}" aria-label="按作者筛选：${escapeHTML(author)}">${escapeHTML(author)}</span>`).join('<span class="path-sep">,</span>')}` : "";
  const tags = item.tag_names && item.tag_names.length ? `<div class="result-meta"><span class="result-size">${item.tag_names.slice(0, 8).map((tag) => `<span class="path-folder" role="button" tabindex="0" data-tag="${escapeHTML(tag)}" aria-label="按标签筛选：${escapeHTML(tag)}">${escapeHTML(tag)}</span>`).join('<span class="path-sep"> / </span>')}</span></div>` : "";
  const snippets = (item.snippet || []).map((snippet) => `<div class="result-snippet">${snippet}</div>`).join("");
  const title = item.title_highlight || escapeHTML(item.title);
  const sourceDownload = item.source_file_count > 1
    ? `<a class="result-action-btn primary" href="/api/download/source-zip/${encodeURIComponent(item.doc_id)}">打包原文件</a>`
    : item.source_file_count === 1
      ? `<a class="result-action-btn primary" href="/api/download/source/${encodeURIComponent(item.doc_id)}">下载原文件</a>`
      : "";
  return `<article class="result-item" role="listitem"><div class="result-file-icon"><span class="ui-icon ui-icon-file" aria-hidden="true"></span></div><div class="result-main"><div class="result-line"><div class="result-info"><div class="result-title">${title}</div><div class="result-path"><span class="path-folder" role="button" tabindex="0" data-source="${escapeHTML(item.publication_name)}" aria-label="按来源筛选：${escapeHTML(item.publication_name)}">${escapeHTML(item.publication_name)}</span>${authorLinks}${type}${dates}</div></div><div class="result-actions"><button class="result-action-btn" data-preview="${escapeHTML(item.doc_id)}" aria-label="预览：${escapeHTML(item.title || item.publication_name || "文章")}">在线预览</button><a class="result-action-btn primary" href="/api/download/txt/${encodeURIComponent(item.doc_id)}" aria-label="下载：${escapeHTML(item.title || item.publication_name || "文章")}">下载 TXT</a>${sourceDownload}</div></div><div class="result-content-row">${snippets}${tags}</div></div></article>`;
}

/** @param {ParseQueueItem} item */
function parseQueueHTML(item) {
  const start = Number(item.page_start) || 1;
  const end = Number(item.page_end) || start;
  const dates = (item.date_display || []).join(" / ");
  return `<article class="result-item parse-queue-item" role="listitem"><div class="result-file-icon"><span class="ui-icon ui-icon-file" aria-hidden="true"></span></div><div class="result-main"><div class="result-line"><div class="result-info"><div class="result-title">${escapeHTML(item.publication_name || item.title)}</div><div class="result-path"><span>Archive ${escapeHTML(item.archive_id)}</span><span class="path-sep">/</span><span>${start === end ? `第 ${start} 页` : `第 ${start}-${end} 页`}</span>${dates ? `<span class="path-sep">/</span><span>${escapeHTML(dates)}</span>` : ""}</div></div><div class="result-actions"><button class="result-action-btn primary" data-parse-open="${escapeHTML(item.doc_id)}" aria-label="开始解析：${escapeHTML(item.publication_name || item.title)}">开始解析</button></div></div></div></article>`;
}

function renderParseQueue({ animate = false } = {}) {
  const pageSize = STATE.parsePageSize;
  const totalPages = Math.max(1, Math.ceil(STATE.parseTotal / pageSize));
  DOM.resultCount.textContent = `待解析 ${STATE.parseTotal.toLocaleString()} 份`;
  DOM.statusText.textContent = "Archive 25";
  DOM.pageInput.value = String(STATE.parsePage);
  DOM.pageInput.max = String(totalPages);
  DOM.pageTotal.textContent = String(totalPages);
  DOM.prevPage.disabled = STATE.parsePage <= 1;
  DOM.nextPage.disabled = STATE.parsePage >= totalPages;
  DOM.results.hidden = !STATE.parseItems.length;
  DOM.emptyState.style.display = STATE.parseItems.length ? "none" : "flex";
  DOM.results.innerHTML = STATE.parseItems.map(parseQueueHTML).join("");
  if (animate) animateResultRows(Array.from(DOM.results.children).slice(0, 20));
}

function cancelParseRequest() {
  parseRequestSeq += 1;
  if (parseAbortController) parseAbortController.abort();
  parseAbortController = null;
}

async function loadParseQueue() {
  if (!STATE.parseMode) return;
  clearTimeout(searchTimer);
  searchRequestSeq += 1;
  if (searchAbortController) searchAbortController.abort();
  searchAbortController = null;
  abortSearchPrefetch();
  cancelPreviewRequests();
  cancelParseRequest();
  const requestSeq = parseRequestSeq;
  const controller = new AbortController();
  parseAbortController = controller;
  STATE.parseItem = null;
  STATE.preview = "";
  clearPreviewDom();
  DOM.statusText.textContent = "正在加载待解析队列...";
  try {
    const raw = await apiWithTimeout(`/api/parse/queue?page=${STATE.parsePage}&page_size=${STATE.parsePageSize}`, { signal: controller.signal });
    if (!STATE.parseMode || requestSeq !== parseRequestSeq) return;
    const data = parseBhaPayload(raw, isParseQueueResponse, "待解析队列");
    STATE.parseTotal = data.total;
    STATE.parseItems = data.items;
    renderParseQueue({ animate: true });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError" || !STATE.parseMode || requestSeq !== parseRequestSeq) return;
    DOM.statusText.textContent = "待解析队列加载失败";
    showToast(`待解析队列加载失败：${error instanceof Error ? error.message : "未知错误"}`);
  } finally {
    if (parseAbortController === controller) parseAbortController = null;
  }
}

/** @param {Record<string, unknown>} article */
function parseDateParts(article) {
  const first = Array.isArray(article.dates) ? article.dates[0] : null;
  const date = isJsonObject(first) ? first : {};
  return { year: date.year || "", month: date.month || "", day: date.day || "" };
}

/** @param {Record<string, unknown>} article @param {number} index @param {number} maxPage */
function parseArticleRowHTML(article, index, maxPage) {
  const date = parseDateParts(article);
  const ocr = escapeHTML(JSON.stringify(article.ocr || {}));
  const exceptions = escapeHTML(JSON.stringify(article.ocr_exceptions || {}));
  const authors = isStringList(article.authors) ? article.authors : [];
  return `<fieldset class="parse-article" data-parse-article data-parse-ocr="${ocr}" data-parse-ocr-exceptions="${exceptions}"><legend>文章 ${index + 1}</legend><div class="parse-article-actions"><button type="button" class="text-btn-sm" data-parse-crop-open>裁剪</button><button type="button" class="text-btn-sm" data-parse-remove ${index === 0 ? "disabled" : ""}>删除</button></div><label class="proofread-field"><span>标题</span><input data-parse-title value="${escapeHTML(article.title || "")}" required></label><label class="proofread-field"><span>作者</span><input data-parse-authors value="${escapeHTML(authors.join("、"))}"></label><div class="parse-date-row"><label class="proofread-field"><span>年</span><input data-parse-year type="number" min="1" max="9999" value="${escapeHTML(date.year)}"></label><label class="proofread-field"><span>月</span><input data-parse-month type="number" min="1" max="12" value="${escapeHTML(date.month)}"></label><label class="proofread-field"><span>日</span><input data-parse-day type="number" min="1" max="31" value="${escapeHTML(date.day)}"></label></div><div class="parse-range-row"><label class="proofread-field"><span>开始页</span><input data-parse-start type="number" min="1" max="${maxPage}" value="${escapeHTML(article.page_start || 1)}" required></label><label class="proofread-field"><span>结束页</span><input data-parse-end type="number" min="1" max="${maxPage}" value="${escapeHTML(article.page_end || maxPage)}" required></label></div><label class="proofread-field parse-body-field"><span class="parse-body-heading"><span>正文</span><span><span class="parse-body-status" data-parse-body-status></span><button type="button" class="text-btn-sm" data-parse-refresh-body>从 OCR 刷新</button></span></span><textarea data-parse-content required>${escapeHTML(article.content || "")}</textarea></label></fieldset>`;
}

/** @param {ValidatedPreview} item */
function parseEditorHTML(item) {
  const article = item.article || {};
  const maxPage = Number(article.page_end) || (item.source_files || []).length || 1;
  const initial = { title: "", authors: [], dates: article.dates || [], page_start: 1, page_end: maxPage };
  const sourceCount = Number(item.source_file_count) || (item.source_files || []).length;
  const sourceDownload = sourceCount > 1
    ? `<a class="text-btn-sm parse-source-download" href="/api/download/source-zip/${encodeURIComponent(item.doc_id)}">打包下载原文件</a>`
    : sourceCount === 1
      ? `<a class="text-btn-sm parse-source-download" href="/api/download/source/${encodeURIComponent(item.doc_id)}">下载原文件</a>`
      : "";
  return `<div class="preview-header"><div class="preview-heading"><div class="preview-title">${escapeHTML(item.publication_name || item.title)}</div><div class="preview-compare-note">待解析 / ${maxPage} 页</div></div><div class="preview-actions"><button type="button" class="text-btn-sm" data-parse-back>返回队列</button><button type="button" class="icon-btn-sm" data-parse-close aria-label="关闭">×</button></div></div><div class="parse-workbench"><section class="parse-source-pane"><div class="proofread-pane-header"><span>原图与 OCR</span><label class="proofread-page-control">页码 <input data-parse-page type="number" min="1" max="${maxPage}" value="1"></label></div><div class="parse-page-tools"><button type="button" class="text-btn-sm" data-parse-page-prev disabled>上一页</button><button type="button" class="text-btn-sm" data-parse-page-next ${maxPage === 1 ? "disabled" : ""}>下一页</button><span data-parse-page-status>1 / ${maxPage}</span>${sourceDownload}<button type="button" class="text-btn-sm parse-crop-toggle" data-parse-crop-toggle>裁剪 OCR</button></div><div class="parse-crop-toolbar" data-parse-crop-toolbar hidden><span data-parse-crop-target>文章 1</span><div class="parse-crop-scope" role="group" aria-label="裁剪范围"><button type="button" class="text-btn-sm primary" data-parse-crop-scope="article">整篇</button><button type="button" class="text-btn-sm" data-parse-crop-scope="page">仅本页</button></div><span data-parse-crop-values>上 0% 下 0% 左 0% 右 0%</span><button type="button" class="text-btn-sm" data-parse-crop-reset>重置</button><button type="button" class="text-btn-sm" data-parse-crop-close>完成</button></div><div class="parse-page-content"><div class="parse-image-stage"><div class="parse-image-wrap"><img data-parse-image alt="原文件第 1 页"><div class="parse-crop-overlay" data-parse-crop-overlay hidden><div class="parse-crop-box" data-parse-crop-box></div></div></div></div><pre class="parse-ocr-text" data-parse-ocr>正在加载 OCR...</pre></div></section><form class="proofread-editor parse-editor" data-parse-form data-max-page="${maxPage}"><div class="proofread-pane-header"><span>文章列表</span><button type="submit" class="result-action-btn primary">提交解析</button></div><div data-parse-articles>${parseArticleRowHTML(initial, 0, maxPage)}</div><div class="proofread-form-actions"><button type="button" class="text-btn-sm" data-parse-add>添加文章</button></div><label class="proofread-field"><span>解析备注</span><textarea data-parse-description></textarea></label><div class="date-error" data-parse-error hidden></div></form></div>`;
}

/** @param {number} page */
async function loadParsePage(page) {
  const item = STATE.parseItem;
  if (!item) return;
  const requestSeq = ++parsePageRequestSeq;
  const maxPage = Number((item.article || {}).page_end) || (item.source_files || []).length || 1;
  page = Math.min(maxPage, Math.max(1, Number(page) || 1));
  STATE.parseCurrentPage = page;
  const cropToolbar = DOM.previewPanel.querySelector("[data-parse-crop-toolbar]");
  if (cropToolbar instanceof HTMLElement && !cropToolbar.hidden) {
    const selected = parseCropTargetRow();
    const range = selected ? parseArticlePageRange(selected) : null;
    const coversPage = range && range.start <= page && range.end >= page;
    if (!coversPage) {
      if (selected) selected.classList.remove("is-crop-target");
      setParseCropTarget(parseCropTargetRow());
    }
  }
  const image = DOM.previewPanel.querySelector("[data-parse-image]");
  const text = DOM.previewPanel.querySelector("[data-parse-ocr]");
  const input = DOM.previewPanel.querySelector("[data-parse-page]");
  const status = DOM.previewPanel.querySelector("[data-parse-page-status]");
  if (input instanceof HTMLInputElement) input.value = String(page);
  if (status) status.textContent = `${page} / ${maxPage}`;
  const previous = DOM.previewPanel.querySelector("[data-parse-page-prev]");
  const next = DOM.previewPanel.querySelector("[data-parse-page-next]");
  if (previous instanceof HTMLButtonElement) previous.disabled = page <= 1;
  if (next instanceof HTMLButtonElement) next.disabled = page >= maxPage;
  if (image instanceof HTMLImageElement) image.src = parseSourceImageURL(item, page);
  if (text) text.textContent = "正在加载 OCR...";
  try {
    const data = await parseOCRPage(item, page);
    if (parsePageRequestSeq !== requestSeq || STATE.parseItem !== item || STATE.parseCurrentPage !== page) return;
    if (text) text.textContent = parseOCRText(data, parseOCRThresholds(parseCropTargetRow(), page));
    updateParseCropUI();
  } catch (error) {
    if (parsePageRequestSeq !== requestSeq || STATE.parseItem !== item || STATE.parseCurrentPage !== page) return;
    if (text) text.textContent = `OCR 加载失败：${error instanceof Error && error.message ? error.message : "未知错误"}`;
  }
}

/** @param {HTMLElement | null} row @param {number} page @returns {number[]} */
function parseOCRThresholds(row, page) {
  if (!row) return [0, 0, 0, 0];
  const base = parseCropThresholdValues(cropOptions(row, "article", page).content_thresholds);
  const override = parseCropThresholdValues(cropOptions(row, "page", page).content_thresholds);
  return override || base || [0, 0, 0, 0];
}

// BEGIN STRICT PARSE OCR ROWS
/** @typedef {{text?: string, box?: number[][]}} ParseOCRRow */
/** @typedef {{dimensions?: {width?: number, height?: number}, ocr_results?: ParseOCRRow[]}} ParseOCRPage */
/** @param {unknown} value @returns {value is ParseOCRPage} */
function isParseOCRPage(value) {
  if (!isJsonObject(value) || !Array.isArray(value.ocr_results)) return false;
  if (value.dimensions !== undefined && (!isJsonObject(value.dimensions) ||
      (value.dimensions.width !== undefined && (typeof value.dimensions.width !== "number" || !Number.isFinite(value.dimensions.width) || value.dimensions.width <= 0)) ||
      (value.dimensions.height !== undefined && (typeof value.dimensions.height !== "number" || !Number.isFinite(value.dimensions.height) || value.dimensions.height <= 0)))) return false;
  return value.ocr_results.every((row) => isJsonObject(row) && (row.text === undefined || typeof row.text === "string") &&
    (row.box === undefined || Array.isArray(row.box) && row.box.every((point) => Array.isArray(point) && point.length >= 2 &&
      point.every((coordinate) => typeof coordinate === "number" && Number.isFinite(coordinate)))));
}
/** @param {ParseOCRPage} data @param {number[]} [thresholds] @returns {ParseOCRRow[]} */
function parseOCRRows(data, thresholds = [0, 0, 0, 0]) {
  const dimensions = data.dimensions || {};
  const width = Number(dimensions.width) || 0;
  const height = Number(dimensions.height) || 0;
  const [top, bottom, left, right] = thresholds;
  return (data.ocr_results || []).filter((row) => {
    if (!width || !height || !Array.isArray(row.box) || !row.box.length) return true;
    const xs = row.box.map((point) => Number(point[0]) || 0);
    const ys = row.box.map((point) => Number(point[1]) || 0);
    const centerX = (Math.min(...xs) + Math.max(...xs)) / 2;
    const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
    return centerX >= left * width && centerX <= (1 - right) * width
      && centerY >= top * height && centerY <= (1 - bottom) * height;
  }).slice().sort((a, b) => ((a.box?.[0]?.[1] ?? 0) - (b.box?.[0]?.[1] ?? 0)) || ((a.box?.[0]?.[0] ?? 0) - (b.box?.[0]?.[0] ?? 0)));
}

/** @param {ParseOCRPage} data @param {number[]} [thresholds] @returns {string} */
function parseOCRText(data, thresholds = [0, 0, 0, 0]) {
  return parseOCRRows(data, thresholds)
    .map((row) => row.text || "").filter(Boolean).join("\n");
}
// END STRICT PARSE OCR ROWS

/** @param {Element} row @returns {{start: number, end: number} | null} */
function parseArticlePageRange(row) {
  const start = row.querySelector("[data-parse-start]");
  const end = row.querySelector("[data-parse-end]");
  if (!(start instanceof HTMLInputElement) || !(end instanceof HTMLInputElement)) return null;
  return { start: Number(start.value), end: Number(end.value) };
}

/** @returns {HTMLElement | null} */
function parseCropTargetRow() {
  const rows = Array.from(DOM.previewPanel.querySelectorAll("[data-parse-article]"))
    .filter((row) => row instanceof HTMLElement);
  const selected = rows.find((row) => row.classList.contains("is-crop-target"));
  if (selected) return selected;
  return rows.find((row) => {
    const range = parseArticlePageRange(row);
    return range && range.start <= STATE.parseCurrentPage && range.end >= STATE.parseCurrentPage;
  }) || rows[0] || null;
}

/** @param {HTMLElement | null} row */
function setParseCropTarget(row) {
  DOM.previewPanel.querySelectorAll("[data-parse-article]").forEach((candidate) => candidate.classList.toggle("is-crop-target", candidate === row));
  updateParseCropUI();
}

/** @returns {"page" | "article"} */
function parseCropScope() {
  const toolbar = DOM.previewPanel.querySelector("[data-parse-crop-toolbar]");
  return toolbar instanceof HTMLElement && toolbar.dataset.scope === "page" ? "page" : "article";
}

/** @param {string} scope */
function setParseCropScope(scope) {
  const toolbar = DOM.previewPanel.querySelector("[data-parse-crop-toolbar]");
  if (!(toolbar instanceof HTMLElement)) return;
  toolbar.dataset.scope = scope === "page" ? "page" : "article";
  toolbar.querySelectorAll("[data-parse-crop-scope]").forEach((button) => {
    if (button instanceof HTMLElement) button.classList.toggle("primary", button.dataset.parseCropScope === toolbar.dataset.scope);
  });
  updateParseCropUI();
}

/** @param {string | undefined} raw @returns {Record<string, unknown>} */
function parseCropObject(raw) {
  try {
    const value = /** @type {unknown} */ (JSON.parse(raw || "{}"));
    return isJsonObject(value) ? value : {};
  } catch (_error) {
    return {};
  }
}

/** @param {unknown} value @returns {number[] | null} */
function parseCropThresholdValues(value) {
  if (!Array.isArray(value) || value.length !== 4) return null;
  /** @type {number[]} */ const values = [];
  for (const entry of value) {
    if (typeof entry !== "number" && typeof entry !== "string") return null;
    const number = Number(entry);
    if (!Number.isFinite(number)) return null;
    values.push(Math.min(0.98, Math.max(0, number)));
  }
  return values[0] + values[1] < 1 && values[2] + values[3] < 1 ? values : null;
}

/** @param {HTMLElement} row @param {"page" | "article"} scope @param {number} page @returns {Record<string, unknown>} */
function cropOptions(row, scope, page) {
  if (scope === "article") return parseCropObject(row.dataset.parseOcr);
  const options = parseCropObject(row.dataset.parseOcrExceptions)[String(page)];
  return isJsonObject(options) ? options : {};
}

/** @param {HTMLElement | null} row @param {"page" | "article"} scope @param {number} page @param {number[]} thresholds @param {boolean} [reset] */
function setParseCropThresholds(row, scope, page, thresholds, reset = false) {
  if (!row) return;
  const key = scope === "page" ? "parseOcrExceptions" : "parseOcr";
  const value = parseCropObject(row.dataset[key]);
  if (scope === "page") {
    if (reset) delete value[String(page)];
    else {
      const prior = value[String(page)];
      value[String(page)] = { ...(isJsonObject(prior) ? prior : {}), content_thresholds: thresholds };
    }
  } else if (reset) {
    delete value.content_thresholds;
  } else {
    value.content_thresholds = thresholds;
  }
  row.dataset[key] = JSON.stringify(value);
}

function updateParseCropUI() {
  const toolbar = DOM.previewPanel.querySelector("[data-parse-crop-toolbar]");
  const box = DOM.previewPanel.querySelector("[data-parse-crop-box]");
  if (!(toolbar instanceof HTMLElement) || !(box instanceof HTMLElement) || toolbar.hidden) return;
  const row = parseCropTargetRow();
  if (!row) return;
  const rows = Array.from(DOM.previewPanel.querySelectorAll("[data-parse-article]"));
  const label = toolbar.querySelector("[data-parse-crop-target]");
  const scope = parseCropScope();
  const page = STATE.parseCurrentPage;
  const options = cropOptions(row, scope, page);
  const thresholds = parseCropThresholdValues(options.content_thresholds) || parseOCRThresholds(row, page);
  if (label) label.textContent = `文章 ${rows.indexOf(row) + 1}${scope === "page" ? ` / 第 ${page} 页` : ""}`;
  box.style.top = `${thresholds[0] * 100}%`;
  box.style.bottom = `${thresholds[1] * 100}%`;
  box.style.left = `${thresholds[2] * 100}%`;
  box.style.right = `${thresholds[3] * 100}%`;
  if (!box.querySelector("[data-parse-crop-handle]")) {
    box.insertAdjacentHTML("beforeend", '<span data-parse-crop-handle="nw"></span><span data-parse-crop-handle="ne"></span><span data-parse-crop-handle="sw"></span><span data-parse-crop-handle="se"></span>');
  }
  box.classList.toggle("has-selection", thresholds.some((value) => value > 0));
  const values = toolbar.querySelector("[data-parse-crop-values]");
  if (values) values.textContent = `上 ${Math.round(thresholds[0] * 100)}% 下 ${Math.round(thresholds[1] * 100)}% 左 ${Math.round(thresholds[2] * 100)}% 右 ${Math.round(thresholds[3] * 100)}%`;
}

/** @param {Element | null} [row] */
function openParseCrop(row = null) {
  const toolbar = DOM.previewPanel.querySelector("[data-parse-crop-toolbar]");
  const overlay = DOM.previewPanel.querySelector("[data-parse-crop-overlay]");
  if (!(toolbar instanceof HTMLElement) || !(overlay instanceof HTMLElement)) return;
  row = row || parseCropTargetRow();
  if (!(row instanceof HTMLElement)) return;
  const range = parseArticlePageRange(row);
  if (!range) return;
  const start = range.start || 1;
  const end = range.end || start;
  if (STATE.parseCurrentPage < start || STATE.parseCurrentPage > end) loadParsePage(start);
  toolbar.hidden = false;
  overlay.hidden = false;
  setParseCropTarget(row);
  setParseCropScope("article");
}

function closeParseCrop() {
  const toolbar = DOM.previewPanel.querySelector("[data-parse-crop-toolbar]");
  const overlay = DOM.previewPanel.querySelector("[data-parse-crop-overlay]");
  if (toolbar instanceof HTMLElement) toolbar.hidden = true;
  if (overlay instanceof HTMLElement) overlay.hidden = true;
  parseCropDrag = null;
}

async function updateParsePageOCR() {
  const item = STATE.parseItem;
  const text = DOM.previewPanel.querySelector("[data-parse-ocr]");
  if (!item || !text) return;
  const page = STATE.parseCurrentPage;
  const requestSeq = ++parsePageRequestSeq;
  try {
    const data = await parseOCRPage(item, page);
    if (parsePageRequestSeq !== requestSeq || STATE.parseItem !== item || STATE.parseCurrentPage !== page) return;
    text.textContent = parseOCRText(data, parseOCRThresholds(parseCropTargetRow(), page));
  } catch (error) {
    if (parsePageRequestSeq !== requestSeq || STATE.parseItem !== item || STATE.parseCurrentPage !== page) return;
    text.textContent = `OCR 加载失败：${error instanceof Error && error.message ? error.message : "未知错误"}`;
  }
}

/** @param {HTMLElement | null} row */
function finishParseCropChange(row) {
  if (!row) return;
  const textarea = row.querySelector("[data-parse-content]");
  const status = row.querySelector("[data-parse-body-status]");
  if (textarea instanceof HTMLTextAreaElement && textarea.dataset.edited === "1") {
    if (status) status.textContent = "裁剪已更改，需刷新正文";
  } else {
    refreshParseArticleBody(row, true, true);
  }
  saveParseDraft();
}

/** @param {PointerEvent} event */
function beginParseCropDrag(event) {
  if (!(event.target instanceof Element)) return;
  const handle = event.target.closest("[data-parse-crop-handle]");
  const box = event.target.closest("[data-parse-crop-box]");
  const overlay = event.target.closest("[data-parse-crop-overlay]");
  const row = parseCropTargetRow();
  if (!(overlay instanceof HTMLElement) || !row || event.button !== 0) return;
  event.preventDefault();
  const page = STATE.parseCurrentPage;
  const scope = parseCropScope();
  const options = cropOptions(row, scope, page);
  const thresholds = parseCropThresholdValues(options.content_thresholds) || parseOCRThresholds(row, page);
  parseCropDrag = {
    row, scope, page, thresholds,
    mode: handle ? "resize" : box && box.classList.contains("has-selection") ? "move" : "select",
    corner: handle instanceof HTMLElement ? handle.dataset.parseCropHandle || "" : "",
    startX: event.clientX, startY: event.clientY, rect: overlay.getBoundingClientRect(),
    changed: false,
  };
  if (box) box.classList.add("is-adjusting");
  if (overlay.setPointerCapture) overlay.setPointerCapture(event.pointerId);
}

/** @param {PointerEvent} event */
function moveParseCrop(event) {
  const drag = parseCropDrag;
  if (!drag || !drag.rect.width || !drag.rect.height) return;
  event.preventDefault();
  const minimum = 0.02;
  const startX = Math.min(1, Math.max(0, (drag.startX - drag.rect.left) / drag.rect.width));
  const startY = Math.min(1, Math.max(0, (drag.startY - drag.rect.top) / drag.rect.height));
  const currentX = Math.min(1, Math.max(0, (event.clientX - drag.rect.left) / drag.rect.width));
  const currentY = Math.min(1, Math.max(0, (event.clientY - drag.rect.top) / drag.rect.height));
  let top = drag.thresholds[0];
  let bottom = 1 - drag.thresholds[1];
  let left = drag.thresholds[2];
  let right = 1 - drag.thresholds[3];
  if (drag.mode === "select") {
    top = Math.min(startY, currentY);
    bottom = Math.max(startY, currentY);
    left = Math.min(startX, currentX);
    right = Math.max(startX, currentX);
  } else if (drag.mode === "move") {
    const width = right - left;
    const height = bottom - top;
    left = Math.min(1 - width, Math.max(0, left + currentX - startX));
    top = Math.min(1 - height, Math.max(0, top + currentY - startY));
    right = left + width;
    bottom = top + height;
  } else {
    if (drag.corner.includes("n")) top = Math.min(bottom - minimum, currentY);
    if (drag.corner.includes("s")) bottom = Math.max(top + minimum, currentY);
    if (drag.corner.includes("w")) left = Math.min(right - minimum, currentX);
    if (drag.corner.includes("e")) right = Math.max(left + minimum, currentX);
  }
  if (bottom - top < minimum || right - left < minimum) return;
  const values = [top, 1 - bottom, left, 1 - right];
  const rounded = values.map((value) => Math.round(value * 10000) / 10000);
  setParseCropThresholds(drag.row, drag.scope, drag.page, rounded);
  drag.changed = true;
  updateParseCropUI();
}

function endParseCrop() {
  if (!parseCropDrag) return;
  const row = parseCropDrag.row;
  const changed = parseCropDrag.changed;
  parseCropDrag = null;
  const box = DOM.previewPanel.querySelector("[data-parse-crop-box]");
  if (box) box.classList.remove("is-adjusting");
  if (!changed) return;
  updateParsePageOCR();
  finishParseCropChange(row);
}

/** @param {{doc_id: string}} item @param {number} page @returns {Promise<ParseOCRPage>} */
async function parseOCRPage(item, page) {
  const key = `${item.doc_id}:${page}`;
  const cached = parseOCRCache.get(key);
  if (cached !== undefined) {
    parseOCRCache.delete(key);
    parseOCRCache.set(key, cached);
    return cached;
  }
  const request = {};
  parseOCRRequests.set(key, request);
  try {
    const raw = await apiWithTimeout(`/api/parse/${encodeURIComponent(item.doc_id)}/ocr?page=${page}`, {}, 30000);
    const data = parseBhaPayload(raw, isParseOCRPage, "OCR");
    if (parseOCRRequests.get(key) === request) {
      parseOCRCache.set(key, data);
      while (parseOCRCache.size > PARSE_OCR_CACHE_LIMIT) {
        const first = parseOCRCache.keys().next().value;
        if (first === undefined) break;
        parseOCRCache.delete(first);
      }
    }
    return data;
  } finally {
    if (parseOCRRequests.get(key) === request) parseOCRRequests.delete(key);
  }
}

/** @param {Element | null} row @param {boolean} [force] @param {boolean} [persist] @param {boolean} [preserveEdited] */
async function refreshParseArticleBody(row, force = false, persist = false, preserveEdited = false) {
  const item = STATE.parseItem;
  if (!(row instanceof HTMLElement) || !item) return;
  const textarea = row.querySelector("[data-parse-content]");
  const status = row.querySelector("[data-parse-body-status]");
  const form = row.closest("[data-parse-form]");
  const range = parseArticlePageRange(row);
  if (!(textarea instanceof HTMLTextAreaElement) || !(form instanceof HTMLElement) || !range) return;
  if (!force && textarea.dataset.edited === "1") return;
  const request = {};
  parseArticleRequests.set(row, request);
  const { start, end } = range;
  const maxPage = Number(form.dataset.maxPage) || 1;
  if (!start || !end || start < 1 || end > maxPage || start > end) {
    row.dataset.basePartCount = "";
    if (status) status.textContent = "页码无效";
    return;
  }
  const sourceValue = textarea.value;
  const preserveCurrent = preserveEdited && textarea.dataset.edited === "1";
  row.dataset.basePartCount = "";
  if (status) status.textContent = "正在读取...";
  try {
    const pages = await Promise.all(Array.from({ length: end - start + 1 }, (_value, index) => parseOCRPage(item, start + index)));
    const currentRange = parseArticlePageRange(row);
    if (parseArticleRequests.get(row) !== request || STATE.parseItem !== item || !row.isConnected ||
        !currentRange || currentRange.start !== start || currentRange.end !== end) return;
    const pageTexts = pages.map((data, index) => parseOCRText(data, parseOCRThresholds(row, start + index)));
    const body = pageTexts.filter(Boolean).join("\n");
    const boxCount = pages.reduce((total, data, index) => total + parseOCRRows(data, parseOCRThresholds(row, start + index)).length, 0);
    if (preserveCurrent || (textarea.dataset.edited === "1" && textarea.value !== sourceValue)) {
      row.dataset.basePartCount = String(boxCount);
      if (status) status.textContent = preserveCurrent ? "已恢复草稿" : "已编辑";
      if (persist) saveParseDraft();
      return;
    }
    textarea.value = body;
    textarea.dataset.edited = "0";
    row.dataset.basePartCount = String(boxCount);
    if (status) status.textContent = `${body.length.toLocaleString()} 字`;
    if (persist) saveParseDraft();
  } catch (error) {
    const currentRange = parseArticlePageRange(row);
    if (parseArticleRequests.get(row) !== request || STATE.parseItem !== item || !row.isConnected ||
        !currentRange || currentRange.start !== start || currentRange.end !== end) return;
    row.dataset.basePartCount = "";
    if (status) status.textContent = "OCR 读取失败";
  }
}

/** @param {string} docId */
async function openParseEditor(docId) {
  if (!STATE.parseMode) return;
  const queueItem = STATE.parseItems.find((item) => item.doc_id === docId);
  if (!queueItem) return;
  cancelParseRequest();
  const requestSeq = parseRequestSeq;
  const controller = new AbortController();
  parseAbortController = controller;
  try {
    const raw = await apiWithTimeout(`/api/preview/${encodeURIComponent(docId)}`, { signal: controller.signal }, 30000);
    if (!STATE.parseMode || requestSeq !== parseRequestSeq) return;
    const item = parsePreviewResponse(raw, docId);
    STATE.parseItem = item;
    STATE.preview = docId;
    applyStateToDom();
    DOM.previewPanel.hidden = false;
    DOM.previewPanel.style.display = "";
    DOM.previewPanel.className = "preview-panel parse-panel";
    speechPreviewClosed();
    DOM.previewPanel.innerHTML = parseEditorHTML(item);
    labelPreviewPanel();
    DOM.results.hidden = true;
    DOM.emptyState.style.display = "none";
    animatePreviewPanel();
    const restoredPage = restoreParseDraft(item, DOM.previewPanel.querySelector("[data-parse-form]"));
    loadParsePage(restoredPage || 1);
    DOM.previewPanel.querySelectorAll("[data-parse-article]").forEach((row) => {
      if (row instanceof HTMLElement && !Number(row.dataset.basePartCount)) refreshParseArticleBody(row, true, false, true);
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError" || !STATE.parseMode || requestSeq !== parseRequestSeq) return;
    showToast(`解析资料加载失败：${error instanceof Error && error.message ? error.message : "未知错误"}`);
  } finally {
    if (parseAbortController === controller) parseAbortController = null;
  }
}

/** @param {Element} form @returns {CollectedParseArticle[]} */
function collectParseArticles(form) {
  return Array.from(form.querySelectorAll("[data-parse-article]")).map((row) => {
    if (!(row instanceof HTMLElement)) throw new Error("文章编辑控件无效");
    const field = (/** @type {string} */ name) => {
      const input = row.querySelector(`[data-parse-${name}]`);
      if (!(input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement)) throw new Error(`缺少文章字段：${name}`);
      return input.value;
    };
    const year = Number(field("year")) || null;
    const month = Number(field("month")) || null;
    const day = Number(field("day")) || null;
    /** @type {{year?: number, month?: number, day?: number}} */ const date = {};
    if (year) date.year = year;
    if (month) date.month = month;
    if (day) date.day = day;
    const ocr = parseCropObject(row.dataset.parseOcr);
    const ocrExceptions = parseCropObject(row.dataset.parseOcrExceptions);
    /** @type {CollectedParseArticle} */ const article = {
      title: field("title").trim(),
      authors: field("authors").split(/[、,，/]/).map((value) => value.trim()).filter(Boolean),
      dates: Object.keys(date).length ? [date] : [],
      is_range_date: false,
      page_start: Number(field("start")),
      page_end: Number(field("end")),
      content: field("content"),
      base_part_count: Number(row.dataset.basePartCount) || 0,
    };
    if (Object.keys(ocr).length) article.ocr = ocr;
    if (Object.keys(ocrExceptions).length) article.ocr_exceptions = ocrExceptions;
    return article;
  });
}

/** @param {string} docId @returns {string} */
function parseDraftKey(docId) {
  return `bha-parse-draft:${docId}`;
}

/** @param {unknown} value @returns {value is string} */
function validDraftBaseline(value) {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}

/** @param {Element | null} form */
function draftRecoveryBlocked(form) {
  return form instanceof HTMLElement && (form.dataset.draftRecoveryBlocked === "1" || form.dataset.draftRecoveryRestoring === "1");
}

/** @param {Element} form */
function blockDraftRecovery(form) {
  if (!(form instanceof HTMLElement)) return;
  delete form.dataset.draftRecoveryRestoring;
  form.dataset.draftRecoveryBlocked = "1";
  if (!form.querySelector("[data-draft-recovery-warning]")) {
    const warning = document.createElement("div");
    warning.dataset.draftRecoveryWarning = "1";
    warning.setAttribute("role", "alert");
    warning.textContent = "发现旧草稿或草稿在别处被更改，无法安全自动恢复。草稿暂存于当前浏览器标签页；请勿关闭或清除标签页，也不要提交或重试，请使用下方按钮复制草稿并联系管理员核对。";
    form.prepend(warning);
  }
}

/** @param {Element | null} form @param {string} key */
function offerDraftCopy(form, key) {
  if (!(form instanceof HTMLElement) || form.dataset.draftRecoveryBlocked !== "1") return;
  const warning = form.querySelector("[data-draft-recovery-warning]");
  if (!warning || warning.querySelector("[data-copy-preserved-draft]")) return;
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.copyPreservedDraft = "1";
  button.textContent = "复制保留的草稿";
  button.addEventListener("click", async () => {
    const raw = sessionGetForSubmission(key);
    if (raw == null) { showToast("无法读取保留的草稿，请勿关闭当前标签页并联系管理员。"); return; }
    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(raw);
      showToast("草稿已复制，请粘贴到安全位置保存；不要公开分享。");
    } catch (_error) {
      showToast("无法复制草稿，请勿关闭当前标签页并联系管理员。");
    }
  });
  warning.append(" ", button);
}

/** @param {Element | null} form @param {string} key */
function offerCurrentFormCopy(form, key) {
  if (!(form instanceof HTMLElement) || form.dataset.draftRecoveryBlocked !== "1") return;
  const isProofread = form.hasAttribute("data-proofread-form");
  const originalDoc = isProofread ? STATE.preview : STATE.parseItem?.doc_id;
  const warning = form.querySelector("[data-draft-recovery-warning]");
  if (!warning || warning.querySelector("[data-copy-current-draft]")) return;
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.copyCurrentDraft = "1";
  button.textContent = "复制当前表单（可能未保存）";
  button.addEventListener("click", async () => {
    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") throw new Error("clipboard unavailable");
      if (isProofread ? STATE.preview !== originalDoc : STATE.parseItem?.doc_id !== originalDoc) throw new Error("editor identity changed");
      const clone = /** @type {HTMLElement} */ (form.cloneNode(true));
      delete clone.dataset.draftRecoveryBlocked;
      delete clone.dataset.draftRecoveryRestoring;
      const sourceControls = form.querySelectorAll("input, textarea, select");
      const clonedControls = clone.querySelectorAll("input, textarea, select");
      sourceControls.forEach((source, index) => {
        const target = clonedControls[index];
        if ((source instanceof HTMLInputElement || source instanceof HTMLTextAreaElement || source instanceof HTMLSelectElement) &&
            (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement)) target.value = source.value;
      });
      const value = isProofread ? collectProofreadDraft(clone) : collectParseDraft(clone);
      if (!value) throw new Error("current form unavailable");
      await navigator.clipboard.writeText(JSON.stringify({key, draft: value}));
      showToast("当前表单已复制，请粘贴到安全位置保存；不要公开分享。");
    } catch (_error) {
      showToast("无法复制当前表单，请勿关闭当前标签页并联系管理员。");
    }
  });
  warning.append(" ", button);
}

/** @param {Element | null} form @param {string} key @param {unknown} baseline */
function protectExistingDraft(form, key, baseline) {
  if (!(form instanceof HTMLElement) || draftRecoveryBlocked(form)) return;
  const raw = safeSessionGet(key);
  if (raw === null) {
    if (form.dataset.draftLastSaved !== undefined) {
      blockDraftRecovery(form);
      offerCurrentFormCopy(form, key);
      showToast("保存的草稿已不可见，当前表单可能含未保存修改，请勿关闭并复制保存。");
    }
    else form.dataset.draftRecoveryChecked = "1";
    offerDraftCopy(form, key);
    return;
  }
  try {
    const value = JSON.parse(raw);
    if (!isJsonObject(value) || value.version !== 2 || !validDraftBaseline(baseline) || value.content_hash !== baseline) blockDraftRecovery(form);
    else if (form.dataset.draftLastSaved !== undefined && form.dataset.draftLastSaved !== raw) {
      blockDraftRecovery(form);
      offerCurrentFormCopy(form, key);
      showToast("草稿在保存前发生变化，当前表单可能含未保存修改，请勿关闭并复制保存。");
    }
    else if (form.dataset.draftRecoveryChecked !== "1") form.dataset.draftRecoveryRestoring = "1";
  } catch (_error) { blockDraftRecovery(form); }
  offerDraftCopy(form, key);
}

/** @param {Element | null} form */
function collectParseDraft(form) {
  if (!(form instanceof HTMLElement) || !STATE.parseItem || form.dataset.parseSubmitted === "1" || draftRecoveryBlocked(form)) return null;
  const rows = Array.from(form.querySelectorAll("[data-parse-article]"));
  const articles = collectParseArticles(form).map((article, index) => {
    const textarea = rows[index]?.querySelector("[data-parse-content]");
    return { ...article, content_edited: textarea instanceof HTMLTextAreaElement && textarea.dataset.edited === "1" };
  });
  const description = form.querySelector("[data-parse-description]");
  return {
    version: 2,
    content_hash: STATE.parseItem.content_hash,
    articles,
    description: description instanceof HTMLTextAreaElement ? description.value : "",
    page: STATE.parseCurrentPage,
  };
}

function flushParseDraft() {
  clearTimeout(parseDraftTimer);
  parseDraftTimer = 0;
  if (!STATE.parseItem) return;
  const form = DOM.previewPanel.querySelector("[data-parse-form]");
  protectExistingDraft(form, parseDraftKey(STATE.parseItem.doc_id), STATE.parseItem.content_hash);
  const draft = collectParseDraft(form);
  if (draft) {
    const raw = JSON.stringify(draft);
    if (safeSessionSet(parseDraftKey(STATE.parseItem.doc_id), raw) && form instanceof HTMLElement) form.dataset.draftLastSaved = raw;
  }
}

function saveParseDraft() {
  clearTimeout(parseDraftTimer);
  parseDraftTimer = setTimeout(flushParseDraft, 400);
}

/** @param {string} docId */
function clearParseDraft(docId) {
  clearTimeout(parseDraftTimer);
  parseDraftTimer = 0;
  try { sessionStorage.removeItem(parseDraftKey(docId)); } catch (_error) { /* storage is optional */ }
}

/** @param {ValidatedPreview} item @param {Element | null} form @returns {number} */
function restoreParseDraft(item, form) {
  if (!(form instanceof HTMLElement)) return 0;
  const raw = safeSessionGet(parseDraftKey(item.doc_id));
  if (raw === null) { form.dataset.draftRecoveryChecked = "1"; return 0; }
  if (form.dataset.draftRecoveryBlocked === "1") return 0;
  /** @type {unknown} */ let draft;
  try { draft = JSON.parse(raw); } catch (_error) { blockDraftRecovery(form); offerDraftCopy(form, parseDraftKey(item.doc_id)); return 0; }
  if (!isJsonObject(draft) || draft.version !== 2 || !validDraftBaseline(item.content_hash) || draft.content_hash !== item.content_hash || !Array.isArray(draft.articles) ||
      !draft.articles.length || !draft.articles.every(isJsonObject) ||
      typeof draft.description !== "string" || !Number.isSafeInteger(draft.page) || Number(draft.page) < 1 ||
      draft.articles.some((article) => typeof article.title !== "string" || typeof article.content !== "string" ||
        !isStringList(article.authors) || !Array.isArray(article.dates) || !article.dates.every(isJsonObject) ||
        !Number.isSafeInteger(article.page_start) || !Number.isSafeInteger(article.page_end) ||
        !Number.isSafeInteger(article.base_part_count) || typeof article.content_edited !== "boolean" ||
        (article.ocr !== undefined && !isJsonObject(article.ocr)) ||
        (article.ocr_exceptions !== undefined && !isJsonObject(article.ocr_exceptions)))) {
    blockDraftRecovery(form);
    offerDraftCopy(form, parseDraftKey(item.doc_id));
    return 0;
  }
  if (form.dataset.draftLastSaved !== undefined && form.dataset.draftLastSaved !== raw) {
    blockDraftRecovery(form);
    offerDraftCopy(form, parseDraftKey(item.doc_id));
    offerCurrentFormCopy(form, parseDraftKey(item.doc_id));
    return 0;
  }
  form.dataset.draftRecoveryRestoring = "1";
  const maxPage = Number(form.dataset.maxPage) || 1;
  try {
    const holder = form.querySelector("[data-parse-articles]");
    if (!(holder instanceof HTMLElement)) throw new Error("draft form unavailable");
    const articles = draft.articles;
    holder.innerHTML = articles.map((article, index) => parseArticleRowHTML(article, index, maxPage)).join("");
    holder.querySelectorAll("[data-parse-article]").forEach((row, index) => {
      const article = articles[index];
      if (!(row instanceof HTMLElement) || !article) return;
      row.dataset.basePartCount = String(Number(article.base_part_count) || "");
      const textarea = row.querySelector("[data-parse-content]");
      if (!(textarea instanceof HTMLTextAreaElement)) return;
      textarea.dataset.edited = article.content_edited ? "1" : "0";
      const status = row.querySelector("[data-parse-body-status]");
      if (status) status.textContent = article.content_edited ? "已恢复草稿" : `${textarea.value.length.toLocaleString()} 字`;
    });
    const description = form.querySelector("[data-parse-description]");
    if (description instanceof HTMLTextAreaElement) description.value = typeof draft.description === "string" ? draft.description : "";
    form.dataset.draftLastSaved = raw;
    form.dataset.draftRecoveryChecked = "1";
    return Math.min(maxPage, Math.max(1, Number(draft.page) || 1));
  } catch (_error) {
    blockDraftRecovery(form);
    offerDraftCopy(form, parseDraftKey(item.doc_id));
    return 0;
  } finally {
    delete form.dataset.draftRecoveryRestoring;
  }
}

// BEGIN STRICT PARSE COVERAGE
/** @typedef {{title: string, content: string, base_part_count: number, page_start: number, page_end: number}} ParseCoverageArticle */
/** @param {ParseCoverageArticle[]} articles @param {number} maxPage @returns {string} */
function parseCoverageError(articles, maxPage) {
  if (articles.some((article) => !article.title)) return "每篇文章都需要标题";
  if (articles.some((article) => !article.content.trim())) return "每篇文章都需要正文";
  if (articles.some((article) => !article.base_part_count)) return "正文 OCR 基线加载失败，请检查网络后重试";
  if (articles.some((article) => article.page_start < 1 || article.page_end > maxPage || article.page_start > article.page_end)) return "文章页码范围无效";
  const covered = new Set();
  articles.forEach((article) => { for (let page = article.page_start; page <= article.page_end; page += 1) covered.add(page); });
  const missing = Array.from({ length: maxPage }, (_value, index) => index + 1).filter((page) => !covered.has(page));
  return missing.length ? `以下页面尚未被文章覆盖：${missing.join("、")}` : "";
}
// END STRICT PARSE COVERAGE

/** @param {CollectedParseArticle} article @param {number} page @returns {number[]} */
function parseArticleThresholds(article, page) {
  const base = article.ocr && parseCropThresholdValues(article.ocr.content_thresholds);
  const override = article.ocr_exceptions?.[String(page)];
  return (isJsonObject(override) && parseCropThresholdValues(override.content_thresholds)) || base || [0, 0, 0, 0];
}

/** @param {{doc_id: string, source_files: PreviewSourceFile[]}} item @param {number} page @returns {string} */
function parseSourceImageURL(item, page) {
  const files = Array.isArray(item.source_files) ? item.source_files : [];
  const fileIndex = files.length === 1 ? 0 : page - 1;
  const file = files[fileIndex] || {};
  const pageCount = Number(file.page_count) || (Array.isArray(file.pages) ? file.pages.length : 0);
  if (pageCount) return pageImageURL(item.doc_id, fileIndex, files.length === 1 ? page : 1);
  return sourceDownloadURL(item, fileIndex, true);
}

/** @param {number[]} thresholds @returns {string} */
function parseCropLabel(thresholds) {
  return `上 ${Math.round(thresholds[0] * 100)}% / 下 ${Math.round(thresholds[1] * 100)}% / 左 ${Math.round(thresholds[2] * 100)}% / 右 ${Math.round(thresholds[3] * 100)}%`;
}

/** @param {ValidatedPreview} item @param {ParseSubmitPayload} payload @returns {string} */
function parseReviewHTML(item, payload) {
  const rows = payload.articles.map((article, index) => {
    const date = (article.dates || []).map((value) => metadataItemText("dates", value)).filter(Boolean).join("、") || "（空）";
    const authors = (article.authors || []).join("、") || "（空）";
    const crops = [];
    for (let page = article.page_start; page <= article.page_end; page += 1) {
      const thresholds = parseArticleThresholds(article, page);
      if (!thresholds.some((value) => value > 0)) continue;
      crops.push(`<div class="parse-review-crop"><div class="parse-review-crop-label">第 ${page} 页 · ${escapeHTML(parseCropLabel(thresholds))}</div><canvas data-parse-review-crop data-page="${page}" data-thresholds="${escapeHTML(JSON.stringify(thresholds))}" aria-label="第 ${page} 页裁剪图"></canvas><span data-parse-review-crop-error hidden>裁剪图加载失败</span></div>`);
    }
    return `<div class="proofread-confirm-item parse-review-article"><strong>文章 ${index + 1} · ${escapeHTML(article.title)}</strong><div class="parse-review-meta"><span>作者：${escapeHTML(authors)}</span><span>日期：${escapeHTML(date)}</span><span>页码：${article.page_start === article.page_end ? article.page_start : `${article.page_start}–${article.page_end}`}</span><span>OCR 基线：${article.base_part_count} 段</span></div><div class="proofread-confirm-field parse-review-content">${escapeHTML(article.content)}</div>${crops.length ? `<div class="parse-review-crops">${crops.join("")}</div>` : ""}</div>`;
  });
  if (payload.description) rows.push(`<div class="proofread-confirm-item"><strong>提交说明</strong><div class="proofread-confirm-field">${escapeHTML(payload.description)}</div></div>`);
  return rows.join("");
}

/** @param {HTMLElement} holder @param {ValidatedPreview} item */
function hydrateParseReviewCrops(holder, item) {
  holder.querySelectorAll("[data-parse-review-crop]").forEach((canvas) => {
    if (!(canvas instanceof HTMLCanvasElement)) return;
    const page = Number(canvas.dataset.page);
    /** @type {unknown} */ let rawThresholds;
    try { rawThresholds = JSON.parse(canvas.dataset.thresholds || "null"); } catch (_error) { rawThresholds = null; }
    const thresholds = parseCropThresholdValues(rawThresholds);
    const error = canvas.parentElement?.querySelector("[data-parse-review-crop-error]");
    if (!thresholds) {
      canvas.hidden = true;
      if (error instanceof HTMLElement) error.hidden = false;
      return;
    }
    const image = new Image();
    image.onload = () => {
      const [top, bottom, left, right] = thresholds;
      const sourceX = Math.floor(left * image.naturalWidth);
      const sourceY = Math.floor(top * image.naturalHeight);
      const sourceWidth = Math.max(1, Math.ceil((1 - right) * image.naturalWidth) - sourceX);
      const sourceHeight = Math.max(1, Math.ceil((1 - bottom) * image.naturalHeight) - sourceY);
      const scale = Math.min(1, 1000 / sourceWidth, 700 / sourceHeight);
      canvas.width = Math.max(1, Math.round(sourceWidth * scale));
      canvas.height = Math.max(1, Math.round(sourceHeight * scale));
      const context = canvas.getContext("2d");
      if (context) context.drawImage(image, sourceX, sourceY, sourceWidth, sourceHeight, 0, 0, canvas.width, canvas.height);
      else {
        canvas.hidden = true;
        if (error instanceof HTMLElement) error.hidden = false;
      }
    };
    image.onerror = () => {
      canvas.hidden = true;
      if (error instanceof HTMLElement) error.hidden = false;
    };
    image.src = parseSourceImageURL(item, page);
  });
}

function removeParseConfirm() {
  const panel = document.querySelector("[data-parse-confirm]");
  if (panel) panel.remove();
}

/** @param {HTMLElement} form @param {ValidatedPreview} item @param {ParseSubmitPayload} payload */
function showParseConfirm(form, item, payload) {
  let confirmDraft = null;
  try { const value = collectParseDraft(form); confirmDraft = value ? JSON.stringify(value) : null; } catch (_error) { /* fail closed on confirmation */ }
  removeParseConfirm();
  const holder = document.createElement("div");
  holder.className = "proofread-confirm";
  holder.dataset.parseConfirm = "true";
  holder.innerHTML = `<div class="proofread-confirm-panel parse-confirm-panel" role="dialog" aria-modal="true" aria-label="解析提交预览"><div class="proofread-confirm-header"><span>解析提交预览 · ${payload.articles.length} 篇</span><button type="button" class="icon-btn-sm" data-cancel-parse aria-label="关闭预览">×</button></div><div class="proofread-confirm-body">${parseReviewHTML(item, payload)}</div><div class="proofread-confirm-actions"><button type="button" class="text-btn-sm" data-cancel-parse>返回继续编辑</button><button type="button" class="result-action-btn primary" data-confirm-parse>确认提交</button></div></div>`;
  document.body.append(holder);
  hydrateParseReviewCrops(holder, item);
  holder.querySelectorAll("[data-cancel-parse]").forEach((button) => button.addEventListener("click", removeParseConfirm));
  const button = holder.querySelector("[data-confirm-parse]");
  if (!(button instanceof HTMLButtonElement)) return;
  button.addEventListener("click", async () => {
    let currentDraft;
    try { const value = collectParseDraft(form); currentDraft = value ? JSON.stringify(value) : null; } catch (_error) { currentDraft = null; }
    if (confirmDraft == null || currentDraft == null || STATE.parseItem !== item || !form.isConnected || currentDraft !== confirmDraft || draftRecoveryBlocked(form)) {
      flushParseDraft();
      showToast("表单已变化，请返回检查后重新预览，草稿已保留。");
      return;
    }
    button.disabled = true;
    button.textContent = "提交中...";
    const submitted = await postParse(form, item, payload);
    if (submitted) removeParseConfirm();
    else if (form.dataset.submissionUnconfirmed === "1") {
      button.disabled = true;
      button.textContent = "结果未确认，请勿重复提交";
    } else {
      button.disabled = false;
      button.textContent = "确认提交";
    }
  });
}

/** @param {HTMLElement} form @param {ValidatedPreview} item @param {ParseSubmitPayload} payload @returns {Promise<boolean>} */
async function postParse(form, item, payload) {
  flushParseDraft();
  if (draftRecoveryBlocked(form) || !validDraftBaseline(item.content_hash)) {
    showToast("草稿或预览基线无法确认，请保留草稿并联系管理员核对。");
    return false;
  }
  const priorDraft = sessionGetForSubmission(parseDraftKey(item.doc_id));
  if (priorDraft === undefined) return false;
  let initialFormDraft = null;
  try { initialFormDraft = STATE.parseItem === item ? JSON.stringify(collectParseDraft(form)) : null; }
  catch (_error) { showToast("无法核对当前草稿，请保留并勿重复提交。"); return false; }
  // A generic HTTP response, timeout or abort is not a durable receipt. This
  // marker lasts only for this mounted form and is not a cross-tab lock.
  if (form.dataset.submissionUnconfirmed === "1") {
    showToast("上次提交结果未确认，请保留草稿并联系管理员核查，勿重复提交。");
    return false;
  }
  form.dataset.submissionUnconfirmed = "1";
  try {
    await apiWithTimeout("/api/parse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ doc_id: item.doc_id, base_content_hash: item.content_hash, ...payload }) }, 30000);
    let liveDraftChanged = false;
    if (initialFormDraft !== null && form.isConnected && STATE.parseItem === item) {
      let currentFormDraft;
      try { currentFormDraft = JSON.stringify(collectParseDraft(form)); } catch (_error) { currentFormDraft = null; }
      if (currentFormDraft !== initialFormDraft) { liveDraftChanged = true; flushParseDraft(); }
    }
    if (liveDraftChanged || sessionGetForSubmission(parseDraftKey(item.doc_id)) !== priorDraft) {
      blockDraftRecovery(form);
      offerDraftCopy(form, parseDraftKey(item.doc_id));
      offerCurrentFormCopy(form, parseDraftKey(item.doc_id));
      if (liveDraftChanged) showToast("提交结果已返回，等待期间的表单修改需另行保存；请勿重复提交并联系管理员核查。");
      else showToast("提交结果已返回，但草稿在等待期间发生变化；请勿重复提交并联系管理员核查。");
      return false;
    }
    showToast("请求响应已收到，但尚无可靠处理回执；请保留草稿并联系管理员核查，勿重复提交。");
    return false;
  } catch (submitError) {
    if (initialFormDraft !== null && form.isConnected && STATE.parseItem === item) {
      let currentFormDraft;
      try { currentFormDraft = JSON.stringify(collectParseDraft(form)); } catch (_error) { currentFormDraft = null; }
      if (currentFormDraft !== initialFormDraft) {
        flushParseDraft();
        blockDraftRecovery(form);
        offerDraftCopy(form, parseDraftKey(item.doc_id));
        offerCurrentFormCopy(form, parseDraftKey(item.doc_id));
      }
    }
    showToast(`解析提交结果未确认，请保留草稿并先核查，勿重复提交：${submitError instanceof Error && submitError.message ? submitError.message : "未知错误"}`);
    return false;
  }
}

/** @param {Element | null} form */
async function submitParseForm(form) {
  const item = STATE.parseItem;
  if (!item || !(form instanceof HTMLElement)) return;
  if (draftRecoveryBlocked(form) || !validDraftBaseline(item.content_hash)) {
    showToast("草稿或预览基线无法确认，请保留草稿并联系管理员核对。");
    return;
  }
  if (form.dataset.submissionUnconfirmed === "1") {
    showToast("上次提交结果未确认，请保留草稿并联系管理员核查，勿重复提交。");
    return;
  }
  let articles = collectParseArticles(form);
  const maxPage = Number(form.dataset.maxPage) || 1;
  const errorNode = form.querySelector("[data-parse-error]");
  if (!(errorNode instanceof HTMLElement)) return;
  if (articles.some((article) => !article.base_part_count)) {
    const button = form.querySelector('button[type="submit"]');
    const label = button?.textContent || "";
    if (button instanceof HTMLButtonElement) {
      button.disabled = true;
      button.textContent = "正在读取 OCR 基线...";
    }
    errorNode.hidden = true;
    const rows = Array.from(form.querySelectorAll("[data-parse-article]")).filter((row) => row instanceof HTMLElement);
    await Promise.all(rows.filter((row) => !Number(row.dataset.basePartCount)).map((row) => (
      refreshParseArticleBody(row, true, false, true)
    )));
    if (button instanceof HTMLButtonElement) {
      button.disabled = false;
      button.textContent = label;
    }
    if (STATE.parseItem !== item || !form.isConnected) return;
    articles = collectParseArticles(form);
  }
  const error = parseCoverageError(articles, maxPage);
  if (error) {
    errorNode.textContent = error;
    errorNode.hidden = false;
    return;
  }
  errorNode.hidden = true;
  flushParseDraft();
  const description = form.querySelector("[data-parse-description]");
  if (!(description instanceof HTMLTextAreaElement)) return;
  showParseConfirm(form, item, { articles, description: description.value.trim() });
}

function scheduleSearch() {
  if (searchComposing) return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    STATE.parseMode = false;
    STATE.q = DOM.searchInput.value.trim();
    STATE.page = 1;
    doSearch();
  }, 120);
}

async function restorePreview(scroll = true) {
  if (!STATE.preview) {
    cancelPreviewRequests();
    clearPreviewDom();
    if (!searchAbortController && !bootstrapPending) renderResults();
    return;
  }
  const docId = STATE.preview;
  const requestSeq = previewRequestSeq + 1;
  previewRequestSeq = requestSeq;
  const speechReady = STATE.proofread || STATE.compare ? null : ensureSpeechClient();
  if (previewAbortController) {
    previewAbortController.abort();
    previewAbortController = null;
  }
  clearActiveMediaViewer();
  // Keep the reader panel inside a modal preview's focus scope while the next
  // article loads; it is detached only right before the DOM is replaced.
  speechHost.CCRDSpeech?.previewPending?.();
  try {
    let item = getCachedPreview(docId);
    if (!item) {
      previewAbortController = new AbortController();
      const controller = previewAbortController;
      item = parsePreviewResponse(await apiWithTimeout(`/api/preview/${encodeURIComponent(docId)}`, { signal: controller.signal }), docId);
      setCachedPreview(docId, item);
      if (previewAbortController === controller) previewAbortController = null;
    }
    if (requestSeq !== previewRequestSeq || STATE.preview !== docId) return;
    clearActiveMediaViewer();
    DOM.previewPanel.classList.remove("proofread-mode");
    speechPreviewClosed();
    DOM.previewPanel.innerHTML = previewHTML(item);
    labelPreviewPanel();
    prefetchPreviewFirstPage(item);
    DOM.previewPanel.hidden = false;
    DOM.previewPanel.style.display = "flex";
    document.body.classList.add("preview-open");
    setPreviewModalState(true);
    requestAnimationFrame(() => {
      const close = DOM.previewPanel.querySelector("[data-close-preview]");
      if (close instanceof HTMLElement) close.focus({ preventScroll: true });
    });
    DOM.emptyState.style.display = "none";
    if (animateNextPreview) animatePreviewPanel();
    animateNextPreview = false;
    if (STATE.proofread) {
      openProofread(item, null, true);
    } else if (STATE.compare) {
      comparePreview(STATE.compare, true);
    } else {
      await speechReady;
      if (requestSeq !== previewRequestSeq || STATE.preview !== docId) return;
      speechPreviewLoaded(docId, item);
    }
    const previewBody = DOM.previewPanel.querySelector(".preview-body");
    if (scroll && previewBody) previewBody.scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) {
    if (requestSeq !== previewRequestSeq || STATE.preview !== docId) return;
    animateNextPreview = false;
    if (error instanceof Error && error.name === "AbortError") return;
    showToast("预览失败");
    closePreview(true);
  }
}

/** @param {string} docId @param {boolean} [scroll] */
function openPreview(docId, scroll = true) {
  if (!docId) return;
  if (!STATE.preview && !DOM.previewPanel.contains(document.activeElement)) {
    previewReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (previewReturnFocus && previewReturnFocus.matches("[data-preview]") && previewReturnFocus.dataset.preview !== undefined) {
      previewReturnFocusSelector = `[data-preview="${CSS.escape(previewReturnFocus.dataset.preview)}"]`;
    }
  }
  flushProofreadDraft();
  cancelPreviewRequests();
  const replacingPreview = history.state && history.state.previewEntry === true;
  if (!replacingPreview) previewResultsScrollTop = DOM.results.scrollTop;
  STATE.preview = docId;
  STATE.compare = "";
  STATE.proofread = false;
  STATE.proofreadFile = "";
  STATE.proofreadPage = "";
  animateNextPreview = true;
  applyStateToDom();
  syncUrl(replacingPreview);
  // Returned so the reader's "回到播放位置" can await the reopened article.
  return restorePreview(scroll);
}

/** @param {ValidatedPreview} item @param {ValidatedPreview | null} [compareItem] */
function previewHTML(item, compareItem = null) {
  const themeAction = `<button type="button" class="icon-btn-sm preview-theme-btn" data-preview-theme aria-label="${STATE.isDark ? "切换到白天模式" : "切换到黑夜模式"}" title="${STATE.isDark ? "白天模式" : "黑夜模式"}">${THEME_ICON[STATE.isDark ? "dark" : "light"]}</button>`;
  const commands = `${previewDownloadHTML(item)}${compareSelectHTML(item)}`;
  const actions = `<div class="preview-commands">${commands}</div><div class="preview-tools">${previewFullscreenButtonHTML()}<button type="button" class="icon-btn-sm" data-close-preview aria-label="关闭预览">×</button>${themeAction}</div>`;
  if (compareItem) {
    const diff = diffHTML(item.content || "", compareItem.content || "");
    return `<div class="preview-header"><div class="preview-heading">${previewTitleHTML(item)}<div class="preview-compare-note">正在对比：${escapeHTML(item.publication_name)} / ${escapeHTML(compareItem.publication_name)}</div></div><div class="preview-actions">${actions}</div></div><div class="preview-compare-grid"><div>${previewMetaHTML(item)}<pre class="preview-body compare-body">${diff.left}</pre></div><div>${previewMetaHTML(compareItem)}<pre class="preview-body compare-body">${diff.right}</pre></div></div>`;
  }
  return `<div class="preview-header"><div class="preview-heading">${previewTitleHTML(item)}${previewMetaHTML(item)}</div><div class="preview-actions">${actions}</div></div><pre class="preview-body" data-proofread-select>${highlightPreviewText(item.content || "")}</pre>`;
}

// BEGIN STRICT PREVIEW DOWNLOAD LINKS
/** @param {{doc_id: string, source_file_count: number}} item @returns {string} */
function previewDownloadHTML(item) {
  if (item.source_file_count > 1) {
    return `<a class="result-action-btn primary" href="/api/download/source-zip/${encodeURIComponent(item.doc_id)}">打包原文件</a>`;
  }
  if (item.source_file_count === 1) {
    return `<a class="result-action-btn primary" href="/api/download/source/${encodeURIComponent(item.doc_id)}">下载原文件</a>`;
  }
  return "";
}

/** @param {{doc_id: string}} item @param {number | string} [fileIndex] @param {boolean} [inline] @returns {string} */
function sourceDownloadURL(item, fileIndex = 0, inline = false) {
  const route = inline ? "/api/preview/source/" : "/api/download/source/";
  return `${route}${encodeURIComponent(item.doc_id)}?file=${encodeURIComponent(fileIndex)}${inline ? "&inline=true" : ""}`;
}

/** @param {string} docId @param {number | string} fileIndex @param {number | string} page @returns {string} */
function pageImageURL(docId, fileIndex, page) {
  return `/api/preview/source-page/${encodeURIComponent(docId)}?file=${encodeURIComponent(fileIndex)}&page=${encodeURIComponent(page)}`;
}
// END STRICT PREVIEW DOWNLOAD LINKS

/** @param {PreviewSourceFile} file @param {number} page @param {string} sourceDownload */
function pageImageViewerHTML(file, page, sourceDownload) {
  const pages = file.pages || [];
  const pageNumbers = file.page_numbers || pages.map((_url, index) => index + 1);
  const pageCount = Math.max(pages.length, Number(file && file.page_count) || 0);
  const pageIndex = Math.min(pageCount - 1, Math.max(0, page - 1));
  const fileIndex = Number(file && file.index) || 0;
  const initialPage = Number(file && file.initial_page_number) || 0;
  const initialURL = file && file.initial_page || "";
  const proxyPages = pages.map((url, index) => /^https?:/i.test(url) ? pageImageURL(STATE.preview, fileIndex, pageNumbers[index] || index + 1) : url);
  const proxyInitial = /^https?:/i.test(initialURL) ? pageImageURL(STATE.preview, fileIndex, initialPage) : initialURL;
  const initialSource = proxyInitial || proxyPages[0] || "";
  return `<div class="proofread-image-viewer proofread-page-viewer" data-page-image-viewer data-page-images="${escapeHTML(JSON.stringify(proxyPages))}" data-page-numbers="${escapeHTML(JSON.stringify(pageNumbers))}" data-page-count="${pageCount}" data-page-initial-index="${initialPage - 1}" data-page-initial-url="${escapeHTML(proxyInitial)}" data-page-doc-id="${escapeHTML(STATE.preview)}" data-page-file-index="${fileIndex}" data-page-index="${pageIndex}"><div class="proofread-image-tools"><button type="button" class="text-btn-sm" data-page-image-prev ${pageIndex === 0 ? "disabled" : ""}>上一页</button><button type="button" class="text-btn-sm" data-page-image-next ${pageIndex === pageCount - 1 ? "disabled" : ""}>下一页</button><button type="button" class="text-btn-sm" data-image-zoom="out">缩小</button><button type="button" class="text-btn-sm" data-image-zoom="reset">原始大小</button><button type="button" class="text-btn-sm" data-image-zoom="in">放大</button><label class="proofread-page-control">页码 <input data-page-image-input type="number" min="1" max="${pageCount}" value="${pageIndex + 1}"></label><a class="text-btn-sm proofread-image-download" href="${sourceDownload}">下载原文件</a></div><div class="proofread-image-stage"><img data-proofread-source-image src="${escapeHTML(initialSource)}" alt="原文件第 ${pageIndex + 1} 页" loading="eager" decoding="async" fetchpriority="high" data-image-scale="1"></div></div>`;
}

/** @param {HTMLElement} viewer @param {number} page */
async function setPageImage(viewer, page) {
  /** @type {unknown} */ let rawPages;
  try { rawPages = JSON.parse(viewer.dataset.pageImages || "[]"); } catch { rawPages = []; }
  const pages = isStringList(rawPages) ? rawPages : [];
  /** @type {unknown} */ let rawPageNumbers;
  try { rawPageNumbers = JSON.parse(viewer.dataset.pageNumbers || "[]"); } catch { rawPageNumbers = []; }
  const pageNumbers = Array.isArray(rawPageNumbers) && rawPageNumbers.every((number) => Number.isSafeInteger(number) && number > 0) ? rawPageNumbers : [];
  const pageCount = Number(viewer.dataset.pageCount) || pages.length;
  if (!pageCount) return;
  const index = Math.min(pageCount - 1, Math.max(0, page - 1));
  const image = viewer.querySelector("[data-proofread-source-image]");
  const input = viewer.querySelector("[data-page-image-input]");
  if (!(image instanceof HTMLImageElement)) return;
  if (cancelPageImagePrefetch) {
    cancelPageImagePrefetch();
    cancelPageImagePrefetch = null;
  }
  if (pageImagePrefetchController) {
    pageImagePrefetchController.abort();
    pageImagePrefetchController = null;
  }
  pageImageRequests.get(viewer)?.abort();
  const pageImageAbortController = new AbortController();
  pageImageRequests.set(viewer, pageImageAbortController);
  const requestId = String(Number(viewer.dataset.pageRequest || 0) + 1);
  viewer.dataset.pageRequest = requestId;
  const knownIndex = pageNumbers.indexOf(index + 1);
  let url = knownIndex >= 0 ? pages[knownIndex] || "" : "";
  if (!url && index === Number(viewer.dataset.pageInitialIndex)) url = viewer.dataset.pageInitialUrl || "";
  if (!url) url = pageImageURL(viewer.dataset.pageDocId || "", viewer.dataset.pageFileIndex || "0", index + 1);
  if (pageImageAbortController.signal.aborted || viewer.dataset.pageRequest !== requestId || !viewer.isConnected) return;
  if (pageImageRequests.get(viewer) === pageImageAbortController) pageImageRequests.delete(viewer);
  viewer.dataset.pageIndex = String(index);
  image.dataset.imageScale = "1";
  delete image.dataset.imageBaseWidth;
  delete image.dataset.imageLoadedSrc;
  image.style.width = "";
  image.style.height = "auto";
  image.src = url;
  image.alt = `原文件第 ${index + 1} 页`;
  if (input instanceof HTMLInputElement) input.value = String(index + 1);
  const previous = viewer.querySelector("[data-page-image-prev]");
  const next = viewer.querySelector("[data-page-image-next]");
  if (previous instanceof HTMLButtonElement) previous.disabled = index === 0;
  if (next instanceof HTMLButtonElement) next.disabled = index === pageCount - 1;
  STATE.proofreadPage = String(index + 1);
  syncUrl(true);
  saveProofreadDraft();
}

/** @typedef {{saveData?: boolean, effectiveType?: string}} ImageNetworkInfo */
/** @param {HTMLElement | null} viewer */
function prefetchAdjacentPageImage(viewer) {
  if (!viewer) return;
  const network = /** @type {Navigator & {connection?: ImageNetworkInfo, mozConnection?: ImageNetworkInfo, webkitConnection?: ImageNetworkInfo}} */ (navigator);
  const connection = network.connection || network.mozConnection || network.webkitConnection;
  if (connection && (connection.saveData || /(^|-)2g$/.test(connection.effectiveType || ""))) return;
  /** @type {unknown} */ let rawPages;
  try { rawPages = JSON.parse(viewer.dataset.pageImages || "[]"); } catch { rawPages = []; }
  const pages = isStringList(rawPages) ? rawPages : [];
  /** @type {unknown} */ let rawPageNumbers;
  try { rawPageNumbers = JSON.parse(viewer.dataset.pageNumbers || "[]"); } catch { rawPageNumbers = []; }
  const pageNumbers = Array.isArray(rawPageNumbers) && rawPageNumbers.every((number) => Number.isSafeInteger(number) && number > 0) ? rawPageNumbers : [];
  const currentIndex = Number(viewer.dataset.pageIndex) || 0;
  const pageCount = Number(viewer.dataset.pageCount) || pages.length;
  const adjacent = [currentIndex - 1, currentIndex + 1].filter((index) => index >= 0 && index < pageCount);
  if (!adjacent.length) return;
  /** @type {string[]} */ const requests = [];
  adjacent.forEach((index) => {
    const knownIndex = pageNumbers.indexOf(index + 1);
    const url = knownIndex >= 0
      ? pages[knownIndex] || ""
      : pageImageURL(viewer.dataset.pageDocId || "", viewer.dataset.pageFileIndex || "0", index + 1);
    const cacheKey = url || `${viewer.dataset.pageDocId}:${viewer.dataset.pageFileIndex}:${index + 1}`;
    if (prefetchedPageImages.has(cacheKey)) return;
    prefetchedPageImages.add(cacheKey);
    while (prefetchedPageImages.size > 32) {
      const first = prefetchedPageImages.values().next().value;
      if (first === undefined) break;
      prefetchedPageImages.delete(first);
    }
    requests.push(url);
  });
  if (!requests.length) return;
  if (pageImagePrefetchController) pageImagePrefetchController.abort();
  const load = () => {
    cancelPageImagePrefetch = null;
    const controller = new AbortController();
    pageImagePrefetchController = controller;
    const latestConnection = network.connection || network.mozConnection || network.webkitConnection;
    if (!viewer.isConnected || latestConnection && (latestConnection.saveData || /(^|-)2g$/.test(latestConnection.effectiveType || ""))) {
      controller.abort();
      return;
    }
    const loadImage = (/** @type {string} */ url) => {
      const image = new Image();
      image.decoding = "async";
      image.fetchPriority = "low";
      image.src = url;
      if (image.decode) image.decode().catch(() => {});
    };
    requests.forEach(loadImage);
  };
  if (cancelPageImagePrefetch) cancelPageImagePrefetch();
  /** @type {number | null} */ let idleHandle = null;
  const debounceHandle = setTimeout(() => {
    if (window.requestIdleCallback) {
      idleHandle = window.requestIdleCallback(load, { timeout: 1500 });
    } else {
      load();
    }
  }, 800);
  cancelPageImagePrefetch = () => {
    clearTimeout(debounceHandle);
    if (idleHandle !== null && window.cancelIdleCallback) window.cancelIdleCallback(idleHandle);
    if (pageImagePrefetchController) pageImagePrefetchController.abort();
  };
}

/** @param {ValidatedPreview} item */
function prefetchPreviewFirstPage(item) {
  const file = (item && item.source_files || []).find((candidate) => candidate.initial_page || Array.isArray(candidate.pages) && candidate.pages[0]);
  if (!file) return;
  const initialPage = Number(file.initial_page_number) || file.page_numbers?.[0] || 1;
  const firstURL = (file.initial_page || (file.pages || [])[0] || "");
  const image = new Image();
  image.decoding = "async";
  image.fetchPriority = "high";
  image.src = /^https?:/i.test(firstURL) ? pageImageURL(item.doc_id, Number(file.index) || 0, initialPage) : firstURL;
}

/** @param {unknown} url @returns {boolean} */
function isImageSource(url) {
  return /\.(?:bmp|gif|jpe?g|png|tiff?|webp)(?:$|\?)/i.test(String(url || ""));
}

/** @param {unknown} url @returns {boolean} */
function isPDFSource(url) {
  return /\.pdf(?:$|\?)/i.test(String(url || ""));
}

/** @returns {Promise<PDFJSLibrary>} */
function loadPDFJS() {
  if (!pdfjsPromise) {
    pdfjsPromise = import("https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/pdf.min.mjs").then((pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = "https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/build/pdf.worker.min.mjs";
      return pdfjs;
    }).catch((error) => {
      pdfjsPromise = null;
      throw error;
    });
  }
  return pdfjsPromise;
}

// BEGIN STRICT PDF RANGE
/** @param {Response} response @param {number} begin @param {number} end @param {number | null} [expectedLength]
 * @returns {Promise<{data: Uint8Array, length: number}>} */
async function readPDFRange(response, begin, end, expectedLength = null) {
  if (response.status !== 206) {
    if (response.body) await response.body.cancel();
    throw new Error(`PDF range request failed: ${response.status}`);
  }
  const contentRange = response.headers.get("Content-Range") || "";
  const match = /^bytes\s+(\d+)-(\d+)\/(\d+)$/.exec(contentRange);
  const rangeBegin = match ? Number(match[1]) : -1;
  const rangeEnd = match ? Number(match[2]) : -1;
  const length = match ? Number(match[3]) : 0;
  const expectedEnd = Math.min(end, length) - 1;
  if (!Number.isSafeInteger(length) || length <= 0 || (expectedLength != null && length !== expectedLength) || rangeBegin !== begin || rangeEnd !== expectedEnd) {
    if (response.body) await response.body.cancel();
    throw new Error("PDF range response is invalid");
  }
  const data = new Uint8Array(await response.arrayBuffer());
  if (data.length !== rangeEnd - rangeBegin + 1) throw new Error("PDF range response length is invalid");
  return { data, length };
}

/** @param {string} url @param {AbortSignal | undefined} signal */
async function fetchInitialPDFRange(url, signal) {
  const initialResponse = await fetch(url, { headers: { Range: "bytes=0-524287" }, signal });
  return readPDFRange(initialResponse, 0, 512 * 1024);
}
// END STRICT PDF RANGE

/** @param {PDFJSLibrary} pdfjs @param {string} url @param {AbortSignal | undefined} signal
 * @param {Promise<{data: Uint8Array, length: number}> | null} [initialPromise] @returns {Promise<PDFRangeTransport>} */
async function createPDFRangeTransport(pdfjs, url, signal, initialPromise = null) {
  const initial = await (initialPromise || fetchInitialPDFRange(url, signal));
  class URLRangeTransport extends pdfjs.PDFDataRangeTransport {
    constructor() {
      super(initial.length, initial.data, false);
      /** @type {Set<AbortController>} */ this.controllers = new Set();
      /** @type {(reason?: unknown) => void} */ this.rejectFailure = () => {};
      /** @type {Promise<never>} */ this.failurePromise = new Promise((_resolve, reject) => { this.rejectFailure = reject; });
      this.failed = false;
    }

    /** @param {number} begin @param {number} end */
    requestDataRange(begin, end) {
      const controller = new AbortController();
      this.controllers.add(controller);
      fetch(url, {
        headers: { Range: `bytes=${begin}-${end - 1}` },
        signal: controller.signal,
      }).then(async (response) => {
        const range = await readPDFRange(response, begin, end, initial.length);
        this.onDataRange(begin, range.data);
      }).catch((error) => {
        this.abort(error);
      }).finally(() => {
        this.controllers.delete(controller);
      });
    }

    /** @param {unknown} [error] */
    abort(error = null) {
      for (const controller of this.controllers) controller.abort();
      this.controllers.clear();
      if (error && !this.failed) {
        this.failed = true;
        this.rejectFailure(error);
      }
    }
  }
  return new URLRangeTransport();
}

/** @param {string} url @param {PDFCacheEntry | undefined} entry */
function destroyPDFCacheEntry(url, entry) {
  if (!entry) return;
  if (pdfDocumentCache.get(url) === entry) pdfDocumentCache.delete(url);
  if (entry.destroyed) return;
  entry.destroyed = true;
  if (entry.initialController) entry.initialController.abort();
  if (entry.range) entry.range.abort();
  if (entry.loadingTask) entry.loadingTask.destroy().catch(() => {});
}

function trimPDFDocumentCache() {
  const limit = STATE.isMobile ? PDF_DOCUMENT_MOBILE_CACHE_LIMIT : PDF_DOCUMENT_CACHE_LIMIT;
  while (pdfDocumentCache.size > limit) {
    const candidate = [...pdfDocumentCache.keys()].find((url) => !activePDFViewer || activePDFViewer.url !== url);
    if (!candidate) break;
    destroyPDFCacheEntry(candidate, pdfDocumentCache.get(candidate));
  }
}

function cleanupPDFDocumentCache(force = false) {
  clearTimeout(pdfCacheCleanupTimer);
  pdfCacheCleanupTimer = 0;
  const now = Date.now();
  for (const [url, entry] of [...pdfDocumentCache]) {
    const active = activePDFViewer && activePDFViewer.url === url;
    if (force || (!active && now - entry.lastUsed >= PDF_DOCUMENT_CACHE_IDLE_MS)) destroyPDFCacheEntry(url, entry);
  }
  if (!force && pdfDocumentCache.size) pdfCacheCleanupTimer = setTimeout(cleanupPDFDocumentCache, 60 * 1000);
}

function schedulePDFCacheCleanup() {
  if (!pdfCacheCleanupTimer) pdfCacheCleanupTimer = setTimeout(cleanupPDFDocumentCache, 60 * 1000);
}

/** @param {string} url @returns {Promise<PDFDocument>} */
async function getCachedPDFDocument(url) {
  const cached = pdfDocumentCache.get(url);
  if (cached) {
    pdfDocumentCache.delete(url);
    cached.lastUsed = Date.now();
    pdfDocumentCache.set(url, cached);
    schedulePDFCacheCleanup();
    if (cached.promise) return cached.promise;
    throw new Error("PDF 缓存尚未初始化");
  }
  /** @type {PDFCacheEntry} */
  const entry = { promise: null, initialController: new AbortController(), range: null, loadingTask: null, document: null, destroyed: false, lastUsed: Date.now() };
  const pdfjsLoad = loadPDFJS();
  const initialLoad = fetchInitialPDFRange(url, entry.initialController.signal);
  entry.promise = Promise.all([pdfjsLoad, initialLoad]).then(async ([pdfjs, initial]) => {
    const range = await createPDFRangeTransport(pdfjs, url, entry.initialController.signal, Promise.resolve(initial));
    entry.range = range;
    const loadingTask = pdfjs.getDocument({
      range,
      disableStream: true,
      disableAutoFetch: true,
      disableRange: false,
      rangeChunkSize: 512 * 1024,
      cMapUrl: "https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/cmaps/",
      cMapPacked: true,
      standardFontDataUrl: "https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/standard_fonts/",
      wasmUrl: "https://cdn.jsdelivr.net/npm/pdfjs-dist@6.5.438/wasm/",
    });
    entry.loadingTask = loadingTask;
    range.failurePromise.catch(() => {
      destroyPDFCacheEntry(url, entry);
    });
    try {
      entry.document = await Promise.race([loadingTask.promise, range.failurePromise]);
      return entry.document;
    } catch (error) {
      destroyPDFCacheEntry(url, entry);
      throw error;
    }
  }).catch((error) => {
    if (pdfDocumentCache.get(url) === entry) pdfDocumentCache.delete(url);
    throw error;
  });
  pdfDocumentCache.set(url, entry);
  trimPDFDocumentCache();
  schedulePDFCacheCleanup();
  return entry.promise;
}

function clearActiveMediaViewer() {
  if (cancelPageImagePrefetch) cancelPageImagePrefetch();
  cancelPageImagePrefetch = null;
  if (pageImagePrefetchController) pageImagePrefetchController.abort();
  pageImagePrefetchController = null;
  DOM.previewPanel.querySelectorAll("[data-page-image-viewer]").forEach((viewer) => {
    if (!(viewer instanceof HTMLElement)) return;
    pageImageRequests.get(viewer)?.abort();
    pageImageRequests.delete(viewer);
  });
  if (activeImageResizeObserver) activeImageResizeObserver.disconnect();
  cancelAnimationFrame(activeImageFitFrame);
  activeImageFitFrame = 0;
  activeImageResizeObserver = null;
  if (!activePDFViewer) return;
  activePDFViewer.generation += 1;
  if (activePDFViewer.renderTask) activePDFViewer.renderTask.cancel();
  if (activePDFViewer.resizeObserver) activePDFViewer.resizeObserver.disconnect();
  cancelAnimationFrame(activePDFViewer.resizeFrame || 0);
  const entry = pdfDocumentCache.get(activePDFViewer.url);
  if (entry && !entry.document) destroyPDFCacheEntry(activePDFViewer.url, entry);
  activePDFViewer = null;
}

/** @param {HTMLElement} viewer */
async function renderPDFViewer(viewer) {
  const url = viewer.dataset.pdfUrl;
  const download = viewer.dataset.pdfDownload;
  const canvas = viewer.querySelector("[data-pdf-canvas]");
  const stage = viewer.querySelector(".proofread-pdf-stage");
  const status = viewer.querySelector("[data-pdf-status]");
  const pageInput = viewer.querySelector("[data-pdf-page]");
  if (!url || !(canvas instanceof HTMLCanvasElement)) return;
  if (activePDFViewer) clearActiveMediaViewer();
  /** @type {PDFViewerLifecycle} */ const lifecycle = { viewer, url, generation: 0, renderTask: null, resizeObserver: null, resizeFrame: 0 };
  activePDFViewer = lifecycle;
  /** @type {PDFDocument | null} */ let pdf = null;
  let pageNumber = Math.max(1, Number(viewer.dataset.pdfPage) || 1);
  let zoomOffset = 0;
  const setStatus = (/** @type {string} */ text) => { if (status) status.textContent = text; };
  const showError = (/** @type {unknown} */ error) => {
    if (activePDFViewer !== lifecycle || error instanceof Error && error.name === "RenderingCancelledException") return;
    if (stage) {
      stage.classList.remove("is-loading");
      stage.classList.add("is-error");
    }
    setStatus("PDF 无法在线预览");
    if (!viewer.querySelector("[data-pdf-download-link], [data-pdf-fallback]")) {
      const fallback = document.createElement("a");
      fallback.className = "text-btn-sm";
      fallback.dataset.pdfFallback = "true";
      fallback.href = download || url;
      fallback.textContent = "下载原文件";
      viewer.append(fallback);
    }
  };
  const renderPage = async () => {
    if (!pdf) return false;
    const generation = lifecycle.generation + 1;
    lifecycle.generation = generation;
    if (lifecycle.renderTask) {
      lifecycle.renderTask.cancel();
      try { await lifecycle.renderTask.promise; } catch (_error) { /* a newer render superseded it */ }
      lifecycle.renderTask = null;
    }
    pageNumber = Math.min(pdf.numPages, Math.max(1, pageNumber));
    const page = await pdf.getPage(pageNumber);
    if (activePDFViewer !== lifecycle || generation !== lifecycle.generation || !viewer.isConnected) return;
    const unitViewport = page.getViewport({ scale: 1 });
    const availableWidth = Math.max(1, (stage ? stage.clientWidth : unitViewport.width) - 32);
    const fitScale = Math.min(1.5, Math.max(0.5, availableWidth / unitViewport.width));
    const scale = Math.min(3, Math.max(0.5, fitScale + zoomOffset));
    const viewport = page.getViewport({ scale });
    const context = canvas.getContext("2d");
    if (!context) throw new Error("PDF 画布不可用");
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    canvas.style.width = `${Math.ceil(viewport.width)}px`;
    canvas.style.height = `${Math.ceil(viewport.height)}px`;
    const renderTask = page.render({ canvasContext: context, viewport });
    lifecycle.renderTask = renderTask;
    try {
      await renderTask.promise;
    } catch (error) {
      if (error instanceof Error && error.name === "RenderingCancelledException") return false;
      throw error;
    } finally {
      if (generation === lifecycle.generation) lifecycle.renderTask = null;
    }
    if (activePDFViewer !== lifecycle || generation !== lifecycle.generation || !viewer.isConnected) return false;
    if (pageInput instanceof HTMLInputElement) {
      pageInput.value = String(pageNumber);
      pageInput.max = String(pdf.numPages);
    }
    viewer.dataset.pdfPage = String(pageNumber);
    STATE.proofreadPage = String(pageNumber);
    syncUrl(true);
    setStatus(`${pageNumber} / ${pdf.numPages}`);
    return true;
  };
  const requestRender = () => { renderPage().catch(showError); };
  try {
    setStatus("正在加载 PDF...");
    pdf = await getCachedPDFDocument(url);
    if (activePDFViewer !== lifecycle || !viewer.isConnected) return;
    const rendered = await renderPage();
    if (!rendered) return;
    if (stage) stage.classList.remove("is-loading");
    viewer.querySelectorAll("[data-pdf-action]").forEach((button) => {
      if (!(button instanceof HTMLButtonElement)) return;
      button.addEventListener("click", async () => {
        const action = button.dataset.pdfAction;
        if (action === "prev") pageNumber -= 1;
        else if (action === "next") pageNumber += 1;
        else if (action === "zoom-in") zoomOffset = Math.min(2.5, zoomOffset + 0.2);
        else if (action === "zoom-out") zoomOffset = Math.max(-1, zoomOffset - 0.2);
        else if (action === "reset") zoomOffset = 0;
        requestRender();
      });
    });
    if (pageInput instanceof HTMLInputElement) pageInput.addEventListener("change", () => { pageNumber = Number(pageInput.value) || 1; requestRender(); });
    if (stage && typeof ResizeObserver !== "undefined") {
      let lastWidth = stage.clientWidth;
      const resizeObserver = new ResizeObserver(() => {
        const width = stage.clientWidth;
        if (Math.abs(width - lastWidth) < 2) return;
        lastWidth = width;
        cancelAnimationFrame(lifecycle.resizeFrame);
        lifecycle.resizeFrame = requestAnimationFrame(requestRender);
      });
      lifecycle.resizeObserver = resizeObserver;
      resizeObserver.observe(stage);
    }
  } catch (error) {
    showError(error);
  }
}

async function openRandomPreview() {
  const requestSeq = randomRequestSeq + 1;
  randomRequestSeq = requestSeq;
  if (randomAbortController) randomAbortController.abort();
  randomAbortController = new AbortController();
  try {
    const item = await apiWithTimeout("/api/random", { signal: randomAbortController.signal });
    if (requestSeq !== randomRequestSeq) return;
    if (!isPreviewResponse(item)) throw new Error("随机文章响应格式无效");
    randomAbortController = null;
    openPreview(item.doc_id);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError" || requestSeq !== randomRequestSeq) return;
    showToast("随机文章加载失败");
  }
}

// BEGIN STRICT COMPARISON DIFF
/** @param {string} leftText @param {string} rightText @returns {{left: string, right: string}} */
function diffHTML(leftText, rightText) {
  const left = splitDiffLines(leftText);
  const right = splitDiffLines(rightText);
  const maxLines = 1200;
  if (left.length > maxLines || right.length > maxLines) {
    return { left: escapeHTML(leftText), right: escapeHTML(rightText) };
  }
  const dp = Array.from({ length: left.length + 1 }, () => new Array(right.length + 1).fill(0));
  for (let i = left.length - 1; i >= 0; i -= 1) {
    for (let j = right.length - 1; j >= 0; j -= 1) {
      dp[i][j] = left[i] === right[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const outLeft = [];
  const outRight = [];
  let i = 0;
  let j = 0;
  while (i < left.length || j < right.length) {
    if (i < left.length && j < right.length && left[i] === right[j]) {
      const line = `${escapeHTML(left[i])}\n`;
      outLeft.push(line);
      outRight.push(line);
      i += 1;
      j += 1;
    } else if (j >= right.length || (i < left.length && dp[i + 1][j] >= dp[i][j + 1])) {
      outLeft.push(`<span class="diff-line diff-del">${escapeHTML(left[i])}</span>\n`);
      outRight.push(`<span class="diff-line diff-gap"></span>\n`);
      i += 1;
    } else {
      outLeft.push(`<span class="diff-line diff-gap"></span>\n`);
      outRight.push(`<span class="diff-line diff-add">${escapeHTML(right[j])}</span>\n`);
      j += 1;
    }
  }
  return { left: outLeft.join(""), right: outRight.join("") };
}

/** @param {unknown} text @returns {string[]} */
function splitDiffLines(text) {
  return String(text).split(/\n+/).map((line) => line.trim()).filter(Boolean);
}
// END STRICT COMPARISON DIFF

// BEGIN STRICT PREVIEW METADATA
/** @param {{title?: string | null}} item @returns {string} */
function previewTitleHTML(item) {
  return `<div class="preview-title">${escapeHTML(item.title)}</div>`;
}

/** @param {{publication_name?: string | null, authors: string[], tag_names: string[]}} item @returns {string} */
function previewMetaHTML(item) {
  const authors = item.authors && item.authors.length ? ` / ${escapeHTML(item.authors.join(", "))}` : "";
  const tagNames = item.tag_names || [];
  const pageSize = 8;
  const totalPages = Math.ceil(tagNames.length / pageSize);
  const tagItems = tagNames.map((tag, index) => {
    const page = Math.floor(index / pageSize) + 1;
    const hasNextOnPage = index + 1 < tagNames.length && Math.floor((index + 1) / pageSize) + 1 === page;
    return `<span data-preview-tag-page="${page}" ${page > 1 ? "hidden" : ""}>${escapeHTML(tag)}${hasNextOnPage ? '<span class="path-sep"> / </span>' : ""}</span>`;
  }).join("");
  const pager = totalPages > 1 ? `<div class="preview-tag-pager"><button type="button" class="text-btn-sm" data-preview-tag-page-change="prev" disabled>上一页</button><span><span data-preview-tag-current>1</span> / ${totalPages}</span><button type="button" class="text-btn-sm" data-preview-tag-page-change="next">下一页</button></div>` : "";
  const tags = tagNames.length ? `<div class="preview-tags" data-preview-tags data-total-pages="${totalPages}"><div class="preview-path preview-tag-list">${tagItems}</div>${pager}</div>` : "";
  return `<div class="preview-meta-row"><div class="preview-path preview-source-author">${escapeHTML(item.publication_name)}${authors}</div>${tags}</div>`;
}
// END STRICT PREVIEW METADATA

// BEGIN STRICT PREVIEW TAG PAGER
/** @param {HTMLElement} button */
function changePreviewTagPage(button) {
  const container = button.closest("[data-preview-tags]");
  if (!(container instanceof HTMLElement)) return;
  const totalPages = parseInt(container.dataset.totalPages || "1", 10);
  const currentNode = container.querySelector("[data-preview-tag-current]");
  const current = parseInt((currentNode && currentNode.textContent) || "1", 10);
  const next = Math.min(totalPages, Math.max(1, current + (button.dataset.previewTagPageChange === "next" ? 1 : -1)));
  container.querySelectorAll("[data-preview-tag-page]").forEach((tag) => {
    if (tag instanceof HTMLElement) tag.hidden = parseInt(tag.dataset.previewTagPage || "0", 10) !== next;
  });
  if (currentNode) currentNode.textContent = String(next);
  const prev = container.querySelector('[data-preview-tag-page-change="prev"]');
  const nextButton = container.querySelector('[data-preview-tag-page-change="next"]');
  if (prev instanceof HTMLButtonElement) prev.disabled = next <= 1;
  if (nextButton instanceof HTMLButtonElement) nextButton.disabled = next >= totalPages;
}
// END STRICT PREVIEW TAG PAGER

/** @param {ValidatedPreview} item */
function compareSelectHTML(item) {
  const proofread = item.article && Array.isArray(item.article.parts)
    ? `<option value="__proofread__">${item.source_files && item.source_files.length ? "对比原文件并校对" : "校对"}</option>`
    : "";
  if ((!item.variants || !item.variants.length) && !proofread) return "";
  const options = item.variants.map((variant) => `<option value="${escapeHTML(variant.doc_id)}">${escapeHTML(variant.publication_name)}</option>`).join("");
  return `<label class="preview-compare-control" title="选择对比版本"><span>对比版本</span><select data-compare-select aria-label="选择对比版本"><option value="">对比版本</option>${proofread}${options}</select></label>`;
}

// BEGIN STRICT CREATE TEXT DELTA
/** @typedef {{Diff_Timeout: number, diff_main: (original: string, edited: string, checkLines: boolean) => [number, string][]}} DiffEngine */
/** @typedef {{new (): DiffEngine}} DiffConstructor */
/** @param {string} original @param {string} edited @returns {string} */
function textDelta(original, edited) {
  if (original === edited) return "";
  const Diff = /** @type {Window & {diff_match_patch?: DiffConstructor}} */ (window).diff_match_patch;
  if (typeof Diff === "function") {
    const differ = new Diff();
    differ.Diff_Timeout = 1;
    return differ.diff_main(original, edited, true).map(([operation, text]) => {
      if (operation === 0) return `=${text.length}`;
      if (operation === -1) return `-${text.length}`;
      return `+${text.replace(/%/g, "%25").replace(/\t/g, "%09")}`;
    }).join("\t");
  }
  let prefix = 0;
  while (prefix < original.length && prefix < edited.length && original[prefix] === edited[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < original.length - prefix && suffix < edited.length - prefix && original[original.length - 1 - suffix] === edited[edited.length - 1 - suffix]) suffix += 1;
  const removedLength = original.length - prefix - suffix;
  const inserted = edited.slice(prefix, edited.length - suffix);
  return [
    prefix ? `=${prefix}` : "",
    removedLength ? `-${removedLength}` : "",
    inserted ? `+${inserted.replace(/%/g, "%25").replace(/\t/g, "%09")}` : "",
    suffix ? `=${suffix}` : "",
  ].filter(Boolean).join("\t");
}
// END STRICT CREATE TEXT DELTA

/** @returns {Promise<void>} */
function loadDiffMatchPatch() {
  if (typeof /** @type {Window & {diff_match_patch?: DiffConstructor}} */ (window).diff_match_patch === "function") return Promise.resolve();
  if (diffMatchPatchPromise) return diffMatchPatchPromise;
  diffMatchPatchPromise = /** @type {Promise<void>} */ (new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = DIFF_MATCH_PATCH_URL;
    script.integrity = DIFF_MATCH_PATCH_INTEGRITY;
    script.crossOrigin = "anonymous";
    script.referrerPolicy = "no-referrer";
    script.onload = () => resolve(undefined);
    script.onerror = () => reject(new Error("差异库加载失败"));
    document.head.appendChild(script);
  })).catch((error) => {
    diffMatchPatchPromise = null;
    console.warn("校订差异库加载失败", error);
  });
  return diffMatchPatchPromise;
}

// BEGIN STRICT APPLY TEXT DELTA
/** @param {unknown} original @param {unknown} delta @returns {string} */
function applyTextDelta(original, delta) {
  if (!delta) return String(original);
  const source = String(original);
  let cursor = 0;
  let output = "";
  for (const token of String(delta).split("\t")) {
    if (!token) continue;
    const operation = token[0];
    const value = token.slice(1);
    if (operation === "=") {
      const size = parseInt(value, 10) || 0;
      output += source.slice(cursor, cursor + size);
      cursor += size;
    } else if (operation === "-") {
      cursor += parseInt(value, 10) || 0;
    } else if (operation === "+") {
      try { output += decodeURIComponent(value.replace(/\+/g, "%2B")); } catch (_error) { /* validated by the server */ }
    }
  }
  if (cursor !== source.length) throw new Error("delta length mismatch");
  return output;
}
// END STRICT APPLY TEXT DELTA

// BEGIN STRICT COMMENT PIVOTS
/** @typedef {{text?: unknown, type?: unknown}} ProofreadPart */
/** @typedef {{part_idx: number | string, offset: number | string, index: number | string}} CommentPivot */
/** @param {Record<string, unknown>} value @returns {{parts: ProofreadPart[], comment_pivots: CommentPivot[]} | null} */
function proofreadArticleParts(value) {
  if (!Array.isArray(value.parts) || !value.parts.every(isJsonObject)) return null;
  if (value.comment_pivots != null && (!Array.isArray(value.comment_pivots) || !value.comment_pivots.every((pivot) => isJsonObject(pivot) &&
      (typeof pivot.part_idx === "number" || typeof pivot.part_idx === "string") &&
      (typeof pivot.offset === "number" || typeof pivot.offset === "string") &&
      (typeof pivot.index === "number" || typeof pivot.index === "string")))) return null;
  return { parts: value.parts, comment_pivots: value.comment_pivots || [] };
}
/** @param {{parts: ProofreadPart[], comment_pivots?: CommentPivot[]}} article @param {number} partIndex @returns {string} */
function partTextWithPivots(article, partIndex) {
  const chars = Array.from(String(article.parts[partIndex].text || ""));
  (article.comment_pivots || [])
    .filter((pivot) => Number(pivot.part_idx) === partIndex)
    .sort((a, b) => Number(b.offset) - Number(a.offset))
    .forEach((pivot) => chars.splice(Number(pivot.offset), 0, `〔${pivot.index}〕`));
  return chars.join("");
}
// END STRICT COMMENT PIVOTS

/** @param {string} kind @param {unknown[]} values @param {{key: string, label: string, type?: string, options?: string[]}[]} fields @returns {string} */
function listEditorHTML(kind, values, fields) {
  const rows = values.map((value) => {
    const inputs = fields.map((field) => {
      const current = isJsonObject(value) ? (value[field.key] == null ? "" : value[field.key]) : value;
      if (field.options) return `<select data-list-field="${field.key}" aria-label="${field.label}"><option value="">${field.label}</option>${field.options.map((option) => `<option value="${escapeHTML(option)}" ${current === option ? "selected" : ""}>${escapeHTML(option)}</option>`).join("")}</select>`;
      return `<input data-list-field="${field.key}" type="${field.type || "text"}" placeholder="${field.label}" value="${escapeHTML(current)}">`;
    }).join("");
    return `<div class="proofread-list-row" data-list-row>${inputs}<button type="button" class="icon-btn-sm" data-remove-list aria-label="删除条目">×</button></div>`;
  }).join("");
  return `<div class="proofread-list" data-list="${kind}">${rows}<button type="button" class="text-btn-sm" data-add-list="${kind}">添加</button></div>`;
}

/** @param {ValidatedPreview} item @returns {string} */
function proofreadHTML(item) {
  const article = item.article || {};
  const sourceFiles = item.source_files || [];
  const requestedPage = Math.max(1, Number(article.page_start) || 1);
  const defaultFile = STATE.proofreadFile !== "" ? Math.max(0, Math.min(sourceFiles.length - 1, Number(STATE.proofreadFile) || 0)) : 0;
  const selectedUrl = sourceFiles[defaultFile] && sourceFiles[defaultFile].url || "";
  const page = Math.max(1, Number(STATE.proofreadPage) || requestedPage);
  const sourcePicker = sourceFiles.length > 1 ? `<select data-proofread-source-file aria-label="选择原文件">${sourceFiles.map((file) => `<option value="${file.index}" ${Number(file.index) === defaultFile ? "selected" : ""}>${escapeHTML(file.name)}</option>`).join("")}</select>` : "";
   const selectedFile = sourceFiles[defaultFile];
  const sourceURL = sourceDownloadURL(item, defaultFile, true);
  const sourceDownload = sourceDownloadURL(item, defaultFile);
   const sourceViewer = sourceFiles.length ? (isImageSource(selectedUrl)
     ? `<div class="proofread-image-viewer" data-image-viewer><div class="proofread-image-tools"><button type="button" class="text-btn-sm" data-image-zoom="out">缩小</button><button type="button" class="text-btn-sm" data-image-zoom="reset">原始大小</button><button type="button" class="text-btn-sm" data-image-zoom="in">放大</button><a class="text-btn-sm proofread-image-download" href="${sourceDownload}">下载原文件</a></div><div class="proofread-image-stage"><img data-proofread-source-image src="${sourceURL}" alt="原文件" loading="eager" decoding="async" fetchpriority="high" data-image-scale="1"></div></div>`
     : (isPDFSource(selectedUrl) && selectedFile && Array.isArray(selectedFile.pages) && selectedFile.pages.length
       ? pageImageViewerHTML(selectedFile, page, sourceDownload)
     : (isPDFSource(selectedUrl)
       ? `<div class="proofread-document-viewer" data-pdf-viewer data-pdf-url="${escapeHTML(sourceURL)}" data-pdf-download="${escapeHTML(sourceDownload)}" data-pdf-page="${page}"><div class="proofread-pdf-tools"><button type="button" class="text-btn-sm" data-pdf-action="prev">上一页</button><button type="button" class="text-btn-sm" data-pdf-action="next">下一页</button><button type="button" class="text-btn-sm" data-pdf-action="zoom-out">缩小</button><button type="button" class="text-btn-sm" data-pdf-action="reset">原始大小</button><button type="button" class="text-btn-sm" data-pdf-action="zoom-in">放大</button><label>页码 <input data-pdf-page type="number" min="1" value="${page}"></label><span data-pdf-status>正在加载 PDF...</span><a class="text-btn-sm proofread-document-download" data-pdf-download-link href="${sourceDownload}">下载原文件</a></div><div class="proofread-pdf-stage is-loading"><canvas data-pdf-canvas></canvas></div></div>`
       : `<div class="proofread-no-source"><strong>此原文件暂不支持在线预览</strong><a class="text-btn-sm" href="${sourceDownload}">下载原文件</a></div>`)))
    : '<div class="proofread-no-source"><strong>没有原文件</strong><span>仍可编辑正文并提交校对。</span></div>';
  const partTypeOptions = (/** @type {unknown} */ current) => PART_TYPES.map((type) => `<option value="${type}" ${type === current ? "selected" : ""}>${type}</option>`).join("");
  const typedArticle = proofreadArticleParts(article);
  const articleParts = typedArticle?.parts || [];
  const parts = articleParts.map((part, index) => `<div class="proofread-part-group" data-proofread-part-group="${index}"><div data-part-insert-before></div><label class="proofread-field proofread-part" data-proofread-part="${index}"><span>段落 ${index + 1} / <select data-proofread-part-type="${index}">${partTypeOptions(isJsonObject(part) ? part.type || "paragraph" : "paragraph")}</select></span><span class="proofread-part-actions"><button type="button" class="text-btn-sm" data-add-part-before="${index}">在上新建段落</button><button type="button" class="text-btn-sm" data-add-part-after="${index}">在下新建段落</button><button type="button" class="text-btn-sm" data-delete-part="${index}">删除当前段落</button></span><textarea data-proofread-part-input="${index}">${escapeHTML(typedArticle ? partTextWithPivots(typedArticle, index) : "")}</textarea></label><div data-part-insert-after></div></div>`).join("");
  const comments = (Array.isArray(article.comments) ? article.comments : []).map((comment, index) => `<label class="proofread-field"><span>注释 ${index + 1}</span><textarea data-proofread-comment="${index}">${escapeHTML(comment)}</textarea></label>`).join("");
  const tags = Array.isArray(article.tags) ? article.tags : [];
  const metadata = `<fieldset><legend>文章元数据</legend><label class="proofread-field"><span>标题</span><input data-meta-title value="${escapeHTML(article.title || item.title || "")}"></label><label class="proofread-field"><span>作者</span>${listEditorHTML("authors", Array.isArray(article.authors) ? article.authors : item.authors, [{ key: "value", label: "作者" }])}</label><label class="proofread-field"><span>日期</span>${listEditorHTML("dates", Array.isArray(article.dates) ? article.dates : [], [{ key: "year", label: "年", type: "number" }, { key: "month", label: "月", type: "number" }, { key: "day", label: "日", type: "number" }])}</label><label class="proofread-field"><span>标签</span>${listEditorHTML("tags", tags, [{ key: "name", label: "名称" }, { key: "type", label: "类型", options: TAG_TYPES }])}</label></fieldset>`;
  const sourceMetadata = `<fieldset><legend>来源元数据</legend><label class="proofread-field"><span>来源名称</span><input data-source-name value="${escapeHTML(item.publication_name || "")}"></label><label class="proofread-field"><span>来源作者</span><input data-source-author value="${escapeHTML(item.publication_author || "")}"></label><label class="proofread-field"><span>来源类型</span><input data-source-type value="${escapeHTML(item.publication_type || "")}"></label><label class="proofread-field"><span>来源文件</span>${listEditorHTML("files", sourceFiles.map((file) => file.url), [{ key: "value", label: "URL" }])}</label></fieldset>`;
   const mobileModeLabel = STATE.isMobile ? "切换到电脑模式" : "切换到手机模式";
   const actions = `<div class="preview-tools proofread-tools">${previewFullscreenButtonHTML()}<button type="button" class="icon-btn-sm" data-close-preview aria-label="关闭预览" title="关闭预览">×</button><button type="button" class="icon-btn-sm proofread-mode-btn" data-preview-mobile aria-label="${mobileModeLabel}" title="${mobileModeLabel}"><span class="ui-icon ${STATE.isMobile ? "ui-icon-phone" : "ui-icon-desktop"}" aria-hidden="true"></span></button><button type="button" class="icon-btn-sm preview-theme-btn" data-preview-theme aria-label="切换到白天模式" title="白天模式">${THEME_ICON[STATE.isDark ? "dark" : "light"]}</button></div>`;
  const fulltextToggle = `<button type="button" class="text-btn-sm" data-fulltext-toggle>收起全文编辑</button>`;
  const partToggle = `<button type="button" class="text-btn-sm" data-part-editor-toggle>段落编辑</button>`;
  const fulltextBox = `<div data-fulltext-box><textarea data-fulltext-input>${escapeHTML(typedArticle ? articleParts.map((_part, index) => partTextWithPivots(typedArticle, index)).join("\n") : "")}</textarea><button type="button" class="text-btn-sm" data-fulltext-apply>应用到段落</button></div>`;
  const fulltextSection = `<div class="proofread-fulltext-section" data-fulltext-section><div class="proofread-form-actions">${fulltextToggle}</div>${fulltextBox}</div>`;
  const bodyFieldset = `<fieldset><legend>正文</legend><div class="proofread-form-actions">${STATE.isMobile ? partToggle : fulltextToggle + partToggle}</div>${STATE.isMobile ? "" : fulltextBox}<div data-part-editor hidden>${parts}${comments}</div><label class="proofread-field"><span>正文说明</span><textarea data-proofread-description>${escapeHTML(article.description || "")}</textarea></label></fieldset>`;
  const noteField = `<label class="proofread-field"><span>校订备注</span><textarea data-proofread-note></textarea></label>`;
  return `<div class="preview-header"><div class="preview-heading">${previewTitleHTML(item)}<div class="preview-compare-note"><span>对比原文件并校对</span><button type="button" class="text-btn-sm proofread-back-btn" data-proofread-back>返回预览</button></div></div><div class="preview-actions">${actions}</div></div><div class="proofread-grid"><section class="proofread-source"><div class="proofread-pane-header"><span>原文件</span>${sourcePicker}</div>${sourceViewer}</section><form class="proofread-editor" data-proofread-form><div class="proofread-pane-header"><span>校订内容</span><button type="submit" class="result-action-btn primary">提交校订</button></div>${STATE.isMobile ? fulltextSection : ""}${metadata}${sourceMetadata}${bodyFieldset}${noteField}</form></div>`;
}

/** @param {ValidatedPreview | null} item @param {number | null} [focusPart] @param {boolean} [restoring] */
function openProofread(item, focusPart = null, restoring = false) {
  if (!item) return;
  stopPreviewAnimation();
  cancelPreviewRequests();
  loadDiffMatchPatch();
  flushProofreadDraft();
  STATE.proofread = true;
  STATE.proofreadItem = item;
  STATE.compare = "";
  if (!restoring) syncUrl(true);
  clearActiveMediaViewer();
  DOM.previewPanel.classList.add("proofread-mode");
  speechPreviewClosed();
  DOM.previewPanel.innerHTML = proofreadHTML(item);
  labelPreviewPanel();
  if (!(item.source_files || []).length) {
    const sourcePane = DOM.previewPanel.querySelector(".proofread-source");
    if (sourcePane) sourcePane.remove();
    const grid = DOM.previewPanel.querySelector(".proofread-grid");
    if (grid) grid.classList.add("proofread-grid-no-source");
  }
  const sourceImage = DOM.previewPanel.querySelector("[data-proofread-source-image]");
  if (sourceImage instanceof HTMLImageElement) {
    const fitImage = () => {
      const stage = sourceImage.closest(".proofread-image-stage");
      const currentSource = sourceImage.currentSrc || sourceImage.src;
      if (!stage || !sourceImage.naturalWidth || sourceImage.dataset.imageLoadedSrc !== currentSource) return;
      sourceImage.hidden = false;
      sourceImage.style.removeProperty("display");
      const error = stage.querySelector("[data-image-error]");
      if (error) error.remove();
      const availableWidth = Math.max(1, stage.clientWidth - 32);
      const fitScale = Math.min(availableWidth / sourceImage.naturalWidth, 1);
      const baseWidth = Math.max(1, Math.round(sourceImage.naturalWidth * fitScale));
      sourceImage.dataset.imageBaseWidth = String(baseWidth);
      sourceImage.style.maxWidth = "none";
      sourceImage.style.width = `${baseWidth}px`;
      sourceImage.style.height = "auto";
    };
    const imageLoaded = () => {
      sourceImage.dataset.imageLoadedSrc = sourceImage.currentSrc || sourceImage.src;
      fitImage();
      const pageViewer = sourceImage.closest("[data-page-image-viewer]");
      if (pageViewer instanceof HTMLElement) prefetchAdjacentPageImage(pageViewer);
    };
    sourceImage.addEventListener("load", imageLoaded);
    if (sourceImage.complete && sourceImage.naturalWidth) imageLoaded();
    if (typeof ResizeObserver !== "undefined") {
      let lastStageWidth = 0;
      let lastStageHeight = 0;
      activeImageResizeObserver = new ResizeObserver(() => {
        cancelAnimationFrame(activeImageFitFrame);
        activeImageFitFrame = requestAnimationFrame(() => {
          const stage = sourceImage.closest(".proofread-image-stage");
          const stageWidth = stage ? stage.clientWidth : 0;
          const stageHeight = stage ? stage.clientHeight : 0;
          if (Math.abs(stageWidth - lastStageWidth) > 2 || Math.abs(stageHeight - lastStageHeight) > 2) {
            lastStageWidth = stageWidth;
            lastStageHeight = stageHeight;
            fitImage();
          }
          const scale = Number(sourceImage.dataset.imageScale || 1);
          const baseWidth = Number(sourceImage.dataset.imageBaseWidth) || sourceImage.naturalWidth;
          sourceImage.style.width = `${Math.max(1, baseWidth * scale)}px`;
        });
      });
      const stage = sourceImage.closest(".proofread-image-stage");
      if (stage) activeImageResizeObserver.observe(stage);
    }
    sourceImage.addEventListener("error", () => {
      const stage = sourceImage.closest("[data-image-stage], .proofread-image-stage");
      delete sourceImage.dataset.imageLoadedSrc;
      sourceImage.hidden = true;
      sourceImage.style.display = "none";
      if (stage && !stage.querySelector("[data-image-error]")) {
        stage.insertAdjacentHTML("beforeend", `<div class="proofread-no-source" data-image-error><strong>图片无法在线显示</strong><a class="text-btn-sm" href="${sourceDownloadURL(item, STATE.proofreadFile || 0)}">下载原文件</a></div>`);
      }
    });
  }
  const form = DOM.previewPanel.querySelector("[data-proofread-form]");
  // Establish protection before setup or restore can trigger form handlers.
  protectExistingDraft(form, proofreadDraftKey(item.doc_id), item.content_hash);
  const pdfViewer = DOM.previewPanel.querySelector("[data-pdf-viewer]");
  if (pdfViewer instanceof HTMLElement) renderPDFViewer(pdfViewer);
  if (form) {
    form.classList.add("proofread-structure-closed");
    const partEditor = form.querySelector("[data-part-editor]");
    if (partEditor instanceof HTMLElement) partEditor.hidden = false;
    form.querySelectorAll("[data-proofread-part-type]").forEach((select) => {
      if (select instanceof HTMLSelectElement) select.insertAdjacentHTML("afterend", `<span class="proofread-part-type-label">${escapeHTML(select.value)}</span>`);
    });
    const structureActions = form.querySelector("fieldset .proofread-form-actions");
    const descriptionInput = form.querySelector("[data-proofread-description]");
    const descriptionField = descriptionInput ? descriptionInput.closest(".proofread-field") : null;
    const descriptionLabel = descriptionField && descriptionField.querySelector("span");
    if (structureActions && descriptionLabel) {
      descriptionLabel.classList.add("proofread-description-heading");
      descriptionLabel.append(structureActions);
      const bodyFieldset = descriptionField.closest("fieldset");
      const bodyLegend = bodyFieldset && bodyFieldset.querySelector("legend");
      if (bodyFieldset) bodyFieldset.insertBefore(descriptionField, bodyLegend ? bodyLegend.nextSibling : bodyFieldset.firstChild);
    }
  }
  const sourceHeader = DOM.previewPanel.querySelector(".proofread-source .proofread-pane-header");
  if (sourceHeader && !sourceHeader.querySelector("[data-proofread-page]") && !DOM.previewPanel.querySelector("[data-page-image-viewer], [data-pdf-viewer]")) {
    sourceHeader.insertAdjacentHTML("beforeend", `<label class="proofread-page-control">页码 <input data-proofread-page type="number" min="1" value="${Math.max(1, Number(STATE.proofreadPage) || Number((item.article || {}).page_start) || 1)}"></label>`);
  }
  if (form && !form.querySelector("[data-fulltext-box]")) {
    const article = proofreadArticleParts(item.article);
    const text = article ? article.parts.map((_part, index) => partTextWithPivots(article, index)).join("\n") : "";
    form.insertAdjacentHTML("afterbegin", `<div data-fulltext-box><textarea data-fulltext-input>${escapeHTML(text)}</textarea><button type="button" class="text-btn-sm" data-fulltext-apply>应用到段落</button></div>`);
    const header = form.querySelector(".proofread-pane-header");
    if (header) header.insertAdjacentHTML("afterbegin", `<button type="button" class="text-btn-sm" data-fulltext-toggle>收起全文编辑</button>`);
  }
  restoreProofreadDraft(item, form);
  syncFulltextFromParts(form);
  if (!STATE.proofreadFile) { const sourceInput = DOM.previewPanel.querySelector("[data-proofread-source-file]"); STATE.proofreadFile = sourceInput instanceof HTMLSelectElement ? sourceInput.value : "0"; }
  if (!STATE.proofreadPage) { const pageInput = form && form.querySelector("[data-proofread-page]"); STATE.proofreadPage = pageInput instanceof HTMLInputElement ? pageInput.value : String(Math.max(1, Number(item.article && item.article.page_start) || 1)); }
  if (!restoring) syncUrl(true);
  if (focusPart != null) requestAnimationFrame(() => {
    const input = DOM.previewPanel.querySelector(`[data-proofread-part-input="${focusPart}"]`);
    if (input instanceof HTMLElement) input.focus();
  });
}

/** @param {Element} form @param {string} kind @returns {(string | Record<string, string | number>)[]} */
function readList(form, kind) {
  return [...form.querySelectorAll(`[data-list="${kind}"] [data-list-row]`)].map((row) => {
    /** @type {Record<string, string | number>} */ const values = {};
    row.querySelectorAll("[data-list-field]").forEach((input) => {
      if (!(input instanceof HTMLInputElement || input instanceof HTMLSelectElement)) return;
      const key = input.dataset.listField;
      if (key && input.value !== "") values[key] = input.type === "number" ? Number(input.value) : input.value.trim();
    });
    return kind === "authors" || kind === "files" ? String(values.value || "") : values;
  }).filter((value) => typeof value === "string" ? Boolean(value) : Object.keys(value).length);
}

/** @param {string} docId @returns {string} */
function proofreadDraftKey(docId) {
  return `bha-proofread-draft:${docId}`;
}

/** @param {string} key @returns {string | null} */
function safeSessionGet(key) {
  try { return sessionStorage.getItem(key); } catch (_error) {
    showToast("浏览器无法读取草稿，请勿关闭当前标签页并尽快复制保存内容。");
    return null;
  }
}

/** @param {string} key @returns {string | null | undefined} */
function sessionGetForSubmission(key) {
  try { return sessionStorage.getItem(key); } catch (_error) {
    showToast("浏览器无法读取草稿，请勿关闭当前标签页并尽快复制保存内容。");
    return undefined;
  }
}

/** @param {string} key @param {string} value @returns {boolean} */
function safeSessionSet(key, value) {
  try { sessionStorage.setItem(key, value); return true; } catch (_error) {
    showToast("浏览器无法保存草稿，请勿关闭当前标签页并尽快复制保存内容。");
    return false;
  }
}

/** @param {Element | null} form */
function collectProofreadDraft(form) {
  if (!form || draftRecoveryBlocked(form)) return null;
  const lists = {authors: readList(form, "authors"), dates: readList(form, "dates"),
    tags: readList(form, "tags"), files: readList(form, "files")};
  const fieldValue = (/** @type {Element | null} */ scope, /** @type {string} */ selector) => {
    const input = scope?.querySelector(selector);
    return input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement || input instanceof HTMLSelectElement ? input.value : "";
  };
  const indexedValues = (/** @type {string} */ selector, /** @type {string} */ key) => Object.fromEntries(
    [...form.querySelectorAll(selector)].flatMap((input) => {
      if (!(input instanceof HTMLElement)) return [];
      const index = input.dataset[key];
      if (index === undefined || !(input instanceof HTMLTextAreaElement || input instanceof HTMLSelectElement)) return [];
      return [[index, input.value]];
    }));
  return {
    version: 2,
    content_hash: STATE.proofreadItem && validDraftBaseline(STATE.proofreadItem.content_hash) ? STATE.proofreadItem.content_hash : null,
    parts: indexedValues("[data-proofread-part-input]", "proofreadPartInput"),
    comments: indexedValues("[data-proofread-comment]", "proofreadComment"),
    title: fieldValue(form, "[data-meta-title]"),
    source: Object.fromEntries(["name", "author", "type"].map((key) => [key, fieldValue(form, `[data-source-${key}]`)])),
    lists,
    note: fieldValue(form, "[data-proofread-note]"),
    description: fieldValue(form, "[data-proofread-description]"),
    // Whole-text edits are separate from paragraph inputs until the user
    // explicitly applies them. Preserve them without inventing a patch.
    pendingFulltext: form.querySelector("[data-fulltext-input]") instanceof HTMLTextAreaElement &&
      form.querySelector("[data-fulltext-input]")?.getAttribute("data-fulltext-dirty") === "1"
      ? fieldValue(form, "[data-fulltext-input]") : null,
    page: fieldValue(DOM.previewPanel, "[data-proofread-page], [data-pdf-page], [data-page-image-input]"),
    newParts: [...form.querySelectorAll("[data-proofread-new-part]")].map((row) => ({
      text: fieldValue(row, "textarea"),
      type: fieldValue(row, "[data-new-part-type]") || "paragraph",
      group: row.closest("[data-proofread-part-group]")?.getAttribute("data-proofread-part-group") || "",
      before: Boolean(row.parentElement && row.parentElement.hasAttribute("data-part-insert-before")),
    })),
    deleted: Object.fromEntries([...form.querySelectorAll("[data-delete-part]")].flatMap((button) =>
      button instanceof HTMLElement && button.dataset.proofreadDeleted === "true" && button.dataset.deletePart !== undefined ? [[button.dataset.deletePart, true]] : [])),
    types: indexedValues("[data-proofread-part-type]", "proofreadPartType"),
  };
}

function flushProofreadDraft() {
  clearTimeout(proofreadDraftTimer);
  proofreadDraftTimer = 0;
  if (!STATE.preview) return;
  const form = DOM.previewPanel.querySelector("[data-proofread-form]");
  protectExistingDraft(form, proofreadDraftKey(STATE.preview), STATE.proofreadItem?.content_hash);
  const draft = collectProofreadDraft(form);
  if (draft) {
    const raw = JSON.stringify(draft);
    if (safeSessionSet(proofreadDraftKey(STATE.preview), raw) && form instanceof HTMLElement) form.dataset.draftLastSaved = raw;
  }
}

function saveProofreadDraft() {
  clearTimeout(proofreadDraftTimer);
  proofreadDraftTimer = setTimeout(flushProofreadDraft, 400);
}

/** @param {ValidatedPreview} item @param {Element | null} form */
function restoreProofreadDraft(item, form) {
  if (!form) return;
  const raw = safeSessionGet(proofreadDraftKey(item.doc_id));
  if (raw === null) { if (form instanceof HTMLElement) form.dataset.draftRecoveryChecked = "1"; return; }
  if (form instanceof HTMLElement && form.dataset.draftRecoveryBlocked === "1") return;
  /** @type {unknown} */ let draft;
  try { draft = JSON.parse(raw); } catch (_error) { blockDraftRecovery(form); offerDraftCopy(form, proofreadDraftKey(item.doc_id)); return; }
  if (!isJsonObject(draft) || draft.version !== 2 || !validDraftBaseline(item.content_hash) || draft.content_hash !== item.content_hash) {
    blockDraftRecovery(form);
    offerDraftCopy(form, proofreadDraftKey(item.doc_id));
    return;
  }
  if (form instanceof HTMLElement && form.dataset.draftLastSaved !== undefined && form.dataset.draftLastSaved !== raw) {
    blockDraftRecovery(form);
    offerDraftCopy(form, proofreadDraftKey(item.doc_id));
    offerCurrentFormCopy(form, proofreadDraftKey(item.doc_id));
    return;
  }
  if (!isJsonObject(draft.parts) || !isJsonObject(draft.comments) || !isJsonObject(draft.source) ||
      !isJsonObject(draft.lists) || !isJsonObject(draft.types) || !isJsonObject(draft.deleted) ||
      typeof draft.title !== "string" || typeof draft.note !== "string" || typeof draft.description !== "string" ||
      (draft.pendingFulltext !== undefined && draft.pendingFulltext !== null && typeof draft.pendingFulltext !== "string") ||
      !Array.isArray(draft.newParts) || !draft.newParts.every(isJsonObject)) {
    blockDraftRecovery(form);
    offerDraftCopy(form, proofreadDraftKey(item.doc_id));
    return;
  }
  const parts = draft.parts;
  const comments = draft.comments;
  const source = draft.source;
  const lists = draft.lists;
  const types = draft.types;
  const deleted = draft.deleted;
  const renderedArticle = proofreadArticleParts(item.article);
  if (!renderedArticle) { blockDraftRecovery(form); offerDraftCopy(form, proofreadDraftKey(item.doc_id)); return; }
  if (!(form.querySelector("[data-meta-title]") instanceof HTMLInputElement) ||
      !(form.querySelector("[data-proofread-note]") instanceof HTMLTextAreaElement) ||
      !(form.querySelector("[data-proofread-description]") instanceof HTMLTextAreaElement) ||
      ["name", "author", "type"].some((key) => !(form.querySelector(`[data-source-${key}]`) instanceof HTMLInputElement ||
                                                  form.querySelector(`[data-source-${key}]`) instanceof HTMLSelectElement)) ||
      ["authors", "dates", "tags", "files"].some((key) => !form.querySelector(`[data-list="${key}"]`))) {
    blockDraftRecovery(form);
    offerDraftCopy(form, proofreadDraftKey(item.doc_id));
    return;
  }
  const expectedParts = Array.from(form.querySelectorAll("[data-proofread-part-input]")).flatMap((node) =>
    node instanceof HTMLTextAreaElement && node.dataset.proofreadPartInput !== undefined ? [node.dataset.proofreadPartInput] : []);
  const expectedComments = Array.from(form.querySelectorAll("[data-proofread-comment]")).flatMap((node) =>
    node instanceof HTMLTextAreaElement && node.dataset.proofreadComment !== undefined ? [node.dataset.proofreadComment] : []);
  const expectedTypes = Array.from(form.querySelectorAll("[data-proofread-part-type]")).flatMap((node) =>
    node instanceof HTMLSelectElement && node.dataset.proofreadPartType !== undefined ? [node.dataset.proofreadPartType] : []);
  const scalarKeys = ["name", "author", "type"];
  const listKeys = ["authors", "dates", "tags", "files"];
  const listValuesValid = (/** @type {string} */ key) => {
    const values = lists[key];
    if (!Array.isArray(values)) return false;
    if (key === "authors" || key === "files") return values.every((value) => typeof value === "string");
    return values.every((value) => isJsonObject(value) && Object.values(value).every((entry) => typeof entry === "string" || typeof entry === "number"));
  };
  const articleComments = Array.isArray(item.article.comments) ? item.article.comments : [];
  if (expectedParts.length !== renderedArticle.parts.length || expectedComments.length !== articleComments.length ||
      expectedTypes.length !== expectedParts.length || expectedParts.some((key) => !expectedTypes.includes(key)) ||
      scalarKeys.some((key) => typeof source[key] !== "string") || listKeys.some((key) => !listValuesValid(key)) ||
      Object.keys(parts).length !== expectedParts.length || expectedParts.some((key) => typeof parts[key] !== "string") ||
      Object.keys(comments).length !== expectedComments.length || expectedComments.some((key) => typeof comments[key] !== "string") ||
      Object.keys(types).length !== expectedParts.length || expectedParts.some((key) => typeof types[key] !== "string") ||
      Object.keys(deleted).some((key) => !expectedParts.includes(key) || deleted[key] !== true) ||
      draft.newParts.some((part) => typeof part.group !== "string" || !expectedParts.includes(part.group) ||
        typeof part.text !== "string" || typeof part.type !== "string" || typeof part.before !== "boolean")) {
    blockDraftRecovery(form);
    offerDraftCopy(form, proofreadDraftKey(item.doc_id));
    return;
  }
  // Restoring list controls can dispatch click handlers synchronously. Avoid
  // overwriting the original draft if restoration fails partway through.
  if (form instanceof HTMLElement) form.dataset.draftRecoveryRestoring = "1";
  try {
  for (const [index, value] of Object.entries(parts)) {
    const input = form.querySelector(`[data-proofread-part-input="${CSS.escape(index)}"]`);
    if (input instanceof HTMLTextAreaElement && typeof value === "string") input.value = value;
  }
  for (const [index, value] of Object.entries(comments)) {
    const input = form.querySelector(`[data-proofread-comment="${CSS.escape(index)}"]`);
    if (input instanceof HTMLTextAreaElement && typeof value === "string") input.value = value;
  }
  const title = form.querySelector("[data-meta-title]");
  if (title instanceof HTMLInputElement) title.value = typeof draft.title === "string" ? draft.title : "";
  for (const key of ["name", "author", "type"]) {
    const input = form.querySelector(`[data-source-${key}]`);
    if ((input instanceof HTMLInputElement || input instanceof HTMLSelectElement) && typeof source[key] === "string") input.value = source[key];
  }
  for (const kind of ["authors", "dates", "tags", "files"]) {
    const values = lists[kind];
    const holder = form.querySelector(`[data-list="${kind}"]`);
    if (!holder || !Array.isArray(values)) continue;
    while (holder.querySelectorAll("[data-list-row]").length < values.length) {
      const count = holder.querySelectorAll("[data-list-row]").length;
      const add = holder.querySelector(`[data-add-list="${kind}"]`);
      if (add instanceof HTMLElement) add.click(); else break;
      if (holder.querySelectorAll("[data-list-row]").length <= count) break;
    }
    while (holder.querySelectorAll("[data-list-row]").length > values.length) {
      const rows = holder.querySelectorAll("[data-list-row]");
      const remove = rows[rows.length - 1].querySelector("[data-remove-list]");
      if (remove instanceof HTMLElement) remove.click(); else break;
      if (holder.querySelectorAll("[data-list-row]").length >= rows.length) break;
    }
    if (holder.querySelectorAll("[data-list-row]").length !== values.length) throw new Error("draft list rows unavailable");
    [...holder.querySelectorAll("[data-list-row]")].forEach((row, index) => {
      const value = values[index];
      row.querySelectorAll("[data-list-field]").forEach((input) => {
        if (!(input instanceof HTMLInputElement || input instanceof HTMLSelectElement)) return;
        const key = input.dataset.listField;
        const next = typeof value === "string" ? value : isJsonObject(value) && key ? value[key] : "";
        input.value = typeof next === "string" || typeof next === "number" ? String(next) : "";
      });
    });
  }
  const note = form.querySelector("[data-proofread-note]");
  if (note instanceof HTMLTextAreaElement) note.value = typeof draft.note === "string" ? draft.note : "";
  const description = form.querySelector("[data-proofread-description]");
  if (description instanceof HTMLTextAreaElement) description.value = typeof draft.description === "string" ? draft.description : "";
  const draftPageInput = DOM.previewPanel.querySelector("[data-proofread-page], [data-pdf-page], [data-page-image-input]");
  if ((typeof draft.page === "string" || typeof draft.page === "number") && draft.page && draftPageInput instanceof HTMLInputElement) draftPageInput.value = String(draft.page);
  for (const [index, type] of Object.entries(types)) {
    const input = form.querySelector(`[data-proofread-part-type="${CSS.escape(index)}"]`);
    if (input instanceof HTMLSelectElement && typeof type === "string") {
      input.value = type;
      if (input.value !== type) throw new Error("draft part type unavailable");
    }
  }
  for (const index of Object.keys(deleted)) {
    const button = form.querySelector(`[data-delete-part="${CSS.escape(index)}"]`);
    if (button instanceof HTMLElement) { button.dataset.proofreadDeleted = "true"; button.textContent = "恢复"; const group = button.closest("[data-proofread-part-group]"); if (group) group.classList.add("proofread-deleted"); else throw new Error("draft deleted part unavailable"); }
    else throw new Error("draft deleted part control unavailable");
  }
  for (const part of Array.isArray(draft.newParts) ? draft.newParts : []) {
    if (!isJsonObject(part) || typeof part.group !== "string") continue;
    const target = form.querySelector(`[data-proofread-part-group="${CSS.escape(part.group)}"] [${part.before ? "data-part-insert-before" : "data-part-insert-after"}]`);
    if (target) {
      target.insertAdjacentHTML("beforeend", newPartHTML());
      const row = target.lastElementChild;
      const input = row?.querySelector("textarea");
      const type = row?.querySelector("[data-new-part-type]");
      if (input instanceof HTMLTextAreaElement) input.value = typeof part.text === "string" ? part.text : "";
      if (type instanceof HTMLSelectElement) {
        type.value = typeof part.type === "string" ? part.type : "paragraph";
        if (type.value !== part.type) throw new Error("draft insertion type unavailable");
      }
    } else { throw new Error("draft insertion target unavailable"); }
  }
  // Existing v2 drafts created before pendingFulltext support cannot prove
  // whether unsaved whole-text input existed. Keep the restored paragraph
  // draft, but make the unprovable history visible without inventing content.
  if (draft.pendingFulltext === undefined) {
    if (!form.querySelector("[data-draft-fulltext-history-notice]")) {
      const notice = document.createElement("div");
      notice.dataset.draftFulltextHistoryNotice = "1";
      notice.setAttribute("role", "status");
      notice.textContent = "此草稿来自旧版本；如曾编辑全文但未点击“应用到段落”，旧版可能未保存该部分，请先自行核对。";
      form.prepend(notice);
    }
  }
  if (typeof draft.pendingFulltext === "string") {
    const input = form.querySelector("[data-fulltext-input]");
    if (!(input instanceof HTMLTextAreaElement)) throw new Error("draft full text control unavailable");
    input.value = draft.pendingFulltext;
    input.dataset.fulltextDirty = "1";
  }
  if (form instanceof HTMLElement) { form.dataset.draftLastSaved = raw; form.dataset.draftRecoveryChecked = "1"; }
  } catch (_error) {
    blockDraftRecovery(form);
    offerDraftCopy(form, proofreadDraftKey(item.doc_id));
  } finally {
    if (form instanceof HTMLElement) delete form.dataset.draftRecoveryRestoring;
  }
}

function newPartHTML() {
  return `<label class="proofread-field proofread-new-part" data-proofread-new-part><span>新增段落</span><select data-new-part-type>${PART_TYPES.map((type) => `<option value="${type}" ${type === "paragraph" ? "selected" : ""}>${type}</option>`).join("")}</select><textarea></textarea><button type="button" class="text-btn-sm" data-remove-new-part>删除</button></label>`;
}

/** @param {Element} form @returns {string} */
function fullTextFromParts(form) {
  /** @type {string[]} */ const lines = [];
  form.querySelectorAll("[data-proofread-part-group]").forEach((group) => {
    if (group.classList.contains("proofread-deleted")) return;
    group.querySelectorAll("[data-part-insert-before] > [data-proofread-new-part]").forEach((row) => {
      const textarea = row.querySelector("textarea");
      lines.push(textarea instanceof HTMLTextAreaElement ? textarea.value : "");
    });
    const partInput = group.querySelector("[data-proofread-part-input]");
    if (partInput instanceof HTMLTextAreaElement) lines.push(partInput.value);
    group.querySelectorAll("[data-part-insert-after] > [data-proofread-new-part]").forEach((row) => {
      const textarea = row.querySelector("textarea");
      lines.push(textarea instanceof HTMLTextAreaElement ? textarea.value : "");
    });
  });
  return lines.join("\n");
}

/** @param {Element | null} form */
function syncFulltextFromParts(form) {
  const input = form && form.querySelector("[data-fulltext-input]");
  if (!form || !(input instanceof HTMLTextAreaElement) || input.dataset.fulltextDirty === "1") return;
  input.value = fullTextFromParts(form);
}

/** @param {Element} form @returns {Element[]} */
function editablePartRows(form) {
  /** @type {Element[]} */ const rows = [];
  form.querySelectorAll("[data-proofread-part-group]").forEach((group) => {
    if (group.classList.contains("proofread-deleted")) return;
    group.querySelectorAll("[data-part-insert-before] > [data-proofread-new-part]").forEach((row) => rows.push(row));
    const input = group.querySelector("[data-proofread-part-input]");
    if (input) rows.push(input);
    group.querySelectorAll("[data-part-insert-after] > [data-proofread-new-part]").forEach((row) => rows.push(row));
  });
  return rows;
}

/** @param {Element} row @returns {string} */
function editablePartRowText(row) {
  const input = row.matches("[data-proofread-new-part]") ? row.querySelector("textarea") : row;
  return input instanceof HTMLTextAreaElement ? input.value : "";
}

/** @param {Element} row @returns {string} */
function editablePartRowType(row) {
  const group = row.closest("[data-proofread-part-group]");
  const input = row.matches("[data-proofread-new-part]")
    ? row.querySelector("[data-new-part-type]")
    : group && group.querySelector("[data-proofread-part-type]");
  return input instanceof HTMLSelectElement ? input.value : "paragraph";
}

/** @param {Element} row */
function removeEditablePartRow(row) {
  if (row.matches("[data-proofread-new-part]")) {
    row.remove();
    return;
  }
  const group = row.closest("[data-proofread-part-group]");
  const button = group && group.querySelector("[data-delete-part]");
  if (button instanceof HTMLElement && group) {
    button.dataset.proofreadDeleted = "true";
    button.textContent = "恢复";
    group.classList.add("proofread-deleted");
  }
}

/** @param {Element} form @param {Element | null} reference @param {Element | null} previous
 * @param {string} text @param {string} type @returns {Element | null} */
function insertEditablePartRow(form, reference, previous, text, type) {
  let row;
  if (previous && previous.matches("[data-proofread-new-part]")) {
    previous.insertAdjacentHTML("afterend", newPartHTML());
    row = previous.nextElementSibling;
  } else if (previous) {
    const target = previous.closest("[data-proofread-part-group]")?.querySelector("[data-part-insert-after]");
    if (!target) return null;
    target.insertAdjacentHTML("beforeend", newPartHTML());
    row = target.lastElementChild;
  } else if (reference && reference.matches("[data-proofread-new-part]")) {
    reference.insertAdjacentHTML("beforebegin", newPartHTML());
    row = reference.previousElementSibling;
  } else if (reference) {
    const target = reference.closest("[data-proofread-part-group]")?.querySelector("[data-part-insert-before]");
    if (!target) return null;
    target.insertAdjacentHTML("beforeend", newPartHTML());
    row = target.lastElementChild;
  } else {
    const active = editablePartRows(form);
    const tail = active[active.length - 1];
    if (!tail) return null;
    if (tail.matches("[data-proofread-new-part]")) {
      tail.insertAdjacentHTML("afterend", newPartHTML());
      row = tail.nextElementSibling;
    } else {
      const target = tail.closest("[data-proofread-part-group]")?.querySelector("[data-part-insert-after]");
      if (!target) return null;
      target.insertAdjacentHTML("beforeend", newPartHTML());
      row = target.lastElementChild;
    }
  }
  if (!row) return null;
  const input = row.querySelector("textarea");
  const select = row.querySelector("[data-new-part-type]");
  if (!(input instanceof HTMLTextAreaElement) || !(select instanceof HTMLSelectElement)) return null;
  input.value = text;
  select.value = type || "paragraph";
  return row;
}

// BEGIN STRICT PROOFREAD LINE ANCHORS
/** @param {string[]} current @param {string[]} desired @returns {[number, number][]} */
function matchingLineAnchors(current, desired) {
  const table = Array.from({ length: current.length + 1 }, () => new Uint32Array(desired.length + 1));
  for (let left = current.length - 1; left >= 0; left -= 1) {
    for (let right = desired.length - 1; right >= 0; right -= 1) {
      table[left][right] = current[left] === desired[right]
        ? table[left + 1][right + 1] + 1
        : Math.max(table[left + 1][right], table[left][right + 1]);
    }
  }
  /** @type {[number, number][]} */ const anchors = [];
  let left = 0;
  let right = 0;
  while (left < current.length && right < desired.length) {
    if (current[left] === desired[right]) {
      anchors.push([left, right]);
      left += 1;
      right += 1;
    } else if (table[left + 1][right] >= table[left][right + 1]) {
      left += 1;
    } else {
      right += 1;
    }
  }
  return anchors;
}
// END STRICT PROOFREAD LINE ANCHORS

/** @param {Element | null} form */
function applyFullText(form) {
  if (!form) return;
  const input = form.querySelector("[data-fulltext-input]");
  if (!(input instanceof HTMLTextAreaElement)) return;
  const lines = input.value.split("\n");
  const rows = editablePartRows(form);
  if (!rows.length) return;
  const current = rows.map(editablePartRowText);
  const anchors = [...matchingLineAnchors(current, lines), [rows.length, lines.length]];
  let oldStart = 0;
  let newStart = 0;
  for (const [oldEnd, newEnd] of anchors) {
    const paired = Math.min(oldEnd - oldStart, newEnd - newStart);
    for (let offset = 0; offset < paired; offset += 1) {
      const row = rows[oldStart + offset];
      const target = row.matches("[data-proofread-new-part]") ? row.querySelector("textarea") : row;
      if (target instanceof HTMLTextAreaElement) target.value = lines[newStart + offset];
    }
    for (let index = oldStart + paired; index < oldEnd; index += 1) removeEditablePartRow(rows[index]);
    const reference = oldEnd < rows.length ? rows[oldEnd] : null;
    const inheritedType = paired
      ? editablePartRowType(rows[oldStart + paired - 1])
      : (oldStart > 0 ? editablePartRowType(rows[oldStart - 1]) : (reference ? editablePartRowType(reference) : "paragraph"));
    let previous = paired ? rows[oldStart + paired - 1] : (oldStart > 0 ? rows[oldStart - 1] : null);
    for (let index = newStart + paired; index < newEnd; index += 1) {
      previous = insertEditablePartRow(form, reference, previous, lines[index], inheritedType);
    }
    oldStart = oldEnd + 1;
    newStart = newEnd + 1;
  }
  saveProofreadDraft();
  delete input.dataset.fulltextDirty;
}

// BEGIN STRICT PROOFREAD COMPARISON
/** @param {unknown} value @returns {unknown} */
function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    const source = /** @type {Record<string, unknown>} */ (value);
    /** @type {Record<string, unknown>} */ const out = {};
    for (const key of Object.keys(source).sort()) {
      const next = canonicalize(source[key]);
      if (next === "" || next == null) continue;
      out[key] = next;
    }
    return out;
  }
  return value;
}

/** @param {unknown} left @param {unknown} right @returns {boolean} */
function deepSame(left, right) {
  return JSON.stringify(canonicalize(left)) === JSON.stringify(canonicalize(right));
}
// END STRICT PROOFREAD COMPARISON

/** @param {ValidatedPreview} item @param {Element} form @returns {ProofreadPayload} */
function proofreadPayload(item, form) {
  const article = item.article;
  const typedArticle = proofreadArticleParts(article);
  if (!typedArticle) throw new Error("文章段落无效，请重新打开校对页");
  const comments = Array.isArray(article.comments) ? article.comments : [];
  /** @type {ProofreadPatch} */ const patch = { version: 2, parts: {}, comments: {}, description: "" };
  const valueOf = (/** @type {Element} */ scope, /** @type {string} */ selector) => {
    const field = scope.querySelector(selector);
    if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement || field instanceof HTMLSelectElement)) {
      throw new Error(`校对字段缺失：${selector}`);
    }
    return field.value;
  };
  const insertions = (/** @type {Element | null} */ group, /** @type {string} */ selector) => {
    const holder = group?.querySelector(selector);
    if (!holder) throw new Error("段落编辑控件缺失");
    return [...holder.querySelectorAll("[data-proofread-new-part]")].map((row) => ({
      text: valueOf(row, "textarea"), type: valueOf(row, "[data-new-part-type]") || "paragraph",
    }));
  };
  form.querySelectorAll("[data-proofread-part-input]").forEach((input) => {
    if (!(input instanceof HTMLTextAreaElement) || !/^\d+$/.test(input.dataset.proofreadPartInput || "")) throw new Error("段落索引无效");
    const index = Number(input.dataset.proofreadPartInput);
    if (!Number.isSafeInteger(index) || index >= typedArticle.parts.length) throw new Error("段落索引超出范围");
    const part = typedArticle.parts[index];
    /** @type {ProofreadChange} */ const change = {};
    const group = input.closest("[data-proofread-part-group]");
    const before = insertions(group, "[data-part-insert-before]");
    const after = insertions(group, "[data-part-insert-after]");
    const deleted = group && group.querySelector("[data-proofread-deleted]");
    if (deleted instanceof HTMLElement && deleted.dataset.proofreadDeleted === "true") {
      /** @type {ProofreadChange} */ const deletion = { delete: true };
      if (before.length) deletion.insertBefore = before;
      if (after.length) deletion.insertAfter = after;
      patch.parts[index] = deletion;
      return;
    }
    const delta = textDelta(partTextWithPivots(typedArticle, index), input.value);
    if (delta) change.diff = delta;
    const typeInput = form.querySelector(`[data-proofread-part-type="${index}"]`);
    const type = typeInput instanceof HTMLSelectElement ? typeInput.value : "";
    if (type && type !== (part.type || "paragraph")) change.type = type;
    if (before.length) change.insertBefore = before;
    if (after.length) change.insertAfter = after;
    if (Object.keys(change).length) patch.parts[index] = change;
  });
  form.querySelectorAll("[data-proofread-comment]").forEach((input) => {
    if (!(input instanceof HTMLTextAreaElement) || !/^\d+$/.test(input.dataset.proofreadComment || "")) throw new Error("注释索引无效");
    const index = Number(input.dataset.proofreadComment);
    if (!Number.isSafeInteger(index) || index >= comments.length) throw new Error("注释索引超出范围");
    const delta = textDelta(String(comments[index] || ""), input.value);
    if (delta) patch.comments[index + 1] = { diff: delta };
  });
  patch.description = textDelta(String(article.description || ""), valueOf(form, "[data-proofread-description]"));
  const articleMeta = {
    title: valueOf(form, "[data-meta-title]").trim(),
    authors: readList(form, "authors"), dates: readList(form, "dates"), tags: readList(form, "tags"),
  };
  const sourceMeta = {
    name: valueOf(form, "[data-source-name]").trim(),
    author: valueOf(form, "[data-source-author]").trim(),
    type: valueOf(form, "[data-source-type]").trim(), files: readList(form, "files"),
  };
  const changedPatch = Object.keys(patch.parts).length || Object.keys(patch.comments).length || patch.description;
  const changedArticle = !deepSame(articleMeta, { title: article.title || item.title || "", authors: article.authors || item.authors || [], dates: article.dates || [], tags: article.tags || [] });
  const changedSource = !deepSame(sourceMeta, { name: item.publication_name || "", author: item.publication_author || "", type: item.publication_type || "", files: (item.source_files || []).map((file) => file.url) });
  return {
    doc_id: item.doc_id,
    base_content_hash: typeof item.content_hash === "string" ? item.content_hash : null,
    patch: changedPatch ? patch : null,
    metadata: changedArticle || changedSource ? { ...(changedArticle ? { article: articleMeta } : {}), ...(changedSource ? { source: sourceMeta } : {}) } : null,
    description: valueOf(form, "[data-proofread-note]").trim(),
  };
}

/** @param {Element | null} form */
async function submitProofreadForm(form) {
  if (!form) return;
  if (form instanceof HTMLElement && form.dataset.submissionUnconfirmed === "1") {
    showToast("上次提交结果未确认，请保留草稿并联系管理员核查，勿重复提交。");
    return;
  }
  if (draftRecoveryBlocked(form)) {
    showToast("草稿基线无法确认，请保留草稿并联系管理员核对。");
    return;
  }
  let item = STATE.proofreadItem || getCachedPreview(STATE.preview);
  if (!item) {
    try {
      const docId = STATE.preview;
      item = parsePreviewResponse(await apiWithTimeout(`/api/preview/${encodeURIComponent(docId)}`, {}, 30000), docId);
      if (STATE.preview !== docId) return;
    } catch (_error) {
      item = null;
    }
  }
  if (!item) {
    showToast("校订内容已过期，请重新打开校对页");
    return;
  }
  if (!validDraftBaseline(item.content_hash)) {
    showToast("预览基线无法确认，请保留草稿并重新打开校对页。");
    return;
  }
  const fulltext = form.querySelector("[data-fulltext-input]");
  if (fulltext instanceof HTMLTextAreaElement && fulltext.dataset.fulltextDirty === "1") {
    flushProofreadDraft();
    showToast("全文编辑尚未应用到段落，请先应用或复制保存草稿。");
    return;
  }
  let payload;
  try { payload = proofreadPayload(item, form); }
  catch (error) {
    showToast(error instanceof Error ? error.message : "校对表单无效");
    return;
  }
  flushProofreadDraft();
  if (!payload.patch && !payload.metadata) {
    showToast("没有需要提交的修改");
    return;
  }
  showProofreadConfirm(item, payload);
}

function removeProofreadConfirm() {
  const panel = document.querySelector("[data-proofread-confirm]");
  if (panel) panel.remove();
}

/** @param {ValidatedPreview} item @param {ProofreadPayload} payload */
function showProofreadConfirm(item, payload) {
  const confirmForm = DOM.previewPanel.querySelector("[data-proofread-form]");
  let confirmDraft = null;
  try { const value = confirmForm ? collectProofreadDraft(confirmForm) : null; confirmDraft = value ? JSON.stringify(value) : null; } catch (_error) { /* fail closed on confirmation */ }
  removeProofreadConfirm();
  const holder = document.createElement("div");
  holder.className = "proofread-confirm";
  holder.dataset.proofreadConfirm = "true";
  holder.innerHTML = `<div class="proofread-confirm-panel" role="dialog" aria-modal="true" aria-label="校订提交预览"><div class="proofread-confirm-header"><span>校订提交预览</span><button type="button" class="icon-btn-sm" data-cancel-proofread aria-label="关闭预览">×</button></div><div class="proofread-confirm-body">${proofreadReviewHTML(item, payload)}</div><div class="proofread-confirm-actions"><button type="button" class="text-btn-sm" data-cancel-proofread>返回继续编辑</button><button type="button" class="result-action-btn primary" data-confirm-proofread>确认提交</button></div></div>`;
  document.body.append(holder);
  holder.querySelectorAll("[data-cancel-proofread]").forEach((button) => {
    button.addEventListener("click", removeProofreadConfirm);
  });
  const confirmButton = holder.querySelector("[data-confirm-proofread]");
  if (!(confirmButton instanceof HTMLButtonElement)) return;
  confirmButton.addEventListener("click", async () => {
    let currentDraft;
    try { const value = confirmForm ? collectProofreadDraft(confirmForm) : null; currentDraft = value ? JSON.stringify(value) : null; } catch (_error) { currentDraft = null; }
    if (confirmDraft == null || currentDraft == null || !confirmForm || !confirmForm.isConnected || STATE.proofreadItem !== item || currentDraft !== confirmDraft || draftRecoveryBlocked(confirmForm)) {
      flushProofreadDraft();
      showToast("表单已变化，请返回检查后重新预览，草稿已保留。");
      return;
    }
    confirmButton.disabled = true;
    confirmButton.textContent = "提交中...";
    const submitted = await postProofread(payload);
    if (confirmForm instanceof HTMLElement && confirmForm.dataset.submissionUnconfirmed === "1") {
      confirmButton.disabled = true;
      confirmButton.textContent = "结果未确认，请勿重复提交";
    } else {
      confirmButton.disabled = false;
      confirmButton.textContent = "确认提交";
    }
    if (submitted) removeProofreadConfirm();
  });
}

/** @param {string} field @param {unknown} value @returns {string} */
function metadataItemText(field, value) {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (!isJsonObject(value)) return JSON.stringify(value) || "";
  if (field === "dates") {
    const parts = [];
    for (const [key, suffix] of [["year", "年"], ["month", "月"], ["day", "日"]]) {
      if (value[key] != null) parts.push(`${value[key]}${suffix}`);
    }
    return parts.join("") || JSON.stringify(value);
  }
  if (field === "tags") {
    const name = value.name || "";
    return value.type ? `${name}（${value.type}）` : String(name);
  }
  return JSON.stringify(value);
}

/** @param {string} field @param {unknown} value @returns {string} */
function metadataCellHTML(field, value) {
  if (["authors", "dates", "tags", "files"].includes(field)) {
    const items = Array.isArray(value) ? value : [];
    if (!items.length) return '<span class="proofread-metadata-empty">（空）</span>';
    return items.map((item) => `<div>${escapeHTML(metadataItemText(field, item))}</div>`).join("");
  }
  const text = value == null || value === "" ? "" : String(value);
  return text ? escapeHTML(text) : '<span class="proofread-metadata-empty">（空）</span>';
}

/** @param {ValidatedPreview} item @param {ProofreadPayload} payload @returns {string} */
function proofreadReviewHTML(item, payload) {
  const article = item.article;
  const typedArticle = proofreadArticleParts(article);
  if (!typedArticle) throw new Error("文章段落无效，请重新打开校对页");
  const comments = Array.isArray(article.comments) ? article.comments : [];
  const rows = [];
  /** @type {ProofreadPatch} */ const patch = payload.patch || { version: 2, parts: {}, comments: {}, description: "" };
  for (const [rawIndex, change] of Object.entries(patch.parts || {})) {
    const index = Number(rawIndex);
    if (!Number.isSafeInteger(index) || index < 0 || index >= typedArticle.parts.length) continue;
    const original = partTextWithPivots(typedArticle, index);
    const pieces = [];
    if (change.delete) pieces.push(`<div class="proofread-confirm-field"><span class="diff-line diff-del">${escapeHTML(original)}</span></div>`);
    if (change.diff) {
      const edited = applyTextDelta(original, change.diff);
      const diff = diffHTML(original, edited);
      pieces.push(`<div class="proofread-confirm-diff"><div class="diff-pane">${diff.left}</div><div class="diff-pane">${diff.right}</div></div>`);
    }
    for (const key of /** @type {const} */ (["insertBefore", "insertAfter"])) {
      for (const part of Array.isArray(change[key]) ? change[key] : []) {
        pieces.push(`<div class="proofread-confirm-newpart"><span class="proofread-confirm-parttype">${escapeHTML(part.type)}</span><span>${escapeHTML(part.text)}</span></div>`);
      }
    }
    if (change.type) {
      const previous = typedArticle.parts[index].type || "paragraph";
      pieces.push(`<div class="proofread-confirm-field">段落类型：${escapeHTML(previous)} → ${escapeHTML(change.type)}</div>`);
    }
    if (pieces.length) rows.push(`<div class="proofread-confirm-item"><strong>段落 ${index + 1}</strong>${pieces.join("")}</div>`);
  }
  for (const [rawIndex, change] of Object.entries(patch.comments || {})) {
    const index = Number(rawIndex) - 1;
    if (!Number.isSafeInteger(index) || index < 0 || index >= comments.length) continue;
    const original = String(comments[index] || "");
    const pieces = [];
    if (change.delete) pieces.push(`<div class="proofread-confirm-field"><span class="diff-line diff-del">${escapeHTML(original)}</span></div>`);
    if (change.diff) {
      const edited = applyTextDelta(original, change.diff);
      const diff = diffHTML(original, edited);
      pieces.push(`<div class="proofread-confirm-diff"><div class="diff-pane">${diff.left}</div><div class="diff-pane">${diff.right}</div></div>`);
    }
    for (const key of /** @type {const} */ (["insertBefore", "insertAfter"])) {
      for (const comment of Array.isArray(change[key]) ? change[key] : []) {
        pieces.push(`<div class="proofread-confirm-newpart"><span class="proofread-confirm-parttype">注释</span><span>${escapeHTML(comment.text || "")}</span></div>`);
      }
    }
    if (pieces.length) rows.push(`<div class="proofread-confirm-item"><strong>注释 ${index + 1}</strong>${pieces.join("")}</div>`);
  }
  if (patch.description) {
    const original = String(article.description || "");
    const edited = applyTextDelta(original, patch.description);
    const diff = diffHTML(original, edited);
    rows.push(`<div class="proofread-confirm-item"><strong>正文说明</strong><div class="proofread-confirm-diff"><div class="diff-pane">${diff.left}</div><div class="diff-pane">${diff.right}</div></div></div>`);
  }
  /** @type {Record<string, string>} */ const fieldLabels = {
    title: "标题", authors: "作者", dates: "日期", tags: "标签",
    name: "来源名称", author: "来源作者", type: "来源类型", files: "来源文件",
  };
  const metadata = payload.metadata || {};
  /** @type {Record<string, unknown>} */ const articleOld = { title: article.title || item.title || "", authors: article.authors || item.authors || [], dates: article.dates || [], tags: article.tags || [] };
  /** @type {Record<string, unknown>} */ const sourceOld = { name: item.publication_name || "", author: item.publication_author || "", type: item.publication_type || "", files: item.source_files.map((file) => file.url) };
  const metadataRows = [];
  for (const [group, values] of /** @type {const} */ ([["文章", metadata.article], ["来源", metadata.source]])) {
    if (!values) continue;
    const oldValues = group === "文章" ? articleOld : sourceOld;
    for (const [key, value] of Object.entries(values)) {
      if (deepSame(value, oldValues[key])) continue;
      metadataRows.push(`<tr><td>${escapeHTML(group)}${escapeHTML(fieldLabels[key] || key)}</td><td>${metadataCellHTML(key, oldValues[key])}</td><td>${metadataCellHTML(key, value)}</td></tr>`);
    }
  }
  if (metadataRows.length) {
    rows.push(`<div class="proofread-confirm-item"><strong>元数据对照</strong><table class="proofread-metadata-table"><thead><tr><th>字段</th><th>原元数据</th><th>修改后元数据</th></tr></thead><tbody>${metadataRows.join("")}</tbody></table></div>`);
  }
  if (payload.description) {
    rows.push(`<div class="proofread-confirm-item"><strong>校订备注</strong><div class="proofread-confirm-field">${escapeHTML(payload.description)}</div></div>`);
  }
  if (!rows.length) return '<div class="proofread-confirm-empty">没有可显示的变更</div>';
  return rows.join("");
}

/** @param {ProofreadPayload} payload @returns {Promise<boolean>} */
async function postProofread(payload) {
  const originatingForm = DOM.previewPanel.querySelector("[data-proofread-form]");
  const originatingItem = STATE.proofreadItem;
  flushProofreadDraft();
  if (draftRecoveryBlocked(originatingForm) || !validDraftBaseline(payload.base_content_hash)) {
    showToast("草稿或预览基线无法确认，请保留草稿并联系管理员核对。");
    return false;
  }
  const fulltext = originatingForm?.querySelector("[data-fulltext-input]");
  if (fulltext instanceof HTMLTextAreaElement && fulltext.dataset.fulltextDirty === "1") {
    showToast("全文编辑尚未应用到段落，请先应用或复制保存草稿。");
    return false;
  }
  const priorDraft = sessionGetForSubmission(proofreadDraftKey(payload.doc_id));
  if (priorDraft === undefined) return false;
  let initialFormDraft = null;
  try { initialFormDraft = originatingForm ? JSON.stringify(collectProofreadDraft(originatingForm)) : null; }
  catch (_error) { showToast("无法核对当前草稿，请保留并勿重复提交。"); return false; }
  if (originatingForm instanceof HTMLElement && originatingForm.dataset.submissionUnconfirmed === "1") {
    showToast("上次提交结果未确认，请保留草稿并联系管理员核查，勿重复提交。");
    return false;
  }
  if (originatingForm instanceof HTMLElement) originatingForm.dataset.submissionUnconfirmed = "1";
  try {
    await apiWithTimeout("/api/proofread", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }, 30000);
    let liveDraftChanged = false;
    if (initialFormDraft !== null && originatingForm && originatingForm.isConnected && STATE.proofreadItem === originatingItem && STATE.preview === payload.doc_id) {
      let currentFormDraft;
      try { currentFormDraft = JSON.stringify(collectProofreadDraft(originatingForm)); } catch (_error) { currentFormDraft = null; }
      if (currentFormDraft !== initialFormDraft) { liveDraftChanged = true; flushProofreadDraft(); }
    }
    if (liveDraftChanged || sessionGetForSubmission(proofreadDraftKey(payload.doc_id)) !== priorDraft) {
      if (originatingForm && originatingForm.isConnected && STATE.proofreadItem === originatingItem && STATE.preview === payload.doc_id) {
        blockDraftRecovery(originatingForm);
        offerDraftCopy(originatingForm, proofreadDraftKey(payload.doc_id));
        offerCurrentFormCopy(originatingForm, proofreadDraftKey(payload.doc_id));
      }
      if (liveDraftChanged) showToast("提交结果已返回，等待期间的表单修改需另行保存；请勿重复提交并联系管理员核查。");
      else showToast("提交结果已返回，但草稿在等待期间发生变化；请勿重复提交并联系管理员核查。");
      return false;
    }
    showToast("请求响应已收到，但尚无可靠处理回执；请保留草稿并联系管理员核查，勿重复提交。");
    return false;
  } catch (error) {
    if (initialFormDraft !== null && originatingForm && originatingForm.isConnected && STATE.proofreadItem === originatingItem && STATE.preview === payload.doc_id) {
      let currentFormDraft;
      try { currentFormDraft = JSON.stringify(collectProofreadDraft(originatingForm)); } catch (_error) { currentFormDraft = null; }
      if (currentFormDraft !== initialFormDraft) {
        flushProofreadDraft();
        blockDraftRecovery(originatingForm);
        offerDraftCopy(originatingForm, proofreadDraftKey(payload.doc_id));
        offerCurrentFormCopy(originatingForm, proofreadDraftKey(payload.doc_id));
      }
    }
    showToast(`校订提交结果未确认，请保留草稿并先核查，勿重复提交：${error instanceof Error && error.message ? error.message : "未知错误"}`);
    return false;
  }
}

/** @param {string} docId @param {boolean} [restoring] */
async function comparePreview(docId, restoring = false) {
  if (!STATE.preview || !docId) return;
  stopPreviewAnimation();
  const baseDocId = STATE.preview;
  STATE.compare = docId;
  STATE.proofread = false;
  if (!restoring) syncUrl(true);
  const requestSeq = compareRequestSeq + 1;
  compareRequestSeq = requestSeq;
  if (compareAbortController) compareAbortController.abort();
  const controller = new AbortController();
  compareAbortController = controller;
  const isCurrent = () => requestSeq === compareRequestSeq && STATE.preview === baseDocId
    && STATE.compare === docId && !STATE.proofread && !STATE.parseMode;
  try {
    const currentCached = getCachedPreview(baseDocId);
    const targetCached = getCachedPreview(docId);
    const [currentRaw, targetRaw] = await Promise.all([
      currentCached || apiWithTimeout(`/api/preview/${encodeURIComponent(baseDocId)}`, { signal: controller.signal }),
      targetCached || apiWithTimeout(`/api/preview/${encodeURIComponent(docId)}`, { signal: controller.signal }),
    ]);
    if (!isCurrent()) return;
    const current = parsePreviewResponse(currentRaw, baseDocId);
    const target = parsePreviewResponse(targetRaw, docId);
    if (!currentCached) setCachedPreview(baseDocId, current);
    if (!targetCached) setCachedPreview(docId, target);
    clearActiveMediaViewer();
    DOM.previewPanel.classList.remove("proofread-mode");
    speechPreviewClosed();
    DOM.previewPanel.innerHTML = previewHTML(current, target);
    labelPreviewPanel();
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError" || !isCurrent()) return;
    showToast("对比加载失败");
  } finally {
    if (compareAbortController === controller) compareAbortController = null;
  }
}

function resetFilters() {
  STATE.sources = [];
  STATE.authors = [];
  STATE.tags = [];
  STATE.excludeSources = [];
  STATE.excludeAuthors = [];
  STATE.excludeTags = [];
  STATE.archiveId = "";
  STATE.publicationType = "";
  clearDateFilter();
  STATE.page = 1;
  renderFilterOptions({ preserveScroll: true });
  applyStateToDom();
  doSearch();
}

function attachEvents() {
  DOM.searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    STATE.parseMode = false;
    STATE.q = DOM.searchInput.value.trim();
    STATE.page = 1;
    doSearch();
  });
  DOM.parseBtn.addEventListener("click", () => {
    flushParseDraft();
    STATE.parseMode = !STATE.parseMode;
    if (STATE.parseMode) {
      STATE.leftOpen = !STATE.isMobile;
      STATE.sidebarExpanded = false;
    }
    STATE.parsePage = 1;
    STATE.parseItem = null;
    STATE.preview = "";
    clearPreviewDom();
    applyStateToDom();
    syncUrl(false);
    if (STATE.parseMode) loadParseQueue(); else doSearch();
  });
  DOM.searchInput.addEventListener("input", scheduleSearch);
  DOM.searchInput.addEventListener("compositionstart", () => {
    searchComposing = true;
  });
  DOM.searchInput.addEventListener("compositionend", () => {
    searchComposing = false;
    scheduleSearch();
  });
  DOM.leftToggle.addEventListener("click", () => {
    STATE.leftOpen = !STATE.leftOpen;
    if (!STATE.leftOpen) STATE.sidebarExpanded = false;
    applyStateToDom();
    syncUrl();
    if (!STATE.leftOpen) DOM.leftToggle.focus({ preventScroll: true });
  });
  DOM.overlay.addEventListener("click", () => {
    STATE.leftOpen = false;
    STATE.sidebarExpanded = false;
    applyStateToDom();
    syncUrl();
  });
  DOM.themeBtn.addEventListener("click", toggleTheme);
   DOM.mobileToggleBtn.addEventListener("click", toggleMobileMode);
  DOM.sidebarExpand.addEventListener("click", () => {
    STATE.sidebarExpanded = !STATE.sidebarExpanded;
    applyStateToDom();
    syncUrl();
  });
  DOM.exactToggle.addEventListener("change", () => {
    STATE.exact = DOM.exactToggle.checked;
    STATE.page = 1;
    doSearch();
  });
  DOM.previewHighlightToggle.addEventListener("change", () => {
    STATE.previewHighlight = DOM.previewHighlightToggle.checked;
    syncUrl();
    if (STATE.preview) restorePreview(false);
  });
  DOM.fulltextToggle.addEventListener("change", () => {
    STATE.fulltext = DOM.fulltextToggle.checked;
    STATE.page = 1;
    doSearch();
  });
  DOM.pageSize.addEventListener("change", () => {
    if (STATE.parseMode) {
      STATE.parsePageSize = parseInt(DOM.pageSize.value, 10);
      STATE.parsePage = 1;
      syncUrl();
      loadParseQueue();
      return;
    }
    STATE.pageSize = parseInt(DOM.pageSize.value, 10);
    STATE.page = 1;
    doSearch();
  });
  DOM.sortFilter.addEventListener("change", () => { STATE.sort = DOM.sortFilter.value; STATE.page = 1; doSearch(); });
  DOM.sourceSearch.addEventListener("input", () => { STATE.filterSearch.source = DOM.sourceSearch.value; STATE.filterPage.source = 1; scheduleFacetSearch("source"); });
  DOM.authorSearch.addEventListener("input", () => { STATE.filterSearch.author = DOM.authorSearch.value; STATE.filterPage.author = 1; scheduleFacetSearch("author"); });
  DOM.tagSearch.addEventListener("input", () => { STATE.filterSearch.tag = DOM.tagSearch.value; STATE.filterPage.tag = 1; scheduleFacetSearch("tag"); });
  for (const [kind, filter] of /** @type {const} */ ([["source", DOM.sourceFilter], ["author", DOM.authorFilter], ["tag", DOM.tagFilter]])) {
    filter.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const pageButton = target.closest(`[data-${kind}-page]`);
      if (pageButton instanceof HTMLButtonElement) { handleFilterPageButton(kind, filter, pageButton); return; }
      if (target.closest(`[data-clear-${kind}]`)) { clearFilterKind(kind); return; }
      if (target.closest(`[data-cycle-${kind}-page]`)) { cycleFilterPage(kind); return; }
      const item = target.closest(`[data-${kind}]`);
      if (!(item instanceof HTMLElement) || target.closest("input")) return;
      const value = item.dataset[kind];
      if (value !== undefined) cycleFilterValue(kind, value);
    });
    filter.addEventListener("keydown", (event) => {
      const item = event.target instanceof Element ? event.target.closest(".filter-choice-item") : null;
      if (!(item instanceof HTMLElement) || (event.key !== "Enter" && event.key !== " ")) return;
      if (item.tagName === "BUTTON") return;
      event.preventDefault();
      item.click();
    });
    filter.addEventListener("keydown", (event) => {
      const input = event.target instanceof Element ? event.target.closest(`[data-${kind}-page-input]`) : null;
      if (!(input instanceof HTMLInputElement) || event.key !== "Enter") return;
      event.preventDefault();
      commitFilterPage(kind, input);
      input.blur();
    });
    filter.addEventListener("focusout", (event) => {
      const input = event.target instanceof Element ? event.target.closest(`[data-${kind}-page-input]`) : null;
      if (input instanceof HTMLInputElement) restoreFilterPageInput(kind, input);
    });
  }
  DOM.archiveFilter.addEventListener("change", () => { STATE.archiveId = DOM.archiveFilter.value; STATE.page = 1; doSearch(); });
  DOM.typeFilter.addEventListener("change", () => { STATE.publicationType = DOM.typeFilter.value; STATE.page = 1; doSearch(); });
  DOM.periodPresets.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest("[data-period-from]") : null;
    if (!(button instanceof HTMLElement)) return;
    const isActive = dateKey("from") === (button.dataset.periodFrom || "") && dateKey("to") === (button.dataset.periodTo || "");
    if (isActive) {
      setDateParts("from", "");
      setDateParts("to", "");
      STATE.page = 1;
      renderDateControls();
      doSearch();
      return;
    }
    setDateParts("from", button.dataset.periodFrom || "");
    setDateParts("to", button.dataset.periodTo || "");
    STATE.page = 1;
    renderDateControls();
    doSearch();
  });
  DOM.dateClear.addEventListener("click", () => {
    clearDateFilter();
    STATE.page = 1;
    renderDateControls();
    doSearch();
  });
  DOM.dateTermAdd.addEventListener("click", () => {
    STATE.dateTerms.push({ year: "", month: "", day: "" });
    renderDateControls();
    syncUrl();
  });
  DOM.dateTerms.addEventListener("click", (event) => {
    const remove = event.target instanceof Element ? event.target.closest("[data-date-term-remove]") : null;
    if (!(remove instanceof HTMLElement)) return;
    const index = Number(remove.dataset.dateTermRemove);
    if (!Number.isInteger(index) || index < 0 || index >= STATE.dateTerms.length) return;
    STATE.dateTerms.splice(index, 1);
    if (!STATE.dateTerms.length) STATE.dateTerms.push({ year: "", month: "", day: "" });
    STATE.page = 1;
    renderDateControls();
    doSearch();
  });
  /** @param {Event} event */
  const updateDateTerm = (event) => {
    const select = event.target instanceof Element ? event.target.closest("[data-date-term-field]") : null;
    if (!(select instanceof HTMLInputElement || select instanceof HTMLSelectElement)) return;
    const row = select.closest("[data-date-term-index]");
    const index = row instanceof HTMLElement ? Number(row.dataset.dateTermIndex) : -1;
    const field = select.dataset.dateTermField;
    if (!Number.isInteger(index) || index < 0 || !STATE.dateTerms[index] ||
        !(field === "year" || field === "month" || field === "day")) return;
    STATE.dateTerms[index][field] = field === "year" ? cleanYear(select.value) : select.value;
    STATE.page = 1;
    renderDateControls();
    doSearch();
  };
  DOM.dateTerms.addEventListener("change", updateDateTerm);
  document.addEventListener("focusin", (event) => {
    const input = yearInputFromTarget(event.target);
    if (input) showYearPicker(input);
  });
  document.addEventListener("input", (event) => {
    const input = yearInputFromTarget(event.target);
    if (input) showYearPicker(input);
  });
  DOM.yearPicker.addEventListener("pointerdown", (event) => {
    const option = event.target instanceof Element ? event.target.closest("[data-year-value]") : null;
    if (!(option instanceof HTMLElement) || !activeYearInput) return;
    yearPickerPointer = { option, x: event.clientX, y: event.clientY };
    event.stopPropagation();
  });
  DOM.yearPicker.addEventListener("pointerup", (event) => {
    const option = event.target instanceof Element ? event.target.closest("[data-year-value]") : null;
    if (!(option instanceof HTMLElement) || !activeYearInput || !yearPickerPointer || yearPickerPointer.option !== option) return;
    const moved = Math.abs(event.clientX - yearPickerPointer.x) > 8 || Math.abs(event.clientY - yearPickerPointer.y) > 8;
    yearPickerPointer = null;
    if (!moved) chooseYear(activeYearInput, option.dataset.yearValue || "");
  });
  DOM.yearPicker.addEventListener("pointercancel", () => {
    yearPickerPointer = null;
  });
  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element) || (!event.target.closest(".year-input") && !event.target.closest("#year-picker"))) hideYearPicker();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !DOM.yearPicker.hidden) hideYearPicker();
  });
  window.addEventListener("scroll", handleYearPickerScroll, true);
  window.addEventListener("resize", repositionYearPicker);
  window.addEventListener("resize", () => schedulePageRecovery(120));
  DOM.searchInput.addEventListener("focus", () => warmConnection());
  window.addEventListener("focus", () => {
    schedulePageRecovery(0);
    warmConnection();
  });
  window.addEventListener("pageshow", () => schedulePageRecovery(0));
  window.addEventListener("pagehide", (event) => {
    flushParseDraft();
    flushProofreadDraft();
    if (event.persisted) return;
    clearActiveMediaViewer();
    cleanupPDFDocumentCache(true);
  });
  window.addEventListener("online", () => warmConnection(true));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      flushParseDraft();
      flushProofreadDraft();
    } else {
      schedulePageRecovery(0);
      warmConnection(true);
    }
  });
  for (const [element, stateKey] of /** @type {const} */ ([
    [DOM.dateFromYear, "dateFromYear"],
    [DOM.dateFromMonth, "dateFromMonth"],
    [DOM.dateFromDay, "dateFromDay"],
    [DOM.dateToYear, "dateToYear"],
    [DOM.dateToMonth, "dateToMonth"],
    [DOM.dateToDay, "dateToDay"],
  ])) {
    element.addEventListener("change", () => {
      updateDateInputState(stateKey, element.value);
      STATE.page = 1;
      renderDateControls();
      doSearch();
    });
  }
  DOM.clearFilters.addEventListener("click", resetFilters);
  DOM.randomBookBtn.addEventListener("click", openRandomPreview);
  DOM.emptyRandomBtn.addEventListener("click", openRandomPreview);
  document.querySelectorAll("[data-filter-toggle]").forEach((toggle) => {
    if (!(toggle instanceof HTMLElement)) return;
    toggle.addEventListener("click", () => {
      const key = toggle.dataset.filterToggle;
      if (!key) return;
      STATE.filterCollapsed[key] = !STATE.filterCollapsed[key];
      applyStateToDom();
    });
  });
  DOM.results.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const parse = event.target.closest("[data-parse-open]");
    if (parse instanceof HTMLElement) {
      if (parse.dataset.parseOpen) openParseEditor(parse.dataset.parseOpen);
      return;
    }
    const preview = event.target.closest("[data-preview]");
    if (preview instanceof HTMLElement) {
      if (preview.dataset.preview) openPreview(preview.dataset.preview);
      return;
    }
    const source = event.target.closest(".path-folder[data-source]");
    if (source instanceof HTMLElement) {
      if (source.dataset.source) toggleResultFilter("source", source.dataset.source);
      return;
    }
    const author = event.target.closest(".path-folder[data-author]");
    if (author instanceof HTMLElement) {
      if (author.dataset.author) toggleResultFilter("author", author.dataset.author);
      return;
    }
    const tag = event.target.closest(".path-folder[data-tag]");
    if (tag instanceof HTMLElement) {
      if (tag.dataset.tag) toggleResultFilter("tag", tag.dataset.tag);
      return;
    }
    const dateTermKey = event.target.closest(".path-folder[data-date-term-key]");
    if (dateTermKey instanceof HTMLElement) {
      toggleResultDateTerm(dateTermKey.dataset.dateTermLabel || dateTermKey.textContent || "");
      return;
    }
  });
  DOM.results.addEventListener("keydown", (event) => {
    const path = event.target instanceof Element ? event.target.closest(".path-folder[role='button']") : null;
    if (!(path instanceof HTMLElement) || (event.key !== "Enter" && event.key !== " ")) return;
    event.preventDefault();
    path.click();
  });
  DOM.previewPanel.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    if (event.target.closest("[data-parse-back], [data-parse-close]")) {
      flushParseDraft();
      STATE.parseItem = null;
      STATE.preview = "";
      clearPreviewDom();
      applyStateToDom();
      renderParseQueue();
      return;
    }
    const cropOpen = event.target.closest("[data-parse-crop-open]");
    if (cropOpen) {
      openParseCrop(cropOpen.closest("[data-parse-article]"));
      return;
    }
    if (event.target.closest("[data-parse-crop-toggle]")) {
      const toolbar = DOM.previewPanel.querySelector("[data-parse-crop-toolbar]");
      if (toolbar instanceof HTMLElement && toolbar.hidden) openParseCrop();
      else closeParseCrop();
      return;
    }
    const cropScope = event.target.closest("[data-parse-crop-scope]");
    if (cropScope instanceof HTMLElement) {
      setParseCropScope(cropScope.dataset.parseCropScope || "article");
      return;
    }
    if (event.target.closest("[data-parse-crop-reset]")) {
      const row = parseCropTargetRow();
      setParseCropThresholds(row, parseCropScope(), STATE.parseCurrentPage, [0, 0, 0, 0], true);
      updateParseCropUI();
      updateParsePageOCR();
      finishParseCropChange(row);
      return;
    }
    if (event.target.closest("[data-parse-crop-close]")) {
      closeParseCrop();
      return;
    }
    if (event.target.closest("[data-parse-page-prev]")) {
      loadParsePage(STATE.parseCurrentPage - 1);
      saveParseDraft();
      return;
    }
    if (event.target.closest("[data-parse-page-next]")) {
      loadParsePage(STATE.parseCurrentPage + 1);
      saveParseDraft();
      return;
    }
    const addArticle = event.target.closest("[data-parse-add]");
    if (addArticle) {
      const form = addArticle.closest("[data-parse-form]");
      if (!(form instanceof HTMLElement) || !STATE.parseItem) return;
      const holder = form.querySelector("[data-parse-articles]");
      if (!(holder instanceof HTMLElement)) return;
      const index = holder.querySelectorAll("[data-parse-article]").length;
      const current = Math.min(Number(form.dataset.maxPage) || 1, Math.max(1, STATE.parseCurrentPage));
      holder.insertAdjacentHTML("beforeend", parseArticleRowHTML({ title: "", authors: [], dates: (STATE.parseItem.article || {}).dates || [], page_start: current, page_end: current }, index, Number(form.dataset.maxPage) || 1));
      refreshParseArticleBody(holder.lastElementChild, false, true);
      saveParseDraft();
      return;
    }
    const refreshBody = event.target.closest("[data-parse-refresh-body]");
    if (refreshBody) {
      const row = refreshBody.closest("[data-parse-article]");
      const textarea = row && row.querySelector("[data-parse-content]");
      if (textarea instanceof HTMLTextAreaElement && textarea.dataset.edited === "1" && !window.confirm("确认用当前页码范围的 OCR 覆盖已编辑正文？")) return;
      refreshParseArticleBody(row, true, true);
      return;
    }
    const removeArticle = event.target.closest("[data-parse-remove]");
    if (removeArticle instanceof HTMLButtonElement && !removeArticle.disabled) {
      const row = removeArticle.closest("[data-parse-article]");
      const wasCropTarget = row && row.classList.contains("is-crop-target");
      if (row) row.remove();
      DOM.previewPanel.querySelectorAll("[data-parse-article] legend").forEach((legend, index) => { legend.textContent = `文章 ${index + 1}`; });
      if (wasCropTarget) setParseCropTarget(parseCropTargetRow());
      saveParseDraft();
      return;
    }
    const partInsert = event.target.closest("[data-add-part-before], [data-add-part-after]");
    if (partInsert instanceof HTMLElement) {
      const group = partInsert.closest("[data-proofread-part-group]");
      const target = group && group.querySelector(partInsert.dataset.addPartBefore != null ? "[data-part-insert-before]" : "[data-part-insert-after]");
      if (target) target.insertAdjacentHTML("beforeend", newPartHTML());
      syncFulltextFromParts(partInsert.closest("[data-proofread-form]"));
      saveProofreadDraft();
      return;
    }
    const deletePart = event.target.closest("[data-delete-part]");
    if (deletePart instanceof HTMLElement) {
      deletePart.dataset.proofreadDeleted = deletePart.dataset.proofreadDeleted === "true" ? "false" : "true";
      deletePart.textContent = deletePart.dataset.proofreadDeleted === "true" ? "恢复" : "删除";
      const group = deletePart.closest("[data-proofread-part-group]"); if (group) group.classList.toggle("proofread-deleted", deletePart.dataset.proofreadDeleted === "true");
      syncFulltextFromParts(deletePart.closest("[data-proofread-form]"));
      saveProofreadDraft();
      return;
    }
    if (event.target.closest("[data-remove-new-part]")) {
      const newPart = event.target.closest("[data-proofread-new-part]"); if (newPart) newPart.remove();
      syncFulltextFromParts(event.target.closest("[data-proofread-form]"));
      saveProofreadDraft();
      return;
    }
    if (event.target.closest("[data-fulltext-toggle]")) {
      const box = DOM.previewPanel.querySelector("[data-fulltext-box]");
      const button = event.target.closest("[data-fulltext-toggle]");
      if (box instanceof HTMLElement && button instanceof HTMLElement) {
        const opening = box.hidden;
        if (opening) {
          const form = DOM.previewPanel.querySelector("[data-proofread-form]");
          const input = box.querySelector("[data-fulltext-input]");
          if (form && input instanceof HTMLTextAreaElement && input.dataset.fulltextDirty !== "1") input.value = fullTextFromParts(form);
        }
        box.hidden = !box.hidden;
        button.textContent = box.hidden ? "全文编辑" : "收起全文编辑";
      }
      return;
    }
    const partEditorToggle = event.target.closest("[data-part-editor-toggle]");
    if (partEditorToggle) {
      const form = DOM.previewPanel.querySelector("[data-proofread-form]");
      if (form) {
        const open = form.classList.toggle("proofread-structure-open");
        form.classList.toggle("proofread-structure-closed", !open);
        partEditorToggle.textContent = open ? "收起段落编辑" : "段落编辑";
        form.querySelectorAll("[data-proofread-part-type]").forEach((select) => {
          const label = select.nextElementSibling;
          if (select instanceof HTMLSelectElement && label && label.classList.contains("proofread-part-type-label")) label.textContent = select.value;
        });
      }
      return;
    }
    const zoomButton = event.target.closest("[data-image-zoom]");
    if (zoomButton instanceof HTMLElement) {
      const image = DOM.previewPanel.querySelector("[data-proofread-source-image]");
      if (!(image instanceof HTMLImageElement)) return;
      const current = Number(image.dataset.imageScale || 1);
      const next = zoomButton.dataset.imageZoom === "reset"
        ? 1
        : Math.min(4, Math.max(0.25, current + (zoomButton.dataset.imageZoom === "in" ? 0.25 : -0.25)));
      image.dataset.imageScale = String(next);
      const baseWidth = Number(image.dataset.imageBaseWidth) || image.naturalWidth;
      image.style.maxWidth = "none";
      image.style.width = `${Math.max(1, baseWidth * next)}px`;
      image.style.height = "auto";
      image.style.transform = "none";
      return;
    }
    const pageButton = event.target.closest("[data-page-image-prev], [data-page-image-next]");
    if (pageButton instanceof HTMLElement) {
      const viewer = pageButton.closest("[data-page-image-viewer]");
      if (viewer instanceof HTMLElement) {
        const current = Number(viewer.dataset.pageIndex) || 0;
        setPageImage(viewer, current + (pageButton.dataset.pageImagePrev != null ? 0 : 2));
      }
      return;
    }
    if (event.target.closest("[data-fulltext-apply]")) {
      applyFullText(event.target.closest("[data-proofread-form]"));
      return;
    }
    const addList = event.target.closest("[data-add-list]");
    if (addList instanceof HTMLElement) {
      const kind = addList.dataset.addList;
      if (!kind) return;
      const fields = kind === "dates"
        ? [{ key: "year", label: "年", type: "number" }, { key: "month", label: "月", type: "number" }, { key: "day", label: "日", type: "number" }]
        : kind === "tags" ? [{ key: "name", label: "名称" }, { key: "type", label: "类型", options: TAG_TYPES }]
          : [{ key: "value", label: kind === "authors" ? "作者" : "URL" }];
      const holder = document.createElement("div");
      holder.innerHTML = listEditorHTML(kind, [kind === "dates" || kind === "tags" ? {} : ""], fields);
      const row = holder.querySelector("[data-list-row]");
      if (!row) return;
      addList.before(row);
      const firstInput = row.querySelector("input");
      if (firstInput instanceof HTMLInputElement) firstInput.focus();
      saveProofreadDraft();
      return;
    }
    const removeList = event.target.closest("[data-remove-list]");
    if (removeList) {
      const row = removeList.closest("[data-list-row]");
      if (row) row.remove();
      saveProofreadDraft();
      return;
    }
    const tagPageButton = event.target.closest("[data-preview-tag-page-change]");
    if (tagPageButton instanceof HTMLElement) {
      changePreviewTagPage(tagPageButton);
      return;
    }
    if (event.target.closest("[data-close-preview]")) {
      closePreview(true);
      return;
    }
    if (event.target.closest("[data-preview-fullscreen]")) {
      setPreviewFullscreen(!STATE.previewFullscreen, true);
      return;
    }
    if (event.target.closest("[data-proofread-back]")) {
      const item = getCachedPreview(STATE.preview);
      if (item) openPreview(item.doc_id, false);
      return;
    }
    if (event.target.closest("[data-preview-theme]")) toggleTheme();
    if (event.target.closest("[data-preview-mobile]")) toggleMobileMode();
  });
  DOM.previewPanel.addEventListener("pointerdown", (event) => {
    if (event.target instanceof Element && event.target.closest("[data-parse-crop-overlay]")) beginParseCropDrag(event);
  });
  window.addEventListener("pointermove", moveParseCrop, { passive: false });
  window.addEventListener("pointerup", endParseCrop);
  window.addEventListener("pointercancel", endParseCrop);
  DOM.previewPanel.addEventListener("change", (event) => {
    if (!(event.target instanceof Element)) return;
    const parsePage = event.target.closest("[data-parse-page]");
    if (parsePage instanceof HTMLInputElement) {
      loadParsePage(Number(parsePage.value));
      saveParseDraft();
      return;
    }
    const parseRange = event.target.closest("[data-parse-start], [data-parse-end]");
    if (parseRange) {
      const row = parseRange.closest("[data-parse-article]");
      refreshParseArticleBody(row, false, true);
      if (row && row.classList.contains("is-crop-target")) updateParseCropUI();
      saveParseDraft();
      return;
    }
    const select = event.target.closest("[data-compare-select]");
    if (select instanceof HTMLSelectElement && select.value) {
      if (select.value === "__proofread__") openProofread(getCachedPreview(STATE.preview));
      else comparePreview(select.value);
      return;
    }
    const sourceFile = event.target.closest("[data-proofread-source-file]");
    if (sourceFile instanceof HTMLSelectElement) {
      STATE.proofreadFile = sourceFile.value;
      const item = getCachedPreview(STATE.preview);
      openProofread(item, null, true);
      syncUrl(true);
      saveProofreadDraft();
      return;
    }
    const pageInput = event.target.closest("[data-proofread-page]");
    if (pageInput instanceof HTMLInputElement) {
      STATE.proofreadPage = String(Math.max(1, Number(pageInput.value) || 1));
      syncUrl(true);
      saveProofreadDraft();
      return;
    }
    const pageImageInput = event.target.closest("[data-page-image-input]");
    if (pageImageInput instanceof HTMLInputElement) {
      const viewer = pageImageInput.closest("[data-page-image-viewer]");
      if (viewer instanceof HTMLElement) setPageImage(viewer, Number(pageImageInput.value) || 1);
      return;
    }
    const form = event.target.closest("[data-proofread-form]");
    if (form) {
      if (!event.target.matches("[data-fulltext-input]")) syncFulltextFromParts(form);
      saveProofreadDraft();
    }
  });
  DOM.previewPanel.addEventListener("input", (event) => {
    if (!(event.target instanceof HTMLElement)) return;
    if (event.target.matches("[data-parse-content]")) event.target.dataset.edited = "1";
    if (event.target.closest("[data-parse-form]")) saveParseDraft();
    if (event.target.matches("[data-fulltext-input]")) event.target.dataset.fulltextDirty = "1";
    const form = event.target.closest("[data-proofread-form]");
    if (form) {
      if (!event.target.matches("[data-fulltext-input]")) syncFulltextFromParts(form);
      saveProofreadDraft();
    }
  });
  DOM.previewPanel.addEventListener("submit", (event) => {
    if (!(event.target instanceof Element)) return;
    const parseForm = event.target.closest("[data-parse-form]");
    if (parseForm) {
      event.preventDefault();
      submitParseForm(parseForm);
      return;
    }
    const form = event.target.closest("[data-proofread-form]");
    if (!form) return;
    event.preventDefault();
    submitProofreadForm(form);
  });
  DOM.previewPanel.addEventListener("mouseup", (event) => {
    if (!(event.target instanceof Element)) return;
    if (!event.target.closest("[data-proofread-select]")) return;
    // A double-click selects a sentence to read aloud, not text to proofread.
    if (event.detail >= 2 && (speechHost.CCRDSpeech || speechClientPromise)) return;
    const selection = window.getSelection();
    const selected = String(selection ? selection.toString() : "").trim();
    const item = getCachedPreview(STATE.preview);
    if (!selected || !item) return;
    const article = proofreadArticleParts(item.article);
    if (!article) return;
    const partIndex = article.parts.findIndex((_part, index) => partTextWithPivots(article, index).includes(selected));
    openProofread(item, partIndex >= 0 ? partIndex : null);
  });
  DOM.prevPage.addEventListener("click", () => {
    if (STATE.parseMode) {
      if (STATE.parsePage > 1) { STATE.parsePage -= 1; syncUrl(); loadParseQueue(); }
      return;
    }
    DOM.pageInput.value = String(STATE.page);
    if (STATE.page > 1) {
      STATE.page -= 1;
      doSearch();
    }
  });
  DOM.nextPage.addEventListener("click", () => {
    if (STATE.parseMode) {
      if (STATE.parsePage < Math.ceil(STATE.parseTotal / STATE.parsePageSize)) { STATE.parsePage += 1; syncUrl(); loadParseQueue(); }
      return;
    }
    DOM.pageInput.value = String(STATE.page);
    if (STATE.page < Math.ceil(STATE.total / STATE.pageSize)) {
      STATE.page += 1;
      doSearch();
    }
  });
  DOM.pageInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (STATE.parseMode) {
      const totalPages = Math.max(1, Math.ceil(STATE.parseTotal / STATE.parsePageSize));
      STATE.parsePage = Math.min(totalPages, Math.max(1, Number(DOM.pageInput.value) || 1));
      syncUrl();
      loadParseQueue();
    } else commitResultPage();
    DOM.pageInput.blur();
  });
  DOM.pageInput.addEventListener("blur", () => {
    DOM.pageInput.value = String(STATE.parseMode ? STATE.parsePage : STATE.page);
  });
  window.addEventListener("popstate", () => {
    cancelPreviewRequests();
    const wasPreviewOpen = Boolean(STATE.preview);
    parseUrl();
    if (wasPreviewOpen && !STATE.preview) {
      history.replaceState({ previewEntry: false }, "", previewCloseUrl);
      parseUrl();
    }
    applyStateToDom();
    if (STATE.parseMode) {
      loadParseQueue();
      return;
    }
    restorePreview(false);
    if (wasPreviewOpen && !STATE.preview) {
      restoreResultsScroll(previewResultsScrollTop);
    } else {
      doSearch();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (STATE.preview && !DOM.previewPanel.hidden && (previewIsModal() || event.target instanceof Node && DOM.previewPanel.contains(event.target))) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (DOM.previewPanel.classList.contains("preview-fullscreen")) {
          setPreviewFullscreen(false, true);
          const button = DOM.previewPanel.querySelector("[data-preview-fullscreen]");
          if (button instanceof HTMLElement) button.focus({ preventScroll: true });
          return;
        }
        closePreview(true);
        return;
      }
      if (event.key === "Tab" && previewIsModal()) {
        const focusable = previewFocusableElements();
        if (!focusable.length) {
          event.preventDefault();
          DOM.previewPanel.focus({ preventScroll: true });
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
        return;
      }
      return;
    }
    if (event.key === "Escape") {
      if (!DOM.yearPicker.hidden) return;
      if (STATE.leftOpen) {
        event.preventDefault();
        STATE.leftOpen = false;
        STATE.sidebarExpanded = false;
        applyStateToDom();
        syncUrl();
        DOM.leftToggle.focus({ preventScroll: true });
        return;
      }
    }
    if (event.key === "/" && document.activeElement !== DOM.searchInput) {
      event.preventDefault();
      DOM.searchInput.focus();
      DOM.searchInput.select();
    }
  });
}

// BEGIN STRICT DOM BINDINGS
/** @template {HTMLElement} T @param {string} id @param {{new (...args: never[]): T}} type @returns {T} */
function requireDom(id, type) {
  const element = document.getElementById(id);
  if (!(element instanceof type)) throw new Error(`Missing or invalid page control: ${id}`);
  return element;
}

/** @returns {void} */
function cacheDom() {
  /** @satisfies {SearchDOM} */
  const bindings = {
    header: requireDom("header", HTMLElement), leftSidebar: requireDom("left-sidebar", HTMLElement),
    sidebarTitle: requireDom("sidebar-title", HTMLElement), overlay: requireDom("overlay", HTMLElement),
    sourceSearch: requireDom("source-search", HTMLInputElement), sourceFilter: requireDom("source-filter", HTMLElement),
    searchForm: requireDom("search-form", HTMLFormElement), searchInput: requireDom("search-input", HTMLInputElement),
    leftToggle: requireDom("left-toggle", HTMLButtonElement), mobileToggleBtn: requireDom("mobile-toggle-btn", HTMLButtonElement),
    mobileToggleIcon: requireDom("mobile-toggle-icon", HTMLElement), themeBtn: requireDom("theme-btn", HTMLButtonElement),
    parseBtn: requireDom("parse-btn", HTMLButtonElement), sidebarExpand: requireDom("sidebar-expand", HTMLButtonElement),
    exactToggle: requireDom("exact-toggle", HTMLInputElement), previewHighlightToggle: requireDom("preview-highlight-toggle", HTMLInputElement),
    fulltextToggle: requireDom("fulltext-toggle", HTMLInputElement), pageSize: requireDom("page-size", HTMLSelectElement),
    parsePageSizeOption: requireDom("parse-page-size-option", HTMLOptionElement), sortFilter: requireDom("sort-filter", HTMLSelectElement),
    authorSearch: requireDom("author-search", HTMLInputElement), authorFilter: requireDom("author-filter", HTMLElement),
    tagSearch: requireDom("tag-search", HTMLInputElement), tagFilter: requireDom("tag-filter", HTMLElement),
    archiveFilter: requireDom("archive-filter", HTMLSelectElement), typeFilter: requireDom("type-filter", HTMLSelectElement),
    periodPresets: requireDom("period-presets", HTMLElement), dateFromYear: requireDom("date-from-year", HTMLInputElement),
    dateFromMonth: requireDom("date-from-month", HTMLSelectElement), dateFromDay: requireDom("date-from-day", HTMLSelectElement),
    dateToYear: requireDom("date-to-year", HTMLInputElement), dateToMonth: requireDom("date-to-month", HTMLSelectElement),
    dateToDay: requireDom("date-to-day", HTMLSelectElement), dateClear: requireDom("date-clear", HTMLButtonElement),
    dateError: requireDom("date-error", HTMLElement), yearPicker: requireDom("year-picker", HTMLElement),
    dateTermAdd: requireDom("date-term-add", HTMLButtonElement), dateTerms: requireDom("date-terms", HTMLElement),
    clearFilters: requireDom("clear-filters", HTMLButtonElement), statusBar: requireDom("status-bar", HTMLElement),
    resultCount: requireDom("result-count", HTMLElement), statusText: requireDom("status-text", HTMLElement),
    indexProgress: requireDom("index-progress", HTMLElement), results: requireDom("results", HTMLElement),
    resultsContainer: requireDom("results-container", HTMLElement), emptyState: requireDom("empty-state", HTMLElement),
    randomBookBtn: requireDom("random-book-btn", HTMLButtonElement), emptyRandomBtn: requireDom("empty-random-btn", HTMLButtonElement),
    previewPanel: requireDom("preview-panel", HTMLElement), prevPage: requireDom("prev-page", HTMLButtonElement),
    nextPage: requireDom("next-page", HTMLButtonElement), pageInput: requireDom("page-input", HTMLInputElement),
    pageTotal: requireDom("page-total", HTMLElement), loadInfo: requireDom("load-info", HTMLElement), toast: requireDom("toast", HTMLElement),
  };
  Object.assign(DOM, bindings);
}
// END STRICT DOM BINDINGS

function init() {
  document.body.classList.toggle("firefox", /Firefox\//.test(navigator.userAgent));
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/sw.js", { scope: "/" }).catch(() => {});
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  cacheDom();
  STATE.isDark = localStorage.getItem("theme") !== "light";
  const savedMobileMode = localStorage.getItem("mobileMode");
  STATE.isMobile = savedMobileMode === "mobile" || (!savedMobileMode && window.innerWidth <= 768);
  parseUrl();
  if (STATE.preview && !(history.state && history.state.previewEntry === true)) {
    const previewDocId = STATE.preview;
    STATE.preview = "";
    syncUrl();
    STATE.preview = previewDocId;
    syncUrl(false);
  }
  applyStateToDom();
  attachEvents();
  const initialSearch = STATE.parseMode
    ? loadParseQueue()
    : canUseInitialBootstrap() ? loadInitialBootstrap() : Promise.all([doSearch(), loadFacets()]);
  if (!STATE.parseMode) restorePreview(false);
  initialSearch.finally(() => {
    lastKeepaliveAt = Date.now();
    window.setInterval(() => warmConnection(), KEEPALIVE_INTERVAL_MS);
    if (STATE.parseMode) {
      updateProgress();
      loadFacets();
    }
  });
}
document.addEventListener("DOMContentLoaded", init);
