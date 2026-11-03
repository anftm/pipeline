import copy
import gzip
import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import pymupdf
from PIL import Image

from scripts import pdf_reading_v3 as v3, pdf_text_layer as text, shared
from tests.test_pdf_text_layer import raw_page


class ReadingV3Tests(unittest.TestCase):
    def test_printed_errata_survive_complete_packaging_without_overwriting_text_or_search(self):
        from tests.test_pdf_text_layer import TextLayerTests
        original,relations=TextLayerTests().errata_fixture()
        proposal=TextLayerTests().visual_proposal(original);proposal['errata_relations']=relations
        accepted=text.accept_proposal(original,proposal,actor='user-source-errata-review')
        layers,pages=self.layers(1);layers[0]=accepted
        ref=v3.build_text_bundle(layers,pages,Path('objects/aa/'+'a'*64+'/'+'b'*16),self.bundle)
        self.generated();verified=[]
        manifest=v3.verify_text_bundle(ref,self.read,'a'*64,1,verified_layers=verified)
        self.assertEqual(verified[0]['errata_relations'],relations)
        self.assertEqual(verified[0]['regions'],original['regions'])
        self.assertEqual(verified[0]['text'],original['text'])
        search=v3.decode(self.read(manifest['search_partitions'][0]['resource']))['pages'][0]['text']
        self.assertEqual(search,original['text'])
        for literal in ['辻の妹恒','野枝の従妹坂口キミ','辻の母美津','野枝の叔母渡辺マツ','↓']:
            self.assertIn(literal,search)

    def test_pdf_shared_render_is_not_adopted_or_read_as_a_new_page_stream(self):
        import imagecodecs
        import numpy as np
        spec = self.spec()
        spec.pop("previews")
        source = spec["source_sha256"]
        root = f"objects/{source[:2]}/{source}/" + "f" * 16
        mapping = v3.primary_page_map(self.read(spec["primary"]), source, "pdf")
        pages = []
        for geometry in mapping["pages"]:
            raw = imagecodecs.jpegxl_encode(np.full((int(geometry["height"]), int(geometry["width"]), 3), 255, dtype=np.uint8))
            media = self.add(root + f"/pages/page-{geometry['page']:06d}.jxl", raw)
            pages.append({"p": geometry["page"], "j": media["path"], "js": media["sha256"], "jb": media["bytes"]})
        descriptor = {"kind": "pdf-render", "complete": True, "source_sha256": source,
                      "key": spec["source_key"], "page_count": 2, "render_profile": "shared", "pages": pages}
        spec["render_manifest"] = self.add(root + "/render-manifest.json", json.dumps(descriptor).encode(), role="provenance")
        with patch.object(pymupdf.Page, "get_pixmap", side_effect=AssertionError("must reuse rendered pixels")), \
                patch.object(v3.reader_page_codec, "encode_image", side_effect=AssertionError("must reuse JXL")):
            generated = v3.generate_previews(spec, self.read, self.bundle)
        self.assertEqual(generated['previews'], [])
        self.assertEqual(list(self.bundle.rglob("*.jxl")), [])
        key = (shared.PDF_PAGES_BUCKET, pages[0]["j"])
        self.objects[key] = b"changed"
        self.assertEqual(v3.generate_previews(spec, self.read, self.bundle)['previews'], [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        self.objects = {}

    def add(self, path, raw, bucket=shared.PDF_PAGES_BUCKET, role="runtime"):
        self.objects[(bucket, path)] = raw
        return {"bucket": bucket, "path": path, "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw), "role": role}

    def read(self, ref):
        return self.objects[(ref["bucket"], ref["path"])]

    def generated(self):
        for path in self.bundle.rglob("*"):
            if path.is_file():
                self.objects[(shared.PDF_PAGES_BUCKET, path.relative_to(self.bundle).as_posix())] = path.read_bytes()

    def layers(self, count):
        layers, pages = [], []
        for number in range(1, count + 1):
            payload = {**raw_page("Text"), "page": number}
            layers.append(text.from_page(payload, "a" * 64))
            pages.append({"p": number})
        return layers, pages

    def test_text_partitions_deep_page_and_full_verification(self):
        layers, pages = self.layers(260)
        ref = v3.build_text_bundle(layers, pages, Path("objects/aa/" + "a" * 64 + "/" + "b" * 16), self.bundle)
        self.generated()
        manifest = v3.verify_text_bundle(ref, self.read, "a" * 64, 260)
        target = v3.partition_for(manifest, 259)
        self.assertEqual((target["start"], target["end"]), (257, 260))
        self.assertEqual(len(manifest["partitions"]), 3)
        with self.assertRaises(ValueError):
            v3.partition_for(manifest, 261)

    def test_corrupt_page_and_wrong_source_block_verification(self):
        layers, pages = self.layers(1)
        ref = v3.build_text_bundle(layers, pages, Path("objects/aa/" + "a" * 64 + "/" + "b" * 16), self.bundle)
        self.generated()
        with self.assertRaises(ValueError):
            v3.verify_text_bundle(ref, self.read, "b" * 64, 1)
        path = next(key for key in self.objects if key[1].endswith("text/page-000001.json.gz"))
        self.objects[path] = b"corrupted"
        with self.assertRaises(ValueError):
            v3.verify_text_bundle(ref, self.read, "a" * 64, 1)

    def test_search_partitions_are_text_only_complete_and_checked_against_pages(self):
        layers, pages = self.layers(70)
        ref = v3.build_text_bundle(layers, pages, Path("objects/aa/" + "a" * 64 + "/" + "b" * 16), self.bundle)
        self.generated()
        manifest = v3.verify_text_bundle(ref, self.read, "a" * 64, 70)
        self.assertEqual([(p["start"], p["end"]) for p in manifest["search_partitions"]],
                         [(1, 32), (33, 64), (65, 70)])
        part = manifest["search_partitions"][-1]
        data = v3.decode(self.read(part["resource"]))
        self.assertEqual(set(data["pages"][0]), {"page", "text", "text_generation"})
        data["pages"][-1]["text"] = "wrong effective text"
        bad = self.add(part["resource"]["path"], gzip.compress(json.dumps(data).encode()))
        manifest["search_partitions"][-1]["resource"] = bad
        bad_index = self.add(ref["path"], json.dumps(manifest).encode())
        with self.assertRaisesRegex(ValueError, "search partition differs"):
            v3.verify_text_bundle(bad_index, self.read, "a" * 64, 70)

    def test_search_partition_byte_budget_preserves_oversized_whole_page(self):
        layers, pages = self.layers(3)
        for n in range(3):
            layers[n] = text.from_page({**raw_page("x" * 300000), "page": n + 1}, "a" * 64)
        ref = v3.build_text_bundle(layers, pages, Path("objects/aa/" + "a" * 64 + "/" + "b" * 16), self.bundle)
        self.generated()
        manifest = v3.verify_text_bundle(ref, self.read, "a" * 64, 3)
        self.assertEqual(len(manifest["search_partitions"]), 3)

    def test_review_evidence_keeps_qualified_input_bucket(self):
        layers, pages = self.layers(1)
        layers[0] = text.from_page(raw_page("Text", .1), "a" * 64)
        evidence = self.add("objects/aa/source/inputs/ocr-input/page-000001.png", b"evidence",
                            shared.PDF_OCR_INPUT_BUCKET, "review")
        pages[0]["i"] = evidence["path"]
        pages[0]["is"] = evidence["sha256"]
        pages[0]["ib"] = evidence["bytes"]
        ref = v3.build_text_bundle(layers, pages, Path("objects/aa/" + "a" * 64 + "/" + "b" * 16), self.bundle)
        self.generated()
        manifest = v3.verify_text_bundle(ref, self.read, "a" * 64, 1, verify_evidence=True)
        review = v3.decode(self.read(manifest["review"]))
        self.assertEqual(review["tasks"][0]["evidence"][0]["bucket"], shared.PDF_OCR_INPUT_BUCKET)

    def spec(self):
        with pymupdf.open() as doc:
            doc.new_page(width=100, height=200)
            doc.new_page(width=100, height=200).set_rotation(90)
            pdf = doc.tobytes()
        primary = self.add("objects/aa/book/primary/document.pdf", pdf)
        mapping = v3.page_map(pdf, "a" * 64)
        previews = []
        for geometry in mapping["pages"]:
            image = Image.new("RGB", (int(geometry["width"]), int(geometry["height"])), "white")
            stream = io.BytesIO()
            image.save(stream, "PNG")
            ref = self.add(f"objects/aa/book/preview/page-{geometry['page']:06d}.png", stream.getvalue())
            previews.append({"page": geometry["page"], "page_identity": geometry["identity"],
                             "codec": "png", "width": image.width, "height": image.height, "resource": ref})
        return {"source_sha256": "a" * 64, "source_key": "repo\0book.pdf", "primary": primary,
                "previews": previews, "require_complete_preview": True}

    def test_full_reading_bundle_preserves_real_rotation(self):
        spec = self.spec()
        ref, manifest = v3.build_reading(spec, self.read, self.bundle)
        self.generated()
        mapping = json.loads(gzip.decompress(self.read(manifest["page_map"])))
        self.assertEqual(mapping["pages"][1]["rotation"], 90)
        self.assertFalse(manifest['preview']['complete'])
        self.assertEqual(manifest['preview']['status'], 'skipped')
        self.assertEqual(manifest['preview']['partitions'], [])
        self.assertEqual(manifest["components"]["searchable_pdf"], "skipped")
        self.assertEqual(v3.decode(self.read(ref)), manifest)
        self.assertEqual(v3.verify_reading(ref, self.read), manifest)
        self.assertEqual(v3.build_reading(spec, self.read, self.bundle)[0], ref)

    def test_vector_pdf_is_read_directly_without_svg_or_raster_preview(self):
        with pymupdf.open() as doc:
            page = doc.new_page(width=200, height=300)
            page.insert_text((30, 70), "Vector page", fontsize=18)
            pdf = doc.tobytes()
        primary = self.add("objects/aa/book/primary/document.pdf", pdf)
        spec = v3.generate_previews({"source_sha256": "a" * 64, "source_key": "repo\0book.pdf",
                                    "primary": primary, "vector_previews": True}, self.read, self.bundle)
        self.generated()
        self.assertEqual(spec['previews'], [])
        self.assertEqual(list(self.bundle.rglob('*.svg')), [])
        ref, _ = v3.build_reading(spec, self.read, self.bundle)
        self.generated()
        self.assertEqual(v3.verify_reading(ref, self.read)['components']['preview'], 'skipped')
        with self.assertRaises(ValueError):
            v3.validate_vector_svg(b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><script/></svg>')

    def test_pdf_missing_preview_is_explicitly_skipped_not_pending_or_complete(self):
        spec = self.spec()
        spec["previews"].pop()
        _, direct = v3.build_reading(spec, self.read, self.bundle)
        self.assertEqual(direct['components']['preview'], 'skipped')
        spec["require_complete_preview"] = False
        _, manifest = v3.build_reading(spec, self.read, self.bundle)
        self.assertFalse(manifest["preview"]["complete"])
        self.assertEqual(manifest["components"]["document"], "ready")

    def test_pdf_builder_discards_obsolete_preview_metadata_without_reading_its_media(self):
        spec = self.spec()
        duplicate = copy.deepcopy(spec)
        duplicate["previews"].append(duplicate["previews"][0])
        self.assertEqual(v3.build_reading(duplicate, self.read, self.bundle)[1]['preview']['partitions'], [])
        for change in ({"page_identity": "b" * 64}, {"codec": "webp"}, {"width": 200}):
            altered = copy.deepcopy(spec)
            altered["previews"][0].update(change)
            self.assertEqual(v3.build_reading(altered, self.read, self.bundle)[1]['preview']['partitions'], [])
        altered = copy.deepcopy(spec)
        altered["previews"][0]["resource"]["bucket"] = shared.PDF_OCR_INPUT_BUCKET
        self.assertEqual(v3.build_reading(altered, self.read, self.bundle)[1]['components']['preview'], 'skipped')

    def test_resource_path_traversal_unsupported_bucket_and_digest_rejected(self):
        valid = self.add("objects/aa/book/document.pdf", b"pdf")
        for change in ({"path": "objects/../secret"}, {"bucket": "foreign/bucket"},
                       {"path": "https://example.org/pdf"}, {"sha256": "invalid"}, {"bytes": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                v3.resource({**valid, **change})

    def test_backfill_uses_existing_page_objects_without_ocr_or_render(self):
        pages = []
        for number in range(1, 3):
            payload = {**raw_page("Text"), "page": number}
            raw = gzip.compress(json.dumps(payload).encode())
            ref = self.add(f"objects/aa/raw/ocr/page-{number:06d}.json.gz", raw)
            pages.append({"p": number, "source": "ocr", "o": ref["path"], "os": ref["sha256"], "ob": ref["bytes"]})
        manifest = {"kind": "pdf-ocr", "complete": True, "source_sha256": "a" * 64,
                    "page_count": 2, "language": "en", "pages": pages}
        ref = v3.backfill_text(manifest, self.read, self.bundle)
        self.generated()
        self.assertEqual(v3.verify_text_bundle(ref, self.read, "a" * 64, 2)["page_count"], 2)
        with self.assertRaises(ValueError):
            v3.backfill_text({**manifest, "pages": pages[:1]}, self.read, self.bundle)
        with self.assertRaises(ValueError):
            v3.backfill_text(manifest, self.read, self.bundle, {"rotation": 90})

    def test_pdf_preview_range_requests_are_noops_without_reading_or_rendering(self):
        spec = self.spec()
        spec["previews"] = []
        one = v3.generate_previews(spec, self.read, self.bundle, start=2, end=2, max_pixels=10000)
        self.assertEqual(one['previews'], [])
        resumed = v3.generate_previews(one, self.read, self.bundle, start=1, end=1, max_pixels=10000)
        self.generated()
        ref, manifest = v3.build_reading(resumed, self.read, self.bundle)
        self.generated()
        self.assertEqual(v3.verify_reading(ref, self.read)['components']['preview'], 'skipped')
        self.assertEqual(resumed['previews'], [])

    def test_bitonal_pdf_keeps_original_without_generating_even_a_lossless_preview(self):
        image = Image.new("1", (100, 200), 1)
        stream = io.BytesIO()
        image.save(stream, "TIFF", compression="group4")
        with pymupdf.open() as doc:
            page = doc.new_page(width=100, height=200)
            page.insert_image(page.rect, stream=stream.getvalue())
            raw = doc.tobytes()
        primary = self.add("objects/aa/binary/document.pdf", raw)
        spec = {"source_sha256": "a" * 64, "source_key": "book", "primary": primary,
                "require_complete_preview": True}
        generated = v3.generate_previews(spec, self.read, self.bundle, max_pixels=10000)
        self.assertEqual(generated['previews'], [])
        self.assertEqual(list(self.bundle.rglob('*.png')), [])
        self.assertEqual(self.read(primary), raw)
        self.generated()
        ref, _ = v3.build_reading(generated, self.read, self.bundle)
        self.generated()
        v3.verify_reading(ref, self.read)
        self.assertEqual(v3.verify_reading(ref, self.read)['components']['preview'], 'skipped')

    def test_skipped_preview_cannot_claim_complete_or_hide_runtime_partitions(self):
        ref, manifest = v3.build_reading(self.spec(), self.read, self.bundle)
        self.generated()
        incomplete = copy.deepcopy(manifest)
        incomplete['preview']['complete'] = True
        with self.assertRaises(ValueError):
            v3.validate_reading_manifest(incomplete)
        extra = copy.deepcopy(manifest)
        extra['preview']['ready_pages'] = 1
        with self.assertRaises(ValueError):v3.validate_reading_manifest(extra)
        broken = copy.deepcopy(manifest)
        broken['components']['preview'] = 'ready'
        with self.assertRaises(ValueError):v3.validate_reading_manifest(broken)

    def test_text_geometry_must_match_pdf_before_reading_publication(self):
        spec = self.spec()
        layers, pages = self.layers(2)
        ref = v3.build_text_bundle(layers, pages, Path("objects/aa/" + "a" * 64 + "/" + "b" * 16), self.bundle)
        self.generated()
        spec["text_layer"] = ref
        # Page two is rotated to landscape; the supplied text coordinates are portrait.
        with self.assertRaises(ValueError):
            v3.build_reading(spec, self.read, self.bundle)

    def test_accepted_bundle_updates_search_and_retains_parent(self):
        layers, pages = self.layers(2)
        ref = v3.build_text_bundle(layers, pages, Path("objects/aa/" + "a" * 64 + "/" + "b" * 16), self.bundle)
        self.generated()
        proposal = {"kind": "pdf-text-correction", "version": 1, "page": 1,
                    "base_generation": layers[0]["generation"], "raw_sha256": layers[0]["raw_sha256"],
                    "page_identity": layers[0]["page_identity"],
                    "replacements": [{"region_id": "b0", "before": "Text", "after": "Corrected text"}]}
        accepted = v3.accept_text_bundle(ref, [proposal], self.read, self.bundle, actor="reviewer")
        self.generated()
        manifest = v3.verify_text_bundle(accepted, self.read, "a" * 64, 2)
        self.assertEqual(manifest["parent"], ref)
        self.assertEqual(manifest["revision"], "effective")
        index = v3.decode(self.read(manifest["book_text"]))
        self.assertEqual([p["text"] for p in index["pages"]], ["Corrected text", "Text"])
        self.assertEqual(index["quality"], "partially-reviewed")
        repacked = v3.repartition_text_bundle(accepted, self.read, self.bundle)
        self.generated()
        refreshed = v3.verify_text_bundle(repacked, self.read, "a" * 64, 2)
        self.assertEqual(refreshed["generation"], manifest["generation"])
        self.assertEqual(refreshed["parent"], accepted)
        self.assertEqual(refreshed["revision"], "effective")
        search = v3.decode(self.read(refreshed["search_partitions"][0]["resource"]))
        self.assertEqual([p["text"] for p in search["pages"]], ["Corrected text", "Text"])
        with self.assertRaises(ValueError):
            v3.accept_text_bundle(accepted, [proposal], self.read, self.bundle, actor="reviewer")
        with self.assertRaises(ValueError):
            v3.accept_text_bundle(ref, [proposal, proposal], self.read, self.bundle, actor="reviewer")

    def test_positioned_reviewed_glyph_reaches_search_with_parent_and_raw_identity_intact(self):
        layers,pages=self.layers(2)
        ref=v3.build_text_bundle(layers,pages,Path('objects/aa/'+'a'*64+'/'+'b'*16),self.bundle)
        self.generated()
        source=layers[0]
        proposal={'kind':'pdf-text-correction','version':1,'page':1,
            'base_generation':source['generation'],'raw_sha256':source['raw_sha256'],
            'page_identity':source['page_identity'],'replacements':[],
            'visual_review':{'kind':'source-image-review','source_sha256':'a'*64,'page':1,
                            'input_png_sha256':'c'*64,'basis':'Full source map and enlarged glyph'},
            'additions':[{'region_id':'v'+'1'*16,'text':'BÉNIN','box':[.1,.3,.4,.4],
                'quad':[[.1,.3],[.4,.3],[.4,.4],[.1,.4]],
                'evidence':{'crop_sha256':'d'*64,'reason':'Retain printed acute accent'}}],
            'region_order':['b0','v'+'1'*16]}
        accepted=v3.accept_text_bundle(ref,[proposal],self.read,self.bundle,actor='explicit-source-review')
        self.generated()
        manifest=v3.verify_text_bundle(accepted,self.read,'a'*64,2)
        self.assertEqual(manifest['parent'],ref)
        search=v3.decode(self.read(manifest['search_partitions'][0]['resource']))
        self.assertEqual(search['pages'][0]['text'],'Text\nBÉNIN')
        index=v3.decode(self.read(manifest['book_text']))
        self.assertEqual(index['pages'][1]['text'],'Text')
        partition=v3.decode(self.read(manifest['partitions'][0]['resource']))
        effective=v3.decode(self.read(partition['pages'][0]['resource']))
        self.assertEqual(effective['raw_sha256'],source['raw_sha256'])
        self.assertEqual(effective['regions'][1]['confidence'],0)
        self.assertEqual(effective['quality'],'partially-reviewed')

    def test_source_flow_graph_survives_complete_text_and_search_packaging(self):
        from tests.test_ocr_flow_diagram import fixture
        from scripts import ocr_flow_diagram as flow
        image,blocks=fixture();graph=flow.detect(image,blocks)
        output,models,graph=flow.filter_detections(image,blocks,{},graph)
        arranged=flow.arrange(output,*image.size,graph)
        layer=text.from_page({'kind':'pdf-ocr-page','page':1,'source':'ocr',
            'width':image.width,'height':image.height,**arranged},'a'*64,'en')
        layers,pages=self.layers(1);layers[0]=layer
        ref=v3.build_text_bundle(layers,pages,Path('objects/aa/'+'a'*64+'/'+'b'*16),self.bundle)
        self.generated();verified=[]
        manifest=v3.verify_text_bundle(ref,self.read,'a'*64,1,verified_layers=verified)
        self.assertEqual(verified[0]['flow_diagrams'],layer['flow_diagrams'])
        search=v3.decode(self.read(manifest['search_partitions'][0]['resource']))
        self.assertEqual(search['pages'][0]['text'],layer['text'])
        self.assertIn('⋮',search['pages'][0]['text'])

    def test_unresolved_margin_evidence_survives_search_bundle_without_publishing_false_digit(self):
        from scripts import ocr_layout
        blocks=[{'t':'Printed text','b':[.1,.1,.8,.2],'s':'ocr','c':1},
                {'t':'9','b':[.01,.3,.04,.35],'s':'ocr','c':.4}]
        payload={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        original=text.from_page(payload,'a'*64,'en')
        proposal={'kind':'pdf-text-correction','version':1,'base_generation':original['generation'],
            'raw_sha256':original['raw_sha256'],'page_identity':original['page_identity'],'replacements':[],
            'annotations':[{'region_id':'b1','before':'9','classification':'marginal-mark','status':'unresolved',
                'literal_text':None,'evidence':{'crop_sha256':'d'*64,'reason':'Complete original pencil mark'}}],
            'visual_review':{'kind':'source-image-review','source_sha256':'a'*64,'page':1,
                'input_png_sha256':'c'*64,'basis':'Complete original user page'},'region_order':['b0']}
        accepted=text.accept_proposal(original,proposal,actor='explicit-user-source-review')
        layers,pages=self.layers(1);layers[0]=accepted
        ref=v3.build_text_bundle(layers,pages,Path('objects/aa/'+'a'*64+'/'+'b'*16),self.bundle)
        self.generated();verified=[]
        manifest=v3.verify_text_bundle(ref,self.read,'a'*64,1,verified_layers=verified)
        self.assertEqual(verified[0]['annotations'],accepted['annotations'])
        search=v3.decode(self.read(manifest['search_partitions'][0]['resource']))
        self.assertEqual(search['pages'][0]['text'],'Printed text')

    def test_source_reviewed_separator_reaches_search_without_changing_raw_or_glyph_geometry(self):
        from scripts import ocr_layout
        blocks=[{'t':'水','b':[.1,.1,.2,.2],'s':'ocr','c':1},
                {'t':'解','b':[.1,.3,.2,.4],'s':'ocr','c':1}]
        payload={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        payload['text']='水解'
        payload['text_spans'][0].update(start=0,end=1)
        payload['text_spans'][1].update(start=1,end=2)
        original=text.from_page(payload,'a'*64,'ch')
        proposal={'kind':'pdf-text-correction','version':1,'page':1,'base_generation':original['generation'],
            'raw_sha256':original['raw_sha256'],'page_identity':original['page_identity'],'replacements':[],
            'visual_review':{'kind':'source-image-review','source_sha256':'a'*64,'page':1,
                'input_png_sha256':'c'*64,'basis':'Two separately printed original lines'},
            'separators':[{'after_region_id':'b0','before_region_id':'b1','before':'','after':'\n',
                'evidence':{'crop_sha256':'d'*64,'reason':'Original physical line boundary'}}]}
        accepted=text.accept_proposal(original,proposal,actor='user-source-boundary-review')
        layers,pages=self.layers(1);layers[0]=accepted
        ref=v3.build_text_bundle(layers,pages,Path('objects/aa/'+'a'*64+'/'+'b'*16),self.bundle)
        self.generated();verified=[]
        manifest=v3.verify_text_bundle(ref,self.read,'a'*64,1,verified_layers=verified)
        self.assertEqual(verified[0]['raw_sha256'],original['raw_sha256'])
        search=v3.decode(self.read(manifest['search_partitions'][0]['resource']))
        self.assertEqual(search['pages'][0]['text'],'水\n解')
        self.assertEqual(verified[0]['acceptances'][-1]['separator_changes'][0]['before'],'')

    def test_undetected_source_annotation_survives_packaging_without_adding_search_text(self):
        layers,pages=self.layers(1);original=layers[0]
        proposal={'kind':'pdf-text-correction','version':1,'page':1,'base_generation':original['generation'],
            'raw_sha256':original['raw_sha256'],'page_identity':original['page_identity'],'replacements':[],
            'visual_review':{'kind':'source-image-review','source_sha256':'a'*64,'page':1,
                'input_png_sha256':'c'*64,'basis':'Undetected original faint interlinear handwriting'},
            'annotation_additions':[{'id':'a'+'1'*16,'classification':'handwritten-annotation','status':'unresolved',
                'literal_text':None,'box':[.8,.2,.9,.3],'quad':[[.8,.2],[.9,.2],[.9,.3],[.8,.3]],
                'evidence':{'crop_sha256':'d'*64,'reason':'Keep source handwriting without a guessed transcription'}}]}
        accepted=text.accept_proposal(original,proposal,actor='user-source-annotation-review');layers[0]=accepted
        ref=v3.build_text_bundle(layers,pages,Path('objects/aa/'+'a'*64+'/'+'b'*16),self.bundle)
        self.generated();verified=[]
        manifest=v3.verify_text_bundle(ref,self.read,'a'*64,1,verified_layers=verified)
        self.assertEqual(verified[0]['annotations'],accepted['annotations'])
        search=v3.decode(self.read(manifest['search_partitions'][0]['resource']))
        self.assertEqual(search['pages'][0]['text'],original['text'])
        self.assertEqual(verified[0]['annotations'][0]['confidence'],0)

    def test_margin_bracket_packaging_preserves_empty_table_cell_footnote_and_truncated_print(self):
        from scripts import ocr_layout
        blocks=[{'t':'Country','b':[.5,.1,.65,.2],'s':'ocr','c':1},
                {'t':'1949–','b':[.7,.3,.85,.4],'s':'ocr','c':1},
                {'t':'velop under all socialist systems,','b':[.1,.55,.45,.65],'s':'ocr','c':1},
                {'t':'¹⁰Summary surveys','b':[.1,.8,.45,.9],'s':'ocr','c':1},
                {'t':'tinn -f 11-','b':[.5,.96,.6,.98],'s':'ocr','c':.52}]
        payload={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        original=text.from_page(payload,'a'*64,'en')
        original['tables']=[{'box':[.5,.1,.9,.5],'source_pixels_sha256':'e'*64,
            'rows':[.1,.25,.5],'columns':[.5,.68,.9],'cells':[
                {'row':0,'column':0,'rowspan':1,'colspan':1,'box':[.5,.1,.68,.25],'region_ids':['b0']},
                {'row':0,'column':1,'rowspan':1,'colspan':1,'box':[.68,.1,.9,.25],'region_ids':[]},
                {'row':1,'column':0,'rowspan':1,'colspan':1,'box':[.5,.25,.68,.5],'region_ids':[]},
                {'row':1,'column':1,'rowspan':1,'colspan':1,'box':[.68,.25,.9,.5],'region_ids':['b1']}]}]
        original['generation']=text.digest({key:value for key,value in original.items() if key!='generation'})
        text.validate(original)
        proposal={'kind':'pdf-text-correction','version':1,'page':1,'base_generation':original['generation'],
            'raw_sha256':original['raw_sha256'],'page_identity':original['page_identity'],'replacements':[],
            'visual_review':{'kind':'source-image-review','source_sha256':'a'*64,'page':1,
                'input_png_sha256':'c'*64,'basis':'User complete spread and isolated bracket crop'},
            'annotation_additions':[{'id':'a'+'2'*16,'classification':'marginal-mark','status':'unresolved',
                'literal_text':None,'box':[.05,.52,.095,.7],'quad':[[.05,.52],[.095,.52],[.095,.7],[.05,.7]],
                'evidence':{'crop_sha256':'d'*64,'reason':'Independent bracket, not a body glyph or scan artifact'}}]}
        accepted=text.accept_proposal(original,proposal,actor='user-source-bracket-review')
        layers,pages=self.layers(1);layers[0]=accepted
        ref=v3.build_text_bundle(layers,pages,Path('objects/aa/'+'a'*64+'/'+'b'*16),self.bundle)
        self.generated();verified=[]
        manifest=v3.verify_text_bundle(ref,self.read,'a'*64,1,verified_layers=verified)
        self.assertEqual(verified[0]['tables'],original['tables'])
        self.assertEqual(verified[0]['regions'],original['regions'])
        self.assertEqual(verified[0]['annotations'],accepted['annotations'])
        search=v3.decode(self.read(manifest['search_partitions'][0]['resource']))['pages'][0]['text']
        self.assertEqual(search,original['text'])
        self.assertIn('tinn -f 11-',search);self.assertIn('¹⁰Summary surveys',search)
        self.assertEqual(verified[0]['tables'][0]['cells'][2]['region_ids'],[])

    def test_wrapped_formula_link_survives_text_and_search_without_joining_physical_lines(self):
        from scripts import ocr_layout
        first='2Ag⁺¹ + 2NO₃⁻¹ + Ca⁺² + 2Cl⁻¹ = 2AgCl↓ +';last='Ca⁺² + 2NO₃⁻¹'
        blocks=[{'t':first,'b':[.1,.1,.9,.2],'s':'ocr','c':.8},
                {'t':last,'b':[.65,.3,.9,.4],'s':'ocr','c':.7}]
        payload={**raw_page(),**ocr_layout.arrange(blocks,100,200,{'writing_mode':'horizontal-ltr'})}
        original=text.from_page(payload,'a'*64,'ch')
        anchor=lambda identity,value,start:{'region_id':identity,'text':value,'start':start,'end':start+len(value)}
        link={'id':'c'+'1'*16,'kind':'printed-formula-continuation','join_separator':' ',
            'from':anchor('b0',first,0),'to':anchor('b1',last,0),'operator':anchor('b0','+',len(first)-1),
            'box':[.1,.1,.9,.4],'quad':[[.1,.1],[.9,.1],[.9,.4],[.1,.4]],
            'evidence':{'crop_sha256':'d'*64,'reason':'Original product ion terms wrap after final+'}}
        proposal={'kind':'pdf-text-correction','version':1,'page':1,'base_generation':original['generation'],
            'raw_sha256':original['raw_sha256'],'page_identity':original['page_identity'],'replacements':[],
            'formula_continuations':[link],'visual_review':{'kind':'source-image-review','source_sha256':'a'*64,
                'page':1,'input_png_sha256':'c'*64,'basis':'Exact user full and enlarged two-line formula'}}
        layers,pages=self.layers(1)
        layers[0]=text.accept_proposal(original,proposal,actor='source-formula-review')
        ref=v3.build_text_bundle(layers,pages,Path('objects/aa/'+'a'*64+'/'+'b'*16),self.bundle)
        self.generated();verified=[]
        manifest=v3.verify_text_bundle(ref,self.read,'a'*64,1,verified_layers=verified)
        self.assertEqual(verified[0]['formula_continuations'],[link])
        self.assertEqual(verified[0]['raw_sha256'],original['raw_sha256'])
        search=v3.decode(self.read(manifest['search_partitions'][0]['resource']))
        self.assertEqual(search['pages'][0]['text'],original['text'])
        self.assertIn('\n',search['pages'][0]['text'])
        self.assertEqual([r['box'] for r in verified[0]['regions']],[b['b'] for b in blocks])


if __name__ == "__main__":
    unittest.main()
