from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / ".github" / "workflows"


class ReaderAssetConcurrencyTests(unittest.TestCase):
    def test_all_reader_asset_publishers_share_one_queue(self):
        for filename in (
            "reader-assets.yml", "prune-reader-assets.yml", "pdf-assets-worker.yml",
            "migrate-pdf-page-manifests.yml", "pdf-range-assets.yml",
        ):
            workflow = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("group: reader-assets", workflow, filename)
            self.assertNotIn("group: reader-assets-pdf", workflow, filename)
            self.assertNotIn("group: pdf-range-assets", workflow, filename)
            self.assertIn("cancel-in-progress: false", workflow, filename)


if __name__ == "__main__":
    unittest.main()
