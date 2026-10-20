import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from huggingface_hub import HfApi
import yaml

from scripts import publish_pdf_ocr_assets as publication
from scripts import reader_gc_graph as gc
from scripts import reader_bucket
from scripts import pdf_ocr_stages as stages


ASSETS, PDF, INPUT = gc.READER_BUCKETS
SIDECAR = "reader-index/reader_assets.json.gz"


class MemoryStore:
    def __init__(self):
        self.objects = {bucket: {} for bucket in gc.BUCKETS}
        self.put(ASSETS, SIDECAR, {"v": 1, "f": {}})

    def put(self, bucket, path, payload):
        raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        self.objects[bucket][path] = gzip.compress(raw) if path.endswith(".gz") else raw

    def read_bytes(self, bucket, path):
        return self.objects[bucket][path]

    def list_files(self, bucket, prefixes):
        return {path for path in self.objects[bucket] if path.startswith(prefixes)}

    def put_json(self, bucket, path, payload):
        self.put(bucket, path, payload)

    def delete(self, bucket, paths):
        for path in paths:
            self.objects[bucket].pop(path, None)


class ReaderGcGraphTests(unittest.TestCase):
    def test_sharded_ocr_progress_roots_are_accepted_and_keep_page_objects(self):
        store = MemoryStore()
        key = "VoiceOfML/Test\0book.pdf"
        page_path = "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/ocr/page-000001.json.gz"
        generation = "c" * 64
        store.put(PDF, page_path, b"ocr page")
        store.put(ASSETS, "reader-index/pdf_ocr_progress/index.json", {
            "version": 1, "kind": "pdf-ocr-progress-index",
            "books": {key: {"generation": generation, "pages": [1]}},
            "migrated_from": {"path": "reader-index/pdf_ocr_progress.json",
                              "sha256": "d" * 64, "bytes": 1024, "books": 1, "pages": 1},
            "legacy_retired": True,
        })
        store.put(ASSETS, "reader-index/pdf_ocr_progress.json", {
            "version": 1, "kind": "pdf-ocr-progress-legacy-tombstone", "files": {},
            "superseded_by": "reader-index/pdf_ocr_progress/index.json"})
        store.put(ASSETS, "reader-index/pdf_ocr_progress/books/aa/" + "e" * 64 + ".json.gz", {
            "version": 1, "kind": "pdf-ocr-progress-book", "key": key,
            "files": {key: {"generation": generation, "pages": {
                "1": {"p": 1, "o": page_path, "os": "f" * 64, "ob": 8}}}}})
        graph = gc.ReferenceGraph(store)
        report = graph.build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertIn((PDF, page_path), graph.references)

    def test_retired_input_root_releases_review_evidence_pngs(self):
        store = MemoryStore()
        source = "a" * 64
        root = f"objects/aa/{source}/{'b' * 16}/ocr-input"
        for n in (1, 2):
            store.put(INPUT, f"{root}/page-00000{n}.png", b"input")
        store.put(ASSETS, "reader-index/v3/retirement.json", {
            "version": 1, "kind": "reader-v3-retirement", "resources": [], "retired_paths": [],
            "replacement_acks": {"hf": True, "pages": True}, "retired_input_roots": {root: {
                "bucket": INPUT, "root": root, "source_sha256": source, "pages": 2}}})
        store.put(ASSETS, "reader-index/open-review.json", {"resources": [{
            "bucket": INPUT, "path": f"{root}/page-000001.png", "role": "review"}]})
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(sorted(report["buckets"][INPUT]["candidates"]),
                         [f"{root}/page-000001.png", f"{root}/page-000002.png"])

    def test_retired_input_with_open_review_evidence_is_collectible(self):
        store = MemoryStore()
        source = "a" * 64
        path = f"objects/aa/{source}/{'b' * 16}/ocr-input/page-000001.png"
        store.put(INPUT, path, b"input")
        store.put(ASSETS, "reader-index/v3/retirement.json", {
            "version": 1, "kind": "reader-v3-retirement", "resources": [], "retired_paths": [],
            "replacement_acks": {"hf": True, "pages": True}, "retired_inputs": {path: {
                "bucket": INPUT, "path": path, "source_sha256": source}}})
        store.put(ASSETS, "reader-index/open-review.json", {"resources": [{
            "bucket": INPUT, "path": path, "role": "review"}]})
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertIn(path, report["buckets"][INPUT]["candidates"])

    def test_pinned_all_format_sidecar_keeps_resources_without_mutable_base_reference(self):
        store = MemoryStore()
        document = "documents/text/txt/" + "a" * 64 + "/document.txt"
        snapshot = "reader-index/snapshots/" + "b" * 64 + "/reader_assets.json.gz"
        store.put(ASSETS, document, b"pinned text")
        store.put(ASSETS, snapshot, {"v": 1, "f": {"repo\0document.txt": {"s": 2, "m": "t", "p": document, "b": ASSETS}}})
        store.put(ASSETS, "reader-index/v3/current.json", {"resources": [{"bucket": ASSETS, "path": snapshot}]})
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["buckets"][ASSETS]["candidates"], [])

    def test_v3_automation_and_model_proposals_keep_pinned_inputs(self):
        store = MemoryStore()
        primary = "derived/migration-test/" + "a" * 32 + "/document.pdf"
        source = "objects/aa/source/ocr/page-000001.json.gz"
        store.put(PDF, primary, b"pdf")
        store.put(PDF, source, b"raw text")
        def ref(path):
            return {"bucket": PDF, "path": path, "sha256": "a" * 64, "bytes": 1, "role": "provenance"}
        store.put(ASSETS, "reader-index/v3/automation.json", {"version": 1, "kind": "reader-v3-automation",
            "tasks": {"task": {"spec": {"primary": ref(primary)}}}, "days": {}})
        store.put(ASSETS, "reader-index/v3/corrections/state.json", {"version": 1, "kind": "reader-v3-correction-state",
            "tasks": {"task": {"text_layer": ref(source)}}, "days": {}})
        store.put(ASSETS, "reader-index/v3/corrections/proposals/task/proposal.json", {
            "version": 1, "kind": "pdf-text-correction", "page": 1,
            "base_generation": "a" * 64, "raw_sha256": "b" * 64, "page_identity": "c" * 64,
            "replacements": [], "evidence": {"primary": ref(primary)}})
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertTrue(all(not value["candidates"] for value in report["buckets"].values()))

    def test_native_djvu_reading_primary_is_retained_and_orphan_collectable(self):
        import hashlib, shutil, tempfile
        from pathlib import Path
        from scripts import pdf_reading_v3 as v3
        from tests.test_djvu_pages import TOOLS, build_djvu
        if not TOOLS:
            self.skipTest("djvulibre command-line tools are not installed")
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            raw = build_djvu(work).read_bytes()
            source = hashlib.sha256(raw).hexdigest()
            primary = f"objects/{source[:2]}/{source}/{'b' * 16}/document.djvu"
            orphan = f"objects/{source[:2]}/{source}/{'c' * 16}/document.djvu"
            store.put(PDF, primary, raw)
            store.put(PDF, orphan, raw)
            ref = {"bucket": PDF, "path": primary, "sha256": source, "bytes": len(raw), "role": "runtime"}
            read = lambda item: store.read_bytes(item["bucket"], item["path"])
            bundle = work / "bundle"
            spec = v3.generate_previews({"source_key": "book", "source_sha256": source, "primary": ref}, read, bundle)
            for path in (item for item in bundle.rglob("*") if item.is_file()):
                store.put(PDF, path.relative_to(bundle).as_posix(), path.read_bytes())
            reading, _ = v3.build_reading(spec, read, bundle)
            for path in (item for item in bundle.rglob("*") if item.is_file()):
                store.put(PDF, path.relative_to(bundle).as_posix(), path.read_bytes())
        store.put(ASSETS, SIDECAR, {"v": 1, "f": {"book": {"s": 2, "m": "p", "p": reading["path"], "b": PDF}}})
        graph = gc.ReferenceGraph(store)
        report = graph.build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertIn((PDF, primary), graph.references)
        self.assertIn(orphan, report["buckets"][PDF]["candidates"])

    def test_v3_text_review_retains_cross_bucket_evidence_and_raw_provenance(self):
        from scripts import pdf_reading_v3 as v3, pdf_text_layer as text
        from tests.test_pdf_text_layer import raw_page
        store = MemoryStore()
        raw_path = "objects/aa/raw/ocr/page-000001.json.gz"
        raw = raw_page("Text", .1)
        raw_bytes = gzip.compress(json.dumps(raw).encode())
        raw_ref = {"bucket": PDF, "path": raw_path, "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                   "bytes": len(raw_bytes), "role": "provenance"}
        store.objects[PDF][raw_path] = raw_bytes
        layer = text.from_page(raw, "a" * 64, "en", raw_ref)
        evidence_path = "objects/aa/input/ocr-input/page-000001.png"
        evidence = b"lossless evidence"
        store.put(INPUT, evidence_path, evidence)
        page = {"p": 1, "i": evidence_path, "is": hashlib.sha256(evidence).hexdigest(), "ib": len(evidence)}
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            ref = v3.build_text_bundle([layer], [page], Path("objects/aa/" + "a" * 64 + "/" + "b" * 16), bundle)
            for path in bundle.rglob("*"):
                if path.is_file():
                    store.objects[PDF][path.relative_to(bundle).as_posix()] = path.read_bytes()
        store.put(ASSETS, "reader-index/text-generation.json", {"resources": [ref]})
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertTrue(all(not b["candidates"] for b in report["buckets"].values()))

    def test_pdf_range_only_reference_protects_png_jxl_and_native_text(self):
        store = MemoryStore()
        root = "objects/aa/book/render"
        descriptor = f"{root}/render-range-000001-000001.json"
        png, jxl, text = f"{root}/ocr-input/page-000001.png", f"{root}/pages/page-000001.jxl", f"{root}/ocr/page-000001.json.gz"
        store.put(ASSETS, "reader-index/pdf_render_progress.json", {"version": 1, "files": {
            "book": {"ranges": {"000001-000001": {"path": descriptor}}},
        }})
        store.put(PDF, descriptor, {"version": 1, "kind": "pdf-render-range", "path": "upstream.pdf", "pages": [
            {"p": 1, "i": png, "j": jxl, "o": text, "ob": 50},
        ]})
        for bucket, path in ((INPUT, png), (INPUT, jxl), (PDF, text)):
            store.put(bucket, path, b"payload")
        store.put(INPUT, "objects/unused/ocr-input/page-000001.png", b"orphan")
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["buckets"][INPUT]["candidates"], ["objects/unused/ocr-input/page-000001.png"])
        self.assertEqual(report["buckets"][PDF]["candidates"], [])

    def test_all_ebook_extensions_and_fallback_resources_are_kept(self):
        store = MemoryStore()
        for ext in ("epub", "mobi", "azw3", "fb2", "chm"):
            root = f"chapters/ebook/{ext}/hash/version"
            manifest = f"{root}/chapter-manifest.json"
            fallback = f"native/ebook/{ext}/hash/document.epub"
            store.put(ASSETS, f"chapters/ebook/{ext}/index.json", {"files": [
                {"manifest": manifest, "fallback": fallback, "bucket": ASSETS},
            ]})
            store.put(ASSETS, manifest, {"kind": "epub-chapters", "chapters": [
                {"path": "chapters/chapter-0001.xhtml"}], "search_index": {"path": "epub-search-index.json.gz"}})
            for path in ("chapters/chapter-0001.xhtml", "resources/chapter-0001/image.jpg",
                         "resources/chapter-0001/font.woff2", "epub-search-index.json.gz"):
                store.put(ASSETS, f"{root}/{path}", b"resource")
            store.put(ASSETS, fallback, b"epub")
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["buckets"][ASSETS]["candidates"], [])

    def test_static_pdf_web_office_text_spreadsheet_and_media_bundles(self):
        store = MemoryStore()
        for category, name in (("documents/pdf/pptx", "document.pdf"),
                               ("documents/web/mhtml", "document.html"),
                               ("documents/office/docx", "document.docx"),
                               ("documents/text/txt", "document.txt"),
                               ("documents/spreadsheet/xlsx", "document.html"),
                               ("media/audio/mp3", "audio.mp3"),
                               ("media/video/mp4", "video.mp4"),
                               ("media/swf/swf", "animation.swf")):
            root = f"{category}/hash"
            store.put(ASSETS, f"{category}/index.json", {"files": [{"object": f"{root}/{name}"}]})
            store.put(ASSETS, f"{root}/{name}", b"document")
            store.put(ASSETS, f"{root}/resources/cover.png", b"resource")
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["buckets"][ASSETS]["candidates"], [])

    def test_image_manifest_is_not_assumed_to_be_a_pdf_manifest(self):
        store = MemoryStore()
        root = "pages/image/tiff/hash"
        store.put(ASSETS, SIDECAR, {"v": 1, "f": {"image": {
            "s": 2, "p": f"{root}/page-manifest.json", "b": ASSETS}}})
        store.put(ASSETS, f"{root}/page-manifest.json", {"kind": "image-page-stream", "page_count": 1,
            "pages": [{"path": "pages/page-000001.webp"}]})
        store.put(ASSETS, f"{root}/pages/page-000001.webp", b"image")
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])

    def test_sidecar_associated_pdf_and_ocr_have_separate_buckets(self):
        store = MemoryStore()
        manifest = "objects/aa/book/stream/page-manifest.json"
        pdf = "documents/pdf/ppt/hash/document.pdf"
        ocr = "objects/aa/book/text/ocr-manifest.json"
        store.put(ASSETS, SIDECAR, {"v": 1, "f": {"book": {
            "p": manifest, "b": PDF, "pd": pdf, "pdb": ASSETS, "o": ocr, "ob": PDF}}})
        store.put(PDF, manifest, {"kind": "pdf-pages", "page_count": 1})
        store.put(PDF, "objects/aa/book/stream/pages/page-000001.webp", b"image")
        store.put(ASSETS, pdf, b"pdf")
        store.put(PDF, ocr, {"kind": "pdf-ocr", "book_text": {"path": "objects/aa/book/text/ocr/book-text.json.gz"}})
        store.put(PDF, "objects/aa/book/text/ocr/book-text.json.gz", b"text")
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertTrue(all(not data["candidates"] for data in report["buckets"].values()))

    def test_derived_pdf_shard_checkpoint_and_pending_review_are_roots(self):
        store = MemoryStore()
        pdf = "derived/weekly-djvu/hash/document.pdf"
        evidence = "objects/aa/book/render/ocr-input/page-000001.png"
        store.put(PDF, "reader-index/derived_pdf_manifest.json", {"files": [{"new_path": pdf, "bucket": PDF}]})
        store.put(PDF, pdf, b"pdf")
        store.put(ASSETS, "reader-index/review_queue.json", {"resources": [{"path": evidence, "bucket": INPUT}]})
        store.put(INPUT, evidence, b"evidence")
        store.put(ASSETS, "pages/image/index.json", {"files": []})
        store.put(ASSETS, "pages/image/index-00.json", {"files": [{"object": "pages/image/png/partial/image.webp"}]})
        store.put(ASSETS, "pages/image/png/partial/image.webp", b"checkpoint")
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertTrue(all(not data["candidates"] for data in report["buckets"].values()))

    def test_missing_or_invalid_manifest_blocks_all_candidates(self):
        for raw in (None, b"invalid JSON", b'{"kind":"future-format"}'):
            with self.subTest(raw=raw):
                store = MemoryStore()
                path = "objects/book/render-manifest.json"
                store.put(ASSETS, SIDECAR, {"v": 1, "f": {"book": {"p": path, "b": PDF}}})
                if raw is not None:
                    store.put(PDF, path, raw)
                store.put(INPUT, "objects/unclaimed/input.png", b"input")
                report = gc.ReferenceGraph(store).build()
                self.assertFalse(report["graph_complete"])
                self.assertEqual(report["buckets"][INPUT]["candidates"], [])

    def test_permission_failure_or_skipped_input_is_incomplete(self):
        store = MemoryStore()
        original = store.list_files
        def denied(bucket, prefixes):
            if bucket == INPUT:
                raise PermissionError("denied")
            return original(bucket, prefixes)
        with patch.object(store, "list_files", side_effect=denied):
            self.assertFalse(gc.ReferenceGraph(store).build()["graph_complete"])
        self.assertFalse(gc.ReferenceGraph(store, include_input=False).build()["graph_complete"])

    def test_root_change_and_new_root_during_scan_converge_on_latest_versions(self):
        store = MemoryStore()
        original = store.read_bytes
        reads = 0
        def racing_read(bucket, path):
            nonlocal reads
            if path == SIDECAR:
                reads += 1
                if reads == 2:
                    store.put(ASSETS, SIDECAR, {"v": 1, "f": {"new": {"s": 4}}})
            return original(bucket, path)
        with patch.object(store, "read_bytes", side_effect=racing_read):
            report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertGreaterEqual(reads, 3)
        original_list = store.list_files
        pdf_lists = 0
        def racing_list(bucket, prefixes):
            nonlocal pdf_lists
            if bucket == PDF:
                pdf_lists += 1
                if pdf_lists == 2:
                    store.put(PDF, "reader-index/new.json", {"files": {}})
            return original_list(bucket, prefixes)
        with patch.object(store, "list_files", side_effect=racing_list):
            report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertIn(f"{PDF}:reader-index/new.json", report["roots"])

    def test_root_that_never_stops_changing_blocks_the_report(self):
        store = MemoryStore()
        original = store.read_bytes
        reads = 0
        def churning_read(bucket, path):
            nonlocal reads
            if path == SIDECAR:
                reads += 1
                store.put(ASSETS, SIDECAR, {"v": 1, "f": {str(reads): {"s": reads}}})
            return original(bucket, path)
        with patch.object(store, "read_bytes", side_effect=churning_read):
            report = gc.ReferenceGraph(store, max_rounds=3).build()
        self.assertFalse(report["graph_complete"])
        self.assertIn("root inventory did not stabilize after 3 rounds", report["blockers"])

    def test_root_in_new_directory_found_by_full_pass_is_read(self):
        store = MemoryStore()
        roots_only = []
        def list_roots(bucket, parents):
            roots_only.append(bucket)
            return {path: None for path in store.objects[bucket] if gc.is_root(path) and (
                path.startswith("reader-index/") or any(
                    path.startswith(parent) and "/" not in path[len(parent):] for parent in parents))}
        store.list_roots = list_roots
        page = "chapters/ebook/new/objects/book/chapter-0001.xhtml"
        store.put(ASSETS, page, b"chapter")
        original = store.list_files
        calls = 0
        def late_index(bucket, prefixes):
            nonlocal calls
            if bucket == ASSETS:
                calls += 1
                if calls == 2:
                    store.put(ASSETS, "chapters/ebook/new/index.json", {"files": {"book": {"path": page}}})
            return original(bucket, prefixes)
        with patch.object(store, "list_files", side_effect=late_index):
            report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertIn(f"{ASSETS}:chapters/ebook/new/index.json", report["roots"])
        self.assertEqual(report["buckets"][ASSETS]["candidates"], [])
        self.assertTrue(roots_only)

    def test_deadline_writes_an_incomplete_report_instead_of_running_on(self):
        store = MemoryStore()
        store.put(ASSETS, "reader-index/manifest.json", {"files": {}})
        report = gc.ReferenceGraph(store, deadline=gc.time.monotonic() - 1).build()
        self.assertFalse(report["graph_complete"])
        self.assertTrue(any("deadline reached" in blocker for blocker in report["blockers"]))

    def test_handoff_published_after_catalog_read_resolves_without_blocker(self):
        store = MemoryStore()
        root = "objects/aa/book/render"
        store.put(PDF, root + "/pages/page-000001.webp", b"page")
        catalog = {"version": 1, "kind": "reader-catalog", "current": {"generation": "old"},
                   "generations": {"old": {"generation": "old", "status": "current", "references": []}}}
        store.put(ASSETS, "reader-index/reader_catalog.json", catalog)
        store.put(ASSETS, "reader-index/processing/released.json", {
            "kind": "reader-processing-roots", "status": "released", "generation": "new",
            "resources": [{"bucket": PDF, "root": root}]})
        original = store.read_bytes
        def publish_after_first_catalog_read(bucket, path):
            raw = original(bucket, path)
            if path == "reader-index/processing/released.json":
                store.put(ASSETS, "reader-index/reader_catalog.json", {
                    **catalog, "current": {"generation": "new"}, "generations": {
                        "old": {"generation": "old", "status": "superseded", "references": []},
                        "new": {"generation": "new", "status": "current", "references": []}}})
            return raw
        with patch.object(store, "read_bytes", side_effect=publish_after_first_catalog_read):
            report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])

    def test_object_listed_after_initial_inventory_is_not_missing(self):
        store = MemoryStore()
        path = "objects/late/book/document.pdf"
        store.put(ASSETS, "reader-index/manifest.json", {"files": {"book": {"object": path, "bucket": PDF}}})
        original = store.list_files
        calls = 0
        def late_list(bucket, prefixes):
            nonlocal calls
            if bucket == PDF:
                calls += 1
                if calls == 2:
                    store.put(PDF, path, b"pdf")
                    store.put(PDF, "objects/late/book/companion.css", b"css")
            return original(bucket, prefixes)
        with patch.object(store, "list_files", side_effect=late_list):
            report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["buckets"][PDF]["candidates"], [])

    def test_reference_dropped_by_newer_root_version_is_not_missing(self):
        store = MemoryStore()
        root_path = "reader-index/manifest.json"
        store.put(ASSETS, root_path, {"files": {"book": {"object": "objects/gone/book.pdf", "bucket": PDF}}})
        original = store.read_bytes
        reads = 0
        def replace_root(bucket, path):
            nonlocal reads
            raw = original(bucket, path)
            if path == root_path:
                reads += 1
                if reads == 1:
                    store.put(ASSETS, root_path, {"files": {}})
            return raw
        with patch.object(store, "read_bytes", side_effect=replace_root):
            report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["missing"], [])

    def test_catalog_stream_keeps_generation_ids_but_only_live_references(self):
        from scripts.reader_gc_stream import catalog_json
        shared = {"bucket": PDF, "path": "objects/shared.pdf"}
        catalog = {"version": 1, "kind": "reader-catalog", "current": {"generation": "c"}, "generations": {
            "a": {"generation": "a", "status": "superseded", "superseded_at": "2026-01-01T00:00:00+00:00",
                  "replacement_acks": {"hf": True, "pages": True},
                  "references": [{"bucket": PDF, "path": "objects/old.pdf"}]},
            "b": {"generation": "b", "status": "superseded", "superseded_at": "2026-01-01T00:00:00+00:00",
                  "references": [shared]},
            "c": {"generation": "c", "status": "current", "references": [shared, {"bucket": PDF, "path": "objects/new.pdf"}]}},
            "retired_generations": {"z": {"replacement_generation": "c", "replacement_acks": {"hf": True, "pages": True}}}}
        raw = json.dumps(catalog, sort_keys=True).encode()
        payload, digest = catalog_json([raw[:17], raw[17:]])
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(set(payload["generations"]), {"a", "b", "c"})
        self.assertTrue(payload["generations"]["a"]["gc_stub"])
        self.assertNotIn("references", payload["generations"]["a"])
        kept = payload["generations"]["b"]["references"] + payload["generations"]["c"]["references"]
        self.assertEqual(sorted(ref["path"] for ref in kept), ["objects/new.pdf", "objects/shared.pdf"])
        self.assertEqual(payload["retired_generations"], catalog["retired_generations"])
        self.assertEqual(payload["current"], {"generation": "c"})

    def test_retirement_journal_can_reclaim_its_own_superseded_roots(self):
        # GC's own bookkeeping used to be permanently unmanaged: the retirement
        # journal said these roots were released, but no tier could delete them.
        store = MemoryStore()
        generation = "a" * 64
        retired = [f"reader-index/v3/promotions/{generation}.json",
                   f"reader-index/v3/generations/{generation}/catalog.json",
                   "reader-index/processing/v3-" + "b" * 32 + ".json"]
        kept = "reader-index/v3/promotions/" + "c" * 64 + ".json"
        for path in retired + [kept]:
            store.put(ASSETS, path, {"kind": "reader-v3-history", "generation": generation,
                                     "resources": []})
        store.put(ASSETS, "reader-index/v3/retirement.json", {
            "version": 1, "kind": "reader-v3-retirement", "resources": [],
            "retired_paths": retired, "replacement_acks": {"hf": True, "pages": True}})
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["buckets"][ASSETS]["retired"], sorted(retired))
        self.assertEqual(report["buckets"][ASSETS]["candidates"], sorted(retired))
        self.assertEqual(report["buckets"][ASSETS]["unmanaged"], [])
        self.assertNotIn(kept, report["buckets"][ASSETS]["candidates"])

    def test_report_only_incomplete_graph_can_exit_zero(self):
        self.assertEqual(gc.report_exit_code({"graph_complete": True}), 0)
        self.assertEqual(gc.report_exit_code({"graph_complete": False}), 1)
        self.assertEqual(gc.report_exit_code({"graph_complete": False}, allow_incomplete=True), 0)

    def test_gc_run_journals_are_retained_not_reported_as_unknown_formats(self):
        store = MemoryStore()
        store.put(ASSETS, "reader-index/gc/123/collection.json", {"version": 1})
        store.put(ASSETS, "reader-index/gc/123/progress.json", {"version": 1})
        store.put(ASSETS, "reader-index/reader_gc_acknowledgments.json", {
            "version": 1, "kind": "reader-gc-acknowledgments",
            "current": {"generation": "a" * 32, "catalog_sha256": "b" * 64}})
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["buckets"][ASSETS]["retained"],
                         ["reader-index/gc/123/collection.json",
                          "reader-index/gc/123/progress.json",
                          "reader-index/reader_gc_acknowledgments.json"])
        self.assertEqual(report["buckets"][ASSETS]["unmanaged"], [])
        self.assertEqual(report["buckets"][ASSETS]["candidates"], [])

    def test_retired_root_that_is_still_pinned_is_not_a_candidate(self):
        store = MemoryStore()
        path = "reader-index/v3/promotions/" + "a" * 64 + ".json"
        store.put(ASSETS, path, {"kind": "reader-v3-history", "generation": "a" * 64,
                                 "resources": []})
        store.put(ASSETS, "reader-index/v3/retirement.json", {
            "version": 1, "kind": "reader-v3-retirement", "resources": [],
            "retired_paths": [path], "replacement_acks": {"hf": True, "pages": True}})
        store.put(ASSETS, "reader-index/audit.json", {"resources": [
            {"bucket": ASSETS, "path": path}]})
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["buckets"][ASSETS]["retired"], [])
        self.assertNotIn(path, report["buckets"][ASSETS]["candidates"])

    def test_unknown_formats_are_reported_and_never_deleted(self):
        store = MemoryStore()
        store.put(ASSETS, "new-format/content.bin", b"data")
        store.put(PDF, "objects/orphan/page.svg", b"svg")
        graph = gc.ReferenceGraph(store)
        report = graph.build()
        self.assertEqual(report["buckets"][ASSETS]["unmanaged"], ["new-format/content.bin"])
        self.assertEqual(report["buckets"][PDF]["formats"][".svg"], 1)
        self.assertFalse(report["deletion_enabled"])

    def test_ambiguous_unqualified_paths_preserve_both_buckets(self):
        store = MemoryStore()
        path = "objects/shared/document.pdf"
        store.put(ASSETS, "reader-index/manifest.json", {"files": {"book": {"path": path}}})
        store.put(ASSETS, path, b"pdf1")
        store.put(PDF, path, b"pdf2")
        report = gc.ReferenceGraph(store).build()
        self.assertEqual(report["ambiguous_paths"], [path])
        self.assertTrue(all(not data["candidates"] for data in report["buckets"].values()))

    def test_released_processing_root_does_not_keep_objects_but_uploaded_does(self):
        store = MemoryStore()
        root = "objects/aa/book/render"
        path = root + "/pages/page-000001.webp"
        store.put(PDF, path, b"page")
        store.put(ASSETS, "reader-index/processing/released.json", {
            "kind": "reader-processing-roots", "status": "released", "generation": "generation", "resources": [
                {"bucket": PDF, "root": root}],
        })
        store.put(ASSETS, "reader-index/reader_catalog.json", {
            "version": 1, "kind": "reader-catalog", "current": {"generation": "generation"},
            "generations": {"generation": {"generation": "generation", "status": "current", "references": []}},
        })
        report = gc.ReferenceGraph(store).build()
        self.assertEqual(report["buckets"][PDF]["candidates"], [path])
        store.put(ASSETS, "reader-index/processing/uploaded.json", {
            "kind": "reader-processing-roots", "status": "uploaded", "resources": [
                {"bucket": PDF, "root": root}],
        })
        report = gc.ReferenceGraph(store).build()
        self.assertEqual(report["buckets"][PDF]["candidates"], [])

    def test_gc_observation_requires_two_pass_grace_and_never_deletes(self):
        store = MemoryStore()
        orphan = "objects/orphan/document.pdf"
        store.put(PDF, orphan, b"orphan")
        report = gc.ReferenceGraph(store).build()
        state, first = gc.plan_retention({"version": 1, "kind": "reader-gc-state", "orphans": {}}, report, 14, 0)
        self.assertEqual((first["marked"], first["past_grace"], first["deletion_authorized"]), (1, 0, False))
        state["orphans"][f"{PDF}:{orphan}"]["first_seen"] = (date.today() - timedelta(days=15)).isoformat()
        report = gc.ReferenceGraph(store).build()
        _state, second = gc.plan_retention(state, report, 14, 0)
        self.assertEqual((second["past_grace"], second["deletion_authorized"]), (1, False))
        self.assertIn(orphan, store.objects[PDF])

    def test_gc_observation_aborts_when_a_root_is_unreadable(self):
        store = MemoryStore()
        store.put(PDF, "reader-index/pdf_ocr_manifest.json", b"bad")
        report = gc.ReferenceGraph(store).build()
        with self.assertRaises(RuntimeError):
            gc.plan_retention({"version": 1, "kind": "reader-gc-state", "orphans": {}}, report, 14, 0)

    def test_released_record_without_generation_keeps_inputs_and_blocks_graph(self):
        store = MemoryStore()
        root = "objects/aa/book/render"
        store.put(INPUT, root + "/ocr-input/page-000001.png", b"input")
        store.put(ASSETS, "reader-index/processing/released.json", {
            "kind": "reader-processing-roots", "status": "released", "generation": "missing",
            "resources": [{"bucket": INPUT, "root": root}],
        })
        report = gc.ReferenceGraph(store).build()
        self.assertFalse(report["graph_complete"])
        self.assertEqual(report["buckets"][INPUT]["unreferenced"], [])

    def test_scheduled_gc_is_artifact_only_without_blocking_writers(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/reader-gc.yml").read_text())
        self.assertIn("reader-gc-report", workflow["concurrency"]["group"])
        self.assertIn("reader-sidecar", workflow["concurrency"]["group"])
        self.assertIn("record_observations", workflow["concurrency"]["group"])
        commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["gc"]["steps"])
        self.assertIn("python scripts/reader_gc_graph.py", commands)
        self.assertIn("--storage s3", commands)
        self.assertLessEqual(workflow["jobs"]["gc"]["timeout-minutes"], 360)
        self.assertNotIn("--apply", commands)
        self.assertIn("--record-observations", commands)
        artifacts = [step for step in workflow["jobs"]["gc"]["steps"] if "upload-artifact" in step.get("uses", "")]
        self.assertEqual(artifacts[0]["if"], "always()")

    def test_upload_protection_precedes_objects_and_failure_retains_roots(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "objects/aa/book/render"
            root.mkdir(parents=True)
            (root / "ocr-input").mkdir()
            (root / "ocr-input" / "page-000001.png").write_bytes(b"partial")
            events = []
            def protect(records, token):
                for path, record in records.items():
                    events.append("protect:" + record["status"])
                    store.put(ASSETS, path, record)
            def upload(*args, **kwargs):
                events.append("upload")
                raise ValueError("failed transfer")
            with patch.object(stages, "publish_json_batch", side_effect=protect), \
                    patch.object(stages, "remote_sizes", return_value={}), \
                    patch.object(stages.shared, "batch_bucket_files_with_retry", side_effect=upload):
                with self.assertRaises(ValueError):
                    stages.upload_objects(Path(directory))
            self.assertEqual(events, ["protect:processing", "upload"])
            store.put(INPUT, "objects/aa/book/render/ocr-input/page-000001.png", b"partial")
            report = gc.ReferenceGraph(store).build()
            self.assertTrue(report["graph_complete"], report["blockers"])
            self.assertEqual(report["buckets"][INPUT]["candidates"], [])

    def test_partial_worker_handoff_keeps_progress_owned_objects(self):
        store = MemoryStore()
        root = "objects/aa/book/render"
        path = "reader-index/processing/worker.json"
        descriptor = root + "/render-range-000001-000001.json"
        result = {"key": "book", "status": "range", "descriptor": {"path": descriptor},
                  "processing_roots": [path]}
        store.put(ASSETS, path, {"kind": "reader-processing-roots", "status": "uploaded",
                                "resources": [{"bucket": PDF, "root": root}]})
        store.put(ASSETS, "reader-index/pdf_render_progress.json", {
            "files": {"book": {"ranges": {"1-1": {"path": descriptor}}}}})
        store.put(PDF, descriptor, {"kind": "pdf-render-range", "pages": []})
        catalog, generation = gc.reader_lifecycle.catalog_generation({"v": 1, "f": {}})
        store.put(ASSETS, "reader-index/reader_catalog.json", catalog)
        with patch.object(stages.publication, "load_sidecar", return_value={"v": 1, "f": {}}), \
                patch.object(stages, "publish_catalog", return_value=generation), \
                patch.object(publication, "read_bucket_json", side_effect=lambda p, token: json.loads(store.read_bytes(ASSETS, p))), \
                patch.object(publication, "publish_json_batch", side_effect=lambda values, token: [store.put(ASSETS, p, v) for p, v in values.items()]):
            stages.handoff_worker_results(HfApi(), "unused", [result])
        self.assertEqual(json.loads(store.read_bytes(ASSETS, path))["status"], "released")
        report = gc.ReferenceGraph(store).build()
        self.assertTrue(report["graph_complete"], report["blockers"])
        self.assertEqual(report["buckets"][PDF]["candidates"], [])

    def test_foreign_processing_root_cannot_be_released(self):
        with patch.object(publication, "read_bucket_json", return_value={
                "kind": "reader-processing-roots", "status": "uploaded",
                "resources": [{"bucket": PDF, "root": "objects/foreign/book/profile"}]}), \
                patch.object(publication, "publish_json_batch") as write:
            with self.assertRaises(ValueError):
                publication.release_processing_roots([{
                    "ocr_manifest": "objects/own/book/profile/ocr-manifest.json",
                    "processing_roots": ["reader-index/processing/foreign.json"]}], "generation", None)
            write.assert_not_called()

    def test_conversion_processing_roots_are_valid_retirement_roots(self):
        import re
        pattern = re.compile(
            r"reader-index/(?:v3/(?:promotions/[0-9a-f]{64}\.json|generations/[0-9a-f]{64}/catalog\.json|candidates/[0-9a-f]{64}\.json)|processing/(?:v3|djvu|office-chapters)-[0-9a-f]{32}\.json)"
        )
        for path in (
                "reader-index/processing/djvu-" + "a" * 32 + ".json",
                "reader-index/processing/office-chapters-" + "b" * 32 + ".json"):
            self.assertRegex(path, pattern)


class CanonicalOcrPublicationTests(unittest.TestCase):
    def test_ready_publication_verifies_manifest_text_checksums_and_page_order(self):
        root = "objects/aa/" + "a" * 64 + "/" + "b" * 16
        text_path = root + "/ocr/book-text.json.gz"
        text = gzip.compress(json.dumps({"kind": "pdf-book-text", "version": 2,
            "complete": True, "page_count": 2, "pages": [{"page": 1}, {"page": 2}]}).encode())
        manifest = json.dumps({"kind": "pdf-ocr", "complete": True, "page_count": 2,
            "source_sha256": "a" * 64, "pages": [{"p": 1}, {"p": 2}], "book_text": {
                "path": text_path, "bytes": len(text), "sha256": hashlib.sha256(text).hexdigest()}}).encode()
        result = {"ocr_manifest": root + "/ocr-manifest.json", "page_count": 2,
            "source_sha256": "a" * 64, "ocr_manifest_bytes": len(manifest),
            "ocr_manifest_sha256": hashlib.sha256(manifest).hexdigest()}
        with patch.object(publication, "read_bucket_bytes", side_effect=[manifest, text]) as read:
            publication.verify_ready_objects(result)
        self.assertTrue(all(call.kwargs["bucket"] == PDF for call in read.call_args_list))
        malformed = json.loads(manifest)
        malformed["pages"] = [{"p": 2}, {"p": 1}]
        raw = json.dumps(malformed).encode()
        with patch.object(publication, "read_bucket_bytes", return_value=raw):
            with self.assertRaises(ValueError):
                publication.verify_ready_objects({**result, "ocr_manifest_bytes": len(raw),
                    "ocr_manifest_sha256": hashlib.sha256(raw).hexdigest()})

    def test_catalog_snapshot_precedes_pointer_and_does_not_claim_consumer_acceptance(self):
        sidecar = {"v": 1, "f": {"book": {"p": "objects/book/document.pdf", "b": PDF}}}
        writes = []
        with patch.object(reader_bucket, "read_json", side_effect=FileNotFoundError), \
                patch.object(reader_bucket, "publish_indexes", side_effect=lambda data, token: writes.append(("snapshot", data))), \
                patch.object(reader_bucket, "publish_json", side_effect=lambda path, data, token: writes.append(("catalog", data))):
            generation = reader_bucket.publish_catalog(sidecar)
        self.assertEqual([kind for kind, data in writes], ["snapshot", "catalog"])
        catalog = writes[1][1]
        entry = catalog["generations"][generation]
        self.assertEqual(entry["acks"], {"hf": False, "pages": False})
        compressed = writes[0][1][entry["snapshot"]["path"]]
        self.assertEqual(json.loads(gzip.decompress(compressed)), sidecar)
        self.assertEqual(entry["snapshot"]["bytes"], len(compressed))

    def test_acknowledgment_rejects_wrong_generation_or_sidecar_identity(self):
        catalog, generation = gc.reader_lifecycle.catalog_generation({"v": 1, "f": {}})
        with patch.object(reader_bucket, "read_json", return_value=catalog), \
                patch.object(reader_bucket, "publish_json") as write:
            for expected, digest in (("stale", catalog["generations"][generation]["sidecar_sha256"]),
                                     (generation, "wrong")):
                with self.assertRaises(ValueError):
                    reader_bucket.acknowledge_catalog("pages", expected, digest)
            write.assert_not_called()

    def test_real_api_publishes_only_canonical_bucket_and_repairs_sidecar(self):
        api = HfApi()
        result = {"key": "book", "status": "ready", "profile": "test", "source_sha256": "a" * 64,
                  "classification": "scan", "ocr_manifest": "objects/text/ocr-manifest.json"}
        existing = {"version": 1, "files": {"book": result}}
        with patch.object(publication, "read_bucket_json", return_value=existing), \
                patch.object(publication, "read_bucket_bytes", return_value=publication.encode_sidecar({"v": 1, "f": {}})), \
                patch.object(publication, "verify_ready_objects"), \
                patch.object(publication, "publish_indexes") as upload, \
                patch.object(publication, "publish_catalog", return_value="gen"), \
                patch.object(api, "repo_info") as repo_info, patch.object(api, "create_commit") as commit:
            publication.publish(api, "unused", [result])
        repo_info.assert_not_called()
        commit.assert_not_called()
        payloads = upload.call_args.args[0]
        self.assertEqual(set(payloads), {reader_bucket.INDEX_FILES["ocr"], reader_bucket.INDEX_FILES["sidecar"]})
        sidecar = json.loads(gzip.decompress(payloads[reader_bucket.INDEX_FILES["sidecar"]]))
        self.assertEqual(sidecar["f"]["book"]["o"], result["ocr_manifest"])

    def test_unreadable_ready_manifest_blocks_registry_publication(self):
        with patch.object(publication, "read_bucket_bytes", side_effect=FileNotFoundError), \
                patch.object(publication, "publish_indexes") as write:
            with self.assertRaises(FileNotFoundError):
                publication.publish(HfApi(), "unused", [{
                    "status": "ready", "ocr_manifest": "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/ocr-manifest.json"}])
            write.assert_not_called()

    def test_corrupt_or_unavailable_bucket_never_falls_back_to_dataset(self):
        api = HfApi()
        for error in (PermissionError("denied"), ValueError("corrupt"), OSError("network")):
            with self.subTest(error=error), patch.object(publication, "read_bucket_json", side_effect=error), \
                    patch.object(api, "hf_hub_download") as download:
                with self.assertRaises(type(error)):
                    publication.load_remote(api, "unused", publication.OCR_MANIFEST_NAME, {})
                download.assert_not_called()
        with patch.object(publication, "read_bucket_bytes", return_value=b"not gzip"), \
                patch.object(api, "hf_hub_download") as download:
            with self.assertRaises(OSError):
                publication.load_sidecar(api, "unused")
            download.assert_not_called()

    def test_staged_registry_does_not_turn_corruption_into_empty_state(self):
        api = HfApi()
        with patch.object(stages, "read_bucket_json", return_value={"version": 1, "files": []}):
            with self.assertRaises(ValueError):
                stages.load_registry(api, "unused", stages.RENDER_REGISTRY)
        with patch.object(stages, "read_bucket_json", side_effect=OSError("network")):
            with self.assertRaises(OSError):
                stages.load_registry(api, "unused", stages.RENDER_REGISTRY)

    def test_index_batch_uses_only_current_bucket(self):
        observed = []
        def upload(bucket, add, token):
            from pathlib import Path
            observed.append((bucket, {remote: Path(local).read_bytes() for local, remote in add}))
        with patch.object(reader_bucket, "batch_bucket_files", side_effect=upload):
            reader_bucket.publish_indexes({"reader-index/a.json": b"{}", "reader-index/b.json.gz": b"gzip"})
        self.assertEqual(observed, [(ASSETS, {"reader-index/a.json": b"{}", "reader-index/b.json.gz": b"gzip"})])


if __name__ == "__main__":
    unittest.main()
