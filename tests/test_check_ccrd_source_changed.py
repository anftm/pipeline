"""Offset-check tests for the CCRD index publication trigger.

The published generation must be rebuilt when the builder or corpus archive
changes, not only when the single changing source TXT changes.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_ccrd_source_changed as checker  # noqa: E402


class CheckCCRDSourceChangedTests(unittest.TestCase):
    def _run(self, manifest: dict | None) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output.txt"
            manifest_path = root / "current.json"
            if manifest is not None:
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            def fake_download(_bucket, files, token=None, raise_on_missing_files=False):
                if manifest is None:
                    raise FileNotFoundError("current.json")
                Path(files[0][1]).write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

            with patch.object(checker, "download_bucket_files", fake_download), \
                    patch.object(checker, "download_override", return_value={"sha256": "s" * 64}), \
                    patch.dict(os.environ, {"HF_TOKEN": "token", "GITHUB_OUTPUT": str(output)}):
                checker.main()
            return output.read_text(encoding="utf-8").strip()

    def test_builder_change_triggers_rebuild(self):
        manifest = {
            "builder": {"sha256": "old-builder"},
            "source_archive": {"sha256": checker.SOURCE_ARCHIVE_SHA256},
            "source_overrides": {str(checker.CORPUS_PATH): {"sha256": "s" * 64}},
        }
        self.assertEqual(self._run(manifest), "changed=true")

    def test_corpus_archive_change_triggers_rebuild(self):
        manifest = {
            "builder": {"sha256": checker.BUILDER_SHA256},
            "source_archive": {"sha256": "old-archive"},
            "source_overrides": {str(checker.CORPUS_PATH): {"sha256": "s" * 64}},
        }
        self.assertEqual(self._run(manifest), "changed=true")

    def test_unchanged_generation_is_a_no_op(self):
        manifest = {
            "builder": {"sha256": checker.BUILDER_SHA256},
            "source_archive": {"sha256": checker.SOURCE_ARCHIVE_SHA256},
            "corpus_migration": checker.CORPUS_MIGRATION_VERSION,
            "source_overrides": {str(checker.CORPUS_PATH): {"sha256": "s" * 64}},
        }
        self.assertEqual(self._run(manifest), "changed=false")

    def test_corpus_layout_change_triggers_rebuild(self):
        manifest = {
            "builder": {"sha256": checker.BUILDER_SHA256},
            "source_archive": {"sha256": checker.SOURCE_ARCHIVE_SHA256},
            "corpus_migration": "old-layout",
            "source_overrides": {str(checker.CORPUS_PATH): {"sha256": "s" * 64}},
        }
        self.assertEqual(self._run(manifest), "changed=true")

    def test_missing_manifest_triggers_rebuild(self):
        self.assertEqual(self._run(None), "changed=true")


if __name__ == "__main__":
    unittest.main()
