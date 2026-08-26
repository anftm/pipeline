import concurrent.futures
import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from huggingface_hub import CommitOperationAdd, CommitOperationDelete
from huggingface_hub.errors import RepositoryNotFoundError

from scripts import build_reader_assets_index, convert_reader_assets, publish_reader_assets
from scripts import prune_reader_assets, publish_search_reader_index
from scripts import reader_assets, scan_reader_assets


class ReaderAssetContractTests(unittest.TestCase):
    def test_first_conversion_set_excludes_pdg(self):
        self.assertEqual(
            set(reader_assets.CONVERTIBLE_EXTENSIONS),
            {"doc", "docx", "htm", "html", "mobi", "azw3", "tif", "tiff", "djvu"},
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
        self.assertEqual(queue[0]["profile"], "docx-native-v1")
        self.assertEqual(queue[0]["reader_mode"], "docx")

    def test_ready_current_profile_is_skipped(self):
        key = reader_assets.asset_key("VoiceOfML/Test", "A/Book.docx")
        manifest = {"version": 1, "files": {key: {
            "status": "ready", "source_revision": "rev1", "profile": "docx-native-v1"
        }}}
        self.assertEqual(scan_reader_assets.build_queue(self.records, self.revisions, manifest), [])

    def test_ready_manual_profile_is_skipped_for_the_same_source_revision(self):
        key = reader_assets.asset_key("VoiceOfML/Test", "A/Book.docx")
        manifest = {"version": 1, "files": {key: {
            "status": "ready", "source_revision": "rev1", "profile": "manual-pdf-v1"
        }}}
        self.assertEqual(scan_reader_assets.build_queue(self.records, self.revisions, manifest), [])
        self.assertEqual(len(scan_reader_assets.build_queue(
            self.records, {"VoiceOfML/Test": "rev2"}, manifest,
        )), 1)

    def test_failed_current_profile_requires_retry_flag(self):
        key = reader_assets.asset_key("VoiceOfML/Test", "A/Book.docx")
        manifest = {"version": 1, "files": {key: {
            "status": "failed", "source_revision": "rev1", "profile": "docx-native-v1"
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

    def test_unfiltered_queue_prioritizes_mature_bulk_formats(self):
        records = [
            {"Repo": "VoiceOfML/Test", "File": "A", "Extension": "doc", "Folder": [], "Size": 1},
            {"Repo": "VoiceOfML/Test", "File": "B", "Extension": "mobi", "Folder": [], "Size": 1},
            {"Repo": "VoiceOfML/Test", "File": "C", "Extension": "tif", "Folder": [], "Size": 1},
            {"Repo": "VoiceOfML/Test", "File": "D", "Extension": "djvu", "Folder": [], "Size": 1},
        ]
        queue = scan_reader_assets.build_queue(
            records, self.revisions, reader_assets.empty_manifest(),
        )
        self.assertEqual([item["extension"] for item in queue], ["tif", "mobi", "djvu", "doc"])

    def test_exact_path_filter_selects_one_asset(self):
        queue = scan_reader_assets.build_queue(
            self.records, self.revisions, reader_assets.empty_manifest(), exact_path="A/Book.docx",
        )
        self.assertEqual([item["path"] for item in queue], ["A/Book.docx"])

    def test_missing_assets_repository_is_an_empty_manifest(self):
        api = Mock()
        response = requests.Response()
        response.status_code = 404
        response.request = requests.Request("GET", "https://huggingface.co/api/datasets/vomebook/Missing").prepare()
        api.file_exists.side_effect = RepositoryNotFoundError("missing", response=response)
        self.assertEqual(scan_reader_assets.remote_manifest(api, "vomebook/Missing"), reader_assets.empty_manifest())

    def test_reusable_objects_include_ready_files_and_orphans(self):
        manifest = {"version": 1, "files": {
            "book": {"status": "ready", "source_sha256": "a" * 64, "profile": "p1",
                     "path": "objects/a", "sha256": "b" * 64, "bytes": 10, "reader_mode": "pdf"},
        }, "orphans": {
            "objects/b": {"source_sha256": "c" * 64, "profile": "p2", "path": "objects/b",
                          "sha256": "d" * 64, "bytes": 20, "reader_mode": "epub", "since": "2026-01-01"},
        }}
        objects = scan_reader_assets.reusable_objects(manifest)
        self.assertEqual(objects["a" * 64 + "\0p1"]["path"], "objects/a")
        self.assertEqual(objects["c" * 64 + "\0p2"]["path"], "objects/b")

    def test_active_keys_can_be_empty_after_all_convertible_files_are_deleted(self):
        self.assertEqual(scan_reader_assets.active_keys([]), [])

    def test_active_keys_do_not_depend_on_revision_availability(self):
        self.assertEqual(
            scan_reader_assets.active_keys(self.records),
            [reader_assets.asset_key("VoiceOfML/Test", "A/Book.docx")],
        )

class ConverterTests(unittest.TestCase):
    def test_tiff_conversion_preserves_multiple_frames(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / "source.tiff", Path(root) / "document.pdf"
            first = Image.new("RGB", (20, 30), "white")
            second = Image.new("RGB", (20, 30), "black")
            first.save(source, save_all=True, append_images=[second])
            def merge(command):
                Path(command[-1]).write_bytes(b"%PDF-merged")

            with patch.object(convert_reader_assets, "run_checked", side_effect=merge) as run:
                convert_reader_assets.convert_tiff(source, target, Path(root))
            self.assertEqual(target.read_bytes()[:5], b"%PDF-")
            self.assertGreater(target.stat().st_size, 0)
            self.assertEqual(run.call_args.args[0][0], "pdfunite")
            self.assertEqual(len(run.call_args.args[0][1:-1]), 2)

    def test_single_frame_tiff_does_not_require_pdfunite(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / "source.tif", Path(root) / "document.pdf"
            Image.new("RGB", (20, 30), "white").save(source)
            with patch.object(convert_reader_assets, "run_checked") as run:
                convert_reader_assets.convert_tiff(source, target, Path(root))
            run.assert_not_called()
            self.assertEqual(target.read_bytes()[:5], b"%PDF-")

    def test_djvu_conversion_uses_ddjvu_pdf_output(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            source, target = work / "source.djvu", work / "document.pdf"
            source.write_bytes(b"DJVU")
            with patch.object(convert_reader_assets, "run_checked") as run:
                convert_reader_assets.convert_file({"extension": "djvu"}, source, target, work)
            self.assertEqual(run.call_args.args[0], [
                "ddjvu", "-format=pdf", str(source), str(target),
            ])

    def test_mobi_conversion_keeps_large_unsplittable_flows(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            source, target = work / "source.mobi", work / "book.epub"
            source.write_bytes(b"BOOKMOBI")
            with patch.object(convert_reader_assets, "run_checked") as run:
                convert_reader_assets.convert_file({"extension": "mobi"}, source, target, work)
            self.assertEqual(run.call_args.args[0], [
                "ebook-convert", str(source), str(target), "--flow-size", "0",
            ])

    def test_djvu_pdf_validation_checks_first_and_last_pages(self):
        with tempfile.TemporaryDirectory() as root:
            work, pdf = Path(root), Path(root) / "document.pdf"
            pdf.write_bytes(b"%PDF-test")

            def render(command):
                Path(command[-1]).with_suffix(".png").write_bytes(b"png")

            with patch.object(convert_reader_assets, "command_output", return_value="Pages:           12\n"), \
                    patch.object(convert_reader_assets, "run_checked", side_effect=render) as run:
                convert_reader_assets.validate_djvu_pdf(pdf, work)
            self.assertEqual([call.args[0][2] for call in run.call_args_list], ["1", "12"])

    def test_doc_conversion_uses_headless_libreoffice_to_docx(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            source, target = work / "source.doc", work / "document.docx"
            source.write_bytes(b"doc")

            def fake_run(command):
                (work / "office" / "source.docx").write_bytes(b"docx")

            with patch.object(convert_reader_assets, "run_checked", side_effect=fake_run) as run:
                convert_reader_assets.convert_file({"extension": "doc"}, source, target, work)
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ["libreoffice", "--headless"])
            self.assertTrue(any(arg.startswith("-env:UserInstallation=file://") for arg in command))
            self.assertEqual(command[command.index("--convert-to") + 1], "docx")
            self.assertEqual(target.read_bytes(), b"docx")

    def test_doc_and_docx_use_distinct_docx_profiles(self):
        self.assertEqual(reader_assets.conversion_contract("doc"), ("libreoffice-docx-v1", "docx", "document.docx"))
        self.assertEqual(reader_assets.conversion_contract("docx"), ("docx-native-v1", "docx", "document.docx"))

    def test_html_and_htm_use_sanitized_html_profile(self):
        expected = ("sanitized-html-v1", "html", "document.html")
        self.assertEqual(reader_assets.conversion_contract("html"), expected)
        self.assertEqual(reader_assets.conversion_contract("htm"), expected)

    def test_html_conversion_preserves_source_html(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            source, target = work / "source.html", work / "document.html"
            source.write_bytes(b"<html><body>text</body></html>")

            convert_reader_assets.convert_file({"extension": "html"}, source, target, work)
            self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_html_conversion_inlines_local_images_and_stylesheets_only(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            source = work / "source.html"
            source.write_text(
                '<link rel="stylesheet" href="css/site.css">'
                '<img src="images/picture.png"><img src="https://evil.test/x.png">'
                '<img src="../secret.png">', encoding="utf-8",
            )
            resources = {
                "https://huggingface.co/datasets/VoiceOfML/Test/resolve/rev/css/site.css": b"body{color:red;background:url(x.png)}",
                "https://huggingface.co/datasets/VoiceOfML/Test/resolve/rev/images/picture.png": b"PNG",
            }

            def download(url, target):
                data = resources[url]
                target.write_bytes(data)
                return hashlib.sha256(data).hexdigest(), len(data)

            with patch.object(convert_reader_assets, "download_source", side_effect=download):
                output = convert_reader_assets.inline_html_resources(
                    source,
                    "https://huggingface.co/datasets/VoiceOfML/Test/resolve/rev/source.html",
                    work,
                )
            text = output.read_text(encoding="utf-8")
            self.assertIn("data:image/png;base64", text)
            self.assertIn("<style>body{color:red;background:}</style>", text)
            self.assertIn('src="https://evil.test/x.png"', text)
            self.assertIn('src="../secret.png"', text)

    def test_page_content_ratio_detects_blank_and_nonblank_pages(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as root:
            blank = Path(root) / "blank.png"
            content = Path(root) / "content.png"
            Image.new("RGB", (100, 100), "white").save(blank)
            image = Image.new("RGB", (100, 100), "white")
            for x in range(20, 80):
                for y in range(20, 80):
                    image.putpixel((x, y), (0, 0, 0))
            image.save(content)
            self.assertEqual(convert_reader_assets.page_content_ratio(blank), 0)
            self.assertGreater(convert_reader_assets.page_content_ratio(content), 0.1)

    def test_docx_validation_requires_document_structure(self):
        import zipfile

        with tempfile.TemporaryDirectory() as root:
            valid = Path(root) / "valid.docx"
            with zipfile.ZipFile(valid, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<w:document/>")
            convert_reader_assets.validate_output(valid, "docx")
            invalid = Path(root) / "invalid.docx"
            with zipfile.ZipFile(invalid, "w") as archive:
                archive.writestr("other.xml", "<x/>")
            with self.assertRaises(RuntimeError):
                convert_reader_assets.validate_output(invalid, "docx")

    def test_docx_password_is_extracted_only_from_explicit_path_marker(self):
        self.assertEqual(convert_reader_assets.PASSWORD_RE.search("资料〔密码：123〕.docx").group(1), "123")
        self.assertEqual(convert_reader_assets.PASSWORD_RE.search("book password: secret.docx").group(1), "secret.docx")
        self.assertIsNone(convert_reader_assets.PASSWORD_RE.search("ordinary.docx"))

    def test_ole_signature_is_detected_for_mislabeled_docx(self):
        self.assertEqual(convert_reader_assets.OLE_SIGNATURE, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

    def test_mislabeled_ole_docx_is_converted_with_doc_extension(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            source, target = work / "source.docx", work / "document.docx"
            source.write_bytes(convert_reader_assets.OLE_SIGNATURE + b"data")
            def fake_run(command):
                (work / "mislabeled-office" / "mislabeled.docx").write_bytes(b"docx")
            with patch.object(convert_reader_assets, "run_checked", side_effect=fake_run) as run:
                convert_reader_assets.convert_file({"extension": "docx", "path": "book.docx"}, source, target, work)
            self.assertTrue(str(run.call_args.args[0][-1]).endswith("mislabeled.doc"))
            self.assertEqual(target.read_bytes(), b"docx")

    def test_conversion_command_timeout_is_bounded(self):
        with patch.object(convert_reader_assets.subprocess, "run", side_effect=convert_reader_assets.subprocess.TimeoutExpired("libreoffice", convert_reader_assets.COMMAND_TIMEOUT_SECONDS)):
            with self.assertRaisesRegex(RuntimeError, "timed out: libreoffice"):
                convert_reader_assets.run_checked(["libreoffice", "--headless"])

    def test_conversion_workers_are_bounded(self):
        self.assertGreaterEqual(convert_reader_assets.CONVERSION_WORKERS, 1)

    def test_html_disguised_as_doc_uses_html_extension(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            source, target = work / "source.doc", work / "document.docx"
            source.write_bytes(b"\xef\xbb\xbf<html><body>text</body></html>")
            def fake_run(command):
                if "odt" in command:
                    (work / "html-office" / "source.odt").write_bytes(b"odt")
                (work / "office" / "source.docx").write_bytes(b"docx")
            with patch.object(convert_reader_assets, "run_checked", side_effect=fake_run) as run:
                convert_reader_assets.convert_file({"extension": "doc"}, source, target, work)
            self.assertTrue(str(run.call_args_list[0].args[0][-1]).endswith("source.html"))
            self.assertTrue(str(run.call_args_list[1].args[0][-1]).endswith("source.odt"))

    def test_duplicate_source_digest_reuses_converted_artifact(self):
        item = {
            "key": "VoiceOfML/Test\0Book.docx", "extension": "docx",
            "source_url": "https://example.test/book.docx", "source_revision": "rev1",
            "profile": "libreoffice-pdf-v2", "reader_mode": "pdf", "output_name": "document.pdf",
        }
        with tempfile.TemporaryDirectory() as root:
            bundle = Path(root)

            def download(_url, target):
                target.write_bytes(b"same source")
                return "a" * 64, 11

            def convert(_item, _source, target, _work):
                target.write_bytes(b"%PDF-converted")

            with patch.object(convert_reader_assets, "download_source", side_effect=download), \
                    patch.object(convert_reader_assets, "convert_file", side_effect=convert) as conversion, \
                    patch.object(convert_reader_assets, "validate_office_pdf"):
                first = convert_reader_assets.convert_item(item, bundle)
                second = convert_reader_assets.convert_item({**item, "key": "VoiceOfML/Test\0Copy.docx"}, bundle)

            self.assertEqual(conversion.call_count, 1)
            self.assertEqual(first["path"], second["path"])
            self.assertIn("/libreoffice-pdf-v2/", first["path"])

    def test_concurrent_duplicate_source_digest_converts_once(self):
        item = {
            "key": "VoiceOfML/Test\0Book.djvu", "extension": "djvu",
            "source_url": "https://example.test/book.djvu", "source_revision": "rev1",
            "profile": "djvulibre-pdf-v1", "reader_mode": "pdf", "output_name": "document.pdf",
        }
        with tempfile.TemporaryDirectory() as root:
            bundle = Path(root)

            def download(_url, target):
                target.write_bytes(b"same source")
                return "a" * 64, 11

            def convert(_item, _source, target, _work):
                target.write_bytes(b"%PDF-converted")

            with patch.object(convert_reader_assets, "download_source", side_effect=download), \
                    patch.object(convert_reader_assets, "convert_file", side_effect=convert) as conversion, \
                    patch.object(convert_reader_assets, "validate_djvu_pdf"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(
                        lambda queued: convert_reader_assets.convert_item(queued, bundle),
                        [item, {**item, "key": "VoiceOfML/Test\0Copy.djvu"}],
                    ))

            self.assertEqual(conversion.call_count, 1)
            self.assertEqual(results[0]["path"], results[1]["path"])
            self.assertEqual(results[0]["sha256"], results[1]["sha256"])

    def test_remote_artifact_is_reused_for_matching_source_and_profile(self):
        item = {
            "key": "VoiceOfML/Test\0Moved.djvu", "extension": "djvu",
            "source_url": "https://example.test/moved.djvu", "source_revision": "rev2",
            "profile": "djvulibre-pdf-v1", "reader_mode": "pdf", "output_name": "document.pdf",
        }
        digest = "a" * 64
        artifact = b"%PDF-reused"
        reusable = {f"{digest}\0djvulibre-pdf-v1": {
            "path": f"objects/aa/{digest}/djvulibre-pdf-v1/document.pdf",
            "bytes": len(artifact), "sha256": hashlib.sha256(artifact).hexdigest(),
        }}
        with tempfile.TemporaryDirectory() as root:
            def download(_url, target):
                target.write_bytes(b"source")
                return digest, 6

            def reuse(_url, target, _digest):
                target.write_bytes(artifact)

            with patch.object(convert_reader_assets, "download_source", side_effect=download), \
                    patch.object(convert_reader_assets, "download_existing", side_effect=reuse) as reused, \
                    patch.object(convert_reader_assets, "convert_file") as conversion:
                result = convert_reader_assets.convert_item(item, Path(root), reusable)

            conversion.assert_not_called()
            reused.assert_called_once()
            self.assertTrue(result["reused"])
            self.assertEqual(result["sha256"], hashlib.sha256(artifact).hexdigest())

    def test_concurrent_remote_reuse_marks_both_results_reused(self):
        item = {
            "key": "VoiceOfML/Test\0One.djvu", "extension": "djvu",
            "source_url": "https://example.test/one.djvu", "source_revision": "rev2",
            "profile": "djvulibre-pdf-v1", "reader_mode": "pdf", "output_name": "document.pdf",
        }
        digest = "a" * 64
        artifact = b"%PDF-reused"
        reusable = {f"{digest}\0djvulibre-pdf-v1": {
            "path": "objects/existing.pdf", "bytes": len(artifact),
            "sha256": hashlib.sha256(artifact).hexdigest(), "reader_mode": "pdf",
        }}
        with tempfile.TemporaryDirectory() as root:
            def download(_url, target):
                target.write_bytes(b"source")
                return digest, 6

            def reuse(_url, target, _digest):
                target.write_bytes(artifact)

            with patch.object(convert_reader_assets, "download_source", side_effect=download), \
                    patch.object(convert_reader_assets, "download_existing", side_effect=reuse) as reused:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(
                        lambda queued: convert_reader_assets.convert_item(queued, Path(root), reusable),
                        [item, {**item, "key": "VoiceOfML/Test\0Two.djvu"}],
                    ))
        self.assertEqual(reused.call_count, 1)
        self.assertTrue(all(result["reused"] for result in results))

    def test_office_pdf_requires_embedded_fonts_and_extractable_cjk(self):
        item = {"path": "目录/中文.docx"}
        with patch.object(convert_reader_assets, "embedded_pdf_fonts", return_value=["NotoSansCJK"]), \
                patch.object(convert_reader_assets, "command_output", return_value="中文内容\n"):
            convert_reader_assets.validate_office_pdf(Path("document.pdf"), item, Path("."))

        with patch.object(convert_reader_assets, "embedded_pdf_fonts", return_value=[]), \
                patch.object(convert_reader_assets, "command_output", return_value="中文内容\n"), \
                patch.object(convert_reader_assets, "outline_pdf_fonts") as outline, \
                patch.object(convert_reader_assets, "validate_output"):
            convert_reader_assets.validate_office_pdf(Path("document.pdf"), item, Path("."))
        outline.assert_called_once()

        with patch.object(convert_reader_assets, "embedded_pdf_fonts", return_value=["NotoSansCJK"]), \
                patch.object(convert_reader_assets, "command_output", return_value="中文内容\n"):
            convert_reader_assets.validate_office_pdf(Path("document.pdf"), item, Path("."))

        with patch.object(convert_reader_assets, "embedded_pdf_fonts", return_value=["NotoSansCJK"]), \
                patch.object(convert_reader_assets, "command_output", return_value="□□□□\n"), \
                patch.object(convert_reader_assets, "rasterize_pdf") as rasterize, \
                patch.object(convert_reader_assets, "validate_output"):
            convert_reader_assets.validate_office_pdf(Path("document.pdf"), item, Path("."))
        rasterize.assert_called_once_with(Path("document.pdf"), Path("."))

    def test_office_pdf_rewrites_missing_embedded_fonts(self):
        with patch.object(convert_reader_assets, "embedded_pdf_fonts", return_value=[]), \
                patch.object(convert_reader_assets, "outline_pdf_fonts") as outline, \
                patch.object(convert_reader_assets, "validate_output"), \
                patch.object(convert_reader_assets, "command_output", return_value="中文内容\n"):
            convert_reader_assets.validate_office_pdf(
                Path("document.pdf"), {"path": "目录/中文.docx"}, Path("work")
            )
        outline.assert_called_once_with(Path("document.pdf"), Path("work"))

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
            "profile": "libreoffice-pdf-v2", "reader_mode": "pdf",
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
        existing = {"status": "ready", "source_revision": "old", "profile": "libreoffice-pdf-v2",
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
                "source_extension": "docx", "profile": "libreoffice-pdf-v2", "error": "RuntimeError",
            }]}), encoding="utf-8")
            manifest, operations = publish_reader_assets.build_publish(api, "vomebook/Test", bundle)
        self.assertEqual(manifest["files"][key], existing)
        self.assertEqual(len(operations), 2)

    def test_publish_removes_inactive_mapping_and_marks_orphan(self):
        removed_key = "VoiceOfML/Test\0Old/Book.djvu"
        existing = {
            "status": "ready", "source_revision": "rev1", "source_sha256": "a" * 64,
            "source_extension": "djvu", "profile": "djvulibre-pdf-v1", "reader_mode": "pdf",
            "path": "objects/aa/document.pdf", "bytes": 10, "sha256": "b" * 64,
        }
        api = Mock()
        api.file_exists.return_value = True
        with tempfile.TemporaryDirectory() as root:
            remote = Path(root) / "remote.json"
            remote.write_text(json.dumps({"version": 1, "files": {removed_key: existing}}), encoding="utf-8")
            api.hf_hub_download.return_value = str(remote)
            bundle = Path(root) / "bundle"
            bundle.mkdir()
            (bundle / "bundle.json").write_text(json.dumps({
            "version": 1, "results": [], "active_keys": ["VoiceOfML/Test\0Other.djvu"],
            "authoritative_snapshot": True,
            }), encoding="utf-8")
            manifest, operations = publish_reader_assets.build_publish(api, "vomebook/Test", bundle)

        self.assertNotIn(removed_key, manifest["files"])
        self.assertEqual(manifest["orphans"][existing["path"]]["since"], date.today().isoformat())
        self.assertEqual(len(operations), 2)

    def test_empty_active_key_snapshot_removes_all_mappings(self):
        key = "VoiceOfML/Test\0Deleted.djvu"
        existing = {"status": "ready", "path": "objects/deleted", "source_sha256": "a" * 64}
        api = Mock()
        api.file_exists.return_value = True
        with tempfile.TemporaryDirectory() as root:
            remote = Path(root) / "remote.json"
            remote.write_text(json.dumps({"version": 1, "files": {key: existing}}), encoding="utf-8")
            api.hf_hub_download.return_value = str(remote)
            bundle = Path(root) / "bundle"
            bundle.mkdir()
            (bundle / "bundle.json").write_text(json.dumps({
                "version": 1, "results": [], "active_keys": [], "authoritative_snapshot": True,
            }), encoding="utf-8")
            manifest, _ = publish_reader_assets.build_publish(api, "vomebook/Test", bundle)
        self.assertEqual(manifest["files"], {})
        self.assertIn(existing["path"], manifest["orphans"])

    def test_reused_result_restores_orphan_without_upload(self):
        path = "objects/aa/document.pdf"
        result = {
            "key": "VoiceOfML/Test\0Moved.djvu", "status": "ready", "source_revision": "rev2",
            "source_sha256": "a" * 64, "source_bytes": 10, "source_extension": "djvu",
            "profile": "djvulibre-pdf-v1", "reader_mode": "pdf", "path": path,
            "bytes": len(b"%PDF-reused"), "sha256": hashlib.sha256(b"%PDF-reused").hexdigest(),
            "reused": True,
        }
        api = Mock()
        api.file_exists.return_value = True
        with tempfile.TemporaryDirectory() as root:
            remote = Path(root) / "remote.json"
            remote.write_text(json.dumps({"version": 1, "files": {}, "orphans": {
                path: {**result, "since": "2026-01-01"},
            }}), encoding="utf-8")
            api.hf_hub_download.return_value = str(remote)
            bundle = Path(root) / "bundle"
            artifact = bundle / path
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"%PDF-reused")
            (bundle / "bundle.json").write_text(json.dumps({
                "version": 1, "results": [result], "active_keys": [result["key"]],
                "authoritative_snapshot": True,
            }), encoding="utf-8")
            manifest, operations = publish_reader_assets.build_publish(api, "vomebook/Test", bundle)

        self.assertNotIn(path, manifest["orphans"])
        self.assertEqual(manifest["files"][result["key"]]["path"], path)
        self.assertEqual(len(operations), 2)

    def test_legacy_bundle_without_authoritative_marker_never_removes_mappings(self):
        key = "VoiceOfML/Test\0Keep.djvu"
        existing = {"status": "ready", "path": "objects/keep", "reader_mode": "pdf"}
        api = Mock()
        api.file_exists.return_value = True
        with tempfile.TemporaryDirectory() as root:
            remote = Path(root) / "remote.json"
            remote.write_text(json.dumps({"version": 1, "files": {key: existing}}), encoding="utf-8")
            api.hf_hub_download.return_value = str(remote)
            bundle = Path(root) / "bundle"
            bundle.mkdir()
            (bundle / "bundle.json").write_text(json.dumps({
                "version": 1, "results": [], "active_keys": [],
            }), encoding="utf-8")
            manifest, _ = publish_reader_assets.build_publish(api, "vomebook/Test", bundle)
        self.assertEqual(manifest["files"][key], existing)

    def test_replacing_ready_mapping_marks_old_object_orphan(self):
        key = "VoiceOfML/Test\0Changed.djvu"
        previous = {"status": "ready", "path": "objects/old", "source_sha256": "a" * 64}
        result = {
            "key": key, "status": "ready", "source_revision": "rev2", "source_sha256": "b" * 64,
            "source_bytes": 12, "source_extension": "djvu", "profile": "djvulibre-pdf-v1",
            "reader_mode": "pdf", "path": "objects/new",
        }
        api = Mock()
        api.file_exists.return_value = True
        with tempfile.TemporaryDirectory() as root:
            remote = Path(root) / "remote.json"
            remote.write_text(json.dumps({"version": 1, "files": {key: previous}}), encoding="utf-8")
            api.hf_hub_download.return_value = str(remote)
            bundle = self.make_bundle(root + "/bundle", result)
            manifest, _ = publish_reader_assets.build_publish(api, "vomebook/Test", bundle)
        self.assertIn(previous["path"], manifest["orphans"])

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

    def test_sidecar_encodes_docx_reader_mode(self):
        manifest = {"version": 1, "files": {"book": {
            "status": "ready", "reader_mode": "docx", "path": "objects/aa/source/docx-native-v1/document.docx",
        }}}
        self.assertEqual(build_reader_assets_index.build_index(manifest)["f"]["book"], {
            "s": 2, "m": "d", "p": "objects/aa/source/docx-native-v1/document.docx",
        })

    def test_sidecar_encodes_html_reader_mode(self):
        manifest = {"version": 1, "files": {"page": {
            "status": "ready", "reader_mode": "html", "path": "objects/aa/source/sanitized-html-v1/document.html",
        }}}
        self.assertEqual(build_reader_assets_index.build_index(manifest)["f"]["page"], {
            "s": 2, "m": "h", "p": "objects/aa/source/sanitized-html-v1/document.html",
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


class PruneTests(unittest.TestCase):
    def test_only_unreferenced_orphans_past_grace_period_expire(self):
        manifest = {"version": 1, "files": {
            "live": {"status": "ready", "path": "objects/live"},
        }, "orphans": {
            "objects/old": {"since": "2026-06-01"},
            "objects/new": {"since": "2026-08-20"},
            "objects/live": {"since": "2026-01-01"},
            "objects/invalid": {"since": "unknown"},
        }}
        self.assertEqual(
            prune_reader_assets.expired_orphans(manifest, date(2026, 8, 26), 30, 100),
            ["objects/old"],
        )

    def test_prune_deletes_objects_and_republishes_manifest_and_sidecar(self):
        manifest = {"version": 1, "files": {}, "orphans": {
            "objects/old": {"since": "2026-01-01"},
            "objects/keep": {"since": "2026-08-20"},
        }}
        updated, operations = prune_reader_assets.build_prune(manifest, ["objects/old"])
        self.assertNotIn("objects/old", updated["orphans"])
        self.assertIn("objects/keep", updated["orphans"])
        self.assertEqual(
            {operation.path_in_repo for operation in operations},
            {"objects/old", "manifest.json", "reader_assets.json.gz"},
        )
        self.assertEqual(sum(isinstance(operation, CommitOperationDelete) for operation in operations), 1)


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_exposes_incremental_controls_and_excludes_pdg(self):
        workflow = Path(".github/workflows/reader-assets.yml").read_text(encoding="utf-8")
        for field in ("repo:", "extension:", "limit:", "checkpoint_batches:", "retry_failed:", "force:", "dry_run:"):
            self.assertIn(field, workflow)
        self.assertNotIn("pdg", workflow.lower())
        self.assertIn("publish_search_reader_index.py", workflow)
        self.assertIn("fonts-noto-cjk", workflow)
        self.assertIn("fonts-wqy-microhei", workflow)
        self.assertIn("ghostscript", workflow)
        self.assertIn("poppler-utils", workflow)
        self.assertIn("tesseract-ocr-chi-sim", workflow)
        self.assertIn("djvulibre-bin", workflow)
        self.assertIn("doc, docx, htm, html, mobi", workflow)
        self.assertIn("htm|html) packages=()", workflow)
        self.assertIn('cron: "17 * * * *"', workflow)
        self.assertIn("inputs.limit || '20'", workflow)
        self.assertIn("inputs.checkpoint_batches || '30'", workflow)
        self.assertIn("python scripts/publish_reader_assets.py", workflow)
        self.assertIn("packages=(djvulibre-bin poppler-utils)", workflow)
        self.assertIn("packages=(calibre)", workflow)
        self.assertIn("tif|tiff) packages=(poppler-utils)", workflow)
        self.assertIn("READER_CONVERSION_WORKERS:", workflow)
        self.assertIn("steps.scan.outputs.extension == 'djvu'", workflow)
        self.assertIn('args=(--repo "${SOURCE_REPO}" --extension "${EXTENSION}"', workflow)
        self.assertIn('max_batches=1', workflow)
        self.assertIn('queue["items"] = items', workflow)
        self.assertIn("stale_count", workflow)
        self.assertIn("if: inputs.dry_run != true", workflow)
        self.assertLess(
            workflow.index("python scripts/publish_reader_assets.py"),
            workflow.index("done\n"),
        )

    def test_prune_workflow_uses_shared_concurrency_and_bounded_grace(self):
        workflow = Path(".github/workflows/prune-reader-assets.yml").read_text(encoding="utf-8")
        self.assertIn("group: reader-assets", workflow)
        self.assertIn('default: "30"', workflow)
        self.assertIn('default: "100"', workflow)
        self.assertIn("python scripts/prune_reader_assets.py", workflow)


if __name__ == "__main__":
    unittest.main()
