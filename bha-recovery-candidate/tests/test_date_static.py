"""Check the exact date parser section, including a real injected type error."""
import json
import random
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "// BEGIN STRICT DATE TERMS"
END = "// END STRICT DATE TERMS"


class DateTermStaticTests(unittest.TestCase):
    def test_real_date_code_is_strict(self):
        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        self.assertEqual(source.count(BEGIN), 1)
        self.assertEqual(source.count(END), 1)
        html_begin, html_end = "// BEGIN STRICT HTML ESCAPE", "// END STRICT HTML ESCAPE"
        self.assertEqual(source.count(html_begin), 1)
        self.assertEqual(source.count(html_end), 1)
        sections = [(BEGIN, END), (html_begin, html_end),
                    ("// BEGIN STRICT DATE RANGES", "// END STRICT DATE RANGES"),
                    ("// BEGIN STRICT DATE INPUT UPDATE", "// END STRICT DATE INPUT UPDATE"),
                    ("// BEGIN STRICT PREVIEW HIGHLIGHT", "// END STRICT PREVIEW HIGHLIGHT"),
                    ("// BEGIN STRICT FACET TYPES", "// END STRICT FACET TYPES"),
                    ("// BEGIN STRICT SEARCH RESPONSE TYPES", "// END STRICT SEARCH RESPONSE TYPES"),
                    ("// BEGIN STRICT SEARCH RESPONSE CONTRACTS", "// END STRICT SEARCH RESPONSE CONTRACTS"),
                    ("// BEGIN STRICT PREVIEW RESPONSE CONTRACTS", "// END STRICT PREVIEW RESPONSE CONTRACTS"),
                    ("// BEGIN STRICT FACET KEYS", "// END STRICT FACET KEYS"),
                    ("// BEGIN STRICT STABLE STRINGIFY", "// END STRICT STABLE STRINGIFY"),
                    ("// BEGIN STRICT PREVIEW CACHE", "// END STRICT PREVIEW CACHE"),
                    ("// BEGIN STRICT PREVIEW DOWNLOAD LINKS", "// END STRICT PREVIEW DOWNLOAD LINKS"),
                    ("// BEGIN STRICT PREVIEW METADATA", "// END STRICT PREVIEW METADATA"),
                    ("// BEGIN STRICT PREVIEW TAG PAGER", "// END STRICT PREVIEW TAG PAGER"),
                    ("// BEGIN STRICT PDF RANGE", "// END STRICT PDF RANGE"),
                    ("// BEGIN STRICT COMPARISON DIFF", "// END STRICT COMPARISON DIFF"),
                    ("// BEGIN STRICT APPLY TEXT DELTA", "// END STRICT APPLY TEXT DELTA"),
                    ("// BEGIN STRICT CREATE TEXT DELTA", "// END STRICT CREATE TEXT DELTA"),
                    ("// BEGIN STRICT PROOFREAD COMPARISON", "// END STRICT PROOFREAD COMPARISON"),
                    ("// BEGIN STRICT PROOFREAD LINE ANCHORS", "// END STRICT PROOFREAD LINE ANCHORS"),
                    ("// BEGIN STRICT COMMENT PIVOTS", "// END STRICT COMMENT PIVOTS"),
                    ("// BEGIN STRICT PARSE COVERAGE", "// END STRICT PARSE COVERAGE"),
                    ("// BEGIN STRICT PARSE OCR ROWS", "// END STRICT PARSE OCR ROWS"),
                    ("// BEGIN STRICT DOM TYPES", "// END STRICT DOM TYPES"),
                    ("// BEGIN STRICT DOM BINDINGS", "// END STRICT DOM BINDINGS")]
        sections.append(("// BEGIN STRICT MOTION", "// END STRICT MOTION"))
        for begin, end in sections:
            self.assertEqual(source.count(begin), 1)
            self.assertEqual(source.count(end), 1)
        state = """const STATE = {q:'',exact:true,previewHighlight:true,dateFromYear:'',dateFromMonth:'',dateFromDay:'',dateToYear:'',dateToMonth:'',dateToDay:'', dateTerms: /** @type {DateTerm[]} */ ([])};
/** @type {Map<string,{value:ValidatedPreview,time:number}>} */ const previewCache = new Map();
let previewCacheChars=0;const PREVIEW_CACHE_TTL=100,PREVIEW_CACHE_LIMIT=20,PREVIEW_CACHE_MAX_CHARS=100000;
function showToast(/** @type {string} */ message) {}\n"""
        state += "function motionAllowed(){return true;}\n/** @type {((event: Event) => void) | null} */ let previewAnimationCleanup=null;\n"
        block = state + "\n".join(source[source.index(begin):source.index(end) + len(end)] for begin, end in sections)
        self.assertNotIn("@ts-ignore", block)
        self.assertNotIn("@ts-nocheck", block)
        with tempfile.TemporaryDirectory(prefix="date-types-") as directory:
            root = Path(directory)
            entry = root / "date.js"
            entry.write_text(block, encoding="utf-8")
            (root / "tsconfig.json").write_text(json.dumps({
                "compilerOptions": {"allowJs": True, "checkJs": True, "strict": True,
                                    "noEmit": True, "target": "ES2022", "lib": ["ES2022", "DOM"]},
                "files": ["date.js"],
            }))
            cmd = ("node", str(ROOT / "node_modules/typescript/bin/tsc"), "-p", str(root / "tsconfig.json"), "--pretty", "false")
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            faults = (("function dateTermParam(term) {", "function dateTermParam(term) {\n  term.year = 12;"),
                      ('function updateDateInputState(key, value) {', 'function updateDateInputState(key, value) {\n  key = "bad-key";'),
                      ('searchInput: requireDom("search-input", HTMLInputElement)', 'searchInput: requireDom("search-input", HTMLButtonElement)'),
                      ("function highlightPreviewText(text) {", "function highlightPreviewText(text) {\n  return 7;"),
                      ('include: kind === "source" ? "sources"', 'include: kind === "source" ? "sourceWrong"'),
                      ('function stableStringify(value) {', 'function stableStringify(value) {\n  return 42;'),
                      ('function parseSearchResponse(value) {', 'function parseSearchResponse(value) {\n  return 42;'),
                      ('function parsePreviewResponse(value, expectedId) {', 'function parsePreviewResponse(value, expectedId) {\n  return 42;'),
                      ('async function readPDFRange(response, begin, end, expectedLength = null) {',
                       'async function readPDFRange(response, begin, end, expectedLength = null) {\n  begin = "invalid";'),
                      ('function splitDiffLines(text) {', 'function splitDiffLines(text) {\n  return 42;'),
                      ('function applyTextDelta(original, delta) {', 'function applyTextDelta(original, delta) {\n  return 42;'),
                      ('function textDelta(original, edited) {', 'function textDelta(original, edited) {\n  return 42;'),
                      ('function getCachedPreview(docId) {', 'function getCachedPreview(docId) {\n  return 42;'),
                      ('function previewDownloadHTML(item) {', 'function previewDownloadHTML(item) {\n  return 42;'),
                      ('function previewTitleHTML(item) {', 'function previewTitleHTML(item) {\n  return 42;'),
                      ('function changePreviewTagPage(button) {', 'function changePreviewTagPage(button) {\n  button = 42;'),
                      ('function animateResultRows(rows) {', 'function animateResultRows(rows) {\n  rows = 42;'),
                      ('function deepSame(left, right) {', 'function deepSame(left, right) {\n  return 42;'),
                      ('function matchingLineAnchors(current, desired) {', 'function matchingLineAnchors(current, desired) {\n  return 42;'),
                      ('function partTextWithPivots(article, partIndex) {', 'function partTextWithPivots(article, partIndex) {\n  partIndex = "wrong";'),
                      ('function parseCoverageError(articles, maxPage) {', 'function parseCoverageError(articles, maxPage) {\n  maxPage = "wrong";'),
                      ('function parseOCRText(data, thresholds = [0, 0, 0, 0]) {', 'function parseOCRText(data, thresholds = [0, 0, 0, 0]) {\n  return 42;'))
            for before, after in faults:
                with self.subTest(function=before):
                    self.assertEqual(block.count(before), 1)
                    entry.write_text(block.replace(before, after, 1), encoding="utf-8")
                    result = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("error TS", result.stdout)

    def test_generated_utf16_deltas_match_backend(self):
        from app.proofread_core import apply_text_delta, article_part_text, delta_edit_cost

        source = (ROOT / "static/app.js").read_text(encoding="utf-8")
        begin, end = "// BEGIN STRICT CREATE TEXT DELTA", "// END STRICT CREATE TEXT DELTA"
        self.assertEqual(source.count(begin), 1)
        self.assertEqual(source.count(end), 1)
        code = source[source.index(begin):source.index(end) + len(end)]
        rng = random.Random(20261102)
        alphabet = ["甲", "乙", "\t", "%", "😀", "𠮷", "+", "文", " ", "\n"]
        cases = [["".join(rng.choice(alphabet) for _ in range(rng.randrange(24))),
                  "".join(rng.choice(alphabet) for _ in range(rng.randrange(24)))] for _ in range(200)]
        program = ("const fs=require('fs'),vm=require('vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));"
                   "const s={window:{}};vm.runInNewContext(x.code,s);"
                   "process.stdout.write(JSON.stringify(x.cases.map(([a,b])=>s.textDelta(a,b))))")
        result = subprocess.run(("node", "-e", program), input=json.dumps({"code": code, "cases": cases}, ensure_ascii=False),
                                text=True, capture_output=True, timeout=30, check=True)
        deltas = json.loads(result.stdout)
        self.assertEqual(len(deltas), len(cases))
        for (original, edited), delta in zip(cases, deltas):
            if original == edited:
                self.assertEqual(delta, "")
                continue
            self.assertIsNotNone(delta_edit_cost(delta, original))
            self.assertEqual(apply_text_delta(original, delta), edited)

        p_begin, p_end = "// BEGIN STRICT COMMENT PIVOTS", "// END STRICT COMMENT PIVOTS"
        self.assertEqual(source.count(p_begin), 1)
        self.assertEqual(source.count(p_end), 1)
        code = source[source.index(p_begin):source.index(p_end) + len(p_end)]
        articles = []
        for _ in range(100):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(24)))
            offsets = rng.sample(range(len(text) + 1), k=min(rng.randrange(5), len(text) + 1))
            articles.append({"parts": [{"text": text}], "comment_pivots": [
                {"part_idx": 0, "offset": offset, "index": index + 1} for index, offset in enumerate(offsets)
            ]})
        program = ("const fs=require('fs'),vm=require('vm');const x=JSON.parse(fs.readFileSync(0,'utf8'));"
                   "const s={};vm.runInNewContext(x.code,s);"
                   "process.stdout.write(JSON.stringify(x.articles.map(a=>s.partTextWithPivots(a,0))))")
        result = subprocess.run(("node", "-e", program), input=json.dumps({"code": code, "articles": articles}, ensure_ascii=False),
                                text=True, capture_output=True, timeout=30, check=True)
        generated = json.loads(result.stdout)
        self.assertEqual(generated, [article_part_text(article, 0) for article in articles])


if __name__ == "__main__":
    unittest.main()
