import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import migrate_pdf_page_manifests as migration


class PdfManifestMigrationTests(unittest.TestCase):
    def v1(self):
        root = "objects/aa/" + "b" * 64 + "/1234567890abcdef"
        return {"version": 1, "kind": "pdf-pages", "source_sha256": "b" * 64,
                "profile": "pdf-pages-v3", "pages": [
                    {"page": 1, "path": root + "/pages/page-000001.webp", "bytes": 3, "sha256": "a" * 64},
                    {"page": 2, "path": root + "/pages/page-000002.webp", "bytes": 4, "sha256": "c" * 64},
                ], "toc": [{"title": "Start", "page": 1}]}

    def test_migrate_preserves_page_root_and_toc_without_hashes(self):
        result = migration.migrate_manifest(self.v1())
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["toc"], [{"title": "Start", "page": 1}])
        self.assertNotIn("pages", result)

    def test_plan_is_batched_and_skips_v2(self):
        manifest = {"version": 1, "files": {
            "r\0a.pdf": {"status": "ready", "page_manifest": {"path": "objects/a/page-manifest.json", "version": 1}},
            "r\0b.pdf": {"status": "ready", "page_manifest": {"path": "objects/b/page-manifest.json", "version": 2}},
            "r\0c.pdf": {"status": "ready", "page_manifest": {"path": "objects/c/page-manifest.json"}},
        }}
        self.assertEqual([key for key, _ in migration.plan(manifest, limit=1, checkpoint=1)], ["r\0c.pdf"])

    def test_invalid_page_sequence_is_rejected(self):
        manifest = self.v1()
        manifest["pages"][1]["page"] = 3
        with self.assertRaises(ValueError):
            migration.migrate_manifest(manifest)

    def test_apply_syncs_only_selected_manifest_paths(self):
        api = Mock()
        pdf_manifest = {"version": 1, "files": {
            "r\0a.pdf": {"status": "ready", "page_manifest": {"path": "objects/a/page-manifest.json", "version": 1}},
        }}
        with tempfile.TemporaryDirectory() as root, \
                patch.object(migration, "load_pdf_manifest", return_value=pdf_manifest), \
                patch.object(migration, "read_bucket_manifest", return_value=self.v1()), \
                patch.object(migration, "sync_bucket") as sync:
            report = migration.migrate(api, "repo", apply=True)
        self.assertEqual(len(report["converted"]), 1)
        sync.assert_called_once()
        self.assertEqual(sync.call_args.kwargs["include"], ["objects/a/page-manifest.json"])


if __name__ == "__main__":
    unittest.main()
