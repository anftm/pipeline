import json
from pathlib import Path
import tempfile
import unittest

from scripts.publish_pdf_range_shards import merge_results


class PDFRangeShardTests(unittest.TestCase):
    def test_merges_results_and_deduplicates_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bundles = []
            for index, key in enumerate(("one", "two")):
                bundle = root / f"bundle-{index}"
                shard = bundle / "checkpoint-0"
                artifact = shard / "objects/aa/hash/document.pdf"
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(b"same")
                (shard / "results.json").write_text(json.dumps([{"key": key, "status": "unchanged"}]))
                bundles.append(bundle)
            merged, results = merge_results(bundles)
            self.assertEqual({result["key"] for result in results}, {"one", "two"})
            self.assertEqual((merged / "objects/aa/hash/document.pdf").read_bytes(), b"same")

    def test_rejects_duplicate_results(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bundles = []
            for index in range(2):
                bundle = root / f"bundle-{index}"
                bundle.mkdir()
                (bundle / "checkpoint/results.json").parent.mkdir(parents=True)
                (bundle / "checkpoint/results.json").write_text(json.dumps([{"key": "same", "status": "unchanged"}]))
                bundles.append(bundle)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                merge_results(bundles)

    def test_parallel_qpdf_ids_share_one_artifact_digest(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bundles = []
            path = "objects/aa/hash/profile/document.pdf"
            for index, body in enumerate((b"one", b"two")):
                bundle = root / f"bundle-{index}"
                artifact = bundle / path
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(body)
                (bundle / "results.json").write_text(json.dumps([{
                    "key": f"alias-{index}", "status": "optimized", "path": path,
                    "sha256": f"digest-{index}", "bytes": len(body),
                }]))
                bundles.append(bundle)
            merged, results = merge_results(bundles)
            self.assertEqual(len(results), 2)
            self.assertEqual({result["sha256"] for result in results}, {"digest-0"})
            self.assertEqual((merged / path).read_bytes(), b"one")


if __name__ == "__main__":
    unittest.main()
