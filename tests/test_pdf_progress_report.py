import hashlib
import unittest

from scripts import pdf_progress_report as progress


class ProgressTests(unittest.TestCase):
    def test_duplicates_overlaps_and_unknown_progress_do_not_inflate_totals(self):
        entry = {"source_sha256": "a" * 64, "page_count": 12, "status": "ready",
                 "profile": "ocr-v1", "render_manifest": {"sha256": "b" * 64}}
        generation = hashlib.sha256(("b" * 64 + "ocr-v1").encode()).hexdigest()
        checkpoint = {"generation": generation, "pages": {"2": {"p": 2}, "3": {"p": 3}}}
        report = progress.summarize({"first": entry, "alias": entry}, {"first": entry},
            {"first": {**entry, "ranges": {"000001-000006": {}, "000004-000008": {}}}},
            {"first": checkpoint, "unbound": checkpoint}, {"files": {}}, {}, {})
        self.assertEqual(report["source_books"], 1)
        self.assertEqual(report["render_ready_pages"], 12)
        self.assertEqual(report["ocr_recognized_pages"], 2)
        self.assertEqual(progress.covered_ranges({"000001-000006": {}, "000004-000008": {}}, 12), 8)
        self.assertEqual(report["unbound_ocr_progress_keys"], ["unbound"])
