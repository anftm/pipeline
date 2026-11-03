import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.plan_reader_stream_matrix import completed_entries, read_json, selected_records
from scripts.pilot_media_stream import select_records as select_media_records


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
        for raw in (b"[]", b"not-json", b'{"files":{}}', b'{"files":[],"failures":{}}'):
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
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" /
                    "reader-office-chapters.yml").read_text(encoding="utf-8")
        self.assertIn("retry_failures:", workflow)
        self.assertEqual(workflow.count("--retry-failures"), 2)

    def test_aac_is_planned_and_selected_only_as_audio(self):
        rows = [{"Repo": "VoiceOfML/Test", "Folder": ["music"], "File": "clip",
                 "Extension": "aac", "Size": 123}]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "search.json"
            source.write_text(json.dumps(rows), encoding="utf-8")
            revisions = {"VoiceOfML/Test": "a" * 40}
            planned = selected_records(source, revisions, "media", {"audio"})
            converted = select_media_records(source, revisions, "audio")
            self.assertEqual([(x["key"], x["extension"]) for x in planned],
                             [("VoiceOfML/Test\0music/clip.aac", "audio")])
            self.assertEqual([(x["path"], x["extension"]) for x in converted],
                             [("music/clip.aac", "aac")])
            self.assertEqual(selected_records(source, revisions, "media", {"video"}), [])
            self.assertEqual(select_media_records(source, revisions, "video"), [])


if __name__ == "__main__":
    unittest.main()
