import copy
import gzip
import hashlib
import json
import sys
import tempfile
import unittest
import yaml
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from scripts import pdf_ocr, pdf_ocr_stages as stages, plan_pdf_ocr, ocr_quality, ocr_layout
from scripts.build_reader_assets_index import build_index


class PdfOcrStagesTests(unittest.TestCase):
    def test_secondary_formula_identity_is_recorded_without_overwriting_primary_model(self):
        from types import SimpleNamespace
        import numpy as np
        result=self.render_fixture(native=False)
        with patch.object(stages,'read_object',side_effect=self.read):
            queue=stages.plan_images({result['key']:result},{},{},target=500)
            task=queue['shards'][0][0]
            primary=SimpleNamespace(recognize_txt=lambda images:SimpleNamespace(txts=['OH-1'],scores=[.84]))
            secondary=SimpleNamespace(recognize_txt=lambda images:SimpleNamespace(txts=['-1'],scores=[.99]))
            calls=[]
            def engine(*args,**kwargs):
                calls.append(kwargs.get('model_size','medium'))
                return secondary if kwargs.get('model_size')=='small' else primary
            def models(*args,**kwargs):
                return {'models':{'det':{'sha256':'a'*64},'rec':{
                    'sha256':stages.ocr_formula_scripts.CONFIRMATION_REC_SHA256 if kwargs.get('model_size')=='small' else 'b'*64}}}
            def refine(image,blocks,read,*,read_secondary=None):
                self.assertEqual(read(np.zeros((20,20,3),np.uint8)),('OH-1',.84))
                self.assertEqual(read_secondary(np.zeros((20,20,3),np.uint8)),('-1',.99))
                return blocks,None
            with patch.object(pdf_ocr,'RAPID_MODEL_SIZE','medium'), \
                    patch.object(pdf_ocr,'ocr_page',return_value=[{'t':'正文','b':[.1,.1,.5,.2],'c':.8,'s':'ocr'}]), \
                    patch.object(pdf_ocr,'get_ocr_engine',side_effect=engine), \
                    patch.object(pdf_ocr,'actual_ocr_models',side_effect=models), \
                    patch.object(stages.ocr_formula_scripts,'refine_tokens',side_effect=refine):
                actual=stages.recognize_task(task,self.root/'secondary-formula')
            self.assertEqual(actual['errors'],[])
            self.assertIn('small',calls)
            for page in actual['pages']:
                model=page['recognition']['models']['models']
                self.assertEqual(model['rec']['sha256'],'b'*64)
                self.assertEqual(model['formula_confirmation_rec']['sha256'],stages.ocr_formula_scripts.CONFIRMATION_REC_SHA256)

    def test_language_override_changes_recognition_profile(self):
        key = "repo\0中文目录/俄文书.pdf"
        options = stages.layout_options({}, key)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overrides.json"
            path.write_text(json.dumps({"version": 1, "overrides": {
                key: {"language": "ru", "backend": "paddle_onnxruntime", "source": "osd-triage"}}}))
            with patch.object(pdf_ocr, "LANGUAGE_OVERRIDES_PATH", path):
                pdf_ocr._LANGUAGE_OVERRIDES = None
                try:
                    overridden = stages.recognition_identity(
                        {"key": key, "ocr_language": "ch", "ocr_backend": "rapidocr_onnxruntime"}, options)
                    self.assertIn("-lang-ru", overridden)
                    self.assertIn("paddle-onnxruntime", overridden)
                finally:
                    pdf_ocr._LANGUAGE_OVERRIDES = None
        plain = stages.recognition_identity(
            {"key": key, "ocr_language": "ch", "ocr_backend": "rapidocr_onnxruntime"}, options)
        self.assertNotEqual(overridden, plain)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.objects = {}
        model_patch = patch.object(pdf_ocr, "actual_ocr_models", return_value={"models": {
            "det": {"sha256": "a" * 64}, "rec": {"sha256": "b" * 64}}})
        model_patch.start()
        self.addCleanup(model_patch.stop)
        probe_patch = patch.object(ocr_quality, "probe_image", return_value={"blank": False,
            "regions": [{"region": "whole", "script": "Han", "confidence": 8, "rotation": 0}]})
        probe_patch.start()
        self.addCleanup(probe_patch.stop)

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
                "source_kind": "upstream", "source_revision": "revision",
                "reader_presentation": {"strategy": "page-stream", "complete": True}}

    def render_fixture(self, native=False, jxl=False, force_image=False, native_only=False, djvu=True):
        source = self.root / ("source.djvu" if djvu else "source.pdf")
        source.write_bytes(b"AT&TFORM-test-source" if djvu else b"%PDF-test-source")
        bundle = self.root / "render"
        def render(_source, page, directory, reader_pixels=None, reader_jxl=False, reader_images=True, **_kwargs):
            path = directory / f"page-{page:06d}.png"
            with Image.new("RGB", (200, 300), "white") as image:
                image.save(path)
                if reader_images:
                    image.resize((100, 150)).save(path.with_suffix(".webp"))
            return path, 200, 300
        def encode(png, destination):
            with Image.open(png) as image:
                self.assertEqual(image.size, (200, 300))
            destination.write_bytes(b"jxl")
        item = {**self.item(), **({'source_format': 'djvu', 'key': 'repo\0small.djvu',
                                 'path': 'small.djvu'} if djvu else {}),
                'reader_presentation': {'strategy': 'page-stream' if djvu else 'preserve-pdf'},
                "probe": {"page_count": 2, "page_chars": [60, 60] if native_only else
                                         [60 if native else 0, 0],
                                         "classification": "native-text" if native_only else "mixed" if native else "scan"},
                **({"force_image_render": True} if force_image else {})}
        native_pages = {page: {"width": 200, "height": 300,
                            "blocks": [{"t": "原生文字", "b": [.1, .1, .9, .2], "c": 1, "s": "native"}],
                            "text": "原生文字"} for page in (1, 2)}
        with patch.object(stages.djvu_pages, 'sniff', return_value='djvu'), \
                patch.object(stages.djvu_pages, 'inspect', return_value=[{'width': 200, 'height': 300}] * 2), \
                patch.object(stages.djvu_pages, 'text_pages', return_value=native_pages), \
                patch.object(stages.djvu_pages, 'render_page', side_effect=render), \
                patch.object(pdf_ocr, "render_page", side_effect=render), \
                patch.object(pdf_ocr, "JXL_ENABLED", jxl), \
                patch.object(pdf_ocr, "encode_jxl", side_effect=encode), \
                patch.object(pdf_ocr, "native_pages", side_effect=lambda _source, pages: {
                    page: {"width": 200, "height": 300,
                           "blocks": [{"t": "原生文字", "b": [.1, .1, .9, .2], "c": 1, "s": "native"}],
                           "text": "原生文字"}
                    for page in pages}), \
                patch.object(pdf_ocr, "ocr_page", side_effect=AssertionError("renderer must not OCR")):
            result = stages.render_book(item, source, bundle)
        self.store(bundle)
        return result

    def test_primary_pdf_keeps_outline_in_document_without_generating_page_stream(self):
        toc = [{"title": "封面", "page": 1, "depth": 0}, {"title": "正文", "page": 2, "depth": 0}]
        with patch.object(stages.pdf_assets, "extract_pdf_outline", return_value=toc) as outline:
            result = self.render_fixture(djvu=False)
        self.assertIsNone(result['page_manifest'])
        self.assertFalse(result['image_rendered'])
        outline.assert_not_called()
        self.assertEqual(stages.valid_toc([{"title": "x", "page": 3, "depth": 0}], 2), [])
        self.assertEqual(stages.valid_toc([{"title": "x", "page": 1, "depth": 0, "extra": 1}], 2), [])

    def test_render_persists_png_jxl_and_native_text_before_ocr(self):
        result = self.render_fixture(native=True, jxl=True)
        manifest = json.loads(self.read(result["render_manifest"]))
        stages.validate_render(result, manifest)
        self.assertEqual(result["status"], "ready")
        self.assertLess(result["source_bytes"], 100 * 1024 ** 2)
        self.assertEqual(result["ocr_pages"], 1)
        for page in manifest["pages"]:
            self.assertNotIn('w', page)
            for field in ("i", "j"):
                self.read(stages.page_meta(page, field))
        native = manifest["pages"][0]
        self.assertEqual(json.loads(gzip.decompress(self.read(stages.page_meta(native, "o"))))["text"], "原生文字")
        self.assertNotIn("o", manifest["pages"][1])

    def test_native_djvu_still_gets_complete_image_stream_and_jxl(self):
        result = self.render_fixture(native_only=True, jxl=True, force_image=True)
        manifest = json.loads(self.read(result["render_manifest"]))
        self.assertTrue(manifest["image_rendered"])
        self.assertTrue(result["image_rendered"])
        self.assertEqual(result["ocr_pages"], 0)
        self.assertIsNotNone(result["page_manifest"])
        for page in manifest["pages"]:
            self.assertNotIn('w', page)
            for field in ("i", "j"):
                self.read(stages.page_meta(page, field))
            self.assertEqual(page["source"], "native")
            self.read(stages.page_meta(page, "o"))
        with patch.object(stages, "read_object", side_effect=self.read):
            queue = stages.plan_images({result["key"]: result}, {}, {})
            self.assertEqual(queue["total_ocr_pages"], 0)
            book = stages.assemble_book(queue["books"][0], {}, self.root / "native-images-text")
        self.store(self.root / "native-images-text")
        text = json.loads(gzip.decompress(self.read(json.loads(self.objects[book["ocr_manifest"]])["book_text"])))
        self.assertEqual([page["text"] for page in text["pages"]], ["原生文字", "原生文字"])
        self.assertTrue(book["stream"])

    def test_stale_pdf_force_image_flag_does_not_require_new_page_images(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF-test-source")
        item = {**self.item(), "probe": {"page_count": 2, "page_chars": [60, 60],
                                        "classification": "native-text"}, "force_image_render": True,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "page_count": 2,
                "profile": pdf_ocr.asset_profile(), "render_profile": stages.render_profile()}
        old = {**item, "start": 1, "end": 2}
        old["force_image_render"] = False
        with patch.object(pdf_ocr, "native_pages", side_effect=lambda _source, pages: {
                page: {"width": 200, "height": 300, "blocks": [
                    {'t': '原生文字', 'b': [.1, .1, .8, .2], 'c': 1., 's': 'native'}], "text": "原生文字"}
                for page in pages}):
            result = stages.render_book(old, source, self.root / "old-range")
        self.store(self.root / "old-range")
        progress = {item["key"]: {**stages.range_identity(item),
                    "ranges": {stages.range_id(1, 2): result["descriptor"]}}}
        with patch.object(stages, "read_object", side_effect=self.read):
            queue = stages.plan_render_ranges({"shards": [{"records": [item]}]}, progress)
        self.assertTrue(queue['saved_ranges'])
        self.assertEqual(queue['shards'], [])

    def test_native_pdf_with_rejected_page_keeps_pdf_and_renders_only_ocr_input(self):
        source = self.root / 'source.pdf'
        source.write_bytes(b'%PDF-test-source')
        native = {'width': 200, 'height': 300, 'text': 'positioned text',
                  'blocks': [{'t': 'positioned text', 'b': [.1, .1, .9, .2], 's': 'native', 'c': 1.}]}
        rejected = {**native, 'native_evidence': {'reasons': ['unverified-type3-font']}}
        def render(_source, number, directory, reader_pixels=None, reader_jxl=False, reader_images=True):
            self.assertEqual(number, 2)
            self.assertFalse(reader_images)
            self.assertFalse(reader_jxl)
            image = directory / 'page-000002.png'
            Image.new('L', (200, 300), 255).save(image)
            return image, 200, 300
        item = {**self.item(), 'probe': {'classification': 'native-text', 'page_count': 2, 'page_chars': [80, 80]}}
        with patch.object(pdf_ocr, 'native_pages', return_value={1: native, 2: rejected}), \
                patch.object(pdf_ocr, 'render_page', side_effect=render), patch.object(pdf_ocr, 'JXL_ENABLED', True):
            result = stages.render_book(item, source, self.root / 'native-rejection')
        self.store(self.root / 'native-rejection')
        manifest = stages.validate_render(result, json.loads(self.read(result['render_manifest'])))
        self.assertIsNone(result['page_manifest'])
        self.assertFalse(result['image_rendered'])
        self.assertTrue(manifest['pages'][0]['native_text_only'])
        page = manifest['pages'][1]
        self.assertEqual(page['native_rejection']['native_evidence'], rejected['native_evidence'])
        self.read(stages.page_meta(page, 'i'))
        del page['i']
        with self.assertRaises((KeyError, ValueError)):
            stages.validate_render(result, manifest)

    def test_image_stream_cannot_hide_missing_native_page_with_text_only_flag(self):
        result = self.render_fixture(native=True, jxl=True)
        manifest = json.loads(self.read(result['render_manifest']))
        page = manifest['pages'][0]
        for field in ['j', 'i']:
            del page[field]
        page['native_text_only'] = True
        with self.assertRaisesRegex(ValueError, 'incomplete reader image stream'):
            stages.validate_render(result, manifest)

    def test_partial_native_text_prevents_source_pixel_cap(self):
        source = self.root / "mixed-input.pdf"
        source.write_bytes(b"%PDF-test-source")
        dimensions = []
        def render(_source, number, directory, reader_pixels=None, reader_jxl=False, reader_images=True):
            self.assertFalse(reader_images)
            dimensions.append(reader_pixels)
            path = directory / f"page-{number:06d}.png"
            with Image.new("RGB", (200, 300), "white") as image:
                image.save(path)
                image.save(path.with_suffix(".webp"))
            return path, 200, 300
        item = {**self.item(), "probe": {"classification": "scan", "page_count": 2,
                                        "page_chars": [12, 0]}}
        with patch.object(pdf_ocr, "scan_reader_images", return_value={1: (100, 150), 2: (100, 150)}), \
                patch.object(pdf_ocr, 'native_pages', return_value={1: {
                    'width': 200, 'height': 300, 'blocks': [], 'text': 'partial text'}}), \
                patch.object(pdf_ocr, "render_page", side_effect=render), \
                patch.object(pdf_ocr, "JXL_ENABLED", False):
            stages.render_book(item, source, self.root / "mixed-input-render")
        self.assertEqual(dimensions, [None, None])

    def test_stale_force_image_flag_does_not_requeue_primary_pdf_for_images(self):
        item = {**self.item(), "force_image_render": True}
        previous = {**item, "status": "ready", "render_profile": stages.render_profile(),
                    "image_rendered": False}
        self.assertEqual(stages.pending_render([item], {item['key']: previous}, {}), [])
        previous["image_rendered"] = True
        self.assertEqual(stages.pending_render([item], {item["key"]: previous}, {}), [])

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
            self.assertEqual((text["language"], text["ocr_version"]), pdf_ocr.resolve_ocr_config("ch", "rapidocr_onnxruntime")[:2])
            self.assertEqual([p["text"] for p in text["pages"]], ["原生文字", "识别结果"])
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["page_manifest"], result["page_manifest"])
            from scripts.pdf_reading_v3 import verify_text_bundle
            index = verify_text_bundle(completed["text_layer"], self.read, completed["source_sha256"], 2)
            self.assertEqual(index["revision"], "raw")
            self.assertEqual(index["quality"], "unreviewed")
            self.assertEqual(text["text_layer"], completed["text_layer"])

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

    def test_recognition_adopts_unrecorded_existing_page_without_ocr(self):
        result = self.render_fixture()
        with patch.object(stages, "read_object", side_effect=self.read):
            queue = stages.plan_images({result["key"]: result}, {}, {})
            task = queue["shards"][0][0]
            page = task["pages"][0]
            root = stages.root_for(task["source_sha256"], task["key"], task["generation"] + task["profile"])
            path = (root / "ocr" / f"page-{page['p']:06d}.json.gz").as_posix()
            blocks = [{"t": "旧", "b": [.1, .1, .9, .2], "c": 1, "s": "ocr"}]
            payload = {**pdf_ocr.page_payload(page["p"], page["width"], page["height"], blocks, "ocr"),
                       **ocr_layout.arrange(blocks, page["width"], page["height"]),
                       "recognition": {"policy": ocr_quality.POLICY, "profile": task["profile"],
                           "source_sha256": task["source_sha256"], "input_sha256": page["is"],
                           "language": "ch", "models": pdf_ocr.actual_ocr_models(),
                           "language_evidence": {"scripts": ["Han"]}}}
            payload["quality_check"] = ocr_quality.check_page(payload)
            raw = gzip.compress(json.dumps(payload).encode(), mtime=0)
            with patch.object(stages, "remote_sizes", return_value={path: len(raw)}), \
                    patch.object(stages, "read_existing_object", return_value=raw), \
                    patch.object(pdf_ocr, "ocr_page", return_value=[]) as engine:
                recognized = stages.recognize_task(task, self.root / "adopt", adopt_existing=True)
            adopted = next(p for p in recognized["pages"] if p["p"] == page["p"])
            self.assertEqual((adopted["os"], adopted["text"]), (hashlib.sha256(raw).hexdigest(), "旧"))
            self.assertEqual(engine.call_count, len(task["pages"]) - 1)
            wrong = gzip.compress(json.dumps(pdf_ocr.page_payload(page["p"] + 1, page["width"], page["height"],
                                                                  [], "ocr")).encode(), mtime=0)
            with patch.object(stages, "remote_sizes", return_value={path: len(wrong)}), \
                    patch.object(stages, "read_existing_object", return_value=wrong), \
                    patch.object(pdf_ocr, "ocr_page", return_value=[]):
                rejected = stages.recognize_task(task, self.root / "adopt-wrong", adopt_existing=True)
            self.assertTrue(any("not this page" in e["error"] for e in rejected["errors"]))

    def test_default_valued_layout_option_does_not_requeue_ready_ocr(self):
        result = self.render_fixture()
        with patch.object(stages, "read_object", side_effect=self.read):
            queue = stages.plan_images({result["key"]: result}, {}, {})
            book = queue["books"][0]
            ready = {**{k: v for k, v in book.items() if k not in {"pages", "saved"}}, "status": "ready",
                      "layout_options": {"default": {"rotation": 0}, "pages": {}}}
            ready['profile'] = stages.recognition_identity(ready, ready['layout_options'])
            self.assertEqual(ready['profile'], book['profile'])
            self.assertEqual(stages.plan_images({result["key"]: result}, {result["key"]: ready}, {})["books"], [])
            rotated = {**ready, "layout_options": {"default": {"rotation": 90}, "pages": {}}}
            self.assertEqual(len(stages.plan_images({result["key"]: result}, {result["key"]: rotated}, {})["books"]), 1)

    def test_degenerate_three_pixel_page_is_retained_without_calling_ocr_engine(self):
        import io
        with Image.new("RGB", (2200, 3), "white") as image:
            stream = io.BytesIO()
            image.save(stream, format="PNG")
        raw = stream.getvalue()
        page = {"p": 283, "width": 2200, "height": 3, "source": "ocr",
                "i": "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/ocr-input/page-000283.png",
                "ib": len(raw), "is": hashlib.sha256(raw).hexdigest()}
        task = {"key": "repo\0degenerate.pdf", "generation": "c" * 64,
                "source_sha256": "d" * 64, "profile": "profile", "ocr_language": "ch",
                "ocr_backend": "rapidocr_onnxruntime", "layout_options": {}, "pages": [page]}
        with patch.object(stages, "read_object", return_value=raw), \
                patch.object(pdf_ocr, "ocr_page", side_effect=AssertionError("degenerate page must skip OCR")):
            result = stages.recognize_task(task, self.root / "degenerate-ocr")
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["pages"]), 1)
        self.store(self.root / "degenerate-ocr")
        saved = result["pages"][0]
        payload = json.loads(gzip.decompress(self.read(stages.page_meta(saved, "o"), ".json.gz")))
        self.assertEqual(payload["text"], "")
        self.assertEqual(payload["blocks"], [])
        self.assertEqual(payload["ocr_warning"], "degenerate-image-below-minimum-side")

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

    def test_large_render_book_ranges_resume_and_publish_only_when_complete(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF-range-source")
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        item = {**self.item(), "source_sha256": sha, "source_bytes": source.stat().st_size,
                "probe": {"page_count": 5, "page_chars": [0] * 5, "classification": "scan"},
                "page_count": 5, "profile": pdf_ocr.asset_profile()}
        queue = {"version": 1, "kind": "pdf-render-queue", "shards": [{"records": [item]}]}
        def render(_source, number, directory, reader_pixels=None, reader_jxl=False, reader_images=True):
            self.assertFalse(reader_images)
            path = directory / f"page-{number:06d}.png"
            with Image.new("RGB", (200, 300), "white") as image:
                image.save(path)
                image.save(path.with_suffix(".webp"))
            return path, 200, 300
        toc = [{"title": "第一章", "page": 1, "depth": 0}, {"title": "第二节", "page": 4, "depth": 1}]
        with patch.object(stages, "RENDER_RANGE_THRESHOLD", 2), \
                patch.object(stages, "RENDER_RANGE_PAGES", 2), \
                patch.object(pdf_ocr, "JXL_ENABLED", False), \
                patch.object(stages.pdf_assets, "extract_pdf_outline", return_value=toc) as outline, \
                patch.object(pdf_ocr, "render_page", side_effect=render):
            planned = stages.plan_render_ranges(queue, {})
            tasks = [task for shard in planned["shards"] for task in shard["records"]]
            self.assertEqual([(task["start"], task["end"]) for task in sorted(tasks, key=lambda t: t["start"])],
                             [(1, 2), (3, 4), (5, 5)])
            book = planned["books"][0]
            descriptors = {}
            for task in tasks[:2]:
                bundle = self.root / f"range-{task['start']}"
                result = stages.render_book(task, source, bundle)
                self.store(bundle)
                descriptors[stages.range_id(task["start"], task["end"])] = result["descriptor"]
            with patch.object(stages, "read_object", side_effect=self.read):
                self.assertIsNone(stages.assemble_render_book(book, descriptors, self.root / "incomplete"))
                progress = {book["key"]: {**stages.range_identity(book), "ranges": descriptors}}
                resumed = stages.plan_render_ranges(queue, progress)
                remaining = [task for shard in resumed["shards"] for task in shard["records"]]
                self.assertEqual([(t["start"], t["end"]) for t in remaining], [(5, 5)])
            bundle = self.root / "range-last"
            final_range = stages.render_book(remaining[0], source, bundle)
            self.store(bundle)
            descriptors[stages.range_id(5, 5)] = final_range["descriptor"]
            with patch.object(stages, "read_object", side_effect=self.read):
                final_bundle = self.root / "render-final"
                result = stages.assemble_render_book(book, descriptors, final_bundle)
                self.store(final_bundle)
                manifest = json.loads(self.read(result["render_manifest"]))
                stages.validate_render(result, manifest)
                self.assertEqual([p["p"] for p in manifest["pages"]], list(range(1, 6)))
                self.assertIsNone(manifest['page_manifest'])
                self.assertFalse(result['image_rendered'])
                outline.assert_not_called()
                with self.assertRaises(ValueError):
                    stages.validate_range(book, 3, 4, descriptors[stages.range_id(1, 2)])
                self.objects[descriptors[stages.range_id(3, 4)]["path"]] = b"damaged"
                retry = stages.plan_render_ranges(queue, {book["key"]: {
                    **stages.range_identity(book), "ranges": descriptors}})
                missing = [task for shard in retry["shards"] for task in shard["records"]]
                self.assertEqual([(t["start"], t["end"]) for t in missing], [(3, 4)])

    def test_render_plan_balances_very_large_books_by_page_range(self):
        book = {**self.item(), "source_sha256": "a" * 64, "page_count": 2001,
                "profile": pdf_ocr.asset_profile()}
        queue = stages.plan_render_ranges({"shards": [{"records": [book]}]}, {})
        tasks = [t for shard in queue["shards"] for t in shard["records"]]
        self.assertEqual(len(tasks), 9)
        self.assertEqual(sum(t["end"] - t["start"] + 1 for t in tasks), 2001)
        self.assertGreater(queue["shard_count"], 1)
        self.assertLessEqual(max(s["page_count"] for s in queue["shards"]), 500)

    def test_cost_sampling_uses_three_real_render_paths_without_uploading(self):
        item = {**self.item(), "source_sha256": "a" * 64, "page_count": 1000}
        samples = [{"timing": {"page_seconds": value, "setup_seconds": 2}} for value in (1, 20, 3)]
        with patch.object(stages, "render_book", side_effect=samples) as render, \
                patch.object(stages, "upload_objects", side_effect=AssertionError("samples stay local")):
            cost = stages.sample_render_cost(item, self.root / "source.pdf")
        self.assertEqual(cost, {"seconds_per_page": 20, "setup_seconds": 2, "source": "sample", "samples": 3})
        self.assertEqual([call.args[0]["start"] for call in render.call_args_list], [1, 500, 1000])
        self.assertTrue(all(call.args[0]["start"] == call.args[0]["end"] for call in render.call_args_list))
        prior = {**stages.range_identity({**item, "render_profile": stages.render_profile()}),
                 "ranges": {"000001-000250": {}}, "range_timings": {
                     "000001-000250": {"page_count": 250, "page_seconds": 5000, "setup_seconds": 2,
                                           "image_rendered": False}}}
        with patch.object(stages, "render_book", side_effect=AssertionError("history skips samples")):
            self.assertEqual(stages.estimate_render_cost(item, self.root / "missing", {item["key"]: prior})["source"], "history")
        with patch.object(stages, "render_book", side_effect=ValueError("invalid page")):
            fallback = stages.sample_render_cost(item, self.root / "source.pdf")
        self.assertEqual(fallback["source"], "fallback")

    def test_sample_timeout_terminates_owned_process_group_and_keeps_completed_measurements(self):
        item = {**self.item(), "source_sha256": "a" * 64, "page_count": 1000}
        process = Mock(pid=12345)
        def wait(timeout=None):
            if timeout is not None:
                raise stages.subprocess.TimeoutExpired("sample", timeout)
            return 0
        process.wait.side_effect = wait
        def start(command, **kwargs):
            self.assertTrue(kwargs["start_new_session"])
            output = Path(command[command.index("--output") + 1])
            pdf_ocr.write_json(output / "render-cost.json", {
                "seconds_per_page": 25, "setup_seconds": 3, "source": "sample", "samples": 1})
            owner = Mock()
            owner.__enter__ = Mock(return_value=process)
            owner.__exit__ = Mock(return_value=False)
            return owner
        with patch.object(stages.subprocess, "Popen", side_effect=start), patch.object(stages.os, "killpg") as kill:
            cost = stages.estimate_render_cost(item, self.root / "source.pdf", {})
        self.assertEqual(cost["seconds_per_page"], 25)
        kill.assert_called_once_with(12345, stages.signal.SIGKILL)

    def test_time_plan_resumes_old_ranges_and_assembles_mixed_sizes(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"pdf")
        item = {**self.item(), "source_sha256": hashlib.sha256(b"pdf").hexdigest(), "page_count": 5,
                "probe": {"page_count": 5, "page_chars": [0] * 5, "classification": "scan"},
                "_render_cost": {"seconds_per_page": 20, "setup_seconds": 0},
                "render_profile": stages.render_profile(), "profile": pdf_ocr.asset_profile()}
        def render(_source, page, directory, reader_pixels=None, **kwargs):
            path = directory / f"page-{page:06d}.png"
            with Image.new("RGB", (20, 30), "white") as image:
                image.save(path)
                image.save(path.with_suffix(".webp"))
            return path, 20, 30
        with patch.object(pdf_ocr, "render_page", side_effect=render), patch.object(pdf_ocr, "JXL_ENABLED", False):
            item['render_profile'] = stages.render_profile()
            old = stages.render_book({**item, "start": 1, "end": 2}, source, self.root / "old")
            self.store(self.root / "old")
            progress = {item["key"]: {**stages.range_identity(item), "ranges": {"000001-000002": old["descriptor"]}}}
            with patch.object(stages, "read_object", side_effect=self.read):
                planned = stages.plan_render_ranges({"shards": [{"records": [item]}]}, progress, 40)
            tasks = stages.expand_render_tasks(planned, [t for shard in planned["shards"] for t in shard["records"]])
            self.assertEqual(sorted((t["start"], t["end"]) for t in tasks), [(3, 3), (4, 4), (5, 5)])
            self.assertTrue(all("probe" not in compact for shard in planned["shards"]
                                for compact in shard["records"]))
            tampered = {**planned["shards"][0]["records"][0], "source_sha256": "f" * 64}
            with self.assertRaisesRegex(ValueError, "identity differs"):
                stages.expand_render_tasks(planned, [tampered])
            descriptors = dict(progress[item["key"]]["ranges"])
            for task in tasks:
                bundle = self.root / str(task["start"])
                result = stages.render_book(task, source, bundle)
                self.store(bundle)
                descriptors[stages.range_id(task["start"], task["end"])] = result["descriptor"]
                self.assertGreater(result["timing"]["page_seconds"], 0)
                self.assertNotIn("timing", json.loads(self.read(result["descriptor"])))
            with patch.object(stages, "read_object", side_effect=self.read):
                complete = stages.assemble_render_book(item, descriptors, self.root / "complete")
            self.store(self.root / "complete")
            self.assertEqual([p["p"] for p in json.loads(self.read(complete["render_manifest"]))["pages"]], list(range(1, 6)))
            overlap = stages.render_book({**item, "start": 2, "end": 3}, source, self.root / "overlap")
            self.store(self.root / "overlap")
            descriptors["000002-000003"] = overlap["descriptor"]
            with patch.object(stages, "read_object", side_effect=self.read):
                self.assertIsNotNone(stages.assemble_render_book(item, descriptors, self.root / "consistent-overlap"))
            body = json.loads(self.read(overlap["descriptor"]))
            body["pages"][0]["text"] = "conflicting page metadata"
            raw = json.dumps(body).encode()
            self.objects[overlap["descriptor"]["path"]] = raw
            descriptors["000002-000003"] = {**overlap["descriptor"], "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            with patch.object(stages, "read_object", side_effect=self.read):
                with self.assertRaisesRegex(ValueError, "conflicting overlapping"):
                    stages.assemble_render_book(item, descriptors, self.root / "conflict")

    def test_timing_merge_keeps_old_ranges_and_invalidates_changed_sources(self):
        book = {**self.item(), "source_sha256": "a" * 64, "page_count": 500,
                "render_profile": stages.render_profile()}
        identity = stages.range_identity(book)
        first = {**identity, "ranges": {"000001-000250": {}}, "range_timings": {
            "000001-000250": {"page_count": 250, "page_seconds": 500, "setup_seconds": 10}}}
        second = {**identity, "ranges": {"000251-000500": {}}, "range_timings": {
            "000251-000500": {"page_count": 250, "page_seconds": 5000, "setup_seconds": 10}}}
        merged = stages.merge_render_ranges(first, second)
        self.assertEqual(len(merged["ranges"]), 2)
        self.assertEqual(len(merged["range_timings"]), 2)
        changed = stages.merge_render_ranges(first, {**second, "source_sha256": "b" * 64})
        self.assertEqual(set(changed["ranges"]), {"000251-000500"})
        self.assertEqual(set(changed["range_timings"]), {"000251-000500"})

    def test_render_publication_saves_partial_progress_then_only_complete_book(self):
        previous = self.render_fixture(djvu=False)
        manifest = json.loads(self.read(previous["render_manifest"]))
        book = {**self.item(), "source_sha256": previous["source_sha256"],
                "source_bytes": previous["source_bytes"], "page_count": 2,
                "profile": previous["profile"], "render_profile": previous["render_profile"],
                "probe": {"classification": "scan", "page_count": 2, "page_chars": [0, 0]}}
        descriptors = {}
        range_bundle = self.root / "range-descriptors"
        root = stages.root_for(book["source_sha256"], book["key"], book["render_profile"])
        for page in manifest["pages"]:
            number = page["p"]
            destination = range_bundle / root / f"render-range-{stages.range_id(number, number)}.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            pdf_ocr.write_json(destination, {**stages.range_identity(book), "version": 1,
                                             "kind": "pdf-render-range", "start": number, "end": number,
                                             "pages": [page]})
            descriptors[number] = stages.metadata(destination, range_bundle)
        self.store(range_bundle)
        updates = []
        def registry_state(_api, _repo, name, **_kwargs):
            return {"version": 1, "files": next(
                (data for saved_name, data in reversed(updates) if saved_name == name), {})}
        with patch.object(stages, "RENDER_RANGE_THRESHOLD", 1), \
                patch.object(stages, "RENDER_RANGE_PAGES", 1), \
                patch.object(stages, "load_registry", side_effect=registry_state), \
                patch.object(stages, "read_object", side_effect=self.read), \
                patch.object(stages, "upload_objects", side_effect=self.store), \
                patch.object(stages, "save_registry", side_effect=lambda api, repo, name, data, **kw:
                             updates.append((name, data))):
            for selected, saved in ((1, {}), (2, {stages.range_id(1, 1): descriptors[1]})):
                task = {**book, "start": selected, "end": selected}
                queue = {"version": 1, "kind": "pdf-render-queue", "books": [book],
                         "shards": [{"records": [task]}], "saved_ranges": {book["key"]: saved},
                         "failed": []}
                location = self.root / f"queue-{selected}.json"
                pdf_ocr.write_json(location, queue)
                results_dir = self.root / f"results-{selected}"
                results_dir.mkdir()
                pdf_ocr.write_json(results_dir / f"results-{selected}.json", {"version": 1, "results": [{
                    **stages.range_identity(book), "start": selected, "end": selected,
                    "status": "range", "descriptor": descriptors[selected], "timing": {
                        "page_count": 1, "image_rendered": True,
                        "page_seconds": 4.0, "setup_seconds": 1.0}}]})
                with patch.object(sys, "argv", ["pdf_ocr_stages.py", "publish-render", "--queue", str(location),
                                                "--results-dir", str(results_dir), "--output", str(self.root)]):
                    stages.main()
                state = updates[-1][1][book["key"]]
                progress_update = updates[-2][1][book["key"]]
                self.assertEqual(progress_update["range_timings"][stages.range_id(selected, selected)]["page_seconds"], 4.0)
                if selected == 1:
                    self.assertEqual(state["status"], "failed")
                    self.assertNotIn("page_manifest", state)
                else:
                    self.assertEqual(state["status"], "ready")
                    finished = json.loads(self.read(state["render_manifest"]))
                    self.assertEqual([page["p"] for page in finished["pages"]], [1, 2])
        self.assertEqual([name for name, _ in updates], [stages.RENDER_PROGRESS_REGISTRY,
                          stages.RENDER_REGISTRY, stages.RENDER_PROGRESS_REGISTRY, stages.RENDER_REGISTRY])

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
        manifest = json.loads(self.read(result["render_manifest"]))
        manifest["pages"][1]["i"] = manifest["pages"][1]["i"].replace(
            result["source_sha256"], "b" * 64)
        with self.assertRaisesRegex(ValueError, "outside book profile"):
            stages.validate_render(result, manifest)
        jxl = self.render_fixture(jxl=True)
        manifest = json.loads(self.read(jxl["render_manifest"]))
        del manifest["pages"][0]["j"]
        with self.assertRaisesRegex(ValueError, 'incomplete reader image stream'):
            stages.validate_render(jxl, manifest)

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
        uploads = []
        with patch.object(stages.shared, "batch_bucket_files_with_retry",
                          side_effect=lambda bucket, additions, token: uploads.append((bucket, additions))), \
                patch.object(stages, "remote_sizes", return_value={}), \
                patch.object(stages, "publish_json_batch"):
            stages.upload_objects(self.root / "render")
        self.assertEqual({bucket for bucket, _additions in uploads},
                         {stages.shared.PDF_PAGES_BUCKET, stages.shared.PDF_OCR_INPUT_BUCKET})
        pdf_upload = next(additions for bucket, additions in uploads if bucket == stages.shared.PDF_PAGES_BUCKET)
        remote_paths = [remote for _local, remote in pdf_upload]
        self.assertIn(result["render_manifest"]["path"], remote_paths)
        self.assertNotEqual({remote.split("/", 1)[0] for remote in remote_paths}, {stages.BUCKET})

    def test_upload_never_overwrites_existing_immutable_objects(self):
        result = self.render_fixture()
        manifest = result["render_manifest"]["path"]
        local = self.root / "render" / manifest
        uploads = []
        def put(bucket, additions, token):
            uploads.extend(remote for _local, remote in additions)
        with patch.object(stages.shared, "batch_bucket_files_with_retry", side_effect=put), \
                patch.object(stages, "publish_json_batch"), \
                patch.object(stages, "remote_sha256", return_value=hashlib.sha256(local.read_bytes()).hexdigest()), \
                patch.object(stages, "remote_sizes", side_effect=lambda bucket, prefix, token:
                             {manifest: local.stat().st_size} if bucket == stages.shared.PDF_PAGES_BUCKET else {}):
            stages.upload_objects(self.root / "render")
        self.assertNotIn(manifest, uploads)
        self.assertTrue(uploads)
        with patch.object(stages.shared, "batch_bucket_files_with_retry", side_effect=put), \
                patch.object(stages, "publish_json_batch"), \
                patch.object(stages, "remote_sizes", side_effect=lambda bucket, prefix, token:
                             {manifest: local.stat().st_size + 3} if bucket == stages.shared.PDF_PAGES_BUCKET else {}):
            with self.assertRaisesRegex(ValueError, "immutable object conflict"):
                stages.upload_objects(self.root / "render")

    def test_planner_includes_small_pdf_and_rebuilds_old_ocr_for_v2_index(self):
        item = {**self.item(), "source_bytes": 1024}
        self.assertEqual(stages.pending_render([item], {}, {}), [item])
        old = {**item, "status": "ready", "profile": pdf_ocr.asset_profile(),
               "ocr_manifest": "objects/aa/" + "a" * 64 + "/" + "b" * 16 + "/ocr-manifest.json"}
        self.assertEqual(stages.pending_render([item], {}, {item["key"]: old}), [item])
        self.assertEqual(stages.pending_render([item], {}, {item["key"]: {**old, "ocr_manifest": ""}}), [item])

    def test_render_partitions_keep_small_and_large_files_independent(self):
        small = {**self.item(), "source_bytes": stages.SMALL_RENDER_MAX_SOURCE_BYTES - 1}
        large = {**self.item(), "key": "repo\0large.pdf", "path": "large.pdf",
                 "source_bytes": stages.SMALL_RENDER_MAX_SOURCE_BYTES}
        self.assertTrue(stages.render_partition_matches(small, "small"))
        self.assertFalse(stages.render_partition_matches(small, "large"))
        self.assertFalse(stages.render_partition_matches(large, "small"))
        self.assertTrue(stages.render_partition_matches(large, "large"))
        self.assertTrue(stages.render_partition_matches(small, "all"))

    def test_obsolete_native_text_stream_option_cannot_enable_pdf_images(self):
        item = {**self.item(), "source_bytes": 1024}
        probe = {"page_count": 2, "page_chars": [80, 80], "classification": "native-text"}
        with patch.object(plan_pdf_ocr, "download_source", return_value=self.root / "source.pdf"), \
                 patch.object(plan_pdf_ocr.shared, "hash_file", return_value=("a" * 64, 1024)), \
                 patch.object(pdf_ocr, "reader_presentation", return_value={"strategy": "page-stream"}), \
                 patch.object(pdf_ocr, "probe_pdf", return_value=probe):
            (self.root / "source.pdf").write_bytes(b"pdf")
            queue = plan_pdf_ocr.plan([item], workers=1, native_text_stream=True)
        planned = queue['shards'][0]['records'][0]
        self.assertFalse(planned.get('force_image_render', False))
        self.assertEqual(planned['reader_presentation']['strategy'], 'preserve-pdf')

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
            recovery = stages.plan_images(entries, {failed["key"]: {"status": "failed"}}, {},
                                          limit=1, retry_failed_only=True)
        self.assertEqual(queue["books"][0]["key"], untouched["key"])
        self.assertEqual([book["key"] for book in recovery["books"]], [failed["key"]])

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
                patch.object(pdf_ocr, "native_pages", side_effect=lambda _source, pages: {
                    page: native(_source, page) for page in pages}):
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
            self.assertEqual(compact["b"], "vomebook/pdf-pages-v2")
            self.assertNotIn("o", compact)

    def test_completed_ocr_page_stream_replaces_existing_pdf_route(self):
        result = self.render_fixture()
        existing = {"s": 2, "m": "p", "p": "objects/old/document.pdf"}
        base = {"files": {result["key"]: {"status": "ready", "reader_mode": "pdf",
                                           "path": "ordinary.pdf"}}}
        ocr = {"files": {result["key"]: {**result, "status": "ready",
                                           "ocr_manifest": "objects/old/ocr-manifest.json"}}}
        merged = build_index(base, ocr_manifest=ocr)["f"][result["key"]]
        self.assertEqual(merged["p"], result["page_manifest"]["path"])
        self.assertEqual(merged["b"], "vomebook/pdf-pages-v2")
        self.assertEqual(merged["o"], "objects/old/ocr-manifest.json")

    def test_failed_native_optimization_replaces_pdf_route_and_keeps_text(self):
        result = {**self.render_fixture(native_only=True, force_image=True),
                  "range_status": "failed", "classification": "native-text"}
        existing = {**result, "status": "ready", "ocr_manifest": "objects/old/ocr-manifest.json",
                    "page_manifest": None}
        base = {"files": {result["key"]: {"status": "ready", "reader_mode": "pdf",
                                     "path": "ordinary.pdf"}}}
        merged = build_index(base, ocr_manifest={"files": {result["key"]: {**existing,
                         "page_manifest": result["page_manifest"]}}})["f"][result["key"]]
        self.assertEqual(merged["p"], result["page_manifest"]["path"])
        self.assertEqual(merged["o"], existing["ocr_manifest"])
        sidecar_path = self.root / "existing-reader.json.gz"
        sidecar_path.write_bytes(gzip.compress(json.dumps({"v": 1, "f": {
            result["key"]: {"s": 2, "m": "p", "p": "ordinary.pdf", "o": existing["ocr_manifest"]}}}).encode()))
        api = Mock()
        api.repo_info.return_value.sha = "pinned-revision"
        api.hf_hub_download.return_value = str(sidecar_path)
        def state(_api, _repo, name, _revision):
            return {"version": 1, "files": {result["key"]: existing} if name == "pdf_ocr_manifest.json" else {}}
        with patch.object(stages, "load_registry", side_effect=state), \
                patch.object(stages, "publish_catalog", return_value="gen"):
            stages.save_registry(api, "test/repo", stages.RENDER_REGISTRY,
                                 {result["key"]: result}, publish_streams=True)
        operations = {op.path_in_repo: op.path_or_fileobj for op in api.create_commit.call_args.kwargs["operations"]}
        stored = json.loads(operations["pdf_ocr_manifest.json"])["files"][result["key"]]
        self.assertEqual(stored["status"], "ready")
        self.assertEqual(stored["ocr_manifest"], existing["ocr_manifest"])
        self.assertEqual(stored["page_manifest"], result["page_manifest"])
        route = json.loads(gzip.decompress(operations["reader_assets.json.gz"]))["f"][result["key"]]
        self.assertEqual(route["p"], result["page_manifest"]["path"])
        self.assertEqual(route["o"], existing["ocr_manifest"])

    def test_render_registry_stream_and_pending_ocr_are_committed_together(self):
        result = self.render_fixture()
        sidecar_path = self.root / "reader.json.gz"
        sidecar_path.write_bytes(gzip.compress(json.dumps({"v": 1, "f": {}}).encode()))
        api = Mock()
        api.repo_info.return_value.sha = "pinned-revision"
        api.hf_hub_download.return_value = str(sidecar_path)
        with patch.object(stages, "load_registry", side_effect=lambda *args: {"version": 1, "files": {}}), \
                patch.object(stages, "publish_catalog", return_value="gen"):
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

    def test_reader_image_refresh_reuses_matching_ocr_png_and_rebuilds_text_index(self):
        result = self.render_fixture(native=True)
        with patch.object(stages, "read_object", side_effect=self.read):
            first = stages.plan_images({result["key"]: result}, {}, {})
            with patch.object(pdf_ocr, "ocr_page", return_value=[
                    {"t": "recognized", "b": [0, 0, 1, 1], "c": 1, "s": "ocr"}]):
                recognized = stages.recognize_task(first["shards"][0][0], self.root / "old-ocr")
            self.store(self.root / "old-ocr")
            progress = stages.collect_progress(first, [recognized])
            old = stages.assemble_book(first["books"][0], progress[result["key"]]["pages"], self.root / "old-text")
            self.store(self.root / "old-text")

            manifest = json.loads(self.read(result["render_manifest"]))
            new_manifest = {**result["page_manifest"], "sha256": "f" * 64}
            manifest["page_manifest"] = new_manifest
            raw = json.dumps(manifest).encode()
            new_result = {**result, "page_manifest": new_manifest,
                          "render_manifest": {**result["render_manifest"],
                                              "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}
            self.objects[result["render_manifest"]["path"]] = raw
            jxl_only = {**new_result, "profile": old["profile"].replace("-jxl-0-", "-jxl-1-")}
            self.assertEqual(set(stages.reuse_recognized_pages(old, jxl_only, manifest["pages"])), {"2"})
            changed_layout = {**jxl_only, "profile": jxl_only["profile"].rsplit("-layout-", 1)[0]
                              + "-layout-v1-" + "e" * 16}
            self.assertEqual(stages.reuse_recognized_pages(old, changed_layout, manifest["pages"]), {})
            queue = stages.plan_images({result["key"]: new_result}, {result["key"]: old}, {})
            self.assertEqual(queue["total_ocr_pages"], 0)
            self.assertEqual(queue["shard_count"], 0)
            self.assertEqual(queue["books"][0]["saved"]["2"]["i"], manifest["pages"][1]["i"])
            complete = stages.assemble_book(queue["books"][0], queue["books"][0]["saved"], self.root / "new-text")
            self.store(self.root / "new-text")
            index = json.loads(gzip.decompress(self.read(json.loads(self.objects[complete["ocr_manifest"]])["book_text"])))
            self.assertEqual([page["text"] for page in index["pages"]], ["原生文字", "recognized"])
            self.assertEqual(json.loads(self.objects[complete["ocr_manifest"]])["page_manifest"], new_manifest)

            changed = copy.deepcopy(manifest)
            changed["pages"][1]["is"] = "e" * 64
            raw = json.dumps(changed).encode()
            self.objects[result["render_manifest"]["path"]] = raw
            altered = {**new_result, "render_manifest": {**new_result["render_manifest"],
                       "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}
            pending = stages.plan_images({result["key"]: altered}, {result["key"]: old}, {})
            self.assertEqual(pending["total_ocr_pages"], 1)

    def test_lossless_reading_png_refresh_preserves_failed_ocr_checkpoint_without_rerun_or_relabel(self):
        source = 'a' * 64
        root = 'objects/aa/' + source + '/' + 'b' * 16
        image = {'p': 1, 'source': 'ocr', 'i': root + '/ocr-input/page-000001.png',
                 'is': 'c' * 64, 'ib': 30, 'width': 400, 'height': 600,
                 'r': root + '/pages/page-000001.png', 'rs': 'd' * 64, 'rb': 20,
                 'rbucket': stages.shared.PDF_PAGES_BUCKET}
        profile = 'original-model-quality-v1-layout-v2-matching'
        prior = {key: value for key, value in image.items() if key not in {'r', 'rs', 'rb', 'rbucket'}}
        prior.update(j=root + '/pages/page-000001.jxl', js='e' * 64, jb=50,
                     o=root + '/ocr/page-000001.json.gz', os='f' * 64, ob=100,
                     text='Literal failed recognition', text_spans=[], layout={}, chars=26,
                     recognition={'backend': 'original-model'}, quality_check={'status': 'failed', 'issues': ['unresolved']})
        previous = {'source_sha256': source, 'profile': profile, 'pages': {'1': prior}}
        with patch.object(pdf_ocr, 'ocr_page', side_effect=AssertionError('page image refresh must not rerun OCR')):
            saved = stages.reuse_progress_pages(previous, {'source_sha256': source, 'profile': profile}, [image])
        self.assertEqual(saved['1']['r'], image['r'])
        self.assertNotIn('j', saved['1'])
        self.assertEqual(saved['1']['o'], prior['o'])
        self.assertEqual(saved['1']['text'], prior['text'])
        self.assertEqual(saved['1']['recognition'], prior['recognition'])
        self.assertEqual(saved['1']['quality_check'], prior['quality_check'])

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

    def test_identical_ready_ocr_publication_is_noop(self):
        book = {"source_sha256": "a" * 64, "source_revision": "b" * 40,
                "source_kind": "upstream", "profile": "ocr-profile", "page_count": 12,
                "page_manifest": {"path": "pages/manifest.json", "sha256": "c" * 64, "bytes": 12}}
        previous = {**book, "status": "ready"}
        self.assertTrue(stages.ocr_publication_is_current(previous, book))
        self.assertFalse(stages.ocr_publication_is_current(previous, {**book, "profile": "changed"}))


    def test_same_source_ignores_revision_drift_for_identical_bytes(self):
        base = {"source_sha256": "a" * 64, "source_kind": "upstream", "reader_assets_path": None}
        previous = {**base, "source_revision": "old"}
        current = {**base, "source_revision": "new"}
        self.assertTrue(stages.same_source(previous, current))
        self.assertFalse(stages.same_source({**previous, "source_sha256": "b" * 64}, current))
        legacy_previous = {"source_kind": "upstream", "reader_assets_path": None,
                           "source_revision": "old"}
        self.assertTrue(stages.same_source(legacy_previous, {**legacy_previous, "source_revision": "old"}))
        self.assertFalse(stages.same_source(legacy_previous, {**legacy_previous, "source_revision": "new"}))

    def test_unprobed_render_sources_reuse_only_pinned_matching_lfs_sha_before_limit(self):
        from types import SimpleNamespace
        def source(name):
            return {"key": "repo\0" + name, "repo": "repo", "path": name, "source_kind": "upstream",
                    "source_format": "djvu", "source_revision": "b" * 40}
        records = [source(name) for name in ("same.djvu", "changed.djvu", "non-lfs.djvu", "missing.djvu")]
        rendered = {item["key"]: {**item, "source_revision": "a" * 40, "source_sha256": "c" * 64,
                                 "status": "ready", "image_rendered": True,
                                 "render_profile": stages.render_profile_for(item)} for item in records}
        api = Mock()
        api.get_paths_info.return_value = [SimpleNamespace(path="same.djvu", lfs={"sha256": "c" * 64}),
            SimpleNamespace(path="changed.djvu", lfs={"sha256": "d" * 64}),
            SimpleNamespace(path="non-lfs.djvu", lfs=None)]
        pinned = stages.pin_unchanged_render_sources(api, records, rendered)
        self.assertEqual(pinned[0]["source_sha256"], "c" * 64)
        self.assertTrue(all("source_sha256" not in item for item in records))
        self.assertTrue(all("source_sha256" not in item for item in pinned[1:]))
        self.assertEqual({item["key"] for item in stages.pending_render(pinned, rendered, {})},
                         {item["key"] for item in records[1:]})
        api.get_paths_info.assert_called_once_with("repo", [item["path"] for item in records],
                                                   repo_type="dataset", revision="b" * 40)

    def test_render_sha_pinning_keeps_profile_failure_and_metadata_access_guards(self):
        from types import SimpleNamespace
        item = {"key": "repo\0book.djvu", "repo": "repo", "path": "book.djvu", "source_kind": "upstream",
                "source_format": "djvu", "source_revision": "b" * 40}
        previous = {**item, "source_revision": "a" * 40, "source_sha256": "c" * 64, "status": "failed",
                    "render_profile": stages.render_profile_for(item)}
        api = Mock()
        api.get_paths_info.return_value = [SimpleNamespace(path=item["path"], lfs=SimpleNamespace(sha256="c" * 64))]
        pinned = stages.pin_unchanged_render_sources(api, [item], {item["key"]: previous})
        self.assertEqual(stages.pending_render(pinned, {item["key"]: previous}, {}), [])
        self.assertEqual(stages.pending_render(pinned, {item["key"]: previous}, {}, retry_failed=True), pinned)
        for changed in ({**previous, "render_profile": "old-profile"}, {**previous, "source_kind": "generated"},
                        {**previous, "reader_assets_path": "different"}, {**previous, "source_sha256": None}):
            api.get_paths_info.reset_mock()
            self.assertEqual(stages.pin_unchanged_render_sources(api, [item], {item["key"]: changed}), [item])
            api.get_paths_info.assert_not_called()
        api.get_paths_info.side_effect = PermissionError("metadata denied")
        with patch.object(stages, "retry", side_effect=lambda operation: operation()), \
                self.assertRaisesRegex(PermissionError, "metadata denied"):
            stages.pin_unchanged_render_sources(api, [item], {item["key"]: previous})

    def test_real_render_planner_pins_unprobed_sha_before_consuming_batch_limit(self):
        from types import SimpleNamespace
        item = {"key": "repo\0a.djvu", "repo": "repo", "path": "a.djvu", "source_kind": "upstream",
                "source_format": "djvu", "source_revision": "b" * 40}
        fresh = {**item, "key": "repo\0z.djvu", "path": "z.djvu"}
        previous = {**item, "source_revision": "a" * 40, "source_sha256": "c" * 64,
                    "status": "ready", "image_rendered": True, "render_profile": stages.render_profile_for(item)}
        api = Mock()
        api.get_paths_info.return_value = [SimpleNamespace(path="a.djvu", lfs={"sha256": "c" * 64})]
        def registry(_api, _repo, name, *args):
            return {"files": {item["key"]: previous} if name == stages.RENDER_REGISTRY else {}}
        empty = {"shards": [], "books": [], "shard_count": 0}
        with patch("sys.argv", ["stages", "plan-render", "--limit", "1", "--queue", str(self.root / "queue.json"),
                                "--output", str(self.root / "plan")]), \
                patch.object(stages, "HfApi", return_value=api), \
                patch.object(stages, "load_registry", side_effect=registry), \
                patch.object(stages, "load_pdf_sources", return_value={}), \
                patch.object(stages.pdf_ocr, "source_records", return_value=[item, fresh]), \
                patch.object(stages.plan_pdf_ocr, "plan", return_value=empty) as plan, \
                patch.object(stages, "plan_render_ranges", return_value=empty):
            self.assertEqual(stages.main(), 0)
        self.assertEqual(plan.call_args.args[0], [fresh])

    def test_same_size_json_descriptor_is_checked_by_sha(self):
        descriptor = self.root / "render-range-000001-000010.json"
        descriptor.write_bytes(b"{}")
        with patch.object(stages, "remote_sizes", return_value={descriptor.name: 2}), \
                patch.object(stages, "remote_sha256", return_value="0" * 64):
            with self.assertRaisesRegex(ValueError, "immutable object conflict"):
                stages.fresh_additions("bucket", "objects/x", [(str(descriptor), descriptor.name)], "token")
        with patch.object(stages, "remote_sizes", return_value={descriptor.name: 2}), \
                patch.object(stages, "remote_sha256", return_value=hashlib.sha256(b"{}").hexdigest()):
            self.assertEqual(stages.fresh_additions("bucket", "objects/x",
                                                    [(str(descriptor), descriptor.name)], "token"), [])

    def test_ready_render_supersedes_revision_only_replay(self):
        book = {"key": "repo\0book.djvu", "source_sha256": "a" * 64,
                "source_revision": "new", "source_kind": "upstream",
                "reader_assets_path": None, "render_profile": "profile", "page_count": 10}
        ready = {**book, "status": "ready", "source_revision": "old"}
        self.assertTrue(stages.render_result_is_current(ready, book))
        self.assertFalse(stages.render_result_is_current({**ready, "page_count": 11}, book))
        self.assertFalse(stages.render_result_is_current({**ready, "render_profile": "other"}, book))
        self.assertFalse(stages.render_result_is_current({**ready, "status": "failed"}, book))

    def test_recorded_unreadable_source_keeps_its_derived_stream(self):
        exception = {"key": "repo\0book.djvu", "missing_source_pages": [330],
                     "source_page_count": 731, "derived_page_count": 730}
        native = {"key": "repo\0book.djvu", "repo": "repo", "path": "book.djvu",
                  "source_format": "djvu", "source_kind": "upstream"}
        with patch.object(stages.djvu_pages, "native_exceptions", return_value={native["key"]: exception}):
            self.assertIs(stages.djvu_pages.native_exception(native), exception)
            # The recorded exception never becomes a native render task.
            self.assertEqual(stages.pending_render([native], {}, {}), [])

    def test_native_text_only_book_keeps_an_assembly_only_queue(self):
        result = self.render_fixture(native_only=True)
        with patch.object(stages, "read_object", side_effect=self.read):
            queue = stages.plan_images({result["key"]: result}, {}, {})
        self.assertEqual(queue["shard_count"], 0)
        self.assertEqual(len(queue["books"]), 1)
        self.assertEqual(queue["books"][0]["key"], result["key"])
        self.assertEqual(sum(len(task["pages"]) for task in queue["shards"]), 0)

    def test_worker_and_publisher_admit_zero_shard_native_text_assembly(self):
        root = Path(__file__).resolve().parents[1] / ".github/workflows"
        worker = yaml.safe_load((root / "pdf-account-worker.yml").read_text())
        self.assertIn("book_count", worker["jobs"]["plan"]["outputs"])
        plan_commands = "\n".join(step.get("run", "") for step in worker["jobs"]["plan"]["steps"])
        self.assertIn("book_count=$(jq -r '(.books // []) | length'", plan_commands)
        self.assertIn('"$STAGE" == auto', plan_commands)
        self.assertIn('output/pdf-ocr/queue.json)" == 0', plan_commands)
        self.assertIn("(.books // []) | length", plan_commands)
        notify = worker["jobs"]["notify"]["if"]
        self.assertIn("needs.plan.outputs.shard_count != '0'", notify)
        self.assertIn("needs.plan.outputs.book_count != '0'", notify)
        if not (root / 'pdf-account-publish.yml').exists():
            # Account repositories intentionally carry only the worker. Their
            # notify step delegates assembly to the central publisher.
            self.assertIn('pdf-account-publish.yml', worker['jobs']['notify']['steps'][0]['run'])
            return
        publisher = yaml.safe_load((root / "pdf-account-publish.yml").read_text())
        conditions = "\n".join(step.get("if", "") for step in publisher["jobs"]["publish"]["steps"])
        self.assertIn("inputs.stage == 'render' || inputs.stage == 'ocr'", conditions)

    def test_native_djvu_renders_before_other_converted_formats(self):
        native = {"key": "repo\0z.djvu", "repo": "repo", "path": "z.djvu", "source_format": "djvu",
                  "source_kind": "upstream"}
        other = {"key": "repo\0a.caj", "repo": "repo", "path": "a.caj", "source_kind": "generated"}
        self.assertEqual([x["key"] for x in stages.pending_render([other, native], {}, {})],
                         ["repo\0z.djvu", "repo\0a.caj"])

    def test_pinned_native_keys_restrict_plan_and_reject_fallbacks(self):
        native = {"key": "repo\0b.djvu", "repo": "repo", "path": "b.djvu",
                  "source_format": "djvu", "source_bytes": 10}
        other = {**native, "key": "repo\0a.djvu", "path": "a.djvu"}
        with patch.object(stages.djvu_pages, "native_enabled", return_value=True):
            self.assertEqual([x["key"] for x in stages.pin_render_records(
                [other, native], ["repo/b.djvu"])], ["repo\0b.djvu"])
            with self.assertRaisesRegex(ValueError, "not in this lane"):
                stages.pin_render_records([native], ["repo/missing.djvu"])
            with self.assertRaisesRegex(ValueError, "not an admitted native DjVu"):
                stages.pin_render_records([{**native, "source_format": "pdf"}], ["repo/b.djvu"])
        self.assertEqual(stages.pinned_keys("repo/a.djvu|repo/b.djvu\nrepo/c.djvu"),
                         ["repo/a.djvu", "repo/b.djvu", "repo/c.djvu"])


if __name__ == "__main__":
    unittest.main()
