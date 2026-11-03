import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import reader_gbk_repair as repair, repair_gbk_pdf, publish_reader_v3 as publication
from tests.test_repair_gbk_pdf import fixture
from tests.test_publish_reader_v3 import Store


class GbkRepairPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.store = Store()
        self.source = fixture(small_song_heading=True)
        source_path, fixed_path = self.workspace / "original.pdf", self.workspace / "fixed.pdf"
        source_path.write_bytes(self.source)
        repair_gbk_pdf.repair_pdf(source_path, fixed_path)
        path = "A4 毛泽东主席/02-03 毛泽东选集 林一章版 第一版/4 1945.8-1949.9.pdf"
        self.key = "VoiceOfML/Teachers\0" + path
        old_path = "derived/old/" + "a" * 32 + "/document.pdf"
        self.spec = {"source_key": self.key, "source_revision": "b" * 40,
            "source_sha256": repair.digest(self.source), "repaired_sha256": repair.digest(fixed_path.read_bytes()),
            "page_count": 2, "previous_pdf": publication.metadata(repair.shared.PDF_PAGES_BUCKET, old_path, self.source),
            "text_probes": [{"page": 1, "text": "建国以来毛泽东文稿"}]}
        self.old_entry = {"key": self.key, "repo": "VoiceOfML/Teachers", "source_path": path,
            "source_extension": "pdf", "source_revision": "b" * 40, "source_sha256": self.spec["source_sha256"],
            "derived_sha256": repair.digest(self.source), "derived_bytes": len(self.source),
            "profile": "gbk-font-repair-v1", "new_path": old_path}
        self.derived = {"version": 1, "kind": "pdf-derived-source-index", "files": [self.old_entry,
            {"key": "other", "new_path": "derived/other/document.pdf", "source_extension": "djvu"}]}
        self.sidecar = {"v": 1, "f": {self.key: repair.mapping(old_path),
            "other": {"s": 2, "m": "a", "p": "media/audio/book/audio.mp3"}}}
        self.store.objects[(repair.shared.PDF_PAGES_BUCKET, old_path)] = self.source
        self.store.objects[(repair.shared.PDF_PAGES_BUCKET, repair.DERIVED)] = publication.encode(self.derived)
        self.store.objects[(publication.ASSETS, repair.SIDECAR)] = gzip.compress(publication.encode(self.sidecar), mtime=0)
        publication.unify(self.store, apply=True)
        self.store.writes.clear()
        self.download = lambda *_args: self.source
        publisher = patch.object(repair, "publish_catalog", return_value="c" * 32)
        self.publisher = publisher.start()
        self.addCleanup(publisher.stop)

    def run_repair(self, apply=False, options=None):
        return repair.run(self.store, options or {"sources": [self.spec]}, self.workspace / "run",
                          apply=apply, download=self.download)

    def test_dry_run_builds_verified_candidate_without_writes(self):
        result = self.run_repair()
        self.assertFalse(result["applied"])
        self.assertEqual(result["books"][0]["pdf"]["sha256"], self.spec["repaired_sha256"])
        self.assertEqual(self.store.writes, [])
        self.publisher.assert_not_called()

    def test_publication_preserves_other_formats_old_pdf_and_coherent_snapshot(self):
        old_pointer, _ = publication.current(self.store)
        result = self.run_repair(apply=True)
        pointer, catalog = publication.current(self.store)
        self.assertNotEqual(pointer, old_pointer)
        projected = publication.pinned_base(self.store, catalog, {})
        self.assertEqual(projected["f"]["other"], self.sidecar["f"]["other"])
        self.assertEqual(projected["f"][self.key], repair.mapping(result["books"][0]["pdf"]["path"]))
        self.assertIn((self.spec["previous_pdf"]["bucket"], self.spec["previous_pdf"]["path"]), self.store.objects)
        derived = json.loads(self.store.read_bytes(repair.shared.PDF_PAGES_BUCKET, repair.DERIVED))
        self.assertEqual(derived["files"][1], self.derived["files"][1])
        ref = result["books"][0]["pdf"]
        self.assertEqual(repair.digest(self.store.read_bytes(ref["bucket"], ref["path"])), ref["sha256"])
        marker = json.loads(self.store.read_bytes(publication.ASSETS, result["processing_root"]))
        self.assertEqual(marker["status"], "released")
        self.assertEqual(marker["generation"], "c" * 32)

    def test_repeated_publication_is_immutable_and_generation_idempotent(self):
        first = self.run_repair(apply=True)
        before = dict(self.store.objects)
        second = self.run_repair(apply=True)
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(first["processing_root"], second["processing_root"])
        self.assertEqual(self.store.objects, before)

    def test_checksum_or_source_scope_errors_have_no_remote_writes(self):
        for field, value in (("source_sha256", "d" * 64), ("repaired_sha256", "e" * 64),
                ("source_revision", "main"), ("source_key", "other\0book.pdf"), ("page_count", 3)):
            with self.subTest(field=field):
                spec = {**self.spec, field: value}
                with self.assertRaises(ValueError):
                    self.run_repair(apply=True, options={"sources": [spec]})
                self.assertEqual(self.store.writes, [])

    def test_existing_ocr_routing_is_not_silently_discarded(self):
        base = copy.deepcopy(self.sidecar)
        base["f"][self.key]["o"] = "objects/old/ocr-manifest.json"
        self.store.objects[(publication.ASSETS, repair.SIDECAR)] = gzip.compress(publication.encode(base), mtime=0)
        with self.assertRaisesRegex(ValueError, "separately published text"):
            self.run_repair(apply=True)
        self.assertEqual(self.store.writes, [])

    def test_published_v3_or_accepted_text_is_protected(self):
        pointer, catalog = publication.current(self.store)
        catalog.pop("generation")
        catalog["parent_generation"] = pointer["generation"]
        source = "f" * 64
        ref = publication.metadata(repair.shared.PDF_PAGES_BUCKET,
            "objects/ff/" + source + "/" + "a" * 16 + "/reading-manifest.json", b"manifest")
        catalog["files"][self.key] = {"resource": ref, "source_sha256": source, "reading_generation": "e" * 64}
        publication.commit_catalog(self.store, catalog, pointer, apply=True)
        self.store.writes.clear()
        with self.assertRaisesRegex(ValueError, "published v3 book"):
            self.run_repair(apply=True)
        self.assertEqual(self.store.writes, [])

    def test_failed_readback_retains_protection_without_promoting_indexes(self):
        previous = publication.current(self.store)[0]
        put = self.store.put_bytes
        def corrupt(bucket, path, raw):
            put(bucket, path, b"damaged" if path.endswith("/document.pdf") else raw)
        with patch.object(self.store, "put_bytes", side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, "differs from candidate"):
                self.run_repair(apply=True)
        self.assertEqual(publication.current(self.store)[0], previous)
        self.assertEqual(json.loads(self.store.read_bytes(repair.shared.PDF_PAGES_BUCKET, repair.DERIVED)), self.derived)
        markers = [json.loads(raw) for (bucket, path), raw in self.store.objects.items()
                   if path.startswith("reader-index/processing/gbk-")]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["status"], "processing")

    def test_writer_guard_duplicate_and_invalid_specs_do_not_mutate(self):
        self.store.allowed = False
        with self.assertRaisesRegex(RuntimeError, "serialized writer"):
            self.run_repair(apply=True)
        self.store.allowed = True
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.run_repair(apply=True, options={"sources": [self.spec, self.spec]})
        with self.assertRaises(ValueError):
            self.run_repair(apply=True, options={"sources": ["bad"]})
        self.assertEqual(self.store.writes, [])


if __name__ == "__main__":
    unittest.main()
