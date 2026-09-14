"""Measure and select lossless PDF layouts with the production PDF.js engine."""
import hashlib
import http.server
import json
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.parse

from pypdf import PdfReader
from pypdf.generic import IndirectObject, StreamObject

ENGINE = "6.3.289"
PROFILE = "pdf-range-v1"
ASSESSMENT = "pdfjs-6.3.289-range-1m-scene-v1-policy-v1"
MI = 1024 * 1024
MIN_BYTES = 4 * MI
METHODS = {
    "objects": ["--object-streams=generate"],
    "linearized": ["--linearize"],
    "objects-linearized": ["--object-streams=generate", "--linearize"],
}


class UnsupportedPDF(ValueError):
    pass


def content_signature(path: Path) -> dict:
    """Compare every page/resource without depending on renumbered object IDs.

    Complex interactive documents stay on their existing readable version until
    a corresponding equivalence check is implemented.
    """
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise UnsupportedPDF("encrypted input; use its existing decrypted asset")
    root = reader.trailer["/Root"]
    supported = {"/Type", "/Pages", "/Outlines", "/PageMode", "/PageLayout", "/Version",
                 "/ViewerPreferences", "/Metadata", "/Lang", "/MarkInfo"}
    if set(root) - supported:
        raise UnsupportedPDF("catalog structures require additional equivalence checks: " +
                             ",".join(sorted(set(root) - supported)))
    memo, active = {}, set()

    def normalize(value):
        if isinstance(value, IndirectObject):
            key = (value.idnum, value.generation)
            if key in active:
                raise UnsupportedPDF("cyclic resource structure")
            if key not in memo:
                active.add(key)
                memo[key] = normalize(value.get_object())
                active.remove(key)
            return memo[key]
        if isinstance(value, dict):
            result = {str(k): normalize(v) for k, v in value.items() if k != "/Length"}
            if isinstance(value, StreamObject):
                result["stream_sha256"] = hashlib.sha256(value._data).hexdigest()
            return result
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        return str(value)

    pages = []
    for page in reader.pages:
        if page.get("/Annots"):
            raise UnsupportedPDF("annotations require additional equivalence checks")
        # PdfReader resolves inherited resources/boxes while flattening pages.
        pages.append({"page": normalize({k: v for k, v in page.items()
                                         if k not in {"/Parent", "/Annots"}}),
                      "crop": list(map(str, page.cropbox)),
                      "media": list(map(str, page.mediabox)), "rotate": page.rotation})

    def outline(items):
        return [outline(item) if isinstance(item, list) else {
            "page": reader.get_destination_page_number(item),
            "destination": normalize({k: v for k, v in item.items() if k != "/Page"}),
        } for item in items]

    return {"pages": pages, "outline": outline(reader.outline),
            "metadata": normalize(dict(reader.metadata or {})),
            "catalog": normalize({k: v for k, v in root.items()
                                  if k not in {"/Pages", "/Outlines"}})}


SCENE = """async () => {
  const lib = await import('/vendor/pdf.mjs');
  if (lib.version !== '6.3.289') throw Error('unexpected PDF.js version');
  lib.GlobalWorkerOptions.workerSrc = '/vendor/worker.mjs';
  const task = lib.getDocument({url:'/document.pdf',rangeChunkSize:1048576,
    disableAutoFetch:true,disableStream:true,cMapUrl:'/vendor/cmaps/',cMapPacked:true,
    standardFontDataUrl:'/vendor/standard_fonts/',wasmUrl:'/vendor/wasm/'});
  const snapshots = {}, renders = [];
  const snap = async () => (await fetch('/stats')).json();
  const work = async () => {
    const pdf = await task.promise;
    snapshots.initialized = await snap();
    async function render(n) {
      const page = await pdf.getPage(n);
      const raw = page.getViewport({scale:1});
      const viewport = page.getViewport({scale:Math.min(1,2048/Math.max(raw.width,raw.height))});
      const canvas = document.createElement('canvas');
      canvas.width = Math.ceil(viewport.width); canvas.height = Math.ceil(viewport.height);
      const context = canvas.getContext('2d');
      await page.render({canvasContext:context,viewport}).promise;
      const text = (await page.getTextContent()).items.map(i=>[i.str,i.transform,i.hasEOL]);
      const pixels = context.getImageData(0,0,canvas.width,canvas.height).data;
      const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',pixels))]
        .map(x=>x.toString(16).padStart(2,'0')).join('');
      renders.push({page:n,width:canvas.width,height:canvas.height,hash,text});
      page.cleanup();
    }
    await render(1); snapshots.first = await snap();
    for (let n=2;n<=Math.min(3,pdf.numPages);n++) await render(n);
    const outline = await pdf.getOutline(); let entries = 0;
    async function walk(items) {
      for (const item of items || []) {
        entries++;
        let dest = item.dest;
        if (typeof dest === 'string') dest = await pdf.getDestination(dest);
        if (dest && typeof dest[0] === 'object') await pdf.getPageIndex(dest[0]);
        await walk(item.items);
      }
    }
    await walk(outline); snapshots.startup = await snap();
    await new Promise(r=>setTimeout(r,750)); snapshots.idle = await snap();
    for (const n of [...new Set([Math.min(10,pdf.numPages),Math.ceil(pdf.numPages/2),pdf.numPages])]) {
      await render(n); snapshots['page'+n] = await snap();
    }
    snapshots.jump = await snap();
    await new Promise(r=>setTimeout(r,750)); snapshots.final = await snap();
    return {pages:pdf.numPages,outline_entries:entries,renders,snapshots};
  };
  let timer;
  try { return await Promise.race([work(),new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(Error('PDF assessment deadline')),90000);
  })]); } finally { clearTimeout(timer); await task.destroy(); }
}"""


def benchmark(path: Path, vendor: Path) -> dict:
    from playwright.sync_api import sync_playwright
    modules = {
        "/vendor/pdf.mjs": next(iter(sorted((vendor / "build").glob("pdf.min.mjs"))), None)
            or next(iter(sorted(vendor.glob("pdf.min.*.mjs"))), None),
        "/vendor/worker.mjs": next(iter(sorted((vendor / "build").glob("pdf.worker.min.mjs"))), None)
            or next(iter(sorted(vendor.glob("pdf.worker.min.*.mjs"))), None),
    }
    if not all(modules.values()):
        raise RuntimeError("PDF_RANGE_VENDOR must contain pinned pdfjs-dist or Reader vendor files")
    stats = {"bytes": 0, "requests": 0, "initial_bytes": 0}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path).path
            if url == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if url == "/document.pdf":
                size = path.stat().st_size
                match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
                begin, end = (int(match[1]), min(int(match[2]) + 1, size)) if match else (0, size)
                if not 0 <= begin < end <= size:
                    self.send_error(416)
                    return
                self.send_response(206 if match else 200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(end - begin))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                if match:
                    self.send_header("Content-Range", f"bytes {begin}-{end-1}/{size}")
                    stats["requests"] += 1
                self.end_headers()
                try:
                    with path.open("rb") as stream:
                        stream.seek(begin)
                        remaining = end - begin
                        while remaining:
                            if not match:
                                time.sleep(.02)
                            block = stream.read(min(65536, remaining))
                            self.wfile.write(block)
                            stats["bytes" if match else "initial_bytes"] += len(block)
                            remaining -= len(block)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if url in {"/", "/stats"}:
                payload = json.dumps(dict(stats)).encode() if url == "/stats" else b"<!doctype html><title>PDF assessment</title>"
                self.send_response(200)
                self.send_header("Content-Type", "application/json" if url == "/stats" else "text/html")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            target = modules.get(url)
            if target is None and url.startswith("/vendor/"):
                target = (vendor / urllib.parse.unquote(url[len('/vendor/'):])).resolve()
                if not target.is_relative_to(vendor.resolve()):
                    self.send_error(404)
                    return
            if target is None or not target.is_file():
                self.send_error(404)
                return
            payload = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript" if target.suffix == ".mjs" else "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page(service_workers="block")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
                origin = f"http://127.0.0.1:{server.server_port}"
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/")
                           else route.abort())
                page.goto(origin)
                result = page.evaluate(SCENE)
                if errors:
                    raise RuntimeError("PDF rendering errors: " + "; ".join(errors)[:500])
                return result
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()


def needs_optimization(result: dict, size: int) -> bool:
    amount = result["snapshots"]["startup"]["bytes"]
    return amount > 8 * MI or (amount >= 4 * MI and amount > size * .25)


def improvement(before: dict, after: dict, source_size: int, output_size: int) -> bool:
    a, b = before["snapshots"], after["snapshots"]
    if before["renders"] != after["renders"] or before["pages"] != after["pages"]:
        return False
    if before["outline_entries"] != after["outline_entries"]:
        return False
    saved = a["startup"]["bytes"] - b["startup"]["bytes"]
    return (saved >= MI and b["startup"]["bytes"] <= a["startup"]["bytes"] * .7
            and output_size <= source_size * 1.05
            and b["startup"]["requests"] <= a["startup"]["requests"]
            and b["jump"]["bytes"] <= a["jump"]["bytes"]
            and b["jump"]["requests"] <= a["jump"]["requests"]
            and b["idle"]["bytes"] == b["startup"]["bytes"]
            and b["final"]["bytes"] == b["jump"]["bytes"])


def assess(source: Path, work: Path, vendor: Path) -> tuple[dict, Path | None]:
    size = source.stat().st_size
    if size < MIN_BYTES:
        return {"status": "unchanged", "reason": "below-4-mib"}, None
    if size > 2 * 1024 * MI:
        return {"status": "unsupported", "reason": "source-exceeds-2-gib"}, None
    signature = content_signature(source)
    before = benchmark(source, vendor)
    report = {"status": "unchanged", "reason": "startup-within-budget", "before": before}
    if not needs_optimization(before, size):
        return report, None
    report.update(status="no-gain", reason="no-passing-candidate", candidates={})
    passing = []
    for method, options in METHODS.items():
        target = work / (method + ".pdf")
        try:
            subprocess.run(["qpdf", *options, "--stream-data=preserve", str(source), str(target)],
                           check=True, capture_output=True, timeout=120)
            subprocess.run(["qpdf", "--check", str(target)], check=True, capture_output=True, timeout=60)
            if "--linearize" in options:
                subprocess.run(["qpdf", "--check-linearization", str(target)],
                               check=True, capture_output=True, timeout=60)
            if content_signature(target) != signature:
                raise ValueError("document content or resource signature differs")
            after = benchmark(target, vendor)
            accepted = improvement(before, after, size, target.stat().st_size)
            report["candidates"][method] = {"accepted": accepted, "bytes": target.stat().st_size,
                                            "measurement": after}
            if accepted:
                passing.append((after["snapshots"]["startup"]["bytes"],
                                after["snapshots"]["startup"]["requests"],
                                after["snapshots"]["jump"]["bytes"], method, target))
        except Exception as error:
            report["candidates"][method] = {"accepted": False, "error": type(error).__name__ + ": " + str(error)[:300]}
    if not passing:
        if any("error" in value for value in report["candidates"].values()):
            report.update(status="failed", reason="candidate-assessment-incomplete")
        return report, None
    chosen = min(passing)
    report.update(status="optimized", reason="measured-improvement", method=chosen[3])
    return report, chosen[4]
