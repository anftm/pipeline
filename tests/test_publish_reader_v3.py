import copy
import hashlib
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import pymupdf
from PIL import Image

from scripts import pdf_reading_v3 as v3, pdf_text_layer as text, publish_reader_v3 as publish
from tests.test_pdf_text_layer import raw_page


class Store:
    def __init__(self):
        self.objects, self.writes = {}, []
        self.allowed = True
        self.fail_pointer = False

    def read_bytes(self, bucket, path):
        if (bucket, path) not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[(bucket, path)]

    def put_bytes(self, bucket, path, raw):
        if self.fail_pointer and path == publish.POINTER:
            raise OSError("pointer transport failed")
        self.objects[(bucket, path)] = raw
        self.writes.append((bucket, path))

    def assert_serialized_writer(self, protocol):
        if not self.allowed or protocol != publish.PROTOCOL:
            raise RuntimeError("no serialized writer")


class V3PublicationTests(unittest.TestCase):
    def test_unified_snapshot_retains_all_formats_and_promotions_preserve_it(self):
        base = {"v": 1, "f": {"ebook": {"s": 2, "m": "e", "p": "book.epub"},
                              "media": {"s": 2, "m": "a", "p": "audio.mp3"}}}
        packed = gzip.compress(publish.encode(base), mtime=0)
        self.store.put_bytes(publish.ASSETS, "reader-index/reader_assets.json.gz", packed)
        first = publish.unify(self.store, apply=True)
        pointer, catalog = publish.current(self.store)
        self.assertEqual(publish.pinned_base(self.store, catalog, {}), base)
        staged = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        publish.promote(self.store, staged["candidate"], first["generation"], apply=True)
        self.assertEqual(publish.current(self.store)[1]["base_sidecar"], catalog["base_sidecar"])
        self.assertTrue(publish.unify(self.store, apply=True)["unchanged"])

    def test_scoped_withdrawal_preserves_other_books_history_and_source_objects(self):
        first = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        one = publish.promote(self.store, first["candidate"], None, apply=True)
        second = publish.stage(self.store, self.candidate("b" * 64, "repo\0second.pdf"), self.bundle, apply=True)
        two = publish.promote(self.store, second["candidate"], one["generation"], apply=True)
        active = publish.current(self.store)[1]["files"]["repo\0second.pdf"]
        options = {"source_key": "repo\0second.pdf", "reading_generation": active["reading_generation"]}
        with patch.dict("os.environ", {"GITHUB_ACTOR": "reviewer"}):
            with self.assertRaisesRegex(ValueError, "stale withdrawal"):
                publish.withdraw(self.store, options, one["generation"], apply=True)
            result = publish.withdraw(self.store, options, two["generation"], apply=True)
        catalog = publish.current(self.store)[1]
        self.assertEqual(set(catalog["files"]), {"repo\0book.pdf"})
        self.assertEqual(catalog["withdrawal"]["resource"], active["resource"])
        self.assertIn((active["resource"]["bucket"], active["resource"]["path"]), self.store.objects)
        self.assertIn((publish.ASSETS, two["pointer"]["catalog"]["path"]), self.store.objects)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        self.store = Store()

    def candidate(self, source="a" * 64, key="repo\0book.pdf", label="Text"):
        root = Path("objects") / source[:2] / source / text.digest({"label": label})[:16]
        with pymupdf.open() as document:
            document.new_page(width=100, height=200)
            pdf = document.tobytes(no_new_id=True)
        primary_path = self.bundle / root / "document.pdf"
        primary_path.parent.mkdir(parents=True, exist_ok=True)
        primary_path.write_bytes(pdf)
        primary = publish.metadata(v3.shared.PDF_PAGES_BUCKET, primary_path.relative_to(self.bundle).as_posix(), pdf)
        mapping = v3.page_map(pdf, source)
        image = Image.new("RGB", (100, 200), "white")
        stream = io.BytesIO(); image.save(stream, "PNG")
        preview_path = self.bundle / root / "preview" / "page-000001.png"
        preview_path.parent.mkdir(parents=True, exist_ok=True); preview_path.write_bytes(stream.getvalue())
        preview = publish.metadata(v3.shared.PDF_PAGES_BUCKET, preview_path.relative_to(self.bundle).as_posix(), stream.getvalue())
        layer = text.from_page(raw_page(label), source, "en")
        layer_ref = v3.build_text_bundle([layer], [{"p": 1}], root, self.bundle)
        spec = {"source_key": key, "source_sha256": source, "primary": primary, "text_layer": layer_ref,
                "previews": [{"page": 1, "page_identity": mapping["pages"][0]["identity"],
                              "width": 100, "height": 200, "codec": "png", "resource": preview}]}
        def read(ref):
            return (self.bundle / ref["path"]).read_bytes()
        return v3.build_reading(spec, read, self.bundle)[0]

    def test_dry_run_has_no_remote_writes_then_stage_readback_and_promote(self):
        ref = self.candidate()
        dry = publish.stage(self.store, ref, self.bundle)
        self.assertFalse(dry["applied"])
        self.assertEqual(self.store.writes, [])
        staged = publish.stage(self.store, ref, self.bundle, apply=True)
        self.assertTrue(staged["applied"])
        self.assertNotIn((publish.ASSETS, publish.POINTER), self.store.objects)
        promoted = publish.promote(self.store, staged["candidate"], None, apply=True)
        pointer, catalog = publish.current(self.store)
        self.assertEqual(pointer["generation"], promoted["generation"])
        self.assertEqual(catalog["files"]["repo\0book.pdf"]["resource"], ref)
        self.assertEqual(self.store.writes[-1], (publish.ASSETS, publish.POINTER))

    def test_pinned_original_import_verifies_identity_size_and_checksum(self):
        raw = b"pinned original PDF bytes"
        source = hashlib.sha256(raw).hexdigest()
        path = f"objects/{source[:2]}/{source}/{'a' * 16}/document.pdf"
        spec = {"source_key": "VoiceOfML/books\0folder/book.pdf", "source_sha256": source,
                "primary": publish.metadata(v3.shared.PDF_PAGES_BUCKET, path, raw),
                "primary_source": {"repo": "VoiceOfML/books", "path": "folder/book.pdf", "revision": "b" * 40}}
        cached = self.bundle / "cached.pdf"
        cached.write_bytes(raw)
        with patch("huggingface_hub.HfApi") as api, patch("huggingface_hub.hf_hub_download", return_value=str(cached)) as download:
            api.return_value.get_paths_info.return_value = [Mock(size=len(raw))]
            publish.import_primary(spec, self.bundle)
            self.assertEqual((self.bundle / path).read_bytes(), raw)
            download.assert_called_once_with("VoiceOfML/books", "folder/book.pdf", repo_type="dataset", revision="b" * 40)
            wrong = copy.deepcopy(spec)
            wrong["primary_source"]["revision"] = "main"
            with self.assertRaisesRegex(ValueError, "pinned original-source identity"):
                publish.import_primary(wrong, self.bundle)
            wrong["primary_source"]["revision"] = "b" * 40
            wrong["primary_source"]["path"] = "other.pdf"
            with self.assertRaisesRegex(ValueError, "pinned original-source identity"):
                publish.import_primary(wrong, self.bundle)
            api.return_value.get_paths_info.return_value = [Mock(size=len(raw) + 1)]
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                publish.import_primary(spec, self.bundle)
            api.return_value.get_paths_info.return_value = [Mock(size=len(raw))]
            cached.write_bytes(b"x" * len(raw))
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                publish.import_primary(spec, self.bundle)
        self.assertEqual(self.store.writes, [])

    def test_two_books_merge_and_stale_parent_cannot_lose_the_first_update(self):
        first = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        second = publish.stage(self.store, self.candidate("b" * 64, "repo\0second.pdf"), self.bundle, apply=True)
        one = publish.promote(self.store, first["candidate"], None, apply=True)
        with self.assertRaisesRegex(ValueError, "stale expected parent"):
            publish.promote(self.store, second["candidate"], None, apply=True)
        two = publish.promote(self.store, second["candidate"], one["generation"], apply=True)
        pointer, catalog = publish.current(self.store)
        self.assertEqual(pointer["generation"], two["generation"])
        self.assertEqual(len(catalog["files"]), 2)
        self.assertEqual(catalog["parent_generation"], one["generation"])
        self.assertIn((publish.ASSETS, one["pointer"]["catalog"]["path"]), self.store.objects)

    def test_idempotent_stage_and_promotion_retain_generation(self):
        ref = self.candidate()
        first = publish.stage(self.store, ref, self.bundle, apply=True)
        second = publish.stage(self.store, ref, self.bundle, apply=True)
        self.assertEqual(first["candidate"], second["candidate"])
        one = publish.promote(self.store, first["candidate"], None, apply=True)
        writes = len(self.store.writes)
        retried = publish.promote(self.store, first["candidate"], None, apply=True)
        self.assertTrue(retried["unchanged"])
        self.assertEqual(retried["generation"], one["generation"])
        self.assertEqual(len(self.store.writes), writes)

    def test_interrupted_pointer_write_keeps_old_generation_and_can_retry(self):
        first = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        one = publish.promote(self.store, first["candidate"], None, apply=True)
        second = publish.stage(self.store, self.candidate(label="Corrected"), self.bundle, apply=True)
        self.store.fail_pointer = True
        with self.assertRaises(OSError):
            publish.promote(self.store, second["candidate"], one["generation"], apply=True)
        self.assertEqual(publish.current(self.store)[0]["generation"], one["generation"])
        self.store.fail_pointer = False
        promoted = publish.promote(self.store, second["candidate"], one["generation"], apply=True)
        self.assertNotEqual(promoted["generation"], one["generation"])

    def test_immutable_collision_and_damaged_dependency_block_promotion(self):
        ref = self.candidate()
        self.store.objects[(ref["bucket"], ref["path"])] = b"existing different object"
        with self.assertRaisesRegex(ValueError, "immutable object conflict"):
            publish.stage(self.store, ref, self.bundle, apply=True)
        self.assertNotIn((publish.ASSETS, publish.POINTER), self.store.objects)
        self.store.objects.pop((ref["bucket"], ref["path"]))
        staged = publish.stage(self.store, ref, self.bundle, apply=True)
        self.store.objects[(ref["bucket"], ref["path"])] = b"corrupt"
        with self.assertRaises(ValueError):
            publish.promote(self.store, staged["candidate"], None, apply=True)
        self.assertNotIn((publish.ASSETS, publish.POINTER), self.store.objects)

    def test_missing_or_denied_writer_has_no_mutation(self):
        ref = self.candidate()
        self.store.allowed = False
        with self.assertRaises(RuntimeError):
            publish.stage(self.store, ref, self.bundle, apply=True)
        self.assertEqual(self.store.writes, [])

    def test_projection_preserves_unrelated_v2_books(self):
        staged = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        publish.promote(self.store, staged["candidate"], None, apply=True)
        catalog = publish.current(self.store)[1]
        base = {"v": 1, "f": {"other": {"s": 4}, "repo\0book.pdf": {"s": 3}}}
        before = copy.deepcopy(base)
        projected = publish.project_sidecar(base, catalog)
        self.assertEqual(base, before)
        self.assertEqual(projected["f"]["other"], base["f"]["other"])
        self.assertTrue(projected["f"]["repo\0book.pdf"]["p"].endswith("/reading-manifest.json"))

    def test_pointer_digest_and_catalog_identity_cannot_be_forged(self):
        staged = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        report = publish.promote(self.store, staged["candidate"], None, apply=True)
        ref = report["pointer"]["catalog"]
        self.store.objects[(publish.ASSETS, ref["path"])] = b"different"
        with self.assertRaisesRegex(ValueError, "checksum"):
            publish.current(self.store)

    def test_rollback_creates_new_generation_and_preserves_forward_history(self):
        first = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        one = publish.promote(self.store, first["candidate"], None, apply=True)
        second = publish.stage(self.store, self.candidate(label="New text"), self.bundle, apply=True)
        two = publish.promote(self.store, second["candidate"], one["generation"], apply=True)
        rollback = publish.rollback(self.store, one["pointer"]["catalog"], two["generation"], apply=True)
        self.assertNotIn(rollback["generation"], {one["generation"], two["generation"]})
        catalog = publish.current(self.store)[1]
        self.assertEqual(catalog["rollback_target"], one["generation"])
        self.assertEqual(catalog["files"], publish.read_index(self.store, one["pointer"]["catalog"])["files"])
        with self.assertRaises(ValueError):
            publish.rollback(self.store, one["pointer"]["catalog"], two["generation"], apply=True)

    def test_receipts_require_current_identity_and_real_projection_acceptance(self):
        staged = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        report = publish.promote(self.store, staged["candidate"], None, apply=True)
        receipt = {"version": 1, "surface": "hf", "active": True, "generation": report["generation"],
                   "catalog_sha256": report["pointer"]["catalog"]["sha256"], "files": 1}
        with self.assertRaises(ValueError):
            publish.acknowledge(self.store, "hf", receipt, apply=True)
        accepted = {**receipt, "checked_url": "https://voiceofml-search.hf.space/api/reader-v3-status",
                    "projection_verified": True}
        result = publish.acknowledge(self.store, "hf", accepted, apply=True)
        self.assertEqual(result["acks"], {"hf": True, "pages": False})
        with self.assertRaises(ValueError):
            publish.acknowledge(self.store, "hf", {**accepted, "generation": "a" * 64}, apply=True)

    def test_ack_all_reports_projection_timeout_without_failing_publication(self):
        staged = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        promoted = publish.promote(self.store, staged["candidate"], None, apply=True)
        clock = [0]
        accepted = {"generation": promoted["generation"], "surface": "hf", "applied": True,
                    "acks": {"hf": True, "pages": False}}

        def observe(_store, surface, _client):
            if surface == "hf":
                return {"version": 1, "surface": "hf", "active": True,
                        "generation": promoted["generation"],
                        "catalog_sha256": promoted["pointer"]["catalog"]["sha256"], "files": 1}
            raise ValueError("Pages projection is still old")

        with patch.object(publish, "observe_consumer", side_effect=observe), \
                patch.object(publish, "acknowledge", return_value=accepted):
            report = publish.acknowledge_all(
                self.store, object(), apply=True, timeout_seconds=5,
                sleep_fn=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                clock=lambda: clock[0])
        self.assertFalse(report["complete"])
        self.assertEqual(report["pending"], ["pages"])
        self.assertEqual(report["reason"], "consumer-projection-timeout")

    def test_live_acceptance_checks_actual_sidecar_not_status_alone(self):
        import gzip
        import httpx
        staged = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        report = publish.promote(self.store, staged["candidate"], None, apply=True)
        catalog = publish.current(self.store)[1]
        sidecar = publish.project_sidecar({"v": 1, "f": {}}, catalog)
        receipt = {"version": 1, "surface": "hf", "active": True, "generation": report["generation"],
                   "catalog_sha256": report["pointer"]["catalog"]["sha256"], "files": 1}
        def handler(request):
            return httpx.Response(200, json=receipt if request.url.path.endswith("status") else sidecar)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            observed = publish.observe_consumer(self.store, "hf", client)
        self.assertTrue(observed["projection_verified"])
        publish.acknowledge(self.store, "hf", observed, apply=True)
        broken = {"v": 1, "f": {}}
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=
                receipt if request.url.path.endswith("status") else broken))) as client:
            with self.assertRaisesRegex(ValueError, "different reading"):
                publish.observe_consumer(self.store, "hf", client)
        packed = gzip.compress(publish.encode(sidecar))
        page_receipt = {**receipt, "surface": "pages", "reader_contract": "pdf-reading-v3-v1",
                        "sidecar_sha256": hashlib.sha256(packed).hexdigest()}
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,
                json=page_receipt) if request.url.path.endswith(".json") else httpx.Response(200, content=packed))) as client:
            observed = publish.observe_consumer(self.store, "pages", client)
        result = publish.acknowledge(self.store, "pages", observed, apply=True)
        self.assertEqual(result["acks"], {"hf": True, "pages": True})

    def test_v3_gc_protects_catalog_reading_and_all_uploaded_objects(self):
        from tests.test_reader_gc_graph import MemoryStore
        from scripts.reader_gc_graph import ReferenceGraph
        staged = publish.stage(self.store, self.candidate(), self.bundle, apply=True)
        publish.promote(self.store, staged["candidate"], None, apply=True)
        store = MemoryStore()
        for (bucket, path), raw in self.store.objects.items():
            store.objects[bucket][path] = raw
        report = ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertTrue(all(not value["candidates"] for value in report["buckets"].values()))

    def test_workflow_and_real_adapter_share_one_central_writer_contract(self):
        from unittest.mock import patch
        import os
        import yaml
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/reader-v3-publish.yml").read_text())
        self.assertEqual(workflow["name"], "Reader/桶 · Publish v3 Reading Generation")
        self.assertEqual(workflow["concurrency"]["group"], "reader-sidecar")
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        self.assertNotIn("workflow_run", workflow[True])
        self.assertFalse(workflow[True]["workflow_dispatch"]["inputs"]["apply"]["default"])
        self.assertIn("retire-ocr-progress", workflow[True]["workflow_dispatch"]["inputs"]["operation"]["options"])
        operation_step = next(step for step in workflow["jobs"]["publish"]["steps"]
                              if step.get("id") == "operation")
        self.assertIn("retire-ocr-progress)", operation_step["run"])
        # Correction is suspended from automation: schedules admit OCR only.
        script = next(step["run"] for step in workflow["jobs"]["publish"]["steps"]
                      if step.get("id") == "operation")
        self.assertIn("OPERATION=auto\n", script)
        self.assertNotIn("OPERATION=correct", script)
        self.assertNotIn("cycle", workflow[True]["workflow_dispatch"]["inputs"]["operation"]["options"])
        ack_step = next(step for step in workflow["jobs"]["publish"]["steps"]
                        if step.get("id") == "ack")
        self.assertIn("operation == 'auto'", ack_step["if"])
        self.assertIn("complete=", ack_step["run"])
        self.assertIn("retire", workflow[True]["workflow_dispatch"]["inputs"]["operation"]["options"])
        self.assertIn("retire|retire-ocr-progress)", operation_step["run"])
        self.assertFalse(any(step.get("name", "").startswith("Retire acknowledged")
                             for step in workflow["jobs"]["publish"]["steps"]))
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "anftm/pipeline", "GITHUB_RUN_ID": "123",
               "GITHUB_WORKFLOW": workflow["name"], "READER_V3_WRITE_PROTOCOL": publish.PROTOCOL,
               "GITHUB_WORKFLOW_REF": "anftm/pipeline/.github/workflows/reader-v3-publish.yml@refs/heads/main"}
        store = publish.CentralHubStore.__new__(publish.CentralHubStore)
        with patch.dict(os.environ, env):
            store.assert_serialized_writer(publish.PROTOCOL)
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "other/pipeline"}):
                with self.assertRaises(RuntimeError):
                    store.assert_serialized_writer(publish.PROTOCOL)
            with patch.dict(os.environ, {"GITHUB_WORKFLOW_REF": env["GITHUB_WORKFLOW_REF"].replace("main", "feature")}):
                with self.assertRaises(RuntimeError):
                    store.assert_serialized_writer(publish.PROTOCOL)

    def test_build_stage_reuses_raw_text_and_generates_whole_book_preview(self):
        ref = self.candidate()
        reader = publish.CandidateReader(self.store, self.bundle)
        reading = v3.verify_reading(ref, reader)
        raw = raw_page("Original text")
        raw_ref = publish.metadata(v3.shared.PDF_PAGES_BUCKET,
                                   "objects/aa/" + "a" * 64 + "/" + "e" * 16 + "/ocr/page-000001.json.gz",
                                   gzip.compress(json.dumps(raw).encode()), "provenance")
        self.store.put_bytes(raw_ref["bucket"], raw_ref["path"], gzip.compress(json.dumps(raw).encode()))
        ocr = {"version": 1, "kind": "pdf-ocr", "complete": True, "source_sha256": "a" * 64,
               "language": "en", "page_count": 1, "pages": [{"p": 1, "source": "ocr", "o": raw_ref["path"],
                                                             "os": raw_ref["sha256"], "ob": raw_ref["bytes"]}]}
        packed = publish.encode(ocr)
        ocr_ref = publish.metadata(v3.shared.PDF_PAGES_BUCKET,
                                   "objects/aa/" + "a" * 64 + "/" + "e" * 16 + "/ocr-manifest.json", packed, "provenance")
        self.store.put_bytes(ocr_ref["bucket"], ocr_ref["path"], packed)
        primary = reading["primary"]["resource"]
        self.store.put_bytes(primary["bucket"], primary["path"], reader(primary))
        spec = {"source_key": "repo\0book.pdf", "source_sha256": "a" * 64,
                "primary": primary, "ocr_manifest": ocr_ref, "dpi": 150}
        with tempfile.TemporaryDirectory() as directory:
            report = publish.build_stage(self.store, spec, Path(directory), apply=True)
        self.assertEqual(report["components"]["preview"], "ready")
        self.assertEqual(report["components"]["text"], "ready")
        self.assertNotIn((publish.ASSETS, publish.POINTER), self.store.objects)
        publish.promote(self.store, report["candidate"], None, apply=True)

    def preview_candidate(self, source="a" * 64, key="repo\0book.pdf"):
        full = self.candidate(source, key)
        manifest = v3.decode((self.bundle / full["path"]).read_bytes())
        previews = [page for part in manifest["preview"]["partitions"]
                    for page in v3.decode((self.bundle / part["resource"]["path"]).read_bytes())["pages"]]
        spec = {"source_key": key, "source_sha256": source, "primary": manifest["primary"]["resource"],
                "previews": previews, "require_complete_preview": True}
        return v3.build_reading(spec, lambda ref: (self.bundle / ref["path"]).read_bytes(), self.bundle)[0]


    def test_preview_can_publish_before_ocr_and_later_fill_text_without_rerender(self):
        preview = self.preview_candidate()
        staged = publish.stage(self.store, preview, self.bundle, apply=True)
        first = publish.promote(self.store, staged["candidate"], None, apply=True)
        before = v3.verify_reading(preview, lambda ref: self.store.read_bytes(ref["bucket"], ref["path"]))
        self.assertIsNone(before["text_layer"])
        self.assertEqual(before["components"]["text"], "pending")
        self.assertEqual(before["components"]["preview"], "ready")
        # The independently built text bundle uses exactly the existing page
        # resources. No image/SVG generation is needed for this later upgrade.
        full = self.candidate()
        full_manifest = v3.decode((self.bundle / full["path"]).read_bytes())
        ocr = {"source_sha256": "a" * 64, "page_count": 1, "text_layer": full_manifest["text_layer"]}
        raw = publish.encode(ocr)
        ocr_ref = publish.metadata(v3.shared.PDF_PAGES_BUCKET,
                                  "objects/aa/" + "a" * 64 + "/" + "e" * 16 + "/ocr-manifest.json", raw, "provenance")
        self.store.put_bytes(ocr_ref["bucket"], ocr_ref["path"], raw)
        # Publish already-built text resources as an OCR assembler would.
        publish.stage(self.store, full, self.bundle, apply=True)
        spec = {"source_key": "repo\0book.pdf", "source_sha256": "a" * 64,
                "primary": before["primary"]["resource"], "ocr_manifest": ocr_ref,
                "reuse_reading": preview, "vector_previews": True}
        with patch.object(v3, "generate_previews", side_effect=AssertionError("must reuse previews")):
            upgraded = publish.build_stage(self.store, spec, self.bundle / "text-upgrade", apply=True)
        publish.promote(self.store, upgraded["candidate"], first["generation"], apply=True)
        active = publish.current(self.store)[1]["files"]["repo\0book.pdf"]
        after = v3.verify_reading(active["resource"], lambda ref: self.store.read_bytes(ref["bucket"], ref["path"]))
        self.assertEqual(after["text_layer"], full_manifest["text_layer"])
        self.assertEqual(after["components"]["text"], "ready")
        with self.assertRaisesRegex(ValueError, "preview-only publication cannot replace"):
            publish.promote(self.store, staged["candidate"], publish.current(self.store)[0]["generation"], apply=True)


    def test_preview_only_builder_never_requires_or_reads_ocr(self):
        ref = self.candidate()
        manifest = v3.decode((self.bundle / ref["path"]).read_bytes())
        primary = manifest["primary"]["resource"]
        self.store.put_bytes(primary["bucket"], primary["path"], (self.bundle / primary["path"]).read_bytes())
        spec = {"source_key": "repo\0book.pdf", "source_sha256": "a" * 64,
                "primary": primary, "preview_only": True, "vector_previews": True}
        with patch.object(v3, "backfill_text", side_effect=AssertionError("must not require OCR")), \
                patch.object(publish, "native_ocr", side_effect=AssertionError("must not extract text")):
            result = publish.build_stage(self.store, spec, self.bundle / "preview-only", apply=True)
        self.assertEqual(result["components"]["preview"], "ready")
        self.assertEqual(result["components"]["text"], "pending")


    def test_preview_only_catalog_and_inflight_task_keep_gc_reference_closure(self):
        from tests.test_reader_gc_graph import MemoryStore
        from scripts.reader_gc_graph import ReferenceGraph
        preview = self.preview_candidate()
        staged = publish.stage(self.store, preview, self.bundle, apply=True)
        publish.promote(self.store, staged["candidate"], None, apply=True)
        store = MemoryStore()
        for (bucket, path), raw in self.store.objects.items():
            store.objects[bucket][path] = raw
        report = ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertTrue(all(not value["candidates"] for value in report["buckets"].values()))


    def test_text_upgrade_cannot_silently_change_existing_preview_resources(self):
        preview = self.preview_candidate()
        staged = publish.stage(self.store, preview, self.bundle, apply=True)
        first = publish.promote(self.store, staged["candidate"], None, apply=True)
        altered = publish.stage(self.store, self.candidate(label="Different preview prefix"), self.bundle, apply=True)
        with self.assertRaisesRegex(ValueError, "preserve the complete existing preview"):
            publish.promote(self.store, altered["candidate"], first["generation"], apply=True)
        self.assertEqual(publish.current(self.store)[0]["generation"], first["generation"])


    def test_candidate_read_cache_is_bounded_and_fresh_readback_detects_mutation(self):
        raw = b"12345"
        ref = publish.metadata(v3.shared.PDF_PAGES_BUCKET,
                               "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/preview/page-000001.png", raw)
        self.store.put_bytes(ref["bucket"], ref["path"], raw)
        reader = publish.CandidateReader(self.store, cache_bytes=5)
        with patch.object(self.store, "read_bytes", wraps=self.store.read_bytes) as read:
            self.assertEqual(reader(ref), raw)
            self.assertEqual(reader(ref), raw)
            self.assertEqual(read.call_count, 1)
        other = {**ref, "path": ref["path"].replace("000001", "000002")}
        self.store.put_bytes(other["bucket"], other["path"], raw)
        reader(other)
        self.assertEqual(reader.cached_bytes, 5)
        self.assertEqual(len(reader.cache), 1)
        self.store.put_bytes(ref["bucket"], ref["path"], b"bad")
        fresh = publish.CandidateReader(self.store)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            fresh(ref)
        reader.close()
        fresh.close()


    def test_batch_upload_still_requires_independent_complete_remote_readback(self):
        ref = self.candidate()
        batches = []
        def upload(bucket, additions):
            batches.append(list(additions))
            for local, path in additions:
                self.store.put_bytes(bucket, path, Path(local).read_bytes())
        self.store.put_files = upload
        report = publish.stage(self.store, ref, self.bundle, apply=True)
        self.assertTrue(report["applied"])
        self.assertEqual(len(batches), 1)
        self.assertGreater(len(batches[0]), 1)
        # A new candidate with an upload that corrupts any one preview must fail
        # before a candidate record/catalog can be promoted.
        ref2 = self.candidate("b" * 64, "repo\0second.pdf")
        def corrupt(bucket, additions):
            for local, path in additions:
                raw = b"bad" if path.endswith(".png") else Path(local).read_bytes()
                self.store.put_bytes(bucket, path, raw)
        self.store.put_files = corrupt
        with self.assertRaises(ValueError):
            publish.stage(self.store, ref2, self.bundle, apply=True)
        self.assertNotIn((publish.ASSETS, publish.POINTER), self.store.objects)

    def test_preview_promotion_refuses_a_newly_completed_ocr_route(self):
        preview = self.preview_candidate()
        staged = publish.stage(self.store, preview, self.bundle, apply=True)
        self.store.put_bytes(publish.ASSETS, "reader-index/pdf_ocr_manifest.json", publish.encode(
            {"version": 1, "files": {"repo\0book.pdf": {"status": "ready", "ocr_manifest": "retained"}}}))
        with self.assertRaisesRegex(ValueError, "cannot hide already available OCR"):
            publish.promote(self.store, staged["candidate"], None, apply=True)
        self.assertNotIn((publish.ASSETS, publish.POINTER), self.store.objects)


if __name__ == "__main__":
    unittest.main()
