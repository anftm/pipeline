import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.publish_pdf_range_shards import apply_results, merge_results
from scripts.continue_pdf_range import continuation_command
from scripts import pdf_range_assets as assets


class PDFRangeShardTests(unittest.TestCase):
    def test_empty_compute_batch_exports_reused_and_metadata_results(self):
        item = {"key": "alias", "repo": "source", "source_path": "alias.pdf", "input_token": "same",
                "input_profile": "upstream", "source_bytes": 10 * 1024 ** 2}
        original = {**item, "key": "original", "identity": assets.identity(item, "qpdf-test"),
                    "status": "optimized", "path": "objects/aa/id/document.pdf", "sha256": "digest", "bytes": 100}
        baseline = {"version": 1, "files": {"original": original, "alias": {**item, "status": "pending", "identity": "old"}}, "inventories": {}}
        items = {"alias": item, "small": {**item, "key": "small", "source_bytes": 10},
                 "original": {**item, "key": "original"}}
        exports = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            search = root / "search.json"; search.write_text("[]")
            revisions = root / "revisions.json"; revisions.write_text("{}")
            api = Mock(); api.repo_info.return_value.sha = "revision"
            bundles = []
            for index in range(3):
                bundle = root / str(index); bundles.append(bundle)
                argv = ["pdf_range_assets", "--search-data", str(search), "--revisions", str(revisions),
                        "--bundle", str(bundle), "--build-only", "--shard-count", "3", "--shard-index", str(index)]
                with patch("sys.argv", argv), patch.object(assets, "HfApi", return_value=api), \
                        patch.object(assets.pdf_range_state, "remote_state", return_value=baseline), \
                        patch.object(assets, "remote_manifest", return_value={}), \
                        patch.object(assets, "remote_pdf_manifest", return_value={}), \
                        patch.object(assets, "discover", return_value=(items, {})), \
                        patch.object(assets.subprocess, "check_output", return_value="qpdf-test\n"), \
                        patch.object(assets, "process") as process:
                    assets.main()
                    process.assert_not_called()
                exports.extend(json.loads((bundle / "results.json").read_text()))
            self.assertEqual({row["key"] for row in exports}, {"alias", "small"})
            self.assertEqual(len(exports), 2)
            merged, results = merge_results(bundles)
            state = {"files": dict(baseline["files"])}
            apply_results(state, results)
            self.assertEqual(state["files"]["alias"]["status"], "optimized")
            self.assertEqual(state["files"]["small"]["reason"], "below-4-mib")
            planned, queue = assets.plan(items, state, "qpdf-test", 100)
            self.assertEqual(queue, [])
            self.assertEqual(assets.planned_results(planned, state, set()), [])

    def test_reused_artifact_must_match_baseline_and_remote_digest(self):
        original = {"key": "original", "identity": "same", "status": "optimized",
                    "path": "objects/aa/id/document.pdf", "sha256": "digest", "bytes": 100}
        result = {**original, "key": "alias"}
        baseline = {"files": {"original": original}}
        state = {"files": {**baseline["files"], "alias": result}}
        api = Mock(); api.repo_info.return_value.sha = "revision"; api.create_commit.return_value.oid = "published"
        file = SimpleNamespace(path=result["path"], size=100, lfs=SimpleNamespace(sha256="digest"))
        api.get_paths_info.return_value = [file]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(assets.pdf_range_state, "remote_state", return_value=baseline), \
                patch.object(assets, "remote_manifest", return_value={}), \
                patch.object(assets, "remote_pdf_manifest", return_value={}), \
                patch.object(assets, "encode_index", return_value=b"index"):
            self.assertEqual(assets.publish(api, "assets", baseline, state, Path(directory), [result]), "published")
            operations = api.create_commit.call_args.kwargs["operations"]
            self.assertEqual([op.path_in_repo for op in operations], ["pdf_range_manifest.json", "reader_assets.json.gz"])
            file.lfs.sha256 = "wrong"
            with self.assertRaisesRegex(ValueError, "reused optimized artifact digest mismatch"):
                assets.publish(api, "assets", baseline, state, Path(directory), [result])
            with self.assertRaisesRegex(ValueError, "missing from bundle and baseline"):
                assets.publish(api, "assets", baseline, state, Path(directory), [{**result, "identity": "changed"}])

    def test_continuation_is_bounded_and_stops_for_an_empty_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "results.json"
            result.write_text("[]")
            self.assertIsNone(continuation_command(root, {"FOLLOWUPS": "3"}))
            result.write_text('[{"status":"failed"}]')
            self.assertIsNone(continuation_command(root, {"FOLLOWUPS": "0"}))
            command = continuation_command(root, {"FOLLOWUPS": "3", "SOURCE_PATH": "a book.pdf",
                                                  "RETRY_STALE_BLOCKED": "true", "BATCH_LIMIT": "100"})
            self.assertIn("followups=2", command)
            self.assertIn("path=a book.pdf", command)
            self.assertIn("retry_stale_blocked=true", command)
            with self.assertRaises(ValueError):
                continuation_command(root, {"FOLLOWUPS": "21"})

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

    def test_new_artifact_digest_takes_precedence_over_reused_alias(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            path = "objects/aa/hash/profile/document.pdf"
            bundles = [root / "a-reused", root / "b-built"]
            for index, bundle in enumerate(bundles):
                bundle.mkdir()
                (bundle / "results.json").write_text(json.dumps([{
                    "key": str(index), "status": "optimized", "path": path,
                    "sha256": "new" if index else "old", "bytes": 3,
                }]))
            artifact = bundles[1] / path
            artifact.parent.mkdir(parents=True); artifact.write_bytes(b"new")
            merged, results = merge_results(bundles)
            self.assertEqual({row["sha256"] for row in results}, {"new"})
            self.assertEqual((merged / path).read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
