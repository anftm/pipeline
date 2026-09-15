from pathlib import Path
import shutil
import tempfile
import unittest
import copy
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from scripts import pdf_range, pdf_range_assets, build_reader_assets_index, pdf_range_state


class RangePdfTests(unittest.TestCase):
    def test_source_fingerprint_and_engine_identity_control_incremental_queue(self):
        item = {"key": "book", "input_token": "sha256:aaa", "input_profile": "office-v1"}
        files, pending = pdf_range_assets.plan({"book": item}, {"files": {}}, "qpdf-test", 10)
        self.assertEqual(len(pending), 1)
        files["book"]["status"] = "no-gain"
        self.assertFalse(pdf_range_assets.plan({"book": {**item, "input_revision": "new-repo-commit"}},
                                              {"files": files}, "qpdf-test", 10)[1])
        self.assertTrue(pdf_range_assets.plan({"book": {**item, "input_token": "sha256:bbb"}},
                                             {"files": files}, "qpdf-test", 10)[1])
        self.assertTrue(pdf_range_assets.plan({"book": item}, {"files": files}, "new-qpdf", 10)[1])

    def test_pending_batches_make_progress_and_failed_inputs_require_retry(self):
        items = {str(i): {"key": str(i), "input_token": str(i), "input_profile": "upstream"} for i in range(3)}
        files, pending = pdf_range_assets.plan(items, {"files": {}}, "qpdf-test", 1)
        files[pending[0]["key"]]["status"] = "failed"
        _, following = pdf_range_assets.plan(items, {"files": files}, "qpdf-test", 1)
        self.assertNotEqual(following[0]["key"], pending[0]["key"])
        _, retries = pdf_range_assets.plan(items, {"files": files}, "qpdf-test", 3, retry_failed=True)
        self.assertEqual(len(retries), 3)

    def test_small_files_do_not_starve_the_assessment_budget(self):
        items = {str(i): {"key": str(i), "input_token": str(i), "input_profile": "upstream",
                          "source_bytes": 1000} for i in range(20)}
        items["large"] = {"key": "large", "input_token": "large", "input_profile": "upstream",
                          "source_bytes": 80 * pdf_range.MI}
        files, pending = pdf_range_assets.plan(items, {"files": {}}, "qpdf", 1)
        self.assertEqual([row["key"] for row in pending], ["large"])
        self.assertEqual(files["0"]["status"], "unchanged")

    def test_generated_and_original_repositories_share_backfill_slots(self):
        items = {f"A/{i}": {"key": f"A/{i}", "repo": "A", "source_kind": "upstream",
                             "input_token": f"a{i}", "input_profile": "upstream"} for i in range(10)}
        items["Z/book"] = {"key": "Z/book", "repo": "Z", "source_kind": "generated",
                           "input_token": "generated", "input_profile": "djvu"}
        files, pending = pdf_range_assets.plan(items, {"files": {}}, "v", 2)
        self.assertEqual({row["source_kind"] for row in pending}, {"upstream", "generated"})
        files["Z/book"]["status"] = "unchanged"
        _, next_batch = pdf_range_assets.plan(items, {"files": files}, "v", 2)
        self.assertEqual(len(next_batch), 2)
        self.assertTrue(all(row["source_kind"] == "upstream" for row in next_batch))

    def test_tool_upgrade_keeps_same_content_route_until_reassessment(self):
        item = {"key": "book", "input_token": "sha256:aaa", "input_profile": "upstream"}
        previous = {**item, "identity": pdf_range_assets.identity(item, "old"), "status": "optimized",
                    "path": "objects/aa/hash/profile/document.pdf"}
        files, pending = pdf_range_assets.plan({"book": item}, {"files": {"book": previous}}, "new", 10)
        self.assertEqual(files["book"]["status"], "optimized")
        self.assertEqual(len(pending), 1)
        files, _ = pdf_range_assets.plan({"book": {**item, "input_token": "sha256:bbb"}},
                                         {"files": {"book": previous}}, "new", 10)
        self.assertEqual(files["book"]["status"], "pending")
        self.assertNotIn("path", files["book"])

    def test_no_gain_results_are_shared_by_content_without_repository_commit_churn(self):
        item = {"key": "a", "input_token": "sha256:aaa", "input_profile": "upstream",
                "input_revision": "first", "input_repo": "repo", "input_path": "a.pdf"}
        cached = {**item, "identity": pdf_range_assets.identity(item, "v"), "status": "no-gain"}
        files, pending = pdf_range_assets.plan({"a": {**item, "input_revision": "next"},
                                                "b": {**item, "key": "b", "input_path": "b.pdf"}},
                                               {"files": {"a": cached}}, "v", 10)
        self.assertFalse(pending)
        self.assertEqual(files["a"]["input_revision"], "first")
        self.assertEqual(files["b"]["key"], "b")
        self.assertEqual(files["b"]["status"], "no-gain")

    def test_generated_pdf_mapping_expires_when_repair_changes(self):
        base = {"files": {"book": {"status": "ready", "reader_mode": "pdf", "path": "original.pdf",
                                   "sha256": "aaa", "profile": "repair-v1"}}}
        state = {"files": {"book": {"status": "optimized", "source_kind": "generated",
                                    "input_sha256": "aaa", "input_profile": "repair-v1",
                                    "path": "objects/aa/hash/range/document.pdf"}}}
        self.assertEqual(build_reader_assets_index.build_index(base, range_manifest=state)["f"]["book"]["p"],
                         state["files"]["book"]["path"])
        base["files"]["book"]["sha256"] = "bbb"
        self.assertEqual(build_reader_assets_index.build_index(base, range_manifest=state)["f"]["book"]["p"],
                         "original.pdf")
        self.assertEqual(build_reader_assets_index.build_index({"files": {}}, range_manifest=state)["f"], {})

    def test_large_gain_does_not_override_changed_pixels_or_more_requests(self):
        def report(amount, requests):
            metric = {"bytes": amount, "requests": requests}
            return {"renders": ["same"], "pages": 10, "outline_entries": 1,
                    "snapshots": {key: dict(metric) for key in ("startup", "idle", "jump", "final")}}
        before, after = report(80 * pdf_range.MI, 80), report(3 * pdf_range.MI, 3)
        self.assertTrue(pdf_range.improvement(before, after, 90 * pdf_range.MI, 89 * pdf_range.MI))
        for mutation in ("pixels", "requests", "background", "growth"):
            candidate = copy.deepcopy(after)
            if mutation == "pixels": candidate["renders"] = ["changed"]
            if mutation == "requests": candidate["snapshots"]["startup"]["requests"] = 81
            if mutation == "background": candidate["snapshots"]["idle"]["bytes"] += 1
            output_size = 100 * pdf_range.MI if mutation == "growth" else 89 * pdf_range.MI
            self.assertFalse(pdf_range.improvement(before, candidate, 90 * pdf_range.MI, output_size))

    def test_strong_gain_can_end_candidate_search_early(self):
        def report(amount, requests):
            metric = {"bytes": amount, "requests": requests}
            return {"renders": ["same"], "pages": 10, "outline_entries": 1,
                    "snapshots": {key: dict(metric) for key in ("startup", "idle", "jump", "final")}}
        before = report(80 * pdf_range.MI, 80)
        strong = report(20 * pdf_range.MI, 20)
        ordinary = report(60 * pdf_range.MI, 70)
        self.assertTrue(pdf_range.strong_improvement(before, strong, 90 * pdf_range.MI, 90 * pdf_range.MI))
        self.assertFalse(pdf_range.strong_improvement(before, ordinary, 90 * pdf_range.MI, 90 * pdf_range.MI))

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf is required")
    def test_conversion_preserves_content_images_geometry_and_outline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.pdf", root / "document.pdf"
            writer = PdfWriter()
            image = DecodedStreamObject()
            image.set_data(bytes([255, 0, 0, 0, 255, 0]))
            image.update({NameObject("/Type"): NameObject("/XObject"),
                          NameObject("/Subtype"): NameObject("/Image"),
                          NameObject("/Width"): NumberObject(2),
                          NameObject("/Height"): NumberObject(1),
                          NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                          NameObject("/BitsPerComponent"): NumberObject(8)})
            image_ref = writer._add_object(image.flate_encode())
            for index in range(3):
                page = writer.add_blank_page(width=400 + index, height=600)
                page.cropbox.lower_left = (20, 30)
                page.cropbox.upper_right = (380, 580)
                page.rotate(180)
                page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"):
                    DictionaryObject({NameObject("/Im1"): image_ref})})
                content = DecodedStreamObject()
                content.set_data(b"q 200 0 0 100 20 30 cm /Im1 Do Q")
                page[NameObject("/Contents")] = writer._add_object(content.flate_encode())
            writer.add_metadata({"/Title": "Range fixture"})
            writer.add_outline_item("Third page", 2)
            writer.write(source)
            original = source.read_bytes()
            subprocess.run(["qpdf", "--object-streams=generate", "--stream-data=preserve", str(source), str(target)], check=True)
            self.assertEqual(pdf_range.content_signature(source), pdf_range.content_signature(target))
            self.assertEqual(source.read_bytes(), original)
            before, after = PdfReader(source), PdfReader(target)
            self.assertEqual(len(after.pages), 3)
            for a, b in zip(before.pages, after.pages):
                self.assertEqual(a.mediabox, b.mediabox)
                self.assertEqual(a.get_contents().get_data(), b.get_contents().get_data())
                self.assertEqual(a["/Contents"]._data, b["/Contents"]._data)
                self.assertEqual(a["/Resources"]["/XObject"]["/Im1"]._data,
                                 b["/Resources"]["/XObject"]["/Im1"]._data)
            self.assertEqual(after.metadata.title, before.metadata.title)
            self.assertEqual(after.outline[0].title, "Third page")
            self.assertEqual(after.get_destination_page_number(after.outline[0]), 2)
            self.assertTrue(after.xref_objStm, "page dictionaries should use object streams")

    def test_all_generated_pdf_formats_use_asset_fingerprint_and_original_path(self):
        records = [{"Repo": "VoiceOfML/Test", "File": "slides.ppt", "Path": "Test", "Extension": "ppt", "Size": 20}]
        # Use the common metadata path helper, as generated corpus shapes vary.
        from scripts import reader_assets
        path = reader_assets.relative_path(records[0])
        key = reader_assets.asset_key("VoiceOfML/Test", path)
        base = {"files": {key: {"status": "ready", "reader_mode": "pdf", "sha256": "abc", "bytes": 5000000,
                               "profile": "office-v1", "path": "objects/aa/bb/office/document.pdf"}}}
        class NoNetwork:
            def list_repo_tree(self, **kwargs):
                raise AssertionError("generated PDF does not need original-file download")
        items, _ = pdf_range_assets.discover(records, {}, base, {"files": {}}, NoNetwork(),
                                             pdf_range_state.empty_state(), "assets", "fixed")
        self.assertEqual(items[key]["input_token"], "sha256:abc")
        self.assertEqual(items[key]["input_path"], base["files"][key]["path"])
        self.assertEqual(items[key]["source_path"], path)

    @unittest.skipUnless(shutil.which("qpdf"), "qpdf is required")
    def test_local_open_action_and_page_labels_survive_object_renumbering(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target, changed = [Path(directory) / name for name in ("source.pdf", "target.pdf", "changed.pdf")]
            writer = PdfWriter()
            for _ in range(3):
                writer.add_blank_page(width=400, height=600)
            action = DictionaryObject({NameObject("/S"): NameObject("/GoTo"),
                NameObject("/D"): ArrayObject([writer.pages[2].indirect_reference, NameObject("/Fit")])})
            writer._root_object[NameObject("/OpenAction")] = writer._add_object(action)
            writer._root_object[NameObject("/PageLabels")] = DictionaryObject({NameObject("/Nums"):
                ArrayObject([NumberObject(0), DictionaryObject({NameObject("/S"): NameObject("/r")})])})
            writer.write(source)
            for options in (["--object-streams=generate"], ["--linearize"]):
                subprocess.run(["qpdf", *options, str(source), str(target)], check=True, capture_output=True)
                self.assertEqual(pdf_range.content_signature(source), pdf_range.content_signature(target))
            action[NameObject("/D")][0] = writer.pages[0].indirect_reference
            writer.write(changed)
            self.assertNotEqual(pdf_range.content_signature(source), pdf_range.content_signature(changed))
            action[NameObject("/S")] = NameObject("/JavaScript")
            writer.write(changed)
            with self.assertRaisesRegex(pdf_range.UnsupportedPDF, "non-local opening"):
                pdf_range.content_signature(changed)

    def test_upstream_inventory_cache_tracks_actual_file_fingerprint(self):
        from scripts import reader_assets
        row = {"Repo": "VoiceOfML/Test", "File": "scan.pdf", "Path": "Test", "Extension": "pdf", "Size": 10}
        path = reader_assets.relative_path(row)
        api = Mock()
        api.list_repo_tree.return_value = [SimpleNamespace(path=path, blob_id="blob", size=10,
                                                           lfs=SimpleNamespace(sha256="content"))]
        args = ([row], {"VoiceOfML/Test": "rev1"}, {"files": {}}, {"files": {}}, api)
        items, inventories = pdf_range_assets.discover(*args, pdf_range_state.empty_state(), "assets", "rev")
        self.assertEqual(next(iter(items.values()))["input_token"], "sha256:content")
        pdf_range_assets.discover(*args, {"inventories": inventories}, "assets", "rev")
        self.assertEqual(api.list_repo_tree.call_count, 1)
        pdf_range_assets.discover([row], {"VoiceOfML/Test": "rev2"}, {"files": {}}, {"files": {}},
                                  api, {"inventories": inventories}, "assets", "rev")
        self.assertEqual(api.list_repo_tree.call_count, 2)

    def test_regular_publication_keeps_optimized_routes_in_atomic_sidecar(self):
        from scripts import publish_reader_assets, reader_assets
        import gzip
        import json
        api = Mock()
        api.repo_info.return_value = SimpleNamespace(sha="parent")
        base = {"version": 1, "files": {}}
        state = {"version": 1, "files": {"scan": {"status": "optimized", "source_kind": "upstream",
                                                   "path": "objects/aa/hash/range/document.pdf"}}}
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "bundle.json").write_text(json.dumps({"version": 1, "results": []}))
            with patch.object(publish_reader_assets, "remote_manifest", return_value=base), \
                 patch.object(publish_reader_assets, "remote_pdf_manifest", return_value=base), \
                 patch.object(publish_reader_assets, "remote_state", return_value=state):
                publish_reader_assets.publish_bundle(api, "assets", bundle)
        operations = api.create_commit.call_args.kwargs["operations"]
        sidecar = next(op.path_or_fileobj for op in operations if op.path_in_repo == "reader_assets.json.gz")
        self.assertEqual(json.loads(gzip.decompress(sidecar))["f"]["scan"]["p"], state["files"]["scan"]["path"])


if __name__ == "__main__":
    unittest.main()
