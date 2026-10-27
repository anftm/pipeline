import gzip
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from scripts import (djvu_pages, pdf_ocr, pdf_ocr_stages as stages, pdf_reading_v3 as v3,
                     pdf_text_layer as text, plan_pdf_ocr, publish_reader_v3 as publish,
                     reader_v3_automation as automation, shared)

TOOLS = all(shutil.which(name) for name in ("cjb2", "c44", "djvm", "djvused", "ddjvu", "djvutxt"))
HIDDEN_TEXT = '(page 0 0 400 600 (line 50 400 350 480 (word 50 400 150 480 "你好") (word 160 400 350 480 "world")))'


def build_djvu(directory: Path, rotation=0) -> Path:
    """Page 1: 600 dpi bitonal JB2 with hidden text; page 2: 150 dpi colour IW44."""
    bitonal = Image.new("1", (400, 600), 1)
    ImageDraw.Draw(bitonal).rectangle((50, 50, 350, 120), fill=0)
    bitonal.save(directory / "p1.pbm")
    colour = Image.new("RGB", (300, 200), (200, 30, 30))
    ImageDraw.Draw(colour).ellipse((50, 50, 250, 150), fill=(20, 200, 40))
    colour.save(directory / "p2.ppm")
    run = lambda *args: subprocess.run(args, check=True, capture_output=True, cwd=directory)
    run("cjb2", "-dpi", "600", "p1.pbm", "p1.djvu")
    run("c44", "-dpi", "150", "p2.ppm", "p2.djvu")
    run("djvm", "-c", "book.djvu", "p1.djvu", "p2.djvu")
    (directory / "t.txt").write_text(HIDDEN_TEXT, encoding="utf-8")
    run("djvused", "book.djvu", "-e", "select 1; set-txt t.txt", "-s")
    if rotation:
        run("djvused", "book.djvu", "-e", f"select 1; set-rotation {rotation}", "-s")
    return directory / "book.djvu"


class PureDjvuTests(unittest.TestCase):
    def test_only_native_worker_entrypoints_remain(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / ".github/workflows/reader-djvu-weekly.yml").exists())
        self.assertFalse((root / "scripts/sync_djvu_pdf_derivatives.py").exists())
        self.assertTrue((root / "scripts/djvu_pages.py").is_file())
        worker = (root / ".github/workflows/pdf-account-worker.yml").read_text(encoding="utf-8")
        self.assertIn("PDF_DJVU_NATIVE:", worker)
        self.assertIn("djvulibre-bin", worker)
        self.assertIn("scripts/pdf_ocr_stages.py plan-render", worker)

    def test_sniff_uses_magic_bytes_not_names(self):
        self.assertEqual(djvu_pages.sniff(b"AT&TFORM\0\0\0\0DJVU"), "djvu")
        self.assertEqual(djvu_pages.sniff(b"%PDF-1.7"), "pdf")
        self.assertEqual(djvu_pages.sniff(b"garbage!"), "unknown")

    def test_native_flag_defaults_off_and_pilot_is_exact(self):
        item = {"key": "VoiceOfML/A\0x.djvu", "repo": "VoiceOfML/A", "path": "x.djvu"}
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(djvu_pages.native_enabled(item))
        with patch.dict(os.environ, {"PDF_DJVU_NATIVE": "pilot", "PDF_DJVU_NATIVE_KEYS": "VoiceOfML/A/x.djvu"}):
            self.assertTrue(djvu_pages.native_enabled(item))
            self.assertFalse(djvu_pages.native_enabled({**item, "key": "VoiceOfML/A\0y.djvu", "path": "y.djvu"}))
        with patch.dict(os.environ, {"PDF_DJVU_NATIVE": "maybe"}), self.assertRaises(ValueError):
            djvu_pages.native_enabled(item)

    def test_profile_namespace_differs_from_pdf_render(self):
        pdf = stages.render_profile()
        djvu = stages.render_profile_for({"source_format": "djvu"})
        self.assertNotEqual(pdf, djvu)
        self.assertIn("djvu-native-v1", djvu)
        self.assertEqual(stages.render_profile_for({"source_extension": "djvu"}), pdf)
        self.assertNotEqual(stages.root_for("a" * 64, "k", pdf), stages.root_for("a" * 64, "k", djvu))

    def test_hidden_text_escapes_and_rotated_coordinates(self):
        parsed = djvu_pages.parse_sexprs(b'(page 0 0 10 20 (line 0 10 5 20 "a \\"q\\" \\303\\251"))\n()')
        self.assertEqual(parsed[0][5][5], 'a "q" é')
        self.assertEqual(parsed[1], [])
        info = {"width": 100, "height": 200, "dpi": 300, "rotation": 0}
        upright = djvu_pages.page_text(parsed[0], {**info, "width": 10, "height": 20})
        self.assertEqual(upright["blocks"][0]["b"], [0.0, 0.0, 0.5, 0.5])
        rotated = djvu_pages.page_text(parsed[0], {**info, "width": 10, "height": 20, "rotation": 90})
        # Top-left quarter of the unrotated page appears top-right after 90° clockwise.
        self.assertEqual(rotated["blocks"][0]["b"], [0.5, 0.0, 1.0, 0.5])
        self.assertEqual((rotated["width"], rotated["height"]), (20, 10))

    def test_sidecar_never_inherits_a_derived_pdf_for_native_djvu(self):
        current = {"s": 2, "m": "p", "p": "objects/aa/x/y/page-manifest.json", "b": shared.PDF_PAGES_BUCKET,
                   "pd": "derived/weekly-djvu/" + "a" * 32 + "/document.pdf", "pdb": shared.PDF_PAGES_BUCKET}
        entry = shared.apply_pdf_pages_sidecar_entry(dict(current), "objects/bb/new/page-manifest.json",
                                                     {"source_format": "djvu"})
        self.assertNotIn("pd", entry)
        merged = shared.merge_pdf_ocr_sidecar_entry(current, {
            "status": "ready", "source_format": "djvu", "ocr_manifest": "objects/o/ocr-manifest.json",
            "page_manifest": {"path": "objects/bb/new/page-manifest.json"}})
        self.assertNotIn("pd", merged)
        self.assertNotIn("p", merged)
        self.assertEqual(merged['o'], 'objects/o/ocr-manifest.json')
        self.assertIn("pd", shared.apply_pdf_pages_sidecar_entry(dict(current), "objects/bb/new/page-manifest.json", {}))

    def test_pdf_magic_fallback_render_is_not_replanned(self):
        item = {"key": "r\0b.djvu", "repo": "r", "path": "b.djvu", "source_format": "djvu",
                "source_kind": "upstream", "source_revision": "r2"}
        previous = {"status": "ready", "source_kind": "generated", "render_profile": stages.render_profile(),
                    "djvu_pdf_fallback": "pdf-magic", "djvu_source_revision": "r2"}
        self.assertEqual(stages.pending_render([item], {item["key"]: previous}, {}), [])
        self.assertEqual(len(stages.pending_render([{**item, "source_revision": "r3"}], {item["key"]: previous}, {})), 1)


@unittest.skipUnless(TOOLS, "djvulibre command-line tools are not installed")
class RealDjvuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = build_djvu(self.root)
        self.raw = self.source.read_bytes()
        self.sha = hashlib.sha256(self.raw).hexdigest()

    def test_inspect_page_map_and_probe(self):
        infos = djvu_pages.inspect(self.raw)
        self.assertEqual([(i["width"], i["height"], i["dpi"], i["layers"]) for i in infos],
                         [(400, 600, 600, "bitonal"), (300, 200, 150, "color")])
        mapping = djvu_pages.page_map(self.raw, self.sha)
        self.assertEqual([p["classification"] for p in mapping["pages"]], ["bitonal-image", "raster-or-mixed"])
        self.assertEqual((mapping["pages"][0]["width"], mapping["pages"][0]["height"]), (48.0, 72.0))
        with patch.object(pdf_ocr, "MIN_NATIVE_PAGE_CHARS", 3):
            probe = djvu_pages.probe(self.source)
        self.assertEqual((probe["page_count"], probe["classification"]), (2, "mixed"))
        self.assertEqual(probe["page_chars"], [7, 0])

    def test_rotation_flag_matches_ddjvu_output(self):
        rotated = build_djvu(Path(tempfile.mkdtemp(dir=self.root)), rotation=1)
        info = djvu_pages.inspect(rotated.read_bytes())[0]
        self.assertEqual(info["rotation"], 270)
        geometry = djvu_pages.page_map(rotated.read_bytes(), self.sha)["pages"][0]
        _, width, height, _, _ = djvu_pages.render(rotated, 1, self.root / "r.png", info=info)
        self.assertEqual((width, height), (600, 400))
        self.assertAlmostEqual(geometry["width"] / geometry["height"], width / height)

    def test_native_bitonal_render_is_exact_and_capped_render_is_bounded(self):
        info = djvu_pages.inspect(self.raw)[0]
        out, width, height, mode, scale = djvu_pages.render(self.source, 1, self.root / "p1.png", info=info)
        self.assertEqual((width, height, mode, scale), (400, 600, "1", 1.0))
        with Image.open(out) as rendered, Image.open(self.root / "p1.pbm") as original:
            self.assertEqual(rendered.convert("1").tobytes(), original.convert("1").tobytes())
        _, width, height, _, _ = djvu_pages.render(self.source, 1, self.root / "c.png", info=info, max_pixels=60000)
        self.assertLessEqual(width * height, 60000)
        self.assertAlmostEqual(width / height, 400 / 600, places=2)

    def item(self):
        return {"key": "VoiceOfML/A\0book.djvu", "repo": "VoiceOfML/A", "path": "book.djvu",
                "source_kind": "upstream", "source_revision": "c" * 40, "source_format": "djvu",
                "source_extension": "djvu", "force_image_render": True}

    def test_render_book_uses_original_identity_and_native_text(self):
        with patch.object(pdf_ocr, "MIN_NATIVE_PAGE_CHARS", 3), patch.object(pdf_ocr, "JXL_ENABLED", False), \
                patch.object(pdf_ocr, "render_page", side_effect=AssertionError("no PDF render")), \
                patch.object(pdf_ocr, "native_pages", side_effect=AssertionError("no Poppler text")):
            result = stages.render_book(self.item(), self.source, self.root / "bundle")
            self.assertEqual(result["render_profile"], stages.render_profile_for(self.item()))
        self.assertEqual(result["source_sha256"], self.sha)
        self.assertEqual(result["ocr_pages"], 1)
        manifest = json.loads((self.root / "bundle" / result["render_manifest"]["path"]).read_text())
        stages.validate_render(result, manifest)
        first, second = manifest["pages"]
        self.assertEqual((first["source"], second["source"]), ("native", "ocr"))
        self.assertEqual(first["text"], "你好 world")
        self.assertTrue(first['native_text_only'])
        self.assertNotIn('i', first)
        self.assertTrue(all(not ({'r', 'w', 'j'} & page.keys()) for page in manifest['pages']))
        self.assertIsNone(result['page_manifest'])
        self.assertFalse(result['image_rendered'])
        with Image.open(self.root / "bundle" / second["i"]) as png:
            self.assertEqual((png.size, png.mode), ((300, 200), "RGB"))
        payload = json.loads(gzip.decompress((self.root / "bundle" / first["o"]).read_bytes()))
        self.assertEqual(payload["source"], "native")
        self.assertTrue(payload["blocks"])

    def test_render_book_refuses_pdf_bytes_under_native_profile(self):
        fake = self.root / "fake.djvu"
        fake.write_bytes(b"%PDF-1.4 not djvu")
        with self.assertRaisesRegex(ValueError, "DjVu source bytes"):
            stages.render_book(self.item(), fake, self.root / "x")

    def test_primary_reading_never_adopts_shared_or_legacy_preview_images(self):
        spec = self.reading_spec()
        spec['render_manifest'] = {'untrusted': 'historical'}
        spec['previews'] = [{'untrusted': 'historical'}]
        with patch.object(djvu_pages, 'render', side_effect=AssertionError('no page render')), \
                patch.object(v3.reader_page_codec, 'encode_image', side_effect=AssertionError('no page encode')):
            generated = v3.generate_previews(spec, lambda _: self.fail('no preview reads'), self.root / 'v3-shared')
        self.assertEqual(generated['previews'], [])
        self.assertFalse((self.root / 'v3-shared').exists())

    def test_bitonal_ocr_has_no_downscale_and_never_produces_reading_images(self):
        directory = self.root / 'bitonal';directory.mkdir()
        expected = self.root / 'original.png'
        info = djvu_pages.inspect(self.raw)[0]
        djvu_pages.render(self.source, 1, expected, info=info)
        with patch.object(pdf_ocr, 'WEBP_MAX_DIMENSION', 100), \
                patch.object(Image.Image, 'resize', side_effect=AssertionError('bitonal text must not downscale')):
            png, width, height = djvu_pages.render_page(self.source, 1, directory, info=info)
        with Image.open(png) as ocr, Image.open(expected) as original:
            self.assertEqual((width, height), (400, 600))
            self.assertEqual(ocr.mode, 'L')
            self.assertEqual(ocr.tobytes(), original.convert('L').tobytes())
        for options in ({'reader_jxl': True}, {'reader_images': True}):
            with self.assertRaisesRegex(ValueError, 'reading images are retired'):
                djvu_pages.render_page(self.source, 1, directory, info=info, **options)
        self.assertFalse((directory / 'page-000001-reader.png').exists())
        self.assertFalse((directory / 'page-000001-reader.ppm').exists())
        self.assertEqual(list(directory.glob('*.webp')), [])

    def test_direct_worker_rejects_image_fields_and_wrong_source_format(self):
        with patch.object(pdf_ocr, 'JXL_ENABLED', False):
            result = stages.render_book(self.item(), self.source, self.root / 'validated')
        original = json.loads((self.root / 'validated' / result['render_manifest']['path']).read_bytes())
        for changed in [{'rbucket': shared.PDF_OCR_INPUT_BUCKET}, {'w': 'redundant'},
                        {'j': 'redundant'}, {'r': 'redundant'}, {'rs': 'g' * 64}, {'rb': True}]:
            manifest = copy.deepcopy(original)
            manifest['pages'][0].update(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                stages.validate_render(result, manifest)
        with self.assertRaisesRegex(ValueError, 'native source format'):
            stages.validate_render(result, {**original, 'source_format': 'pdf'})

    def test_range_resume_uploads_ocr_only_in_processing_bucket(self):
        bundle = self.root / 'range'
        with patch.object(pdf_ocr, 'JXL_ENABLED', False):
            result = stages.render_book({**self.item(), 'start': 1, 'end': 2}, self.source, bundle)
        book = {**self.item(), **stages.range_identity(result), 'probe': djvu_pages.probe(self.source),
                'classification': 'mixed', 'profile': pdf_ocr.asset_profile()}
        reads = lambda ref, *_a: (bundle / ref['path']).read_bytes()
        with patch.object(stages, 'read_object', side_effect=reads):
            pages = stages.validate_range(book, 1, 2, result['descriptor'])
            assembled = stages.assemble_render_book(book, {'000001-000002': result['descriptor']}, bundle)
        self.assertIsNone(assembled['page_manifest'])
        self.assertTrue(all(not ({'r', 'w', 'j'} & page.keys()) for page in pages))
        uploaded = {}
        with patch.object(shared, 'batch_bucket_files_with_retry', side_effect=lambda bucket, additions, _token:
                          uploaded.setdefault(bucket, []).extend(remote for _local, remote in additions)), \
                patch.object(stages, 'remote_sizes', return_value={}), patch.object(stages, 'publish_json_batch'):
            stages.upload_objects(bundle)
        for page in pages:
            if 'i' in page:
                self.assertIn(page['i'], uploaded[shared.PDF_OCR_INPUT_BUCKET])
                self.assertNotIn(page['i'], uploaded.get(shared.PDF_PAGES_BUCKET, []))

    def test_600dpi_bitonal_ocr_png_preserves_native_samples_without_reader_images(self):
        info = djvu_pages.inspect(self.raw)[0]
        folder = self.root / 'ocr-600'; folder.mkdir()
        png, width, height = djvu_pages.render_page(self.source, 1, folder, info=info, reader_images=False)
        with Image.open(png) as image, Image.open(self.root / 'p1.pbm') as original:
            self.assertEqual((width, height), (400, 600))
            self.assertEqual(image.mode, 'L')
            self.assertEqual(image.tobytes(), original.convert('L').tobytes())
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['page-000001.png'])

    def test_plan_inspects_native_or_keeps_pdf_magic_on_derived_path(self):
        derived = {"key": self.item()["key"], "source_kind": "generated", "reader_assets_path":
                   "derived/weekly-djvu/" + "a" * 32 + "/document.pdf", "source_revision": "old"}
        pdf = self.root / "derived.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        def download(item):
            return pdf if item.get("source_kind") == "generated" else self.source
        with patch.object(plan_pdf_ocr, "download_source", side_effect=download), \
                patch.object(pdf_ocr, "probe_pdf", return_value={"page_count": 1, "page_chars": [0],
                                                                 "classification": "scan"}), \
                patch.object(pdf_ocr, "reader_presentation", return_value={"strategy": "page-stream"}):
            queue = plan_pdf_ocr.plan([{**self.item(), "_derived_fallback": derived}], workers=1)
            book = queue["shards"][0]["records"][0]
            self.assertEqual((book["source_format"], book["page_count"]), ("djvu", 2))
            self.assertNotIn("_derived_fallback", book)
            disguised = self.root / "disguised.djvu"
            disguised.write_bytes(b"%PDF-1.4 disguised")
            with patch.object(plan_pdf_ocr, "download_source",
                              side_effect=lambda item: pdf if item.get("source_kind") == "generated" else disguised):
                fallback = plan_pdf_ocr.plan([{**self.item(), "_derived_fallback": derived}], workers=1)
                book = fallback["shards"][0]["records"][0]
                self.assertEqual((book["source_kind"], book["djvu_pdf_fallback"]), ("generated", "pdf-magic"))
                self.assertIsNone(book.get("worker_lane"))
                routed = plan_pdf_ocr.plan([{**self.item(), "worker_lane": "converted",
                                             "_derived_fallback": derived}], workers=1)
                self.assertEqual(routed["shards"][0]["records"][0]["worker_lane"], "converted")
                failed = plan_pdf_ocr.plan([self.item()], workers=1)
                self.assertIn("awaits its derived PDF", failed["failed"][0]["error"])

    def test_source_records_gate_single_record_per_key(self):
        search = self.root / "search.json"
        revisions = self.root / "commits.json"
        records = [{"Repo": "VoiceOfML/A", "File": "book", "Extension": "djvu", "Folder": [], "Size": len(self.raw)},
                   {"Repo": "VoiceOfML/A", "File": "doc", "Extension": "pdf", "Folder": [], "Size": 9}]
        search.write_text(json.dumps(records))
        revisions.write_text(json.dumps({"VoiceOfML/A": "c" * 40}))
        assets = {"files": {"VoiceOfML/A\0book.djvu": {"status": "ready", "reader_mode": "pdf", "bytes": 5,
                  "path": "derived/weekly-djvu/" + "a" * 32 + "/document.pdf", "source_revision": "c" * 40}}}
        with patch.dict(os.environ, {"PDF_DJVU_NATIVE": "false"}):
            off = pdf_ocr.source_records(search, revisions, assets)
        with patch.dict(os.environ, {"PDF_DJVU_NATIVE": "true"}):
            on = pdf_ocr.source_records(search, revisions, assets)
        self.assertEqual([(r["path"], r["source_kind"]) for r in off],
                         [("book.djvu", "generated"), ("doc.pdf", "upstream")])
        book = next(r for r in on if r["path"] == "book.djvu")
        self.assertEqual(len(on), 2)
        self.assertEqual((book["source_kind"], book["source_format"]), ("upstream", "djvu"))
        self.assertEqual(book["_derived_fallback"]["source_kind"], "generated")

    def reading_spec(self):
        path = f"objects/{self.sha[:2]}/{self.sha}/{'d' * 16}/document.djvu"
        self.objects = {(shared.PDF_PAGES_BUCKET, path): self.raw}
        primary = {"bucket": shared.PDF_PAGES_BUCKET, "path": path, "sha256": self.sha,
                   "bytes": len(self.raw), "role": "runtime"}
        return {"source_key": self.item()["key"], "source_sha256": self.sha, "primary": primary,
                "require_complete_preview": True, "vector_previews": True}

    def read(self, ref):
        return self.objects[(ref["bucket"], ref["path"])]

    def test_v3_reading_with_djvu_primary_and_separate_text_layer(self):
        bundle = self.root / "v3"
        spec = v3.generate_previews(self.reading_spec(), self.read, bundle, dpi=600)
        for path in bundle.rglob("*"):
            if path.is_file():
                self.objects[(shared.PDF_PAGES_BUCKET, path.relative_to(bundle).as_posix())] = path.read_bytes()
        self.assertEqual(spec['previews'], [])
        mapping = djvu_pages.page_map(self.raw, self.sha)
        layers = [text.from_page({"version": 1, "kind": "pdf-ocr-page", "page": number, "width": g["width"],
                                  "height": g["height"], "source": "ocr", "text": "", "blocks": []}, self.sha)
                  for number, g in enumerate(mapping["pages"], 1)]
        spec["text_layer"] = v3.build_text_bundle(layers, [{"p": 1}, {"p": 2}],
                                                  Path(f"objects/{self.sha[:2]}/{self.sha}/{'e' * 16}"), bundle)
        for path in bundle.rglob("*"):
            if path.is_file():
                self.objects[(shared.PDF_PAGES_BUCKET, path.relative_to(bundle).as_posix())] = path.read_bytes()
        ref, manifest = v3.build_reading(spec, self.read, bundle)
        self.objects[(ref["bucket"], ref["path"])] = (bundle / ref["path"]).read_bytes()
        for path in bundle.rglob("*"):
            if path.is_file():
                self.objects[(shared.PDF_PAGES_BUCKET, path.relative_to(bundle).as_posix())] = path.read_bytes()
        self.assertEqual(manifest["primary"]["kind"], "djvu")
        self.assertEqual(manifest['preview']['status'], 'skipped')
        self.assertTrue(v3.reading_surface_ready(manifest))
        self.assertFalse(list(bundle.rglob('*.jxl')))
        self.assertFalse(list(bundle.rglob('*.png')))
        self.assertEqual(v3.verify_reading(ref, self.read)["generation"], manifest["generation"])
        local = bundle / spec["primary"]["path"]
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.raw)
        reader = publish.CandidateReader(None, bundle)
        self.assertEqual(reader(spec["primary"]), self.raw)
        self.assertEqual(publish.CandidateReader(None, bundle)(ref), self.objects[(ref["bucket"], ref["path"])])
        wrong = {**manifest, "primary": {**manifest["primary"], "kind": "pdf"}}
        with self.assertRaisesRegex(ValueError, "primary"):
            v3.validate_reading_manifest(wrong)

    def test_djvu_primary_rejects_pdf_bytes_and_mismatched_kind(self):
        spec = self.reading_spec()
        self.objects[(shared.PDF_PAGES_BUCKET, spec["primary"]["path"])] = b"%PDF" + self.raw[4:]
        spec["primary"]["sha256"] = hashlib.sha256(b"%PDF" + self.raw[4:]).hexdigest()
        changed = b"%PDF" + self.raw[4:]
        digest = hashlib.sha256(changed).hexdigest()
        spec['source_sha256'] = digest
        spec['primary']['path'] = f"objects/{digest[:2]}/{digest}/" + 'd' * 16 + '/document.djvu'
        self.objects[(shared.PDF_PAGES_BUCKET, spec['primary']['path'])] = changed
        with self.assertRaisesRegex(ValueError, "DjVu magic"):
            v3.build_reading(spec, self.read, self.root / "bad")
        outside = {**self.reading_spec()["primary"], "path": "derived/x/" + "a" * 32 + "/document.djvu"}
        with self.assertRaises(ValueError):
            v3.primary_kind(outside)

    def test_v3_primary_spec_and_import_for_native_djvu(self):
        entry = {"status": "ready", "source_sha256": self.sha, "source_bytes": len(self.raw), "page_count": 2,
                 "source_kind": "upstream", "repo": "VoiceOfML/A", "path": "book.djvu",
                 "source_revision": "c" * 40, "source_format": "djvu"}
        spec, count = automation.primary_spec(self.item()["key"], entry)
        self.assertEqual((count, spec["primary_format"]), (2, "djvu"))
        self.assertTrue(spec["primary"]["path"].endswith("/document.djvu"))
        with self.assertRaises(ValueError):
            automation.primary_spec(self.item()["key"], {**entry, "source_format": None})
        cached = self.root / "cached.djvu"
        cached.write_bytes(self.raw)
        from unittest.mock import Mock
        with patch("huggingface_hub.HfApi") as api, patch("huggingface_hub.hf_hub_download", return_value=str(cached)):
            api.return_value.get_paths_info.return_value = [Mock(size=len(self.raw))]
            publish.import_primary(spec, self.root / "ws")
            self.assertEqual((self.root / "ws" / spec["primary"]["path"]).read_bytes(), self.raw)
            cached.write_bytes(b"%PDF" + self.raw[4:])
            changed = {**spec, "source_sha256": hashlib.sha256(cached.read_bytes()).hexdigest()}
            other = changed["source_sha256"]
            changed["primary"] = {**spec["primary"], "sha256": other,
                                  "path": f"objects/{other[:2]}/{other}/" + spec["primary"]["path"].split("/")[3] + "/document.djvu"}
            with self.assertRaisesRegex(ValueError, "magic"):
                publish.import_primary(changed, self.root / "ws2")
        self.assertTrue(publish.PUBLIC_OBJECT.fullmatch(spec["primary"]["path"]))


@unittest.skipUnless(TOOLS, "djvulibre command-line tools are not installed")
class DjvuPromotionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_publish_reader_v3 import Store, V3PublicationTests
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        self.store = Store()
        self.pdf_case = V3PublicationTests()
        self.pdf_case.bundle, self.pdf_case.store = self.bundle, self.store
        self.raw = build_djvu(Path(tempfile.mkdtemp(dir=self.bundle))).read_bytes()
        self.sha = hashlib.sha256(self.raw).hexdigest()

    def djvu_candidate(self, key):
        path = self.bundle / f"objects/{self.sha[:2]}/{self.sha}/{'d' * 16}/document.djvu"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.raw)
        read = lambda ref: (self.bundle / ref["path"]).read_bytes()
        primary = publish.metadata(shared.PDF_PAGES_BUCKET, path.relative_to(self.bundle).as_posix(), self.raw)
        spec = v3.generate_previews({"source_key": key, "source_sha256": self.sha, "primary": primary,
                                     "require_complete_preview": True}, read, self.bundle)
        mapping = djvu_pages.page_map(self.raw, self.sha)
        layers = [text.from_page({"version": 1, "kind": "pdf-ocr-page", "page": n, "width": g["width"],
                                  "height": g["height"], "source": "ocr", "text": "", "blocks": []}, self.sha)
                  for n, g in enumerate(mapping["pages"], 1)]
        spec["text_layer"] = v3.build_text_bundle(layers, [{"p": 1}, {"p": 2}],
                                                  Path(f"objects/{self.sha[:2]}/{self.sha}/{'e' * 16}"), self.bundle)
        return v3.build_reading(spec, read, self.bundle)[0]

    def test_native_djvu_stages_and_replaces_only_named_derived_source(self):
        key = "repo\0book.pdf"
        pdf = publish.stage(self.store, self.pdf_case.candidate(), self.bundle, apply=True)
        first = publish.promote(self.store, pdf["candidate"], None, apply=True)
        staged = publish.stage(self.store, self.djvu_candidate(key), self.bundle, apply=True)
        self.assertIn((shared.PDF_PAGES_BUCKET, f"objects/{self.sha[:2]}/{self.sha}/{'d' * 16}/document.djvu"),
                      self.store.objects)
        with self.assertRaisesRegex(ValueError, "migration does not match"):
            publish.promote(self.store, staged["candidate"], first["generation"], apply=True,
                            replace_source="f" * 64)
        publish.promote(self.store, staged["candidate"], first["generation"], apply=True,
                        replace_source="a" * 64)
        entry = publish.current(self.store)[1]["files"][key]
        self.assertEqual(entry["source_sha256"], self.sha)

    def test_direct_migration_keeps_exact_corrected_text_reference_without_ocr(self):
        key = "VoiceOfML/A\0book.djvu"
        initial_ref = self.djvu_candidate(key)
        prior = json.loads((self.bundle / initial_ref['path']).read_bytes())
        first = publish.stage(self.store, initial_ref, self.bundle, apply=True)
        publish.promote(self.store, first['candidate'], None, apply=True)
        with patch.object(v3, 'repartition_text_bundle', side_effect=AssertionError('text must remain untouched')), \
                patch.object(v3, 'backfill_text', side_effect=AssertionError('OCR must not run')):
            result = publish.build_stage(self.store, {'source_key': key, 'source_sha256': self.sha,
                    'direct_reading': initial_ref}, self.bundle / 'direct', apply=False)
        updated = json.loads((self.bundle / 'direct' / result['reading']['path']).read_bytes())
        self.assertEqual(updated['text_layer'], prior['text_layer'])
        self.assertEqual(result['preserved_text_layer'], prior['text_layer'])
        self.assertEqual(updated['preview']['status'], 'skipped')
        self.assertEqual(updated['primary'], prior['primary'])
        self.assertFalse(list((self.bundle / 'direct').rglob('*.png')))

    def test_direct_migration_does_not_require_or_fetch_old_preview_partition(self):
        key = "VoiceOfML/A\0book.djvu"
        ref = self.djvu_candidate(key)
        direct = json.loads((self.bundle / ref['path']).read_bytes())
        legacy = copy.deepcopy(direct)
        missing_path = ref['path'].replace('/reading-manifest.json', '/preview/partition-000001-000002-manifest.json')
        missing_ref = {'bucket': shared.PDF_PAGES_BUCKET, 'path': missing_path,
                       'sha256': 'f' * 64, 'bytes': 10, 'role': 'runtime'}
        legacy['preview'] = {'page_count': 2, 'complete': True, 'ready_pages': 2,
                             'partitions': [{'start': 1, 'end': 2, 'complete': True, 'resource': missing_ref}]}
        legacy['components']['preview'] = 'ready'
        # A historical manifest is still an immutable, content-addressed v3 root.
        old_path = self.bundle / f"objects/{self.sha[:2]}/{self.sha}/{'a' * 16}/reading-manifest.json"
        old_path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(legacy, sort_keys=True).encode(); old_path.write_bytes(raw)
        old_ref = publish.metadata(shared.PDF_PAGES_BUCKET, old_path.relative_to(self.bundle).as_posix(), raw)
        for path in (item for item in self.bundle.rglob('*') if item.is_file()):
            self.store.put_bytes(shared.PDF_PAGES_BUCKET, path.relative_to(self.bundle).as_posix(), path.read_bytes())
        with patch.object(v3, 'repartition_text_bundle', side_effect=AssertionError('do not touch text')):
            result = publish.build_stage(self.store, {'source_key': key, 'source_sha256': self.sha,
                       'direct_reading': old_ref}, self.bundle / 'direct-old', apply=False)
        self.assertEqual(result['preserved_text_layer'], legacy['text_layer'])
        self.assertEqual(result['components']['preview'], 'skipped')
        self.assertNotIn((shared.PDF_PAGES_BUCKET, missing_path), self.store.objects)


class MigrationGateTests(unittest.TestCase):
    def test_migration_requires_flag_and_matching_probe(self):
        task = {"spec": {"primary_format": "djvu", "source_key": "k", "source_sha256": "a" * 64},
                "djvu_migration": {"allowed": True, "active_generation": "g", "active_source_sha256": "b" * 64}}
        active = {"reading_generation": "g", "source_sha256": "b" * 64}
        with patch.dict(os.environ, {"READER_V3_DJVU_MIGRATION": "false"}):
            self.assertFalse(automation._can_migrate_djvu(task, active))
        with patch.dict(os.environ, {"READER_V3_DJVU_MIGRATION": "true"}):
            self.assertTrue(automation._can_migrate_djvu(task, active))
            self.assertFalse(automation._can_migrate_djvu(task, {**active, "reading_generation": "h"}))
            task["djvu_migration"]["allowed"] = False
            self.assertFalse(automation._can_migrate_djvu(task, active))


if __name__ == "__main__":
    unittest.main()
