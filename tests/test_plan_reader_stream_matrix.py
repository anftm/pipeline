import io
import unittest
from unittest.mock import Mock

from scripts.plan_reader_stream_matrix import completed_entries, read_json


class ReaderStreamMatrixIndexTests(unittest.TestCase):
    def test_only_a_missing_index_is_treated_as_empty(self):
        fs = Mock()
        fs.open.side_effect = FileNotFoundError("missing index")
        self.assertEqual(read_json(fs, "bucket", "chapters/ebook/epub/index.json"), {})

        for error in (PermissionError("denied"), OSError("network read failed")):
            fs = Mock()
            fs.open.side_effect = error
            with self.assertRaises(type(error)):
                read_json(fs, "bucket", "chapters/ebook/epub/index.json")

    def test_invalid_indexes_fail_closed(self):
        for raw in (b"[]", b"not-json"):
            fs = Mock()
            fs.open.return_value = io.BytesIO(raw)
            with self.assertRaises(ValueError):
                read_json(fs, "bucket", "chapters/ebook/epub/index.json")

    def test_failed_sources_are_skipped_unless_retry_is_explicit(self):
        index = {"files": [{"key": "good", "source_revision": "r1"}],
                 "failures": [{"key": "bad", "source_revision": "r2"}]}
        self.assertEqual(completed_entries(index), {"good": "r1", "bad": "r2"})
        self.assertEqual(completed_entries(index, retry_failures=True), {"good": "r1"})

    def test_office_workflow_exposes_and_passes_explicit_failure_retry(self):
        from pathlib import Path
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" /
                    "reader-office-chapters.yml").read_text(encoding="utf-8")
        self.assertIn("retry_failures:", workflow)
        self.assertEqual(workflow.count("--retry-failures"), 2)


if __name__ == "__main__":
    unittest.main()
