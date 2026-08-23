import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from huggingface_hub import CommitOperationAdd
from huggingface_hub.errors import RepositoryNotFoundError

from scripts import build_reader_assets_index, convert_reader_assets, publish_reader_assets
from scripts import publish_search_reader_index
from scripts import reader_assets, scan_reader_assets


class ReaderAssetContractTests(unittest.TestCase):
    def test_first_conversion_set_excludes_pdg(self):
        self.assertEqual(
            set(reader_assets.CONVERTIBLE_EXTENSIONS),
            {"doc", "docx", "mobi", "azw3", "tif", "tiff"},
        )

    def test_source_url_pins_revision_and_encodes_path(self):
        self.assertEqual(
            reader_assets.source_url("VoiceOfML/Test", "abc123", "目录/a b.docx"),
            "https://huggingface.co/datasets/VoiceOfML/Test/resolve/abc123/"
            "%E7%9B%AE%E5%BD%95/a%20b.docx",
        )


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {"Repo": "VoiceOfML/Test", "File": "Book", "Extension": "docx", "Folder": ["A"], "Size": 10},
            {"Repo": "VoiceOfML/Test", "File": "Scan", "Extension": "pdg", "Folder": [], "Size": 20},
            {"Repo": "VoiceOfML/Test", "File": "Photo", "Extension": "jpg", "Folder": [], "Size": 30},
        ]
        self.revisions = {"VoiceOfML/Test": "rev1"}

    def test_queues_only_supported_changed_files(self):
        queue = scan_reader_assets.build_queue(
            self.records, self.revisions, reader_assets.empty_manifest()
        )
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["path"], "A/Book.docx")
        self.assertEqual(queue[0]["profile"], "libreoffice-pdf-v1")

    def test_ready_current_profile_is_skipped(self):
        key = reader_assets.asset_key("VoiceOfML/Test", "A/Book.docx")
        manifest = {"version": 1, "files": {key: {
            "status": "ready", "source_revision": "rev1", "profile": "libreoffice-pdf-v1"
        }}}
        self.assertEqual(scan_reader_assets.build_queue(self.records, self.revisions, manifest), [])

    def test_failed_current_profile_requires_retry_flag(self):
        key = reader_assets.asset_key("VoiceOfML/Test", "A/Book.docx")
        manifest = {"version": 1, "files": {key: {
            "status": "failed", "source_revision": "rev1", "profile": "libreoffice-pdf-v1"
        }}}
        self.assertEqual(scan_reader_assets.build_queue(self.records, self.revisions, manifest), [])
        self.assertEqual(
            len(scan_reader_assets.build_queue(self.records, self.revisions, manifest, retry_failed=True)), 1
        )

    def test_filter_and_limit_are_deterministic(self):
        records = self.records + [
            {"Repo": "VoiceOfML/Test", "File": "A", "Extension": "mobi", "Folder": [], "Size": 1},
            {"Repo": "VoiceOfML/Other", "File": "B", "Extension": "mobi", "Folder": [], "Size": 1},
        ]
        revisions = {**self.revisions, "VoiceOfML/Other": "rev2"}
        queue = scan_reader_assets.build_queue(
            records, revisions, reader_assets.empty_manifest(),
            repo="VoiceOfML/Test", extension="mobi", limit=1,
        )
        self.assertEqual([item["path"] for item in queue], ["A.mobi"])

    def test_missing_assets_repository_is_an_empty_manifest(self):
        api = Mock()
        response = requests.Response()
        response.status_code = 404
        response.request = requests.Request("GET", "https://huggingface.co/api/datasets/vomebook/Missing").prepare()
        api.file_exists.side_effect = RepositoryNotFoundError("missing", response=response)
        self.assertEqual(scan_reader_assets.remote_manifest(api, "vomebook/Missing"), reader_assets.empty_manifest())


class ConverterTests(unittest.TestCase):
    def test_tiff_conversion_preserves_multiple_frames(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / "source.tiff", Path(root) / "document.pdf"
            first = Image.new("RGB", (20, 30), "white")
            second = Image.new("RGB", (20, 30), "black")
            first.save(source, save_all=True, append_images=[second])
            convert_reader_assets.convert_tiff(source, target)
            self.assertEqual(target.read_bytes()[:5], b"%PDF-")
            self.assertGreater(target.stat().st_size, 0)

    def test_office_conversion_uses_headless_libreoffice(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            source, target = work / "source.docx", work / "document.pdf"
            source.write_bytes(b"docx")

            def fake_run(command):
                (work / "office" / "source.pdf").write_bytes(b"%PDF-test")

            with patch.object(convert_reader_assets, "run_checked", side_effect=fake_run) as run:
                convert_reader_assets.convert_file({"extension": "docx"}, source, target, work)
            self.assertEqual(run.call_args.args[0][:3], ["libreoffice", "--headless", "--convert-to"])
            self.assertEqual(target.read_bytes(), b"%PDF-test")

    def test_duplicate_source_digest_reuses_converted_artifact(self):
        item = {
            "key": "VoiceOfML/Test\0Book.docx", "extension": "docx",
            "source_url": "https://example.test/book.docx", "source_revision": "rev1",
            "profile": "libreoffice-pdf-v1", "reader_mode": "pdf", "output_name": "document.pdf",
        }
        with tempfile.TemporaryDirectory() as root:
            bundle = Path(root)

            def download(_url, target):
                target.write_bytes(b"same source")
                return "a" * 64, 11

            def convert(_item, _source, target, _work):
                target.write_bytes(b"%PDF-converted")

            with patch.object(convert_reader_assets, "download_source", side_effect=download), \
                    patch.object(convert_reader_assets, "convert_file", side_effect=convert) as conversion:
                first = convert_reader_assets.convert_item(item, bundle)
                second = convert_reader_assets.convert_item({**item, "key": "VoiceOfML/Test\0Copy.docx"}, bundle)

            self.assertEqual(conversion.call_count, 1)
            self.assertEqual(first["path"], second["path"])


class PublicationTests(unittest.TestCase):
    def make_bundle(self, root: str, result: dict) -> Path:
        bundle = Path(root)
        artifact = bundle / result["path"]
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"%PDF-converted")
        result["bytes"] = artifact.stat().st_size
        result["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        (bundle / "bundle.json").write_text(
            json.dumps({"version": 1, "results": [result]}), encoding="utf-8"
        )
        return bundle

    def test_publish_contains_artifact_manifest_and_sidecar(self):
        key = "VoiceOfML/Test\0A/Book.docx"
        result = {
            "key": key, "status": "ready", "source_revision": "rev1",
            "source_sha256": "a" * 64, "source_bytes": 10, "source_extension": "docx",
            "profile": "libreoffice-pdf-v1", "reader_mode": "pdf",
            "path": "objects/aa/source/document.pdf",
        }
        api = Mock()
        api.file_exists.return_value = False
        with tempfile.TemporaryDirectory() as root:
            bundle = self.make_bundle(root, result)
            manifest, operations = publish_reader_assets.build_publish(api, "vomebook/Test", bundle)
        self.assertEqual(manifest["files"][key]["status"], "ready")
        self.assertEqual(len(operations), 3)
        self.assertTrue(all(isinstance(operation, CommitOperationAdd) for operation in operations))
        self.assertEqual(
            {operation.path_in_repo for operation in operations},
            {result["path"], "manifest.json", "reader_assets.json.gz"},
        )

    def test_failed_retry_does_not_replace_existing_ready_asset(self):
        key = "VoiceOfML/Test\0A/Book.docx"
        existing = {"status": "ready", "source_revision": "old", "profile": "libreoffice-pdf-v1",
                    "reader_mode": "pdf", "path": "objects/old/document.pdf"}
        api = Mock()
        api.file_exists.return_value = True
        with tempfile.TemporaryDirectory() as root:
            remote = Path(root) / "remote.json"
            remote.write_text(json.dumps({"version": 1, "files": {key: existing}}), encoding="utf-8")
            api.hf_hub_download.return_value = str(remote)
            bundle = Path(root) / "bundle"
            bundle.mkdir()
            (bundle / "bundle.json").write_text(json.dumps({"version": 1, "results": [{
                "key": key, "status": "failed", "source_revision": "new",
                "source_extension": "docx", "profile": "libreoffice-pdf-v1", "error": "RuntimeError",
            }]}), encoding="utf-8")
            manifest, operations = publish_reader_assets.build_publish(api, "vomebook/Test", bundle)
        self.assertEqual(manifest["files"][key], existing)
        self.assertEqual(len(operations), 2)

    def test_sidecar_is_compact_and_deterministic(self):
        manifest = {"version": 1, "files": {
            "b": {"status": "failed"},
            "a": {"status": "ready", "reader_mode": "epub", "path": "objects/a/book.epub"},
        }}
        one = build_reader_assets_index.encode_index(manifest)
        two = build_reader_assets_index.encode_index(manifest)
        self.assertEqual(one, two)
        self.assertEqual(json.loads(gzip.decompress(one)), {
            "v": 1, "f": {"a": {"s": 2, "m": "e", "p": "objects/a/book.epub"}, "b": {"s": 4}}
        })


class SearchIndexPublicationTests(unittest.TestCase):
    def test_pages_publish_copies_only_sidecar_and_pushes(self):
        commands = []

        def fake_run(args, cwd=None, env=None):
            commands.append((list(args), cwd, env))
            if args[:2] == ["git", "clone"]:
                Path(args[-1], "data").mkdir(parents=True, exist_ok=True)
                return 0, "", ""
            if args[:3] == ["git", "status", "--porcelain"]:
                return 0, " M data/reader_assets.json.gz", ""
            return 0, "", ""

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "reader_assets.json.gz"
            source.write_bytes(b"gzip")
            with patch.object(publish_search_reader_index, "run", side_effect=fake_run):
                publish_search_reader_index.publish_to_pages(
                    source, "vomebook/search", "token", dry_run=False
                )
        argv = [item[0] for item in commands]
        self.assertIn(["git", "add", "data/reader_assets.json.gz"], argv)
        commit = next(args for args in argv if args[:2] == ["git", "commit"])
        self.assertNotIn("skip ci", " ".join(commit).lower())
        self.assertEqual(argv[-1], ["git", "push"])

    def test_dry_run_never_uses_remote_publishers(self):
        publish_search_reader_index.publish_to_pages(
            Path("missing"), "", "", dry_run=True
        )


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_exposes_incremental_controls_and_excludes_pdg(self):
        workflow = Path(".github/workflows/reader-assets.yml").read_text(encoding="utf-8")
        for field in ("repo:", "extension:", "limit:", "retry_failed:", "force:", "dry_run:"):
            self.assertIn(field, workflow)
        self.assertNotIn("pdg", workflow.lower())
        self.assertIn("publish_search_reader_index.py", workflow)


if __name__ == "__main__":
    unittest.main()
