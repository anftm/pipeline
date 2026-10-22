import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from scripts import pilot_static_pdf_stream as stream


class StaticPdfCheckpointTests(unittest.TestCase):
    def test_strict_failure_checkpoints_successes_and_failures_before_exiting(self):
        source = {"repo": "repo", "extension": "tif", "revision": "revision"}
        old_entry = {"key": "repo\0old.tif", "source_revision": "revision", "extension": "tif"}
        new_entry = {"key": "repo\0good.tif", "source_revision": "revision", "extension": "tif"}
        previous = {"files": [old_entry], "failures": []}
        uploaded = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revisions = root / "commits.json"
            revisions.write_text('{}', encoding="utf-8")
            args = SimpleNamespace(limit=0, extension="tif", bucket="bucket", apply=True,
                                   retry_failures=False, strict=True, revisions=revisions,
                                   search_data=root / "search_data.json")
            output = root / "document.pdf"
            output.write_bytes(b"%PDF-fixture")

            def upload(_bucket, *, add, token):
                for local, remote in add:
                    uploaded[remote] = Path(local).read_bytes()

            with patch.object(stream, "parse_args", return_value=args), \
                    patch.object(stream, "records", return_value=[
                        {**source, "path": "old.tif"}, {**source, "path": "good.tif"},
                        {**source, "path": "bad.tif"}]), \
                    patch.object(stream, "existing_index", return_value=previous), \
                    patch.object(stream, "build", side_effect=[
                        (new_entry, {"document.pdf": str(output)}), RuntimeError("HTTP 429")]) as build, \
                    patch.object(stream, "batch_bucket_files", side_effect=upload):
                self.assertEqual(stream.main(), 1)
            self.assertEqual(build.call_count, 2)
            checkpoint = json.loads(uploaded["documents/pdf/tif/index.json"])
            self.assertEqual({entry["key"] for entry in checkpoint["files"]},
                             {old_entry["key"], new_entry["key"]})
            self.assertEqual([failure["key"] for failure in checkpoint["failures"]], ["repo\0bad.tif"])
            self.assertIn("HTTP 429", checkpoint["failures"][0]["error"])
            self.assertEqual(uploaded["document.pdf"], b"%PDF-fixture")

    def test_strict_retry_preserves_completed_files_and_clears_recovered_failures(self):
        source = {"repo": "repo", "path": "retry.tif", "extension": "tif", "revision": "revision"}
        recovered = {"key": "repo\0retry.tif", "source_revision": "revision", "extension": "tif"}
        previous = {"files": [{"key": "repo\0old.tif", "source_revision": "revision"}],
                    "failures": [{"key": "repo\0retry.tif", "source_revision": "revision"}]}
        uploaded = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revisions = root / "commits.json"
            revisions.write_text('{}', encoding="utf-8")
            args = SimpleNamespace(limit=0, extension="tif", bucket="bucket", apply=True,
                                   retry_failures=True, strict=True, revisions=revisions,
                                   search_data=root / "search_data.json")

            def upload(_bucket, *, add, token):
                for local, remote in add:
                    uploaded[remote] = Path(local).read_bytes()

            with patch.object(stream, "parse_args", return_value=args), \
                    patch.object(stream, "records", return_value=[source]), \
                    patch.object(stream, "existing_index", return_value=previous), \
                    patch.object(stream, "build", return_value=(recovered, {})), \
                    patch.object(stream, "batch_bucket_files", side_effect=upload):
                self.assertEqual(stream.main(), 0)
            checkpoint = json.loads(uploaded["documents/pdf/tif/index.json"])
            self.assertEqual(len(checkpoint["files"]), 2)
            self.assertEqual(checkpoint["failures"], [])

    def test_workflows_use_strict_conversion_and_block_failed_finalization(self):
        workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
        for name, job in (("reader-static-pdf-stream.yml", "stream"), ("reader-image-stream.yml", "build")):
            workflow = yaml.safe_load((workflows / name).read_text(encoding="utf-8"))
            with self.subTest(workflow=name):
                commands = "\n".join(step.get("run", "") for step in workflow["jobs"][job]["steps"])
                self.assertIn("--apply --strict)", commands)
                self.assertIn(f"needs.{job}.result == 'success'", workflow["jobs"]["finalize"]["if"])


if __name__ == "__main__":
    unittest.main()
