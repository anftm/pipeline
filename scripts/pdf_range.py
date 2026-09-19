"""Measure and select lossless PDF layouts with the production PDF.js engine."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.parse

from pypdf import PdfReader
from pypdf.generic import IndirectObject, NullObject, StreamObject

ENGINE = "6.3.289"
PROFILE = "pdf-range-v1"
ASSESSMENT = "pdfjs-6.3.289-range-1m-scene-v2-policy-v4"
MI = 1024 * 1024
MIN_BYTES = 4 * MI
METHODS = {
    "objects": ["--object-streams=generate"],
    "linearized": ["--linearize"],
    "objects-linearized": ["--object-streams=generate", "--linearize"],
}
EARLY_ACCEPT_STARTUP_BYTES = MI


class UnsupportedPDF(ValueError):
    pass


def candidate_methods() -> dict[str, list[str]]:
    """Run all candidates by default; allow explicit lightweight diagnostics."""
    if os.environ.get("PDF_RANGE_TRY_HEAVY", "1").lower() in {"0", "false", "no"}:
        return {"objects": METHODS["objects"]}
    return METHODS


def content_signature(path: Path) -> dict:
    """Compare the reachable graph, including cycles, independent of PDF object IDs.

    Assign graph IDs in deterministic traversal order. A flat object table keeps
    parent/child cycles and shared resources intact without recursive expansion.
    Only XMP compression is normalized; image/font/content bytes stay exact.
    """
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise UnsupportedPDF("password required; use its existing decrypted asset")

    def resolve(value):
        return value.get_object() if isinstance(value, IndirectObject) else value

    root = reader.trailer["/Root"]
    supported = {"/Type", "/Pages", "/Outlines", "/PageMode", "/PageLayout", "/Version",
                 "/ViewerPreferences", "/Metadata", "/Lang", "/MarkInfo", "/OpenAction", "/PageLabels",
                 "/StructTreeRoot", "/AcroForm", "/Names", "/Dests", "/OutputIntents",
                 "/OCProperties", "/PieceInfo", "/Extensions", "/LastModified", "/URI",
                 "/SpiderInfo", "/Threads", "/DefaultGray", "/DefaultRGB", "/DefaultCMYK"}
    if set(root) - supported:
        raise UnsupportedPDF("catalog structures require additional equivalence checks: " +
                             ",".join(sorted(set(root) - supported)))
    page_numbers = {(page.indirect_reference.idnum, page.indirect_reference.generation): number
                    for number, page in enumerate(reader.pages)}
    opening = root.get("/OpenAction")
    if opening is not None and not isinstance(resolve(opening), NullObject):
        opening = resolve(opening)
        if isinstance(opening, dict):
            if resolve(opening.get("/S")) != "/GoTo" or set(opening) - {"/S", "/D", "/Type"}:
                raise UnsupportedPDF("non-local opening action requires additional equivalence checks")
            opening = opening.get("/D")
            opening = opening.get_object() if opening is not None else None
        if isinstance(opening, str):
            destination = reader.named_destinations.get(opening)
            if destination is None or reader.get_destination_page_number(destination) is None:
                raise UnsupportedPDF("opening action has an unresolved named destination")
        elif (not isinstance(opening, list) or len(opening) < 2
              or not isinstance(opening[0], IndirectObject)
              or not isinstance(resolve(opening[0]), dict)
              or resolve(resolve(opening[0]).get("/Type")) != "/Page"):
            raise UnsupportedPDF("opening action is not an explicit local page destination")
        # Orphan /Page destinations occur in merged books. Keep their complete
        # reachable graph below, rather than dropping or redirecting the action.

    nodes, pending, references = [], [], {}

    def normalize(value):
        if isinstance(value, IndirectObject):
            key = (value.idnum, value.generation)
            if key in page_numbers:
                return {"page_index": page_numbers[key]}
            value = value.get_object()
        # PDF specifies missing indirect objects as null; qpdf writes an
        # explicit null when repairing such a reference.
        if value is None or isinstance(value, NullObject):
            return None
        if isinstance(value, (dict, list)):
            key = id(value)
            if key not in references:
                references[key] = len(nodes)
                nodes.append(None)
                pending.append(value)  # Also retain direct objects for stable id().
            return {"node": references[key]}
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        return {"type": type(value).__name__, "value": str(value)}

    pages = []
    for page in reader.pages:
        # PdfReader resolves inherited resources/boxes while flattening pages.
        pages.append({"page": normalize({k: v for k, v in page.items()
                                          if k != "/Parent"}),
                      "crop": list(map(str, page.cropbox)),
                      "media": list(map(str, page.mediabox)), "rotate": page.rotation})

    signature = {"pages": pages, "metadata": normalize(dict(reader.metadata or {})),
                 "catalog": normalize({k: v for k, v in root.items() if k != "/Pages"}),
                 "nodes": nodes}
    if reader.is_encrypted:
        # qpdf preserves encryption by default. Verify the full encryption
        # dictionary (permissions, recipients/keys, crypt filters) and the
        # permanent document ID, which is part of older encryption keys.
        encryption = dict(reader.trailer["/Encrypt"])
        if "/P" in encryption:
            # The permissions are a 32-bit bitmask; writers may spell the
            # identical bits as a signed or unsigned PDF integer.
            encryption["/P"] = int(encryption["/P"]) & 0xffffffff
        signature["encryption"] = normalize(encryption)
        signature["permanent_id"] = normalize(reader.trailer["/ID"][0])
    for number, value in enumerate(pending):
        if isinstance(value, list):
            nodes[number] = [normalize(v) for v in value]
            continue
        object_type = resolve(value.get("/Type"))
        action = resolve(value.get("/S"))
        if (resolve(value.get("/FT")) == "/Sig" or object_type in ("/Sig", "/DocTimeStamp")
                or "/ByteRange" in value):
            raise UnsupportedPDF("digital signatures require the original file bytes")
        active_keys = sorted(set(value) & {"/XFA", "/AA", "/JavaScript"})
        if active_keys:
            raise UnsupportedPDF("active content or embedded files require additional equivalence checks: " +
                                 ",".join(active_keys))
        if action in ("/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE"):
            raise UnsupportedPDF("non-local action requires additional equivalence checks: " + str(action))
        is_stream = isinstance(value, StreamObject)
        is_xmp = is_stream and object_type == "/Metadata" and resolve(value.get("/Subtype")) == "/XML"
        omitted = {"/Length"} if is_stream else set()
        if is_xmp:
            omitted |= {"/Filter", "/DecodeParms"}
        result = {}
        for key in sorted(value):
            if key in omitted:
                continue
            child = normalize(value.raw_get(key) if hasattr(value, "raw_get") else value[key])
            # A null dictionary entry is absent under PDF semantics. Null
            # array elements retain their position, including broken targets.
            if child is not None:
                result[str(key)] = child
        if is_stream:
            result["stream_sha256"] = hashlib.sha256(value.get_data() if is_xmp else value._data).hexdigest()
        nodes[number] = result
    return signature


def check_supported_structure(path: Path) -> None:
    """Reject structures we cannot prove lossless before starting PDF.js."""
    content_signature(path)


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
    const invalidDestinations = [];
    async function walk(items, parents=[]) {
      for (const [index,item] of (items || []).entries()) {
        const location = [...parents,index];
        entries++;
        let dest = item.dest;
        if (typeof dest === 'string') dest = await pdf.getDestination(dest);
        if (dest && typeof dest[0] === 'object') {
          try { await pdf.getPageIndex(dest[0]); }
          catch (error) {
            const message = String(error?.message);
            const known = ['The reference does not point to a /Page dictionary.',
              "Kid reference not found in parent's kids.",
              'Page dictionary kid reference points to wrong type of object.'];
            if (!known.includes(message)) throw error;
            invalidDestinations.push({location,message});
          }
        }
        await walk(item.items,location);
      }
    }
    await walk(outline); snapshots.startup = await snap();
    await new Promise(r=>setTimeout(r,750)); snapshots.idle = await snap();
    for (const n of [...new Set([Math.min(10,pdf.numPages),Math.ceil(pdf.numPages/2),pdf.numPages])]) {
      await render(n); snapshots['page'+n] = await snap();
    }
    snapshots.jump = await snap();
    await new Promise(r=>setTimeout(r,750)); snapshots.final = await snap();
    return {pages:pdf.numPages,outline_entries:entries,invalid_destinations:invalidDestinations,renders,snapshots};
  };
  let timer;
  try { return await Promise.race([work(),new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(Error('PDF assessment deadline')),90000);
  })]); } catch (error) {
    // PDF.js worker exceptions are plain objects; Playwright otherwise drops
    // their useful message/details and reports only UnknownErrorException.
    throw new Error([error?.name,error?.message,error?.details].filter(Boolean).join(': '));
  } finally { clearTimeout(timer); await task.destroy(); }
}"""


def benchmark(path: Path, vendor: Path, browser=None) -> dict:
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
    def run_page(active_browser):
        page = active_browser.new_page(service_workers="block")
        try:
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
            page.close()
    try:
        if browser is not None:
            return run_page(browser)
        with sync_playwright() as playwright:
            active_browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                return run_page(active_browser)
            finally:
                active_browser.close()
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
    if before.get("invalid_destinations", []) != after.get("invalid_destinations", []):
        return False
    saved = a["startup"]["bytes"] - b["startup"]["bytes"]
    return (saved >= MI and b["startup"]["bytes"] <= a["startup"]["bytes"] * .7
            and output_size <= source_size * 1.05
            and b["startup"]["requests"] <= a["startup"]["requests"]
            and b["jump"]["bytes"] <= a["jump"]["bytes"]
            and b["jump"]["requests"] <= a["jump"]["requests"]
            and b["idle"]["bytes"] == b["startup"]["bytes"]
            and b["final"]["bytes"] == b["jump"]["bytes"])


def strong_improvement(before: dict, after: dict, source_size: int, output_size: int) -> bool:
    return (improvement(before, after, source_size, output_size)
            and after["snapshots"]["startup"]["bytes"] < EARLY_ACCEPT_STARTUP_BYTES)


def run_qpdf(args: list[str], *, timeout: int) -> str:
    """Run qpdf while treating exit 3 as a warning pending later validation."""
    completed = subprocess.run(args, capture_output=True, timeout=timeout)
    if completed.returncode not in {0, 3}:
        raise subprocess.CalledProcessError(
            completed.returncode, args, output=completed.stdout, stderr=completed.stderr)
    if completed.returncode == 3:
        detail = completed.stderr or completed.stdout or b"qpdf warning"
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        return detail[:2000]
    return ""


def failure_details(error: Exception) -> dict:
    """Keep actionable diagnostics instead of losing subprocess/worker details."""
    detail = type(error).__name__ + ": " + str(error)
    if isinstance(error, subprocess.CalledProcessError):
        output = error.stderr or error.stdout or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        detail = f"qpdf exit {error.returncode}: {output}"
        category = "qpdf-error"
    elif isinstance(error, subprocess.TimeoutExpired) or "deadline" in detail.lower() or "Timeout" in detail:
        category = "timeout"
    elif "document content or resource signature differs" in detail:
        category = "content-mismatch"
    elif "cyclic page" in detail.lower():
        category = "cyclic-page-tree"
    elif "Page.evaluate" in detail or "PDF rendering errors" in detail:
        category = "pdfjs-error"
    else:
        category = "assessment-error"
    return {"error_category": category, "error": detail[:1600]}


def reconstruct_pdf(source: Path, target: Path) -> None:
    """Use the independent parser in a bounded process; callers must prove equality."""
    command = [sys.executable, "-c",
               "import sys; from pypdf import PdfWriter; "
               "writer = PdfWriter(clone_from=sys.argv[1]); "
               "writer.write(sys.argv[2]); writer.close()", str(source), str(target)]
    result = subprocess.run(command, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError("PDF reconstruction failed: " + result.stderr.decode("utf-8", "replace")[-1200:])


def assess(source: Path, work: Path, vendor: Path) -> tuple[dict, Path | None]:
    size = source.stat().st_size
    if size < MIN_BYTES:
        return {"status": "unchanged", "reason": "below-4-mib"}, None
    if size > 2 * 1024 * MI:
        return {"status": "unsupported", "reason": "source-exceeds-2-gib"}, None
    signature = content_signature(source)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            before = benchmark(source, vendor, browser)
            report = {"status": "unchanged", "reason": "startup-within-budget", "before": before}
            if not needs_optimization(before, size):
                return report, None
            report.update(status="no-gain", reason="no-passing-candidate", candidates={})
            passing = []
            methods = candidate_methods()
            report["candidate_policy"] = "full" if len(methods) > 1 else "objects-only"

            def evaluate(method, options, conversion_source):
                target = work / (method + ".pdf")
                warnings = []
                after = None
                try:
                    warning = run_qpdf(["qpdf", *options, "--stream-data=preserve", str(conversion_source), str(target)],
                                       timeout=120)
                    if warning:
                        warnings.append(warning)
                    if not target.is_file() or target.stat().st_size == 0:
                        raise RuntimeError("qpdf produced no candidate PDF")
                    warning = run_qpdf(["qpdf", "--check", str(target)], timeout=60)
                    if warning:
                        warnings.append(warning)
                    if "--linearize" in options:
                        warning = run_qpdf(["qpdf", "--check-linearization", str(target)], timeout=60)
                        if warning:
                            warnings.append(warning)
                    after = benchmark(target, vendor, browser)
                    accepted = improvement(before, after, size, target.stat().st_size)
                    if accepted:
                        # Full graph comparison is only needed for a candidate that
                        # already demonstrated a measurable Reader improvement.
                        if content_signature(target) != signature:
                            raise ValueError("document content or resource signature differs")
                        passing.append((after["snapshots"]["startup"]["bytes"],
                                        after["snapshots"]["startup"]["requests"],
                                        after["snapshots"]["jump"]["bytes"], method, target))
                    report["candidates"][method] = {"accepted": accepted, "bytes": target.stat().st_size,
                                                    "measurement": after}
                    if warnings:
                        report["candidates"][method]["qpdf_warnings"] = warnings
                    if (method in {"objects", "reconstructed-objects"}
                            and strong_improvement(before, after, size, target.stat().st_size)):
                        report["candidates"][method]["early_stop"] = True
                        report.update(status="optimized", reason="measured-improvement", method=method)
                        return target
                except Exception as error:
                    candidate = {"accepted": False, **failure_details(error)}
                    if warnings:
                        candidate["qpdf_warnings"] = warnings
                    if after is not None:
                        candidate["measurement"] = after
                    report["candidates"][method] = candidate
                return None

            for method, options in methods.items():
                chosen = evaluate(method, options, source)
                if chosen:
                    return report, chosen
            if (not passing and "encryption" not in signature and
                    any(value.get("error_category") == "qpdf-error" for value in report["candidates"].values())):
                repaired = work / "reconstructed.pdf"
                try:
                    reconstruct_pdf(source, repaired)
                    warning = run_qpdf(["qpdf", "--check", str(repaired)], timeout=60)
                    if content_signature(repaired) != signature:
                        raise ValueError("document content or resource signature differs after reconstruction")
                    report["reconstruction"] = {"validated": True}
                    if warning:
                        report["reconstruction"]["qpdf_warnings"] = [warning]
                    for method, options in methods.items():
                        chosen = evaluate("reconstructed-" + method, options, repaired)
                        if chosen:
                            return report, chosen
                except Exception as error:
                    report["reconstruction"] = {"validated": False, **failure_details(error)}
            if not passing:
                if any("error" in value for value in report["candidates"].values()):
                    report.update(status="failed", reason="candidate-assessment-incomplete")
                return report, None
            chosen = min(passing)
            report.update(status="optimized", reason="measured-improvement", method=chosen[3])
            return report, chosen[4]
        finally:
            browser.close()
