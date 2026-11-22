"""Offline browser acceptance of exact candidate bytes in both deployed HTML Readers."""
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

import httpx

PAGES = 'https://vomebook.github.io/search'
SPACE = 'https://voiceofml-search.hf.space'
RUNTIME = 'https://huggingface.co/api/spaces/VoiceOfML/Search/runtime'
FORMAT = 'reader/formats/reader-html-format.mjs'
STYLES = 'reader/ui/reader-document-styles.mjs'
BINDINGS = 'reader/core/reader-bindings.mjs'
FIXTURE = 'https://reader-profile.invalid/'
SHA = re.compile(r'[0-9a-f]{40}')


def fetch(client, url, limit=1024 * 1024):
    for attempt in range(3):
        try:
            with client.stream('GET', url) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > limit:
                        raise ValueError('Reader replay input exceeds limit')
                    raw.extend(chunk)
                return bytes(raw)
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == 2 or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (429, 502, 503, 504)):
                raise
            time.sleep(attempt + 1)


def space_revision(client):
    info = json.loads(fetch(client, RUNTIME))
    rev = info.get('sha')
    if info.get('stage') != 'RUNNING' or not isinstance(rev, str) or not SHA.fullmatch(rev):
        raise RuntimeError('Reader replay requires an exact running Space revision')
    return rev


def sources(client, origin, root):
    resource = fetch(client, root + '/reader-resources.js').decode('utf-8')
    digest, = re.findall(r'purify\s*:\s*\{[^}]*\bfile:\s*"purify\.min\.js"[^}]*\bsha256:\s*"([a-f0-9]{64})"', resource)
    purify = fetch(client, f'{origin}/static/vendor/purify.min.{digest[:12]}.js')
    if hashlib.sha256(purify).hexdigest() != digest:
        raise ValueError('Reader replay sanitizer digest differs from the pinned source')
    source = fetch(client, f'{root}/{FORMAT}').decode('utf-8')
    marker = '    sanitizeCssReferences,'
    if source.count(marker) != 1:
        raise ValueError('Reader HTML replay hook changed')
    source = source.replace(marker, marker + ' prepareHtmlDocument, buildHtmlFrameSource, normalizeHtmlIndent,')
    # Scroll listener is outside this offline render test and never invoked.
    return {FORMAT: source, STYLES: fetch(client, f'{root}/{STYLES}').decode('utf-8'),
            BINDINGS: 'export function bindReaderScrollIntent() { throw new Error("unexpected scroll wiring"); }'}, purify.decode('utf-8')


PROBE = r'''async ([format, styles]) => {
  const {createReaderHtmlFormat} = await import('/' + format), st = await import('/' + styles);
  globalThis.replay = async text => {
    function inspect(doc) {
      const w = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT), texts = [];
      const norm = s => s.normalize('NFC').replace(/\s+/gu, '');
      for (let n; (n = w.nextNode());) {
        if (!n.parentElement?.closest('style,script,template,noscript')) texts.push(n.data);
      }
      const refs = [...doc.body.querySelectorAll('a[href^="#"]')].map(a => a.getAttribute('href').slice(1));
      const matched = refs.filter(x => {
        if (!x) return false;
        try { x = decodeURIComponent(x); } catch {}
        return doc.getElementById(x) || doc.getElementsByName(x).length;
      }).sort();
      const imgs = [...doc.querySelectorAll('img[src]')];
      return {stable: {body: norm(texts.join('')), refs: matched, src: imgs.map(i => i.getAttribute('src')),
        table: [...doc.querySelectorAll('td,th,caption')].map(x => [x.tagName, norm(x.textContent)]),
        pre: [...doc.querySelectorAll('pre')].map(x => x.textContent)},
        decoded: imgs.filter(i => i.complete && i.naturalWidth > 0).length};
    }
    const source = inspect(new DOMParser().parseFromString(text, 'text/html'));
    const r = createReaderHtmlFormat({...st}), data = r.prepareHtmlDocument(text);
    const frame = document.createElement('iframe'); frame.sandbox = 'allow-same-origin';
    const loaded = new Promise(resolve => frame.addEventListener('load', resolve, {once:true}));
    frame.srcdoc = r.buildHtmlFrameSource(data); document.body.replaceChildren(frame);
    let timer;
    try { await Promise.race([loaded, new Promise((_, reject) => timer = setTimeout(() => reject(new Error('Reader frame timeout')), 8000))]); }
    finally { clearTimeout(timer); }
    const doc = frame.contentDocument;
    st.wrapEbookTables(doc.body); r.normalizeHtmlIndent(doc);
    await Promise.race([Promise.all([...doc.images].map(i => i.decode().catch(() => {}))), new Promise(resolve => setTimeout(resolve, 3000))]);
    const result = inspect(doc), nl = doc.documentElement;
    const mismatch = Object.keys(source.stable).filter(k => JSON.stringify(source.stable[k]) !== JSON.stringify(result.stable[k]));
    return {mismatch, images: source.stable.src.length, decoded: result.decoded, width: nl.clientWidth, scroll: nl.scrollWidth};
  };
}'''


def surface(browser, modules, purify, documents):
    blocked = []
    context = browser.new_context(viewport={'width': 390, 'height': 844}, service_workers='block')
    try:
        def route(r):
            url = r.request.url
            if url == FIXTURE:
                return r.fulfill(content_type='text/html', body='<!doctype html><meta charset=utf-8><style>body{margin:0}iframe{width:390px;height:844px;border:0}</style>')
            if url.startswith(FIXTURE) and url[len(FIXTURE):] in modules:
                return r.fulfill(content_type='text/javascript', body=modules[url[len(FIXTURE):]])
            blocked.append(urlsplit(url).scheme)
            return r.abort()
        context.route('**/*', route)
        page = context.new_page()
        page.goto(FIXTURE, timeout=12000)
        page.add_script_tag(content=purify)
        page.evaluate(PROBE, [FORMAT, STYLES])
        results = {}
        for sha, raw in documents.items():
            blocked.clear()
            facts = page.evaluate('text => replay(text)', raw.decode('utf-8'))
            facts['blocked_requests'] = len(blocked)
            facts['passed'] = (not blocked and not facts['mismatch'] and facts['images'] == facts['decoded']
                               and facts['width'] == 390 and facts['scroll'] <= 391)
            results[sha] = facts
        return results
    finally:
        context.close()


def verify(documents, pages_revision, *, executable_path=None):
    from playwright.sync_api import sync_playwright

    if not documents or not isinstance(pages_revision, str) or not SHA.fullmatch(pages_revision):
        raise ValueError('Reader replay requires nonempty exact bytes and a Pages code revision')
    if any(hashlib.sha256(raw).hexdigest() != sha for sha, raw in documents.items()):
        raise ValueError('Reader replay candidate digest mismatch')
    with httpx.Client(timeout=20, follow_redirects=True, headers={'Cache-Control': 'no-cache'}) as client:
        hf_revision = space_revision(client)
        inputs = {
            'pages': (pages_revision, sources(client, PAGES, f'https://raw.githubusercontent.com/vomebook/search/{pages_revision}/static')),
            'space': (hf_revision, sources(client, SPACE, f'https://huggingface.co/spaces/VoiceOfML/Search/resolve/{hf_revision}/static')),
        }
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=executable_path, args=['--no-sandbox', '--disable-dev-shm-usage'])
            try:
                report = {name: {'code': revision, 'documents': surface(browser, *source, documents)}
                          for name, (revision, source) in inputs.items()}
            finally:
                browser.close()
        if space_revision(client) != hf_revision:
            raise RuntimeError('Space changed during the Reader replay')
    return report


def rejected(report, documents):
    if set(report) != {'space', 'pages'} or any(set(site['documents']) != set(documents) for site in report.values()):
        raise ValueError('Reader replay did not cover exact candidates on both sites')
    return {sha for sha in documents if any(site['documents'][sha]['passed'] is not True for site in report.values())}
