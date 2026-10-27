"""Direct-document publication gates. No network, model, or bucket writes."""
import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from PIL import Image

from scripts import (build_reader_assets_index, build_reader_assets_v2_sidecar, djvu_pages,
                     pdf_assets, pdf_ocr, pdf_ocr_stages as stages, pdf_reading_v3 as v3, shared)


class DirectDocumentPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_pdf_and_djvu_builders_never_generate_or_adopt_document_images(self):
        with pymupdf.open() as doc:
            doc.new_page(width=100, height=200)
            pdf = doc.tobytes()
        djvu = b"a pinned native DjVu document for mocked structural parsing"
        for kind, raw in (("pdf", pdf), ("djvu", djvu)):
            with self.subTest(kind=kind):
                source = hashlib.sha256(raw).hexdigest()
                path = f"objects/{source[:2]}/{source}/{'a'*16}/document.{kind}"
                ref = {"bucket": shared.PDF_PAGES_BUCKET, "path": path, "sha256": source,
                       "bytes": len(raw), "role": "runtime"}
                spec = {"source_key": "repo\0book." + kind, "source_sha256": source,
                        "primary": ref, "previews": [{"must_not": "read"}],
                        "require_complete_preview": True}
                def read(requested):
                    if requested == ref:
                        return raw
                    return (self.root / kind / requested['path']).read_bytes()
                geometry = {"version": 1, "kind": "pdf-page-map", "source_sha256": source,
                            "source_format": "djvu", "pages": [{"page": 1, "width": 100, "height": 200}]}
                with patch.object(djvu_pages, "sniff", return_value="djvu"), \
                        patch.object(djvu_pages, "page_map", return_value=geometry), \
                        patch.object(pdf_ocr, "encode_jxl", side_effect=AssertionError("not a document output")):
                    generated = v3.generate_previews(spec, lambda _: self.fail("no preview reads"), self.root / kind)
                    self.assertEqual(generated["previews"], [])
                    reading, manifest = v3.build_reading(spec, read, self.root / kind)
                    self.assertEqual(v3.verify_reading(reading, read), manifest)
                self.assertEqual(manifest["preview"], {"page_count": 1, "complete": False,
                    "ready_pages": 0, "partitions": [], "status": "skipped"})
                self.assertTrue(v3.reading_surface_ready(manifest))
                self.assertFalse(list((self.root / kind).rglob("*.png")))
                self.assertFalse(list((self.root / kind).rglob("*.jxl")))
                if kind == "djvu":
                    bad = copy.deepcopy(manifest)
                    bad["primary"]["resource"]["sha256"] = "f" * 64
                    with self.assertRaisesRegex(ValueError, "identity"):
                        v3.validate_reading_manifest(bad)
                    with self.assertRaisesRegex(ValueError, "identity"):
                        v3.build_reading({**spec, "primary": bad["primary"]["resource"]}, read, self.root / kind)

    def test_stale_sidecars_cannot_advertise_document_pages_but_images_survive(self):
        image = {"status": "ready", "reader_mode": "image", "path": "images/scan.png"}
        old = {"status": "rendered", "source_format": "djvu", "render_manifest": {"path": "x"},
               "page_manifest": {"path": "objects/old/page-manifest.json"}}
        index = build_reader_assets_index.build_index({"files": {"image": image}},
                 ocr_manifest={"files": {"book": old}})
        self.assertNotIn("book", index["f"])
        self.assertEqual(index["f"]["image"]["p"], "images/scan.png")
        files = {"image": {"s": 2, "m": "i", "p": "image-page-manifest.json"}}
        build_reader_assets_v2_sidecar.add_pdf_streams(files, {"files": {"book": old}}, shared.PDF_PAGES_BUCKET)
        self.assertEqual(set(files), {"image"})

    def test_old_document_renderer_cannot_create_djvu_or_pdf_page_stream(self):
        source = self.root / 'source.djvu'
        source.write_bytes(b'AT&TFORM-example')
        with patch.object(pdf_assets, '_render', side_effect=AssertionError('retired producer must not render')):
            result = pdf_assets.build_item({'extension': 'djvu', 'range_risk_candidate': True}, source, self.root)
        self.assertEqual((result['status'], result['strategy']), ('skipped', 'preserve-djvu'))
        with self.assertRaisesRegex(ValueError, 'page-image rendering is retired'):
            pdf_assets._render(source, 1, self.root, 'djvu')
        old = {'v': 1, 'f': {'book': {'s': 2, 'm': 'p', 'p': 'old-route'}}}
        stale = {'key': 'book', 'source_format': 'djvu', 'status': 'ready',
                 'page_manifest': {'path': 'objects/old/page-manifest.json'}}
        self.assertEqual(json.loads(gzip.decompress(pdf_assets.update_sidecar(old, [stale]))), old)

    def test_ocr_render_does_not_make_reader_png_webp_jxl_or_rerender_native_text(self):
        source = self.root / "book.djvu"
        source.write_bytes(b"AT&TFORM-test-source")
        item = {"key": "repo\0book.djvu", "source_format": "djvu", "source_kind": "upstream",
                "source_revision": "r1", "force_image_render": True,
                "probe": {"page_count": 2, "page_chars": [60, 0], "classification": "mixed"}}
        native = {1: {"width": 200, "height": 300, "text": "useful native text",
                      "blocks": [{"t": "useful native text", "b": [0, 0, 1, .2], "c": 1, "s": "native"}]}}
        calls = []
        def render(_source, number, folder, *, info, reader_jxl, reader_images):
            calls.append((number, reader_jxl, reader_images))
            path = folder / f"page-{number:06d}.png"
            Image.new("L", (200, 300), 255).save(path)
            return path, 200, 300
        with patch.object(djvu_pages, "sniff", return_value="djvu"), \
                patch.object(djvu_pages, "inspect", return_value=[{}, {}]), \
                patch.object(djvu_pages, "text_pages", return_value=native), \
                patch.object(djvu_pages, "render_page", side_effect=render), \
                patch.object(pdf_ocr, "encode_jxl", side_effect=AssertionError("no reader image")):
            result = stages.render_book(item, source, self.root / "bundle")
        manifest = json.loads((self.root / "bundle" / result["render_manifest"]["path"]).read_bytes())
        stages.validate_render(result, manifest)
        self.assertEqual(calls, [(2, False, False)])
        self.assertEqual(result["reader_presentation"]["strategy"], "preserve-djvu")
        self.assertIsNone(result["page_manifest"])
        self.assertFalse(result["image_rendered"])
        self.assertTrue(manifest["pages"][0]["native_text_only"])
        self.assertTrue(all(not ({"w", "j", "r"} & page.keys()) for page in manifest["pages"]))
        self.assertEqual(len(list((self.root / "bundle").rglob("*.png"))), 1)
        for field in ("w", "j", "r"):
            bad = copy.deepcopy(manifest)
            bad["pages"][1][field] = "unexpected"
            with self.subTest(field=field), self.assertRaises(ValueError):
                stages.validate_render(result, bad)

    def test_delivery_change_does_not_reschedule_completed_or_failed_ocr(self):
        item = {"key": "repo\0book.djvu", "repo": "repo", "path": "book.djvu", "source_kind": "upstream",
                "source_revision": "r1", "source_format": "djvu", "source_sha256": "a" * 64}
        old_render = {**item, "status": "ready", "render_profile": "old-image-stream"}
        for status in ("ready", "skipped", "failed"):
            prior = {**item, "status": status}
            self.assertEqual(stages.pending_render([item], {item["key"]: old_render}, {item["key"]: prior}), [])
        self.assertEqual(len(stages.pending_render([item], {item["key"]: old_render}, {})), 1)

    def test_stale_worker_render_policy_cannot_reach_central_publication(self):
        current = stages.render_profile_for({'source_format': 'djvu'})
        for profile in (current.replace(djvu_pages.DIRECT_READER_PROFILE, djvu_pages.BITONAL_READER_PROFILE),
                        current + '-pretend-new', 'unversioned'):
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, 'stale document render policy'):
                stages.require_current_render_policy({'source_format': 'djvu', 'render_profile': profile})
        stages.require_current_render_policy({'source_format': 'djvu', 'render_profile': current})
        stages.require_current_render_policy({'render_profile': stages.render_profile()})

    def test_document_uploader_rejects_page_images_before_any_remote_writes(self):
        root = self.root / 'bundle' / 'objects' / 'aa' / ('a' * 64) / ('b' * 16)
        for index, relative in enumerate(('pages/page-000001.png', 'page-manifest.json')):
            bundle = self.root / f'bundle-{index}'
            target = bundle / root.relative_to(self.root / 'bundle') / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'old')
            with patch.object(stages, 'publish_json_batch', side_effect=AssertionError('must not write')):
                with self.subTest(path=relative), self.assertRaisesRegex(ValueError, 'reader page images are retired'):
                    stages.upload_objects(bundle)

    def test_native_discovery_does_not_silently_keep_recorded_derived_pdf(self):
        search = self.root / 'search.json'
        revisions = self.root / 'revisions.json'
        source = 'VoiceOfML/A\0book.djvu'
        search.write_text(json.dumps([{'Repo': 'VoiceOfML/A', 'File': 'book', 'Extension': 'djvu',
                                       'Folder': [], 'Size': 100}]))
        revisions.write_text(json.dumps({'VoiceOfML/A': 'c' * 40}))
        assets = {'files': {source: {'status': 'ready', 'reader_mode': 'pdf', 'bytes': 5,
                  'path': 'derived/weekly-djvu/' + 'a' * 32 + '/document.pdf', 'source_revision': 'c' * 40}}}
        with patch.dict('os.environ', {'PDF_DJVU_NATIVE': 'true'}), \
                patch.object(djvu_pages, 'native_exceptions', return_value={source: {'key': source}}):
            records = pdf_ocr.source_records(search, revisions, assets)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['source_kind'], 'upstream')
        self.assertEqual(records[0]['source_format'], 'djvu')


if __name__ == "__main__":
    unittest.main()
