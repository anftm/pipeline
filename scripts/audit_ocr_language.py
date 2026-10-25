#!/usr/bin/env python3
"""Audit published OCR language and text usability, read-only by default.

The gate answers two questions per OCR book before its text is trusted:

* does the published text still look like the declared language
  (Cyrillic for ru, kana for japan, Han for ch, Latin for en);
* is there enough text to be useful at all (density, empty pages).

Flagged books are listed, never edited. ``--overrides-out`` writes a draft
exact-key override file for the confirmed language mismatches; applying it is
a separate, reviewed step. ``--probe-images N`` resolves books whose text is
empty by reading the first N rendered pages with tesseract OSD.
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRIPT_LANGUAGES = {
    "Cyrillic": {"ru", "be", "uk", "bg", "sr", "mk", "kk", "ky", "tg", "mn"},
    "Han": {"ch", "chinese_cht"},
    "Japanese": {"japan"},
    "Korean": {"korean"},
    "Arabic": {"ar", "fa", "ur", "ps", "ug"},
    "Greek": {"el"},
    "Hebrew": {"he"},
    "Thai": {"th"},
    "Latin": {"en", "fr", "de", "es", "it", "pt", "nl", "pl", "cs", "tr", "vi",
              "id", "ms", "sv", "da", "no", "fi", "hu", "ro", "hr", "sk", "sl"},
}
LANGUAGE_SCRIPTS = {language: script for script, languages in SCRIPT_LANGUAGES.items()
                    for language in languages}
MIN_USEFUL_CHARS = 200
MIN_PAGE_CHARS = 20
MAX_EMPTY_PAGE_RATIO = 0.5
MIN_SCRIPT_SHARE = {"ru": 0.05, "japan": 0.01, "korean": 0.02, "ar": 0.05, "fa": 0.05,
                    "el": 0.05, "he": 0.05, "th": 0.05, "ch": 0.05, "en": 0.30}


def script_counts(text: str) -> dict[str, int]:
    return {
        "Cyrillic": sum("\u0400" <= c <= "\u052f" for c in text),
        "Han": sum(("\u3400" <= c <= "\u9fff") or ("\uf900" <= c <= "\ufaff") for c in text),
        "Japanese": sum("\u3040" <= c <= "\u30ff" for c in text),
        "Korean": sum("\uac00" <= c <= "\ud7af" for c in text),
        "Arabic": sum("\u0600" <= c <= "\u06ff" for c in text),
        "Greek": sum("\u0370" <= c <= "\u03ff" for c in text),
        "Hebrew": sum("\u0590" <= c <= "\u05ff" for c in text),
        "Thai": sum("\u0e00" <= c <= "\u0e7f" for c in text),
        "Latin": sum(("A" <= c <= "Z") or ("a" <= c <= "z") for c in text),
    }


def observed_script(counts: dict[str, int]) -> tuple[str | None, float]:
    total = sum(counts.values())
    if not total:
        return None, 0.0
    # Han pages frequently contain a few kana; count a page as Japanese only
    # when kana compete with Han instead of being a stray glyph.
    counts = dict(counts)
    if counts["Japanese"] and counts["Japanese"] * 10 < counts["Han"]:
        counts.pop("Japanese")
    script, value = max(counts.items(), key=lambda item: item[1])
    return script, value / total


def judge(key: str, entry: dict, pages: list[str]) -> dict:
    text = "".join(pages)
    counts = script_counts(text)
    script, share = observed_script(counts)
    language = str(entry.get("ocr_language") or entry.get("language") or "").strip().lower()
    expected = LANGUAGE_SCRIPTS.get(language)
    flags = []
    total = sum(counts.values())
    if total < MIN_USEFUL_CHARS:
        flags.append("low-text-density")
    empty = sum(1 for page in pages if len(page.strip()) < MIN_PAGE_CHARS)
    if pages and empty / len(pages) > MAX_EMPTY_PAGE_RATIO:
        flags.append("mostly-empty-pages")
    if expected and script and share:
        same_family = script == expected
        if not same_family:
            minimum = MIN_SCRIPT_SHARE.get(language, 0.05)
            own = counts.get(expected, 0) / total if total else 0
            if own < minimum:
                flags.append("script-mismatch")
    elif expected and not script:
        flags.append("no-detectable-text")
    return {"key": key, "declared_language": language or None, "expected_script": expected,
            "observed_script": script, "observed_share": round(share, 4),
            "chars": total, "pages": len(pages), "empty_pages": empty,
            "counts": counts, "flags": flags}


def audit(entries: dict, texts: dict, limit: int | None = None) -> dict:
    books, flagged = {}, {}
    for index, key in enumerate(sorted(entries)):
        if limit is not None and index >= limit:
            break
        entry = entries[key]
        if entry.get("status") != "ready" or not texts.get(key):
            continue
        row = judge(key, entry, texts[key])
        books[key] = row
        if row["flags"]:
            flagged[key] = row
    return {"version": 1, "kind": "ocr-language-audit",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "checked": len(books), "flagged": len(flagged),
            "books": books, "flags": flagged}


def select_flag_updates(entries: dict, report: dict) -> list[dict]:
    """Registry entries to mark as language_suspect, never rewriting text."""
    updates = []
    for key, row in report.get("flags", {}).items():
        entry = entries.get(key)
        if not isinstance(entry, dict) or entry.get("status") != "ready":
            continue
        if "script-mismatch" not in row.get("flags", []):
            continue
        if entry.get("language_suspect"):
            continue
        updates.append({**entry, "language_suspect": True,
                        "language_check": {"observed_script": row.get("observed_script"),
                                           "share": row.get("observed_share")}})
    return updates


def draft_overrides(flagged: dict, entries: dict) -> dict:
    overrides = {}
    for key, row in flagged.items():
        if "script-mismatch" not in row["flags"] or not row["observed_script"]:
            continue
        candidates = SCRIPT_LANGUAGES.get(row["observed_script"]) or set()
        current = row["declared_language"]
        if not candidates or current in candidates:
            continue
        target = "ch" if row["observed_script"] == "Han" else (
            "japan" if row["observed_script"] == "Japanese" else (
                "ru" if row["observed_script"] == "Cyrillic" else (
                    "en" if row["observed_script"] == "Latin" else sorted(candidates)[0])))
        overrides[key] = {"language": target, "source": "text-audit-v1",
                          "evidence": {"script": row["observed_script"],
                                       "share": row["observed_share"]}}
    return {"version": 1, "kind": "ocr-language-overrides", "overrides": overrides}


def probe_pages(store, render: dict, key: str, pages: int = 2) -> list[str]:
    entry = render.get(key) or {}
    if not entry.get("page_manifest") or not entry.get("page_count"):
        return []
    root = entry["page_manifest"]["path"].rsplit("/", 1)[0]
    count = entry["page_count"]
    scripts = []
    for percent in (25, 60):
        page = max(1, min(count, round(count * percent / 100)))
        try:
            raw = store.read_bytes("vomebook/pdf-pages-v2", f"{root}/pages/page-{page:06d}.webp")
        except FileNotFoundError:
            continue
        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as handle:
            handle.write(raw)
            path = Path(handle.name)
        try:
            result = subprocess.run(["tesseract", str(path), "stdout", "--psm", "0", "-l", "osd"],
                                    capture_output=True, text=True, timeout=120)
            for line in result.stdout.splitlines():
                if line.startswith("Script:"):
                    scripts.append(line.split(":", 1)[1].strip())
                    break
        finally:
            path.unlink(missing_ok=True)
    return scripts


def frame_lines(path, width: int = 800, threshold: int = 200, ratio: float = 0.5) -> dict:
    """Count long straight lines that frame a page into boxes.

    A boxed article page (subscription notices, newspaper panels) makes the
    recognizer merge text across borders; text density and language alone do
    not reveal that. Count runs of dark pixels longer than ``ratio`` of the
    page edge instead of dark density, which text lines also produce.
    """
    import numpy as np
    from PIL import Image
    image = Image.open(path).convert("L")
    if image.width > width:
        image = image.resize((width, max(1, round(image.height * width / image.width))),
                             Image.Resampling.BILINEAR)
    dark = np.asarray(image) < threshold
    height, width_px = dark.shape

    def longest(row):
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
        return int((edges[1::2] - edges[0::2]).max()) if len(edges) else 0

    horizontal = sum(1 for y in range(height) if longest(dark[y]) >= ratio * width_px)
    vertical = sum(1 for x in range(width_px) if longest(dark[:, x]) >= ratio * height)
    return {"horizontal": horizontal, "vertical": vertical,
            "boxed": horizontal + vertical >= 8 and (horizontal >= 6 or vertical >= 4)}


def page_frames(store, render: dict, key: str, percents=(25, 60)) -> list[dict]:
    entry = render.get(key) or {}
    if not entry.get("page_manifest") or not entry.get("page_count"):
        return []
    root = entry["page_manifest"]["path"].rsplit("/", 1)[0]
    count = entry["page_count"]
    found = []
    for percent in percents:
        page = max(1, min(count, round(count * percent / 100)))
        try:
            raw = store.read_bytes("vomebook/pdf-pages-v2", f"{root}/pages/page-{page:06d}.webp")
        except FileNotFoundError:
            continue
        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as handle:
            handle.write(raw)
            path = Path(handle.name)
        try:
            found.append({"page": page, **frame_lines(path)})
        finally:
            path.unlink(missing_ok=True)
    return found


def decode(raw: bytes) -> bytes:
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def load_texts(store, entries: dict, limit: int | None) -> dict:
    texts = {}
    for index, key in enumerate(sorted(entries)):
        if limit is not None and index >= limit:
            break
        entry = entries[key]
        if entry.get("status") != "ready":
            continue
        try:
            if entry.get("text_layer") is not None:
                layer = json.loads(decode(store.read_bytes(
                    entry["text_layer"].get("bucket", "vomebook/reader-assets-v2"),
                    entry["text_layer"]["path"])))
                ref = layer["book_text"]
                document = json.loads(decode(store.read_bytes(
                    ref.get("bucket", "vomebook/reader-assets-v2"), ref["path"])))
            elif entry.get("book_text"):
                document = json.loads(decode(store.read_bytes(
                    "vomebook/pdf-pages-v2", entry["book_text"]["path"])))
            else:
                continue
            texts[key] = [str(page.get("text") or "") for page in document.get("pages", [])]
        except (FileNotFoundError, KeyError, ValueError):
            continue
    return texts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("output/ocr-language-audit.json"))
    parser.add_argument("--overrides-out", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--frame-limit", type=int, default=0,
                        help="sample rendered pages for boxed layout on this many books")
    parser.add_argument("--probe-limit", type=int, default=0,
                        help="OSD-probe this many text-less books from rendered pages")
    parser.add_argument("--apply-flags", action="store_true",
                        help="write language_suspect into the OCR registry (writer lock required)")
    parser.add_argument("--upload", action="store_true",
                        help="write the report to reader-index/ocr_language_audit.json")
    args = parser.parse_args()
    from scripts.reader_bucket_store import HubBucketStore
    store = HubBucketStore()
    raw = decode(store.read_bytes("vomebook/reader-assets-v2", "reader-index/pdf_ocr_manifest.json"))
    entries = dict(sorted(json.loads(raw)["files"].items())[args.offset:])
    texts = load_texts(store, entries, args.limit)
    report = audit(entries, texts, args.limit)
    if args.probe_limit:
        render = json.loads(decode(store.read_bytes(
            "vomebook/reader-assets-v2", "reader-index/pdf_render_manifest.json")))["files"]
        probed = {}
        for key, entry in entries.items():
            if len(probed) >= args.probe_limit:
                break
            if entry.get("status") != "ready" or key in texts:
                continue
            scripts = probe_pages(store, render, key)
            if not scripts:
                continue
            script = Counter(scripts).most_common(1)[0][0]
            expected = LANGUAGE_SCRIPTS.get(str(entry.get("ocr_language") or "").lower())
            probed[key] = {"script": script, "mismatch": bool(expected and expected != script)}
        report["osd_probe"] = probed
        report["osd_mismatch"] = sum(1 for value in probed.values() if value["mismatch"])
    if args.frame_limit:
        render = json.loads(decode(store.read_bytes(
            "vomebook/reader-assets-v2", "reader-index/pdf_render_manifest.json")))["files"]
        frames = {}
        for key, entry in entries.items():
            if len(frames) >= args.frame_limit:
                break
            if entry.get("status") != "ready":
                continue
            pages = page_frames(store, render, key)
            if not pages:
                continue
            if any(page["boxed"] for page in pages):
                frames[key] = pages
        report["boxed_pages"] = frames
        # Boxed pages are flagged for review, not blocked: tables and forms
        # legitimately use frames, and only recognition sampling can tell
        # whether the text survived. Never auto re-OCR from this signal.
        report["layout_risk"] = {
            key: {"reason": "boxed-pages",
                  "pages": [page["page"] for page in pages if page["boxed"]]}
            for key, pages in frames.items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    if args.overrides_out:
        args.overrides_out.write_text(json.dumps(draft_overrides(report["flags"], entries),
                                                 ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    if args.apply_flags:
        import os
        from huggingface_hub import HfApi
        from scripts import publish_pdf_ocr_assets as publication
        updates = select_flag_updates(entries, report)
        if updates:
            publication.publish(HfApi(token=os.environ["HF_TOKEN"]), "vomebook/Reader-Assets", updates)
        report["flagged_applied"] = len(updates)
    if args.upload:
        store.put_json("vomebook/reader-assets-v2", "reader-index/ocr_language_audit.json", report)
    print(json.dumps({"checked": report["checked"], "flagged": report["flagged"],
                      "flags": Counter(flag for row in report["flags"].values()
                                       for flag in row["flags"]).most_common()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
