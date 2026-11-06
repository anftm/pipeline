"""Report executed UTF-16 source units from V8 profiles, for every JS entry.

Usage: python tests/js_coverage_report.py RAW_DIRECTORY

Raw profiles are produced by Chromium Profiler.takePreciseCoverage and by
NODE_V8_COVERAGE for the service worker. Browser and worker source must be the
unminified files in this checkout. Nested V8 ranges are resolved by taking
the most specific enclosing range, then merged across independently run tests.
The denominator includes the *entire* source of app.js, tts-client.js and sw.js,
including whitespace and files never loaded. Missing profiles count as zero and
are called out explicitly. This is a source-character metric,
not statement, line, branch, or condition coverage.
"""
from __future__ import annotations

import heapq
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ("app.js", "tts-client.js", "sw.js")


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def profile_mask(script: dict, length: int) -> bytearray:
    """Resolve nested function/block ranges without counting uncalled bodies."""
    events = []
    sequence = 0
    for function in script.get("functions", []):
        for item in function.get("ranges", []):
            start, end, count = item["startOffset"], item["endOffset"], item["count"]
            if not 0 <= start <= end <= length:
                raise ValueError(f"coverage range outside source: {start}:{end}/{length}")
            if start == end:
                continue
            # More specific (shorter) wins; break equal spans by later start
            # and later occurrence, never by count (which could overstate hit).
            priority = (end - start, -start, -sequence)
            events.append((start, 1, priority, sequence, count))
            events.append((end, -1, priority, sequence, count))
            sequence += 1
    events.sort(key=lambda event: (event[0], event[1]))
    active: set[int] = set()
    heap = []
    mask = bytearray(length)
    index = 0
    previous = 0
    while index < len(events):
        position = events[index][0]
        while heap and heap[0][1] not in active:
            heapq.heappop(heap)
        if position > previous and heap and heap[0][2] > 0:
            mask[previous:position] = b"\x01" * (position - previous)
        while index < len(events) and events[index][0] == position:
            _, action, priority, ident, count = events[index]
            if action < 0:
                active.discard(ident)
            else:
                active.add(ident)
                heapq.heappush(heap, (priority, ident, count))
            index += 1
        previous = position
    return mask


def main(directory: Path) -> None:
    manifest_path = directory / "capture-manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("missing capture-manifest.json; run tests/capture_js_coverage.py first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in ENTRIES:
        actual = hashlib.sha256((ROOT / "static" / name).read_bytes()).hexdigest()
        if manifest["source_sha256"].get(name) != actual:
            raise SystemExit(f"source changed after capture: static/{name}")
    totals = {name: utf16_length((ROOT / "static" / name).read_text(encoding="utf-8")) for name in ENTRIES}
    masks = {name: bytearray(length) for name, length in totals.items()}
    scripts = {name: 0 for name in ENTRIES}
    paths = sorted([*directory.glob("browser-*.json"), *directory.glob("coverage-*.json")])
    if not paths:
        raise SystemExit(f"no profiles found in {directory}")
    for path in paths:
        for script in json.loads(path.read_text(encoding="utf-8"))["result"]:
            url = script.get("url", "")
            parsed = urlsplit(url)
            script_path = unquote(parsed.path)
            name = Path(script_path).name
            if name not in totals or not script_path.endswith("/static/" + name):
                continue
            expected = manifest["source_sha256"][name]
            if path.name.startswith("browser-"):
                if script.get("source_sha256") != expected:
                    raise ValueError(f"unverified or mismatched browser script: {url}")
            else:
                # Node's coverage output contains no source digest. Only accept
                # the exact local worker path, with source checked by manifest.
                if name != "sw.js" or parsed.scheme != "file" or Path(script_path).resolve() != (ROOT / "static" / name).resolve():
                    raise ValueError(f"unverified node script path: {url}")
            mask = profile_mask(script, totals[name])
            target = masks[name]
            for i, value in enumerate(mask):
                target[i] |= value
            scripts[name] += 1
    print("V8 UTF-16 source-unit coverage (whole source incl. whitespace; not statements/branches)")
    for name in ENTRIES:
        covered, total = sum(masks[name]), totals[name]
        missing = " MISSING PROFILE (counted as zero)" if scripts[name] == 0 else ""
        print(f"{name:16} {covered:8}/{total:<8} {100 * covered / total:6.2f}%  profiles={scripts[name]}{missing}")
    covered, total = sum(sum(mask) for mask in masks.values()), sum(totals.values())
    print(f"{'TOTAL':16} {covered:8}/{total:<8} {100 * covered / total:6.2f}%")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
