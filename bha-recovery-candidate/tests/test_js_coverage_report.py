"""The V8 source-unit reporter must not count uncalled nested ranges."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import js_coverage_report
from tests.js_coverage_report import profile_mask, utf16_length


class CoverageReportTests(unittest.TestCase):
    def test_uncalled_function_and_nested_branches(self):
        script = {"functions": [
            {"ranges": [{"startOffset": 0, "endOffset": 20, "count": 1}]},
            {"ranges": [{"startOffset": 2, "endOffset": 12, "count": 0}]},
            {"ranges": [{"startOffset": 12, "endOffset": 20, "count": 1},
                        {"startOffset": 14, "endOffset": 17, "count": 0}]},
        ]}
        self.assertEqual(list(profile_mask(script, 20)), [1, 1] + [0] * 10 + [1, 1] + [0] * 3 + [1] * 3)

    def test_non_bmp_and_invalid_ranges(self):
        self.assertEqual(utf16_length("甲😀乙"), 4)
        with self.assertRaises(ValueError):
            profile_mask({"functions": [{"ranges": [{"startOffset": 0, "endOffset": 5, "count": 1}]}]}, 4)

    def test_missing_file_profile_is_counted_as_zero_in_total(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            static = root / "static"
            static.mkdir()
            for name in js_coverage_report.ENTRIES:
                (static / name).write_text("1234567890", encoding="utf-8")
            manifest = {name: hashlib.sha256((static / name).read_bytes()).hexdigest() for name in js_coverage_report.ENTRIES}
            (root / "capture-manifest.json").write_text(json.dumps({"source_sha256": manifest}), encoding="utf-8")
            profile = {"result": [{"url": "http://example/static/app.js", "source_sha256": manifest["app.js"], "functions": [
                {"ranges": [{"startOffset": 0, "endOffset": 10, "count": 1}]}
            ]}]}
            (root / "browser-test.json").write_text(json.dumps(profile), encoding="utf-8")
            output = io.StringIO()
            with patch.object(js_coverage_report, "ROOT", root), contextlib.redirect_stdout(output):
                js_coverage_report.main(root)
            self.assertIn("MISSING PROFILE (counted as zero)", output.getvalue())
            self.assertIn("33.33%", output.getvalue())
            profile["result"][0]["source_sha256"] = "0" * 64
            (root / "browser-test.json").write_text(json.dumps(profile), encoding="utf-8")
            with patch.object(js_coverage_report, "ROOT", root), self.assertRaisesRegex(ValueError, "mismatched browser script"):
                js_coverage_report.main(root)
            del profile["result"][0]["source_sha256"]
            (root / "browser-test.json").write_text(json.dumps(profile), encoding="utf-8")
            with patch.object(js_coverage_report, "ROOT", root), self.assertRaisesRegex(ValueError, "unverified"):
                js_coverage_report.main(root)
            (root / "browser-test.json").unlink()
            profile["result"][0]["url"] = "file:///other/static/sw.js"
            (root / "coverage-test.json").write_text(json.dumps(profile), encoding="utf-8")
            with patch.object(js_coverage_report, "ROOT", root), self.assertRaisesRegex(ValueError, "unverified node"):
                js_coverage_report.main(root)
            (static / "app.js").write_text("changed", encoding="utf-8")
            with patch.object(js_coverage_report, "ROOT", root), self.assertRaisesRegex(SystemExit, "source changed"):
                js_coverage_report.main(root)


if __name__ == "__main__":
    unittest.main()
