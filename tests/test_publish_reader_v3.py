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


    def test_unify_preserves_every_format_and_rejects_moved_pointers(self):
        base = {'v': 1, 'f': {'ebook': {'s': 2, 'm': 'e', 'p': 'book.epub'},
                              'image': {'s': 2, 'm': 'i', 'p': 'image.webp'},
                              'media': {'s': 2, 'm': 'a', 'p': 'audio.mp3'}}}
        self.store.put_bytes(publish.ASSETS, 'reader-index/reader_assets.json.gz',
                             gzip.compress(publish.encode(base), mtime=0))
        first = publish.unify(self.store, apply=True)
        pointer, catalog = publish.current(self.store)
        self.assertEqual(publish.pinned_base(self.store, catalog, {}), base)
        self.assertEqual(publish.project_sidecar(base, catalog)['f'], base['f'])
        self.assertTrue(publish.unify(self.store, apply=True)['unchanged'])
        self.assertEqual(pointer['generation'], first['generation'])
        candidate = {'version': 3, 'kind': 'reader-reading-catalog', 'files': {},
                     'base_sidecar': catalog['base_sidecar'], 'parent_generation': 'f' * 64}
        with self.assertRaisesRegex(ValueError, 'parent mismatch'):
            publish.commit_catalog(self.store, candidate, pointer, apply=True)
        self.assertEqual(publish.current(self.store)[0], pointer)

    def test_catalog_checksum_and_consumer_receipts_require_exact_identity(self):
        base = {'v': 1, 'f': {'book': {'s': 2, 'm': 'e', 'p': 'book.epub'}}}
        self.store.put_bytes(publish.ASSETS, 'reader-index/reader_assets.json.gz',
                             gzip.compress(publish.encode(base), mtime=0))
        report = publish.unify(self.store, apply=True)
        pointer = report['pointer']
        receipt = {'version': 1, 'surface': 'hf', 'active': True, 'generation': report['generation'],
                   'catalog_sha256': pointer['catalog']['sha256'], 'files': 0}
        with self.assertRaisesRegex(ValueError, 'projection acceptance'):
            publish.acknowledge(self.store, 'hf', receipt, apply=True)
        accepted = {**receipt, 'checked_url': 'https://example.test/status', 'projection_verified': True}
        result = publish.acknowledge(self.store, 'hf', accepted, apply=True)
        self.assertEqual(result['acks'], {'hf': True, 'pages': False})
        with self.assertRaisesRegex(ValueError, 'current'):
            publish.acknowledge(self.store, 'hf', {**accepted, 'generation': 'a' * 64}, apply=True)
        ref = pointer['catalog']
        self.store.objects[(publish.ASSETS, ref['path'])] = b'different'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            publish.current(self.store)

    def test_pinned_original_read_verifies_identity_size_and_checksum_without_copy(self):
        raw = b"pinned original PDF bytes"
        source = hashlib.sha256(raw).hexdigest()
        primary = {"kind": "dataset-source", "repo": "VoiceOfML/books", "path": "folder/book.pdf",
                   "revision": "b" * 40, "sha256": source, "bytes": len(raw), "role": "runtime"}
        cached = self.bundle / "cached.pdf"
        cached.write_bytes(raw)
        with patch("huggingface_hub.HfApi") as api, patch("huggingface_hub.hf_hub_download", return_value=str(cached)) as download:
            api.return_value.get_paths_info.return_value = [Mock(size=len(raw))]
            self.assertEqual(publish.read_dataset_primary(primary), raw)
            download.assert_called_once_with("VoiceOfML/books", "folder/book.pdf", repo_type="dataset", revision="b" * 40)
            with self.assertRaisesRegex(ValueError, "dataset primary identity"):
                publish.read_dataset_primary({**primary, "revision": "main"})
            api.return_value.get_paths_info.return_value = [Mock(size=len(raw) + 1)]
            with self.assertRaisesRegex(ValueError, "size mismatch"):
                publish.read_dataset_primary(primary)
            api.return_value.get_paths_info.return_value = [Mock(size=len(raw))]
            cached.write_bytes(b"x" * len(raw))
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                publish.read_dataset_primary(primary)
        self.assertEqual(self.store.writes, [])













    def test_closure_pins_review_png_evidence_without_reading_it(self):
        from scripts import shared
        source = "a" * 64
        png = {"bucket": shared.PDF_OCR_INPUT_BUCKET, "role": "review", "sha256": "b" * 64, "bytes": 1234,
               "path": f"objects/aa/{source}/0123456789abcdef/ocr-input/page-000001.png"}
        raw = json.dumps({"tasks": [{"page": 1, "evidence": [png]}]}).encode()
        review = {"bucket": shared.PDF_PAGES_BUCKET, "role": "provenance", "bytes": len(raw),
                  "sha256": hashlib.sha256(raw).hexdigest(),
                  "path": f"objects/aa/{source}/0123456789abcdef/text/review.json"}
        store = Store()
        store.objects[(review["bucket"], review["path"])] = raw
        reader = publish.CandidateReader(store)
        try:
            reader(review)
            reader.closure()  # the PNG is absent from the store: it must not be read
        finally:
            reader.close()
        self.assertEqual(reader.refs[(png["bucket"], png["path"])], png)
        self.assertNotIn((png["bucket"], png["path"]), reader.verified)

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
        self.assertEqual(workflow[True]["workflow_dispatch"]["inputs"]["operation"]["options"],
                         ["inspect", "unify", "ack", "retire", "withdraw", "reset-documents",
                          "remove-route", "restore-route", "verify-route", "migrate-derived", "resume-derived", "redirect-derived", "reconcile-derived-index", "reconcile-sidecar", "compact-legacy-history", "publish-generation", "verify-generation"])
        operation_step = next(step for step in workflow["jobs"]["publish"]["steps"]
                              if step.get("id") == "operation")
        # Detached OCR admission and correction are retired; retention remains
        # explicit rather than restarting a daily mutating journal.
        script = next(step["run"] for step in workflow["jobs"]["publish"]["steps"]
                      if step.get("id") == "operation")
        self.assertNotIn("OPERATION=auto\n", script)
        self.assertIn("Scheduled v3 retirement is disabled", script)
        self.assertNotIn("schedule", workflow[True])
        self.assertNotIn("OPERATION=correct", script)
        self.assertNotIn("cycle", workflow[True]["workflow_dispatch"]["inputs"]["operation"]["options"])
        ack_step = next(step for step in workflow["jobs"]["publish"]["steps"]
                        if step.get("id") == "ack")
        self.assertNotIn("operation == 'auto'", ack_step["if"])
        self.assertIn("complete=", ack_step["run"])
        self.assertIn("retire", workflow[True]["workflow_dispatch"]["inputs"]["operation"]["options"])
        self.assertIn("retire) ;;", operation_step["run"])
        for name in ('HF_S3_ACCESS_KEY_ID', 'HF_S3_SECRET_ACCESS_KEY'):
            value = operation_step['env'][name]
            self.assertIn('"migrate-derived"', value)
            self.assertIn('"resume-derived"', value)
            self.assertIn('"redirect-derived"', value)
            self.assertIn('"reconcile-derived-index"', value)
            self.assertIn('"reconcile-sidecar"', value)
            self.assertIn('"compact-legacy-history"', value)
            for operation in ('unify', 'remove-route', 'restore-route'):
                self.assertIn('"' + operation + '"', value)
            self.assertIn('secrets.' + name, value)
            self.assertNotIn('inputs.apply', value)
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


    def preview_candidate(self, source="a" * 64, key="repo\0book.pdf"):
        full = self.candidate(source, key)
        manifest = v3.decode((self.bundle / full["path"]).read_bytes())
        previews = [page for part in manifest["preview"]["partitions"]
                    for page in v3.decode((self.bundle / part["resource"]["path"]).read_bytes())["pages"]]
        spec = {"source_key": key, "source_sha256": source, "primary": manifest["primary"]["resource"],
                "previews": previews, "require_complete_preview": True}
        return v3.build_reading(spec, lambda ref: (self.bundle / ref["path"]).read_bytes(), self.bundle)[0]










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





if __name__ == "__main__":
    unittest.main()
