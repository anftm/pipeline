"""Small offline effective-text fixtures; no model, source corpus or uploads."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import export_reader_txt as exporter, pdf_reading_v3 as v3, pdf_text_layer as text, shared
from tests.test_pdf_text_layer import raw_page


class ExportReaderTxtTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        self.objects = {}
        self.contents = ["原始手机😀", "", "手。机\nABC 原始末页"]
        self.spec = {"origin_repo": "VoiceOfML/VOMEBOOK", "origin_rel_path": "中文目录/子目录/book.v2.pdf",
                     "origin_revision": "b" * 40, "origin_sha256": "a" * 64,
                     "source_sha256": "a" * 64, "page_count": 3, "doc_number": 7}

    def make_bundle(self, *, accepted=True):
        layers = [text.from_page({**raw_page(content), "page": number}, "a" * 64)
                  for number, content in enumerate(self.contents, 1)]
        if accepted:
            first = layers[0]
            proposal = {"kind": "pdf-text-correction", "version": 1, "base_generation": first["generation"],
                        "raw_sha256": first["raw_sha256"], "page_identity": first["page_identity"],
                        "replacements": [{"region_id": first["regions"][0]["id"], "before": first["text"], "after": "已接受手机😀"}]}
            layers[0] = text.accept_proposal(first, proposal, actor="fixture-reviewer")
        ref = v3.build_text_bundle(layers, [{"p": number} for number in range(1, 4)],
                                   Path("objects/aa/" + "a" * 64 + "/" + "b" * 16), self.bundle)
        for path in self.bundle.rglob("*"):
            if path.is_file(): self.objects[(shared.PDF_PAGES_BUCKET, path.relative_to(self.bundle).as_posix())] = path.read_bytes()
        return {**self.spec, "text_manifest": ref}, layers

    def read(self, ref):
        return self.objects[(ref["bucket"], ref["path"])]

    def test_complete_plain_text_uses_only_effective_text_and_preserves_empty_pages(self):
        spec, layers = self.make_bundle()
        before = copy.deepcopy(layers)
        result = exporter.build_export(spec, self.read)
        manifest = result["manifest"]
        raw = result["files"][manifest["text"]["path"]]
        self.assertEqual(raw.decode(), "\n\n".join(layer["text"] for layer in layers))
        self.assertNotIn(b'"box"', raw)
        self.assertNotIn(b'"regions"', raw)
        self.assertEqual(layers, before)
        self.assertEqual(manifest["accepted_pages"], 1)
        self.assertEqual(manifest["quality"], "partially-reviewed")
        self.assertEqual(manifest["page_count"], 3)
        for page, layer in zip(manifest["pages"], layers):
            self.assertEqual(raw.decode()[page["start"]:page["end"]], layer["text"])
        self.assertEqual(manifest["pages"][1]["start"], manifest["pages"][1]["end"])

    def test_source_path_hierarchy_and_explicit_stable_id_are_preserved(self):
        spec, _ = self.make_bundle()
        result = exporter.build_export(spec, self.read)
        self.assertEqual(result["manifest"]["doc_id"], "OCR:7")
        self.assertEqual(result["manifest"]["readable_rel_path"], "VoiceOfML/VOMEBOOK/中文目录/子目录/book.v2.txt")
        self.assertEqual(result, exporter.build_export(spec, self.read))

    def test_raw_ocr_is_explicit_and_never_claimed_reviewed(self):
        spec, _ = self.make_bundle(accepted=False)
        with self.assertRaisesRegex(ValueError, "explicit admission"):
            exporter.build_export(spec, self.read)
        result = exporter.build_export(spec, self.read, allow_unreviewed=True)
        self.assertEqual(result["manifest"]["accepted_pages"], 0)
        self.assertEqual(result["manifest"]["quality"], "unreviewed")

    def test_empty_whole_book_and_binary_controls_are_not_silently_cleaned(self):
        for contents in (["", "", ""], ["●", "", ""], ["text\0damaged", "", ""]):
            with self.subTest(contents=contents):
                self.contents = contents
                spec, _ = self.make_bundle(accepted=False)
                with self.assertRaises(ValueError):
                    exporter.build_export(spec, self.read, allow_unreviewed=True)

    def test_corrupt_page_blocks_the_entire_export(self):
        spec, _ = self.make_bundle()
        key = next(key for key in self.objects if key[1].endswith("page-000003.json.gz"))
        self.objects[key] += b"damage"
        with self.assertRaisesRegex(ValueError, "checksum"):
            exporter.build_export(spec, self.read)

    def test_wrong_source_or_page_count_blocks_export(self):
        spec, _ = self.make_bundle()
        for change in ({"source_sha256": "c" * 64}, {"page_count": 2}, {"page_count": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                exporter.build_export({**spec, **change}, self.read)

    def test_derived_primary_requires_exact_original_provenance(self):
        spec, _ = self.make_bundle()
        with self.assertRaises(ValueError):
            exporter.build_export({**spec, "origin_sha256": "c" * 64}, self.read)
        payload = {"kind": "pdf-derived-source", "version": 1, "repo": spec["origin_repo"],
                   "source_path": spec["origin_rel_path"], "source_revision": spec["origin_revision"],
                   "source_sha256": "c" * 64, "derived_sha256": spec["source_sha256"]}
        raw = exporter.json_bytes(payload)
        ref = {"bucket": shared.PDF_PAGES_BUCKET, "path": "derived/test/derived-manifest.json",
               "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "role": "provenance"}
        self.objects[(ref["bucket"], ref["path"])] = raw
        exported = exporter.build_export({**spec, "origin_sha256": "c" * 64, "derivation_manifest": ref}, self.read)
        self.assertEqual(exported["manifest"]["origin_sha256"], "c" * 64)
        self.assertIn(ref, exported["manifest"]["retained_reader_resources"])

    def test_invalid_origin_or_unpinned_identity_is_rejected(self):
        spec, _ = self.make_bundle()
        for change in ({"origin_repo": "../bad"}, {"origin_rel_path": "../book.pdf"},
                       {"origin_rel_path": "a//book.pdf"}, {"origin_revision": "main"},
                       {"doc_number": 0}, {"doc_number": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                exporter.build_export({**spec, **change}, self.read)

    def test_budget_rejects_whole_book_instead_of_truncating(self):
        spec, _ = self.make_bundle()
        with self.assertRaisesRegex(ValueError, "budget"):
            exporter.build_export(spec, self.read, max_input_bytes=1)

    def test_export_manifest_retains_checked_reader_resources(self):
        spec, _ = self.make_bundle()
        result = exporter.build_export(spec, self.read)
        refs = result["manifest"]["retained_reader_resources"]
        self.assertIn(spec["text_manifest"], refs)
        self.assertTrue(any(ref["path"].endswith("page-000003.json.gz") for ref in refs))
        self.assertEqual(result["manifest"]["text"]["sha256"], hashlib.sha256(result["files"][result["manifest"]["text"]["path"]]).hexdigest())

    def test_workflow_can_only_stage_an_artifact_outside_the_writer_lock(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/stage-ccrd-ocr-text.yml").read_text()
        self.assertIn("consumer_revision:", workflow)
        self.assertIn("builder_sha256:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("actions/upload-artifact@v7", workflow)
        self.assertIn("path: output/ccrd-ocr-candidate/", workflow)
        self.assertIn("--allow-unreviewed", workflow)
        self.assertNotIn("reader-sidecar", workflow)
        self.assertNotIn("--apply", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("workflow run", workflow)


if __name__ == "__main__":
    unittest.main()
