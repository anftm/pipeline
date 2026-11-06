"""Run browser tests and record Chromium V8 ranges before navigation/close.

Usage (from repository root; directory must be empty):
  python -B tests/capture_js_coverage.py /path/to/empty-dir tests.test_browser
  NODE_V8_COVERAGE=/path/to/empty-dir node tests/test_service_worker.cjs
  python -B tests/js_coverage_report.py /path/to/empty-dir

Use the normal test environment (APP_ROOT or CCRD_BROWSER_BASE_URL); tests may
mock APIs, audio, or mobile devices as documented in those tests. The scripts
measure local unminified sources, not the deployed/minified browser bundle.
"""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
import uuid
from pathlib import Path

from playwright.sync_api import BrowserContext, Page

ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ("app.js", "tts-client.js", "sw.js")


def hashes():
    return {name: hashlib.sha256((ROOT / "static" / name).read_bytes()).hexdigest() for name in ENTRIES}


def run(directory: Path, modules: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise SystemExit(f"capture directory must be empty: {directory}")
    before = hashes()
    sessions = {}
    source_hashes = {}
    failures = []
    original_new_page, original_close = BrowserContext.new_page, BrowserContext.close
    original_goto, original_reload = Page.goto, Page.reload

    def checkpoint(page):
        session = sessions.get(id(page))
        if not session:
            return
        try:
            profile = session.send("Profiler.takePreciseCoverage")
            # Associate ranges with the actual loaded source, not just a URL.
            # A stale cache or intercepted/mismatched asset must never be
            # reported as coverage of this checkout's production script.
            for script in profile["result"]:
                url = script.get("url", "").split("?", 1)[0]
                if not any(url.endswith("/static/" + name) for name in ENTRIES):
                    continue
                digest = source_hashes.get(id(page), {}).get(script["scriptId"])
                if digest is None:
                    text = session.send("Debugger.getScriptSource", {"scriptId": script["scriptId"]})["scriptSource"]
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                script["source_sha256"] = digest
            (directory / f"browser-{uuid.uuid4().hex}.json").write_text(json.dumps(profile), encoding="utf-8")
        except Exception as exc:
            failures.append(f"checkpoint: {exc}")

    def new_page(context, *args, **kwargs):
        page = original_new_page(context, *args, **kwargs)
        try:
            session = context.new_cdp_session(page)
            source_hashes[id(page)] = {}
            def remember_script(event):
                url = event.get("url", "").split("?", 1)[0]
                if not any(url.endswith("/static/" + name) for name in ENTRIES):
                    return
                try:
                    text = session.send("Debugger.getScriptSource", {"scriptId": event["scriptId"]})["scriptSource"]
                    source_hashes[id(page)][event["scriptId"]] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                except Exception:
                    # A late scriptParsed callback can race page closure. Any
                    # script actually present in coverage still needs a hash at
                    # checkpoint or that checkpoint fails below.
                    pass
            session.on("Debugger.scriptParsed", remember_script)
            session.send("Debugger.enable")
            session.send("Profiler.enable")
            session.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})
            sessions[id(page)] = session
        except Exception as exc:
            failures.append(f"start: {exc}")
        return page

    def goto(page, *args, **kwargs):
        checkpoint(page)
        return original_goto(page, *args, **kwargs)

    def reload(page, *args, **kwargs):
        checkpoint(page)
        return original_reload(page, *args, **kwargs)

    def close(context, *args, **kwargs):
        for page in context.pages:
            checkpoint(page)
            session = sessions.pop(id(page), None)
            source_hashes.pop(id(page), None)
            if session:
                try:
                    session.send("Profiler.stopPreciseCoverage")
                except Exception as exc:
                    failures.append(f"stop: {exc}")
        return original_close(context, *args, **kwargs)

    BrowserContext.new_page, BrowserContext.close = new_page, close
    Page.goto, Page.reload = goto, reload
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        BrowserContext.new_page, BrowserContext.close = original_new_page, original_close
        Page.goto, Page.reload = original_goto, original_reload
    if sessions:
        failures.append(f"unclosed instrumented pages: {len(sessions)}")
    if hashes() != before:
        failures.append("production JS sources changed during capture")
    print("coverage instrumentation failures:", failures, flush=True)
    if failures or not result.wasSuccessful():
        raise SystemExit(1)
    (directory / "capture-manifest.json").write_text(json.dumps({
        "source_sha256": before, "test_modules": modules,
        "tests_run": result.testsRun, "skipped": len(result.skipped),
        "method": "V8 precise block ranges; checkpoint before navigation and close",
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    # unittest module imports must resolve the repository's tests package.
    sys.path.insert(0, str(ROOT))
    run(Path(sys.argv[1]), sys.argv[2:])
