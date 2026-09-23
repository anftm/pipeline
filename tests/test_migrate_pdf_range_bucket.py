import unittest

from scripts import migrate_pdf_range_bucket as migration
from scripts import shared


class PDFRangeBucketMigrationTests(unittest.TestCase):
    def test_legacy_objects_are_unique_and_protect_active_reader_assets(self):
        path = "objects/aa/" + "a" * 64 + "/pdf-range-v1-document/document.pdf"
        state = {"version": 1, "files": {
            "one": {"status": "optimized", "path": path, "sha256": "a", "bytes": 1},
            "alias": {"status": "optimized", "path": path, "sha256": "a", "bytes": 1},
            "other": {"status": "optimized", "path": "objects/bb/" + "b" * 64
                       + "/pdf-range-v1-document/document.pdf"},
        }}
        manifest = {"files": {"active": {"status": "ready", "path": path}}}
        objects = migration.legacy_objects(state, manifest, 100, 0)
        self.assertEqual([item["path"] for item in objects], [state["files"]["other"]["path"]])

    def test_migrated_state_is_not_planned_again(self):
        state = {"version": 1, "artifact_bucket": shared.PDF_RANGE_BUCKET,
                 "files": {"book": {"status": "optimized", "path": "objects/aa/" + "a" * 64
                                      + "/pdf-range-v1-document/document.pdf"}}}
        self.assertEqual(migration.legacy_objects(state, {"files": {}}, 100, 0), [])


if __name__ == "__main__":
    unittest.main()
