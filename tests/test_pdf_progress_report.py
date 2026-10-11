import hashlib
import unittest
from unittest.mock import patch

from scripts import pdf_progress_report as progress
from tests.test_publish_reader_v3 import Store


class ProgressTests(unittest.TestCase):
    def test_recorded_history_separates_net_growth_from_recognition_rate(self):
        store = Store()
        first = {"observed_at": "2026-10-11T01:00:00+00:00", "source_books": 4,
                 "render_ready_books": 2, "render_ready_pages": 300, "ocr_ready_books": 1,
                 "ocr_ready_pages": 150, "ocr_recognized_pages": 90, "v3_promoted_books": 1}
        second = {**first, "observed_at": "2026-10-11T02:00:00+00:00", "ocr_recognized_pages": 120}
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "anftm/pipeline",
               "GITHUB_WORKFLOW_REF": "anftm/pipeline/.github/workflows/pdf-account-publish.yml@refs/heads/main"}
        with patch.dict("os.environ", env):
            progress.record_progress(store, first)
            progress.record_progress(store, second)
        self.assertEqual(second["since_previous_observation"]["hourly_net_growth"]["ocr_recognized_pages"], 30)
        self.assertIn("not-model", second["since_previous_observation"]["scope"])

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
