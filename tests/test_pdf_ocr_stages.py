import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
import yaml

from scripts import pdf_ocr, pdf_ocr_stages as stages
from scripts.build_reader_assets_index import build_index


class PdfOcrStagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.objects = {}

    def store(self, bundle):
        for path in bundle.rglob("*"):
            if path.is_file():
                self.objects[path.relative_to(bundle).as_posix()] = path.read_bytes()

    def read(self, meta, suffix=None):
        pdf_ocr.validate_ocr_object_path(meta["path"], suffix)
        data = self.objects[meta["path"]]
        if len(data) != meta["bytes"] or hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise ValueError("object checksum mismatch")
        return data

    def item(self):
        return {"key": "repo\0small.pdf", "repo": "repo", "path": "small.pdf",
                "source_kind": "upstream", "source_revision": "revision"}

    def render_fixture(self, native=False, jxl=False):
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF-test-source")
        bundle = self.root / "render"
        def render(_source, page, directory):
            path = directory / f"page-{page:06d}.png"
            with Image.new("RGB", (200, 300), "white") as image:
                image.save(path)
                image.resize((100, 150)).save(path.with_suffix(".webp"))
            return path, 200, 300
        def encode(png, destination):
            with Image.open(png) as image:
                self.assertEqual(image.size, (200, 300))
            destination.write_bytes(b"jxl")
        item = {**self.item(), "probe": {"page_count": 2, "page_chars": [60 if native else 0, 0],
                                        "classification": "mixed" if native else "scan"}}
        with patch.object(pdf_ocr, "render_page", side_effect=render), \
                patch.object(pdf_ocr, "JXL_ENABLED", jxl), \
                patch.object(pdf_ocr, "encode_jxl", side_effect=encode), \
                patch.object(pdf_ocr, "native_page", return_value={"width": 200, "height": 300,
                             "blocks": [], "text": "原生文字"}), \
                patch.object(pdf_ocr, "ocr_page", side_effect=AssertionError("renderer must not OCR")):
            result = stages.render_book(item, source, bundle)
        self.store(bundle)
        return result

    def test_render_persists_png_webp_jxl_and_native_text_before_ocr(self):
        result = self.render_fixture(native=True, jxl=True)
        manifest = json.loads(self.read(result["render_manifest"]))
        stages.validate_render(result, manifest)
        self.assertEqual(result["status"], "ready")
        self.assertLess(result["source_bytes"], 100 * 1024 ** 2)
        self.assertEqual(result["ocr_pages"], 1)
        for page in manifest["pages"]:
            for field in ("i", "w", "j"):
                self.read(stages.page_meta(page, field))
        native = manifest["pages"][0]
        self.assertEqual(json.loads(gzip.decompress(self.read(stages.page_meta(native, "o"))))["text"], "原生文字")
        self.assertNotIn("o", manifest["pages"][1])

    def test_png_only_ocr_resume_and_complete_book_publication(self):
        result = self.render_fixture(native=True)
        with patch.object(stages, "read_object", side_effect=self.read), \
                patch.object(stages, "source_path", side_effect=AssertionError("OCR must not download PDF")), \
                patch.object(pdf_ocr, "render_page", side_effect=AssertionError("OCR must not render")):
            queue = stages.plan_images({result["key"]: result}, {}, {}, target=500)
            self.assertEqual(queue["total_ocr_pages"], 1)
            book = queue["books"][0]
            incomplete = stages.assemble_book(book, {}, self.root / "incomplete")
            self.assertEqual(incomplete["status"], "failed")
            def recognize(path, width, height):
                self.assertEqual(path.suffix, ".png")
                self.assertEqual((width, height), (200, 300))
                return [{"t": "识别结果", "b": [0, 0, 1, 1], "c": 1, "s": "ocr"}]
            with patch.object(pdf_ocr, "ocr_page", side_effect=recognize) as engine:
                output = self.root / "ocr"
                recognized = stages.recognize_task(queue["shards"][0][0], output)
                self.store(output)
            engine.assert_called_once()
            progress = stages.collect_progress(queue, [recognized])
            resumed = stages.plan_images({result["key"]: result}, {}, progress)
            self.assertEqual(resumed["shard_count"], 0)
            finished = self.root / "finished"
            completed = stages.assemble_book(resumed["books"][0], progress[result["key"]]["pages"], finished)
            self.assertEqual(completed["status"], "ready")
            self.store(finished)
            manifest = json.loads(self.objects[completed["ocr_manifest"]])
            text = json.loads(gzip.decompress(self.read(manifest["book_text"])))
            self.assertEqual((text["version"], text["kind"], text["complete"]), (2, "pdf-book-text", True))
            self.assertEqual([p["text"] for p in text["pages"]], ["原生文字", "识别结果"])
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["page_manifest"], result["page_manifest"])

    def test_page_checksum_failure_does_not_publish_ready(self):
        result = self.render_fixture()
        with patch.object(stages, "read_object", side_effect=self.read):
            queue = stages.plan_images({result["key"]: result}, {}, {})
            task = queue["shards"][0][0]
            self.objects[task["pages"][0]["i"]] = b"corrupt"
            with patch.object(pdf_ocr, "ocr_page", return_value=[]) as engine:
                recognized = stages.recognize_task(task, self.root / "ocr")
            self.assertEqual(len(recognized["pages"]), 1)
            self.assertEqual(len(recognized["errors"]), 1)
            self.assertEqual(engine.call_count, 1)
            progress = stages.collect_progress(queue, [recognized])
            self.assertEqual(stages.assemble_book(queue["books"][0], progress[result["key"]]["pages"],
                                                  self.root / "result")["status"], "failed")
            retried = stages.plan_images({result["key"]: result}, {}, progress)
            self.assertEqual(retried["total_ocr_pages"], 1)

    def test_500_page_tasks_split_large_books_and_keep_every_page(self):
        result = self.render_fixture()
        manifest = json.loads(self.read(result["render_manifest"]))
        template = manifest["pages"][0]
        pages = []
        for number in range(1, 1202):
            page = copy.deepcopy(template)
            page["p"] = number
            for field in ("i", "w"):
                page[field] = page[field].replace("000001", f"{number:06d}")
            pages.append(page)
        manifest["pages"] = pages
        manifest["page_count"] = result["page_count"] = len(pages)
        with patch.object(stages, "read_object", return_value=json.dumps(manifest).encode()):
            queue = stages.plan_images({result["key"]: result}, {}, {})
        self.assertEqual(queue["shard_count"], 3)
        numbers = [p["p"] for shard in queue["shards"] for task in shard for p in task["pages"]]
        self.assertEqual(sorted(numbers), list(range(1, 1202)))
        self.assertEqual(max(len(task["pages"]) for shard in queue["shards"] for task in shard), 500)

    def test_missing_native_or_duplicate_page_is_rejected(self):
        result = self.render_fixture(native=True)
        manifest = json.loads(self.read(result["render_manifest"]))
        del manifest["pages"][0]["o"]
        with self.assertRaises(KeyError):
            stages.validate_render(result, manifest)
        manifest = json.loads(self.read(result["render_manifest"]))
        manifest["pages"][1]["p"] = 1
        with self.assertRaises(ValueError):
            stages.validate_render(result, manifest)

    def test_read_png_uses_resolve_without_api_metadata_lookup(self):
        data = b"image"
        meta = {"path": "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/ocr-input/page-000001.png",
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        response = Mock(content=data)
        with patch.object(stages, "get_session") as session, patch.object(stages, "hf_raise_for_status"):
            session.return_value.get.return_value = response
            self.assertEqual(stages.read_object(meta), data)
            url = session.return_value.get.call_args.args[0]
            self.assertIn("/resolve/objects/", url)
            self.assertNotIn("/api/", url)

    def test_transfers_scope_each_book_not_whole_bucket(self):
        result = self.render_fixture()
        with patch.object(stages, "HfApi") as api:
            stages.upload_objects(self.root / "render")
        call = api.return_value.sync_bucket.call_args
        self.assertTrue(call.args[1].endswith(str(Path(result["render_manifest"]["path"]).parent)))
        self.assertNotEqual(call.args[1], stages.BUCKET)

    def test_planner_includes_small_pdf_and_rebuilds_old_ocr_for_v2_index(self):
        item = {**self.item(), "source_bytes": 1024}
        self.assertEqual(stages.pending_render([item], {}, {}), [item])
        old = {**item, "status": "ready", "profile": pdf_ocr.asset_profile(),
               "ocr_manifest": "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/ocr-manifest.json"}
        self.assertEqual(stages.pending_render([item], {}, {item["key"]: old}), [item])
        self.assertEqual(stages.pending_render([item], {}, {item["key"]: {**old, "ocr_manifest": ""}}), [item])

    def test_failed_books_do_not_starve_untouched_backlog(self):
        failed = {**self.item(), "key": "repo\0a.pdf", "path": "a.pdf"}
        untouched = {**self.item(), "key": "repo\0z.pdf", "path": "z.pdf"}
        rendered = {failed["key"]: {**failed, "render_profile": stages.render_profile(), "status": "failed"}}
        self.assertEqual([x["key"] for x in stages.pending_render(
            [failed, untouched], rendered, {}, retry_failed=True)],
            [untouched["key"], failed["key"]])

        with patch.object(stages, "read_object", return_value=b"{}"), \
                patch.object(stages, "validate_render", return_value={"pages": []}):
            entries = {key: {**record, "status": "ready", "render_profile": "current", "profile": "p",
                             "source_sha256": "a" * 64, "page_count": 0,
                             "render_manifest": {"sha256": "b" * 64}}
                       for key, record in [(failed["key"], failed), (untouched["key"], untouched)]}
            queue = stages.plan_images(entries, {failed["key"]: {"status": "failed"}}, {}, limit=1)
        self.assertEqual(queue["books"][0]["key"], untouched["key"])

    def test_native_only_pdf_builds_complete_book_without_png_or_ocr_worker(self):
        source = self.root / "native.pdf"
        source.write_bytes(b"%PDF-native")
        bundle = self.root / "native-render"
        item = {**self.item(), "probe": {"page_count": 2, "page_chars": [80, 80],
                                        "classification": "native-text"}}
        def native(_source, page):
            return {"width": 500, "height": 700,
                    "blocks": [{"t": "竖排正文", "b": [.7, .1, .75, .5], "c": 1, "s": "native"}],
                    "text": f"竖排正文{page}"}
        with patch.object(pdf_ocr, "render_page", side_effect=AssertionError("native PDF must not render")), \
                patch.object(pdf_ocr, "native_page", side_effect=native):
            result = stages.render_book(item, source, bundle)
        self.assertIsNone(result["page_manifest"])
        self.store(bundle)
        with patch.object(stages, "read_object", side_effect=self.read):
            queue = stages.plan_images({result["key"]: result}, {}, {})
            self.assertEqual(queue["shard_count"], 0)
            self.assertEqual(queue["total_ocr_pages"], 0)
            finished = self.root / "native-finished"
            completed = stages.assemble_book(queue["books"][0], {}, finished)
            self.assertTrue(completed["ocr_manifest"])
            self.assertFalse(completed["stream"])
            self.store(finished)
            text = json.loads(gzip.decompress(self.read(json.loads(self.objects[completed["ocr_manifest"]])["book_text"])))
            self.assertEqual(len(text["pages"]), 2)
            self.assertEqual((text["version"], text["kind"], text["complete"]), (2, "pdf-book-text", True))
            self.assertTrue(text["pages"][0]["text_spans"])
            self.assertIn("text_spans", text["pages"][0])

    def test_sidecar_rebuild_preserves_rendered_stream_without_advertising_ocr(self):
        result = self.render_fixture()
        for status in ("rendered", "failed"):
            entry = {**result, "status": status}
            state = {"version": 1, "files": {entry["key"]: entry}}
            pdf_ocr.validate_manifest(state)
            index = build_index({"files": {}}, ocr_manifest=state)
            compact = index["f"][entry["key"]]
            self.assertEqual(compact["p"], result["page_manifest"]["path"])
            self.assertEqual(compact["b"], "vomebook/pdf-pages")
            self.assertNotIn("o", compact)

    def test_workflows_separate_rendering_and_recognition(self):
        root = Path(__file__).resolve().parents[1]
        render_text = (root / ".github/workflows/pdf-render-inputs.yml").read_text()
        ocr_text = (root / ".github/workflows/pdf-ocr-assets.yml").read_text()
        render = yaml.safe_load(render_text)
        ocr = yaml.safe_load(ocr_text)
        self.assertIn("pdf_ocr_stages.py render", render_text)
        self.assertNotIn("requirements-pdf-ocr", render_text)
        self.assertNotIn("poppler-utils", ocr_text)
        self.assertNotIn("fetch_and_parse", ocr_text)
        self.assertIn("Render PDF OCR Inputs", ocr_text)
        self.assertIn("!cancelled()", ocr["jobs"]["publish"]["if"])
        self.assertEqual(render["jobs"]["publish"]["concurrency"]["group"],
                         ocr["jobs"]["publish"]["concurrency"]["group"])

    def test_scheduled_render_drains_pending_in_jxl_batches(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / ".github/workflows/pdf-render-inputs.yml").read_text()
        workflow = yaml.safe_load(text)
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["limit"]["default"], "100")
        self.assertTrue(inputs["generate_jxl"]["default"])
        self.assertIn("github.event_name == 'schedule'", workflow["env"]["PDF_JXL_ENABLED"])
        self.assertEqual(workflow["jobs"]["plan"]["steps"][5]["env"]["CHECKPOINT"], "${{ inputs.checkpoint || '0' }}")

    def test_render_registry_stream_and_pending_ocr_are_committed_together(self):
        result = self.render_fixture()
        sidecar_path = self.root / "reader.json.gz"
        sidecar_path.write_bytes(gzip.compress(json.dumps({"v": 1, "f": {}}).encode()))
        api = Mock()
        api.repo_info.return_value.sha = "pinned-revision"
        api.hf_hub_download.return_value = str(sidecar_path)
        with patch.object(stages, "load_registry", side_effect=lambda *args: {"version": 1, "files": {}}):
            stages.save_registry(api, "test/repo", stages.RENDER_REGISTRY,
                                 {result["key"]: result}, publish_streams=True)
        call = api.create_commit.call_args.kwargs
        self.assertEqual(call["parent_commit"], "pinned-revision")
        operations = {op.path_in_repo: op.path_or_fileobj for op in call["operations"]}
        self.assertEqual(set(operations), {stages.RENDER_REGISTRY, "pdf_ocr_manifest.json", "reader_assets.json.gz"})
        ocr_state = json.loads(operations["pdf_ocr_manifest.json"])
        self.assertEqual(ocr_state["files"][result["key"]]["status"], "rendered")
        reader = json.loads(gzip.decompress(operations["reader_assets.json.gz"]))
        self.assertNotIn("o", reader["f"][result["key"]])

    def test_old_generation_progress_is_not_reused(self):
        result = self.render_fixture()
        progress = {result["key"]: {"generation": "old", "pages": {"1": {"o": "stale"}}}}
        with patch.object(stages, "read_object", side_effect=self.read):
            queue = stages.plan_images({result["key"]: result}, {}, progress)
        self.assertEqual(queue["total_ocr_pages"], 2)
        with self.assertRaisesRegex(ValueError, "generation"):
            stages.collect_progress(queue, [{"key": result["key"], "generation": "old", "pages": []}])

    def test_result_discovery_handles_one_flat_artifact_and_multiple_nested_artifacts(self):
        results = self.root / "results"
        results.mkdir()
        flat = results / "results-0.json"
        flat.write_text('{"version":1,"results":[]}')
        nested = results / "artifact-1" / "results-1.json"
        nested.parent.mkdir()
        nested.write_text('{"version":1,"results":[]}')
        self.assertEqual(set(stages.result_paths([], results, True)), {flat, nested})
        with self.assertRaisesRegex(ValueError, "no result artifacts"):
            stages.result_paths([], self.root / "missing", True)

    def test_layout_change_invalidates_saved_recognition_not_rendered_png(self):
        entry = self.render_fixture()
        with patch.object(stages, "read_object", side_effect=self.read):
            first = stages.plan_images({entry["key"]: entry}, {}, {})
            changed = stages.plan_images({entry["key"]: entry}, {}, {}, overrides={entry["key"]: {
                "default": {"writing_mode": "vertical-rl"}, "pages": {"1": {"rotation": 90}}}})
        self.assertNotEqual(first["books"][0]["profile"], changed["books"][0]["profile"])
        self.assertNotEqual(stages.generation_for(first["books"][0]), stages.generation_for(changed["books"][0]))
        self.assertEqual(first["books"][0]["render_manifest"], changed["books"][0]["render_manifest"])


if __name__ == "__main__":
    unittest.main()
