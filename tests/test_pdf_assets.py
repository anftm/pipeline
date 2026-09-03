import json
import requests
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from huggingface_hub.errors import HfHubHTTPError

from scripts import pdf_assets
from scripts import plan_pdf_assets
from scripts import publish_pdf_assets


class PdfAssetsTests(unittest.TestCase):
    def test_compact_records_are_decoded_and_queue_is_stable(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            search = root / "search.json"
            revisions = root / "commits.json"
            search.write_text(json.dumps({"rp": ["VoiceOfML/A"], "fd": [[]],
                "rc": [[0, "book", "pdf", 0, 60000000, False],
                       [0, "other", "txt", 0, 1, False]]}), encoding="utf-8")
            revisions.write_text(json.dumps({"VoiceOfML/A": "rev"}), encoding="utf-8")
            records = pdf_assets.load_records(search, revisions)
            self.assertEqual([x["path"] for x in pdf_assets.queue(records, 1, 0)], ["book.pdf"])
            self.assertEqual(pdf_assets.queue(records, 1, 1), [])

    def test_small_pdf_is_skipped_without_tools(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "x.pdf"
            source.write_bytes(b"x" * (pdf_assets.MIN_BYTES - 1))
            item = {"key": "r\0x.pdf", "repo": "r", "path": "x.pdf", "source_revision": "rev"}
            with patch.object(pdf_assets, "_run") as run:
                result = pdf_assets.build_item(item, source, root / "bundle")
            self.assertEqual(result["status"], "skipped")
            run.assert_not_called()

    def test_weighted_shards_use_descending_page_count_and_stable_ties(self):
        records = [{"key": key, "page_count": pages} for key, pages in (
            ("a", 10), ("b", 9), ("c", 8), ("d", 7), ("e", 6), ("f", 5))]
        shards = pdf_assets.weighted_shards(records, 3)
        self.assertEqual([[item["key"] for item in shard] for shard in shards],
                         [["a", "f"], ["b", "e"], ["c", "d"]])
        self.assertEqual([sum(item["page_count"] for item in shard) for shard in shards], [15, 15, 15])

    def test_weighted_shards_tie_breaks_by_key(self):
        records = [{"key": key, "page_count": 4} for key in ("c", "a", "b")]
        shards = pdf_assets.weighted_shards(records, 2)
        self.assertEqual([[item["key"] for item in shard] for shard in shards], [["a", "c"], ["b"]])

    def test_plan_splits_only_oversized_pdfs_into_deterministic_ranges(self):
        records = [{"key": "r\0large.pdf", "repo": "r", "path": "large.pdf"},
                   {"key": "r\0normal.pdf", "repo": "r", "path": "normal.pdf"}]
        with patch.object(plan_pdf_assets.pdf_assets, "_pages", side_effect=[2501, 1000]), patch.object(
                plan_pdf_assets, "source_path", side_effect=[Path("large.pdf"), Path("normal.pdf")]):
            planned = plan_pdf_assets.plan(records, None, "assets", 10, workers=1)
        tasks = [task for shard in planned["shards"] for task in shard["records"]]
        ranges = sorted((task["page_start"], task["page_end"]) for task in tasks
                        if task["key"] == "r\0large.pdf")
        self.assertEqual(ranges, [(1, 1000), (1001, 2000), (2001, 2501)])
        self.assertEqual(len([task for task in tasks if task["key"] == "r\0normal.pdf"]), 1)
        self.assertEqual((planned["total_records"], planned["total_tasks"]), (2, 4))
        self.assertEqual(planned["ordinary_shard_count"], 10)
        self.assertEqual(planned["shard_count"], 13)
        self.assertEqual(planned["shard_ids"], list(range(13)))
        self.assertEqual(
            [shard["index"] for shard in planned["shards"]
             if any("page_start" in task for task in shard["records"])],
            [10, 11, 12],
        )
        self.assertTrue(all(len(shard["records"]) == 1 for shard in planned["shards"][10:]))
        self.assertEqual(len({task["task_key"] for task in tasks if "task_key" in task}), 3)
        self.assertTrue(all(
            "page_start" not in task
            for shard in planned["shards"][:10]
            for task in shard["records"]
        ))

    def test_plan_requires_exactly_ten_ordinary_shards(self):
        with self.assertRaisesRegex(ValueError, "ordinary PDF shard count must be 10"):
            plan_pdf_assets.plan([], None, "assets", 9)

    def test_generated_records_pin_reader_assets_revision(self):
        manifest = {"files": {"r\0book.caj": {
            "status": "ready", "reader_mode": "pdf", "path": "objects/a/document.pdf",
            "bytes": pdf_assets.MIN_BYTES, "source_revision": "source-rev",
        }}}
        records = pdf_assets.load_generated_records(manifest, "reader-assets", assets_revision="assets-rev")
        self.assertEqual(records[0]["reader_assets_revision"], "assets-rev")

    def test_generated_source_path_uses_pinned_revision(self):
        item = {"source_kind": "generated", "reader_assets_repo": "reader-assets",
                "reader_assets_path": "objects/a/document.pdf", "reader_assets_revision": "assets-rev"}
        with patch.object(plan_pdf_assets, "hf_hub_download", return_value="/tmp/document.pdf") as download:
            self.assertEqual(plan_pdf_assets.source_path(item, None, "reader-assets"), Path("/tmp/document.pdf"))
        self.assertEqual(download.call_args.kwargs["revision"], "assets-rev")

    def test_download_failure_does_not_reuse_previous_source(self):
        records = [
            {"key": "r\0first.pdf", "source_kind": "generated", "reader_assets_repo": "assets",
             "reader_assets_path": "first.pdf", "reader_assets_revision": "rev", "source_bytes": 1},
            {"key": "r\0second.pdf", "source_kind": "generated", "reader_assets_repo": "assets",
             "reader_assets_path": "second.pdf", "reader_assets_revision": "rev", "source_bytes": 2},
        ]
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "first.pdf"
            source.write_bytes(b"first")
            args = SimpleNamespace(queue_file=root / "queue.json", shard_count=1, shard_index=0,
                                   bundle=root / "bundle", dry_run=False, build_only=True)
            with patch.object(pdf_assets, "parse_args", return_value=args), patch.object(
                    pdf_assets, "load_planned_shard", return_value=records), patch.object(
                    pdf_assets, "download_hf_source", side_effect=[source, OSError("download failed")]), patch.object(
                    pdf_assets, "build_item", return_value={"key": records[0]["key"], "status": "skipped",
                                                             "strategy": "none", "source_sha256": "first-sha"}):
                self.assertEqual(pdf_assets.main(), 0)
            results = json.loads((args.bundle / "bundle.json").read_text(encoding="utf-8"))["results"]
        self.assertEqual(results[1]["source_sha256"], "")
        self.assertEqual(results[1]["source_bytes"], 2)

    def test_object_root_is_unique_per_source_entry(self):
        self.assertNotEqual(
            pdf_assets.object_root("a" * 64, "repo\0first.pdf"),
            pdf_assets.object_root("a" * 64, "repo\0second.pdf"),
        )

    def test_publish_manifest_is_separate_and_content_addressed(self):
        result = {"key": "r\0x.pdf", "status": "skipped", "reason": "estimated-webp-over-90-percent",
                  "strategy": "sampled-webp", "source_sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as root:
            manifest, operations = pdf_assets.build_publish(pdf_assets.empty_manifest(), [result], Path(root))
        self.assertIn("r\0x.pdf", manifest["files"])
        self.assertEqual(operations[-1].path_in_repo, "pdf_manifest.json")
        self.assertEqual(pdf_assets.MANIFEST_NAME, "pdf_manifest.json")

    def test_pending_records_exclude_completed_and_small_sources(self):
        records = [
            {"key": "r\0small.pdf", "source_kind": "upstream", "source_bytes": pdf_assets.MIN_BYTES - 1},
            {"key": "r\0done.pdf", "source_kind": "upstream", "source_bytes": pdf_assets.MIN_BYTES},
            {"key": "r\0new.pdf", "source_kind": "upstream", "source_bytes": pdf_assets.MIN_BYTES},
        ]
        pending = plan_pdf_assets.pending_records(records, {"files": {
            "r\0done.pdf": {"status": "ready"},
        }})
        self.assertEqual([item["key"] for item in pending], ["r\0new.pdf"])

    def test_merge_bundles_combines_multiple_shards_and_empty_shards(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            first, second, merged = (root / name for name in ("shard-0", "shard-1", "merged"))
            first.mkdir()
            second.mkdir()
            results = [
                {"key": "r\0a.pdf", "status": "ready", "reason": "scan",
                 "strategy": "sampled-webp", "source_revision": "1", "source_sha256": "a" * 64,
                 "source_extension": "pdf", "profile": "p", "path": "objects/a/page.webp",
                 "pages": [], "page_manifest": {}},
            ]
            (first / "bundle.json").write_text(json.dumps({"version": 1, "results": results}), encoding="utf-8")
            (second / "bundle.json").write_text(json.dumps({"version": 1, "results": []}), encoding="utf-8")
            (first / "objects/a/page.webp").parent.mkdir(parents=True)
            (first / "objects/a/page.webp").write_bytes(b"webp")
            merged_results = publish_pdf_assets.merge_bundles([second, first], merged)
            _, operations = pdf_assets.build_publish(pdf_assets.empty_manifest(), merged_results, merged)
        self.assertEqual([result["key"] for result in merged_results], ["r\0a.pdf"])
        self.assertIn("objects/a/page.webp", {operation.path_in_repo for operation in operations})

    def test_merge_bundles_aggregates_complete_ranges_to_one_manifest_result(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bundles = []
            for index, (start, end) in enumerate(((1, 1000), (1001, 1200))):
                bundle = root / f"bundle-{index}"
                bundle.mkdir()
                object_path = f"objects/sha/pages/page-{start:06d}.webp"
                (bundle / object_path).parent.mkdir(parents=True)
                (bundle / object_path).write_bytes(f"{start}".encode())
                result = {"key": "r\0large.pdf", "task_key": f"task-{index}", "status": "ready",
                          "strategy": "sampled-webp", "source_revision": "1", "source_sha256": "a" * 64,
                          "source_extension": "pdf", "profile": "p", "page_count": 1200,
                          "page_start": start, "page_end": end, "range_page_count": end - start + 1,
                          "pages": [{"page": page, "path": f"objects/sha/pages/page-{page:06d}.webp"}
                                    for page in range(start, end + 1)]}
                (bundle / "bundle.json").write_text(json.dumps({"version": 1, "results": [result]}), encoding="utf-8")
                bundles.append(bundle)
            results = publish_pdf_assets.merge_bundles(bundles, root / "merged")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["key"], "r\0large.pdf")
        self.assertTrue(results[0]["page_manifest"]["path"].endswith("page-manifest.json"))

    def test_merge_bundles_rejects_overlapping_ranges(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bundle = root / "bundle"
            bundle.mkdir()
            results = [{"key": "r\0large.pdf", "status": "ready", "source_sha256": "a" * 64,
                        "page_count": 1200, "page_start": 1, "page_end": 1000, "pages": []},
                       {"key": "r\0large.pdf", "status": "ready", "source_sha256": "a" * 64,
                        "page_count": 1200, "page_start": 1000, "page_end": 1200, "pages": []}]
            (bundle / "bundle.json").write_text(json.dumps({"version": 1, "results": results}), encoding="utf-8")
            with self.assertRaises(ValueError):
                publish_pdf_assets.merge_bundles([bundle], root / "merged")

    def test_merge_bundles_rejects_colliding_range_artifact_paths(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            bundle = root / "bundle"
            bundle.mkdir()
            result = {"key": "r\0large.pdf", "status": "ready", "source_sha256": "a" * 64,
                      "page_count": 2, "page_start": 1, "page_end": 2,
                      "pages": [{"page": 1, "path": "objects/sha/page.webp"},
                                {"page": 2, "path": "objects/sha/page.webp"}]}
            (bundle / "bundle.json").write_text(
                json.dumps({"version": 1, "results": [result]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicting PDF page artifact paths"):
                publish_pdf_assets.merge_bundles([bundle], root / "merged")

    def test_result_chunks_are_stable_and_bounded(self):
        results = [{"key": str(index)} for index in range(5)]
        self.assertEqual(
            publish_pdf_assets.result_chunks(results, 2),
            [[results[0], results[1]], [results[2], results[3]], [results[4]]],
        )

    def test_publish_retries_parent_race_and_reuses_successful_commit(self):
        result = {"key": "r\0a.pdf", "status": "skipped", "reason": "native-text-pdf",
                  "strategy": "native-text", "source_revision": "1", "source_sha256": "a" * 64,
                  "source_extension": "pdf", "profile": "p"}
        response = requests.Response()
        response.status_code = 409
        response.request = requests.Request("POST", "https://huggingface.co/commit").prepare()
        api = Mock()
        api.repo_info.side_effect = [Mock(sha="parent-1"), Mock(sha="parent-2")]
        api.create_commit.side_effect = [HfHubHTTPError("conflict", response=response), None]
        with tempfile.TemporaryDirectory() as root, patch.object(
                pdf_assets, "remote_manifest", return_value=pdf_assets.empty_manifest()), patch.object(
                pdf_assets, "remote_sidecar", return_value={"v": 1, "f": {}}), patch.object(
                pdf_assets.time, "sleep"):
            pdf_assets.publish(api, "repo", pdf_assets.empty_manifest(), [result], Path(root))
        self.assertEqual(api.create_commit.call_count, 2)
        self.assertEqual([call.kwargs["parent_commit"] for call in api.create_commit.call_args_list],
                         ["parent-1", "parent-2"])

    def test_empty_publication_is_a_noop(self):
        api = Mock()
        pdf_assets.publish(api, "repo", pdf_assets.empty_manifest(), [], Path("/tmp/unused"))
        api.repo_info.assert_not_called()

    def test_hf_source_download_retries_rate_limit(self):
        response = requests.Response()
        response.status_code = 429
        error = HfHubHTTPError("rate limited", response=response)
        with patch("huggingface_hub.hf_hub_download", side_effect=[error, "/tmp/source.pdf"]), patch.object(pdf_assets.time, "sleep") as sleep:
            self.assertEqual(pdf_assets.download_hf_source("repo", "book.pdf", "rev", "token"), Path("/tmp/source.pdf"))
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
