import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from PIL import Image

from scripts import force_ocr_book as repair, pdf_ocr, pdf_ocr_stages as stages
from scripts import publish_pdf_ocr_assets as publication
from scripts.reader_gc_fence import producer_path


class ForceOcrBookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        self.source.write_bytes(b"%PDF-test-approved-source")
        self.key = next(iter(repair.APPROVED))
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.entry = {"key": self.key, "repo": repair.REPO, "path": self.key.split("\0")[1],
                      "source_sha256": self.digest, "source_bytes": self.source.stat().st_size,
                      "source_kind": "generated", "source_revision": "a" * 40,
                      "page_count": 2, "status": "ready", "text_revision": "raw",
                      "text_quality": "unreviewed", "ocr_manifest": "old-ocr-manifest",
                      "ocr_manifest_sha256": "b" * 64, "ocr_manifest_bytes": 123,
                      "text_layer": {"path": "old-text-layer"}}
        self.probe = {"classification": "native-text", "page_count": 2, "page_chars": [800, 900]}
        self.presentation = {"strategy": "preserve-pdf", "complete": True, "reason": "bitonal-raster"}
        self.objects = {}
        self.uploads = 0

    def upload(self, bundle):
        self.uploads += 1
        for file in bundle.rglob("*"):
            if file.is_file():
                path, raw = file.relative_to(bundle).as_posix(), file.read_bytes()
                if path in self.objects:
                    self.assertEqual(self.objects[path], raw, "immutable object overwritten")
                self.objects[path] = raw
        return [f"reader-index/processing/test-{self.uploads}.json"]

    def read(self, meta, suffix=None):
        raw = self.objects[meta["path"]]
        self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (meta["bytes"], meta["sha256"]))
        return raw

    @staticmethod
    def render(source, number, directory, reader_pixels=None, **kwargs):
        assert kwargs["reader_images"] is False
        path = directory / f"page-{number:06d}.png"
        with Image.new("RGB", (200, 300), "white") as image:
            image.save(path)
        return path, 200, 300

    def prepare(self):
        with patch.dict(repair.APPROVED, {self.key: (self.digest, 2, "ch")}), \
                patch.object(repair, "load_entry", return_value=copy.deepcopy(self.entry)), \
                patch.object(pdf_ocr, "OCR_LANG", "auto"), \
                patch.object(pdf_ocr, "probe_pdf", return_value=self.probe), \
                patch.object(pdf_ocr, "reader_presentation", return_value=self.presentation), \
                patch.object(pdf_ocr, "render_page", side_effect=self.render), \
                patch.object(pdf_ocr, "native_pages", side_effect=AssertionError("must not extract broken native text")), \
                patch.object(pdf_ocr, "ocr_page", return_value=[{"t": "完整识别正文", "b": [0, 0, 1, 1], "s": "ocr", "c": 1}]), \
                patch.object(stages, "upload_objects", side_effect=self.upload), \
                patch.object(stages, "read_object", side_effect=self.read), \
                patch.object(publication, "read_bucket_bytes", side_effect=lambda path, *a, **kw: self.objects[path]), \
                patch.object(stages, "save_registry", side_effect=AssertionError("prepare cannot write registries")), \
                patch.object(stages, "save_ocr_progress", side_effect=AssertionError("prepare cannot change shared progress")), \
                patch.object(publication, "publish", side_effect=AssertionError("prepare cannot publish")):
            return repair.prepare(object(), self.key, self.source, self.root / "out", apply=True)

    def test_only_two_pinned_sources_and_page_languages_are_approved(self):
        self.assertEqual(len(repair.APPROVED), 2)
        for key, (digest, pages, language) in repair.APPROVED.items():
            self.assertEqual(repair.approve(key, digest, language), (digest, pages, language))
            with self.assertRaises(ValueError):
                repair.approve(key, "0" * 64)
            with self.assertRaises(ValueError):
                repair.approve(key, digest, "en" if language == "ch" else "ch")
        with self.assertRaises(ValueError):
            repair.approve("other\0file.ps", "0" * 64)

    def test_forced_render_uses_real_probe_and_distinct_immutable_namespace(self):
        report = self.prepare()
        self.assertTrue(report["complete"])
        entry = report["entry"]
        self.assertEqual(entry["ocr_pages"], 2)
        self.assertEqual(entry["reader_presentation"], self.presentation)
        self.assertFalse(entry["image_rendered"])
        self.assertIsNone(entry["page_manifest"])
        self.assertTrue(entry["force_ocr_native"])
        self.assertEqual(entry["original_classification"], "native-text")
        self.assertTrue(entry["render_profile"].endswith("-force-ocr-native-v1"))
        self.assertEqual(self.probe["classification"], "native-text")
        self.assertEqual(self.probe["page_chars"], [800, 900])
        normal = stages.root_for(self.digest, self.key, stages.render_profile()).as_posix()
        self.assertFalse(entry["render_manifest"]["path"].startswith(normal + "/"))
        manifest = json.loads(self.read(entry["render_manifest"]))
        self.assertEqual([p["source"] for p in manifest["pages"]], ["ocr", "ocr"])
        self.assertEqual(entry["text_quality"], "unreviewed")
        self.assertEqual(entry["text_revision"], "raw")
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["source"], repair.identity(self.entry))

    def test_stale_publication_and_incomplete_artifacts_fail_closed(self):
        report = self.prepare()
        current = copy.deepcopy(self.entry)
        with patch.dict(repair.APPROVED, {self.key: (self.digest, 2, "ch")}), \
                patch.object(repair, "load_entry", return_value=current), \
                patch.object(publication, "publish") as publisher:
            planned = repair.publish(object(), self.key, report)
            self.assertFalse(planned["applied"])
            publisher.assert_not_called()
            current["ocr_manifest_sha256"] = "c" * 64
            with self.assertRaisesRegex(ValueError, "changed during preparation"):
                repair.publish(object(), self.key, report, apply=True)
            current.update(self.entry)
            report["complete"] = False
            with self.assertRaisesRegex(ValueError, "complete prepared"):
                repair.publish(object(), self.key, report, apply=True)
            publisher.assert_not_called()

    def test_publisher_requires_real_central_workflow_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                repair.assert_publisher()
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "anftm/pipeline", "GITHUB_RUN_ID": "1",
               "GITHUB_WORKFLOW_REF": "anftm/pipeline/.github/workflows/force-ocr-book.yml@refs/heads/main",
               "FORCE_OCR_PUBLICATION_PROTOCOL": repair.PROTOCOL}
        with patch.dict(os.environ, env, clear=True):
            repair.assert_publisher()
            os.environ["GITHUB_WORKFLOW_REF"] = env["GITHUB_WORKFLOW_REF"].replace("main", "other")
            with self.assertRaises(RuntimeError):
                repair.assert_publisher()

    def test_workflow_separates_build_from_shared_writer_and_gc_fences_both(self):
        path = Path(__file__).parents[1] / ".github/workflows/force-ocr-book.yml"
        workflow = yaml.safe_load(path.read_text())
        prepare, publish = workflow["jobs"]["prepare"], workflow["jobs"]["publish"]
        self.assertNotIn("concurrency", prepare)
        self.assertEqual(publish["concurrency"]["group"], "reader-sidecar")
        self.assertFalse(publish["concurrency"]["cancel-in-progress"])
        self.assertIn("actions/upload-artifact@v7", [s.get("uses") for s in prepare["steps"]])
        self.assertNotIn("promote", repair.main.__code__.co_consts)
        self.assertTrue(producer_path(".github/workflows/force-ocr-book.yml"))


if __name__ == "__main__":
    unittest.main()
