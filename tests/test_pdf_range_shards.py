import json
from pathlib import Path
import tempfile
import unittest

from scripts.publish_pdf_range_shards import apply_results, merge_results


class PDFRangeShardTests(unittest.TestCase):
    def test_rule_upgrade_publishes_only_against_the_assessed_identity(self):
        state = {"files": {"upgrade": {"identity": "old"}, "stale": {"identity": "newer"}}}
        results = [{"key": "upgrade", "identity": "new", "_previous_identity": "old", "status": "optimized"},
                   {"key": "stale", "identity": "new", "_previous_identity": "old"},
                   {"key": "deleted", "identity": "new", "_previous_identity": "old"},
                   {"key": "fresh", "identity": "new", "_previous_identity": None}]
        accepted = apply_results(state, results)
        self.assertEqual([row["key"] for row in accepted], ["upgrade", "fresh"])
        self.assertEqual(state["files"]["stale"]["identity"], "newer")
        self.assertNotIn("deleted", state["files"])
        self.assertNotIn("_previous_identity", state["files"]["upgrade"])

    def test_legacy_bundles_still_require_matching_identity(self):
        state = {"files": {"current": {"identity": "same"}, "stale": {"identity": "newer"}}}
        accepted = apply_results(state, [{"key": "current", "identity": "same"},
                                         {"key": "stale", "identity": "old"}])
        self.assertEqual([row["key"] for row in accepted], ["current"])

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
