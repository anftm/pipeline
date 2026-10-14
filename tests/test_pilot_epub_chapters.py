import hashlib
import gzip
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from scripts import pilot_epub_chapters
from scripts.epub_chapters import _parse_package_xml


class ChmChapterPilotTests(unittest.TestCase):
    def test_failed_only_checkpoint_keys(self):
        previous = {'failures': [{'key': 'VoiceOfML/Test\0bad.chm'}]}
        self.assertEqual(pilot_epub_chapters.failed_keys(previous),
                         {'VoiceOfML/Test\0bad.chm'})

    def test_source_download_honors_hf_retry_countdown(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "source.chm"
            error = urllib.error.HTTPError(
                "https://example.test/book.chm", 429, "rate limited", {},
                io.BytesIO(b"429 Too Many Requests. Retry after 23 seconds (0/5000 requests remaining)."))
            response = unittest.mock.Mock()
            response.__enter__ = lambda value: value
            response.__exit__ = lambda *args: None
            response.read.return_value = b"CHM"
            with patch.object(pilot_epub_chapters.urllib.request, "urlopen",
                              side_effect=[error, response]), \
                    patch.object(pilot_epub_chapters.time, "sleep") as sleep:
                pilot_epub_chapters.download("https://example.test/book.chm", target, "token")
            self.assertEqual(target.read_bytes(), b"CHM")
            sleep.assert_called_once_with(23)

    def test_existing_index_only_treats_not_found_as_an_empty_index(self):
        fs = unittest.mock.Mock()
        fs.open.side_effect = FileNotFoundError("missing index")
        with patch.object(pilot_epub_chapters, "HfFileSystem", return_value=fs):
            result = pilot_epub_chapters.existing_index("bucket", "token", "chapters/ebook/epub")
        self.assertEqual(result["files"], [])

        for error in (PermissionError("denied"), OSError("network read failed")):
            fs = unittest.mock.Mock()
            fs.open.side_effect = error
            with patch.object(pilot_epub_chapters, "HfFileSystem", return_value=fs):
                with self.assertRaises(type(error)):
                    pilot_epub_chapters.existing_index("bucket", "token", "chapters/ebook/epub")

        fs = unittest.mock.Mock()
        fs.open.return_value = io.BytesIO(b"[]")
        with patch.object(pilot_epub_chapters, "HfFileSystem", return_value=fs):
            with self.assertRaisesRegex(ValueError, "invalid ebook chapter index"):
                pilot_epub_chapters.existing_index("bucket", "token", "chapters/ebook/epub")

    def test_shared_epub_index_merge_preserves_other_extensions(self):
        epub = {"key": "VoiceOfML/Test\0book.epub", "source_extension": "epub",
                "source_revision": "old-epub"}
        docx = {"key": "VoiceOfML/Test\0book.docx", "source_extension": "docx",
                "extension": "epub", "source_revision": "docx-rev"}
        merged = pilot_epub_chapters.merge_index_entries({"files": [epub]}, [docx])
        self.assertEqual(merged[epub["key"]], epub)
        self.assertEqual(merged[docx["key"]], docx)

    def test_failures_are_checkpoints_until_explicit_retry_and_merge_across_extensions(self):
        epub_key = "VoiceOfML/Test\0old.epub"
        doc_key = "VoiceOfML/Test\0old.doc"
        docx_key = "VoiceOfML/Test\0book.docx"
        previous = {"files": [{"key": epub_key, "source_revision": "epub-rev"}],
                    "failures": [{"key": doc_key, "source_revision": "doc-rev", "error": "bad doc"},
                                 {"key": docx_key, "source_revision": "docx-rev", "error": "old failure"}]}
        self.assertEqual(pilot_epub_chapters.completed_revisions(previous)[doc_key], "doc-rev")
        self.assertNotIn(doc_key, pilot_epub_chapters.completed_revisions(previous, retry_failures=True))
        success = {"key": docx_key, "source_revision": "docx-rev", "extension": "epub"}
        retry = {"key": doc_key, "source_revision": "doc-rev-2", "error": "still invalid"}
        result = pilot_epub_chapters.merged_index_payload(previous, [success], [retry])
        self.assertEqual({item["key"] for item in result["files"]}, {epub_key, docx_key})
        self.assertEqual(result["failures"], [retry])

    def test_chm_source_text_profile_requeues_existing_chapter_streams_once(self):
        key = "VoiceOfML/Test\0legacy.chm"
        previous = {"files": [{"key": key, "source_revision": "same-revision",
                               "profile": "epub-chapters-v8-bucket"}], "failures": []}
        self.assertEqual(pilot_epub_chapters.chapter_profile("chm"), pilot_epub_chapters.CHM_PROFILE)
        self.assertNotIn(key, pilot_epub_chapters.completed_revisions(
            previous, profile=pilot_epub_chapters.CHM_PROFILE))
        upgraded = {"files": [{"key": key, "source_revision": "same-revision",
                               "profile": pilot_epub_chapters.CHM_PROFILE}], "failures": []}
        self.assertEqual(pilot_epub_chapters.completed_revisions(
            upgraded, profile=pilot_epub_chapters.CHM_PROFILE)[key], "same-revision")

    def test_failure_only_batch_can_publish_a_durable_failure_checkpoint(self):
        previous = {"files": [], "failures": []}
        failure = {"key": "VoiceOfML/Test\0bad.doc", "source_revision": "rev", "error": "invalid"}
        payload = pilot_epub_chapters.merged_index_payload(previous, [], [failure])
        self.assertEqual(payload["files"], [])
        self.assertEqual(payload["failures"], [failure])

    def test_office_upload_processing_root_is_bucket_qualified(self):
        entry = {"key": "VoiceOfML/Test\0book.docx", "source_sha256": "a" * 64,
                 "root": "chapters/ebook/epub/" + "b" * 64 + "/" + "c" * 16}
        bucket = "vomebook/reader-assets-v2"
        path, record = pilot_epub_chapters.processing_record_for_entry(
            entry, bucket, {"repository": "anftm/pipeline", "run_id": "123"})
        self.assertRegex(path, r"^reader-index/processing/office-chapters-[0-9a-f]{32}\.json$")
        self.assertEqual(record["status"], "processing")
        self.assertEqual(record["resources"], [{"bucket": bucket, "root": entry["root"]}])

    def test_package_repair_does_not_declare_reserved_xmlns_prefix(self):
        raw = (b'<package xmlns="urn:opf">'
               b'<metadata xmlns:dc="urn:dc"><dc:date xsi:type="dcterms:W3CDTF">x</dc:date>'
               b'</metadata></package>')
        root = _parse_package_xml(raw)
        self.assertEqual(root.tag, "{urn:opf}package")

    def test_chm_publishes_chapter_tree_and_native_epub_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source_bytes = b"source chm"
            source_digest = hashlib.sha256(source_bytes).hexdigest()
            item = {"repo": "VoiceOfML/Test", "path": "book.chm", "revision": "rev",
                    "source_bytes": len(source_bytes), "extension": "chm"}

            def download(_url, target, _token):
                target.write_bytes(source_bytes)

            def convert(queue_item, output):
                chapter_root = output / "objects" / "chapters" / "epub-chapters"
                (chapter_root / "chapters").mkdir(parents=True)
                (chapter_root / "chapters" / "chapter-0001.xhtml").write_text("<p>body</p>")
                (chapter_root / "epub-search-index.json.gz").write_bytes(b"search")
                manifest = {"version": 1, "kind": "epub-chapters", "chapters": [{
                    "index": 1, "title": "Book", "path": "chapters/chapter-0001.xhtml",
                    "bytes": 11, "sha256": "a" * 64,
                }]}
                (chapter_root / "chapter-manifest.json").write_text(json.dumps(manifest))
                native = output / "objects" / "document.epub"
                native.parent.mkdir(parents=True, exist_ok=True)
                native.write_bytes(b"native epub")
                return {"path": "objects/document.epub", "profile": queue_item["profile"],
                        "chapter_manifest": "objects/chapters/epub-chapters/chapter-manifest.json",
                        "chapter_bundle_error": None}

            with patch.object(pilot_epub_chapters, "download", side_effect=download), \
                    patch.object(pilot_epub_chapters, "convert_item", side_effect=convert):
                entry, uploads = pilot_epub_chapters.build_one(item, work / "book", "token", "vomebook/reader-assets-v2")

            self.assertEqual(entry["source_sha256"], source_digest)
            self.assertEqual(entry["source_extension"], "chm")
            self.assertEqual(entry["profile"], pilot_epub_chapters.CHM_PROFILE)
            self.assertTrue(entry["fallback"].startswith("native/ebook/chm/"))
            self.assertTrue(any(path.endswith("chapter-manifest.json") and path.startswith("chapters/ebook/chm/")
                                for path in uploads))
            self.assertIn(entry["fallback"], uploads)

    def test_office_sources_share_epub_chapter_namespace_and_bound_flow(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source_bytes = b"office source"
            item = {"repo": "VoiceOfML/Test", "path": "book.docx", "revision": "rev",
                    "source_bytes": len(source_bytes), "extension": "docx"}
            bundle = work / "book" / "bundle"
            (bundle / "chapters").mkdir(parents=True)
            chapter = bundle / "chapters" / "chapter-0001.xhtml"
            chapter.write_text("<html><body><h1>Office</h1><p>body</p></body></html>")
            search_bytes = gzip.compress(b"search", mtime=0)
            (bundle / "epub-search-index.json.gz").write_bytes(search_bytes)
            (bundle / "chapter-manifest.json").write_text(json.dumps({
                "version": 1, "kind": "epub-chapters", "chapters": [{
                    "index": 1, "title": "Office", "path": "chapters/chapter-0001.xhtml",
                    "bytes": chapter.stat().st_size, "sha256": hashlib.sha256(chapter.read_bytes()).hexdigest(),
                }], "search_index": {"path": "epub-search-index.json.gz", "bytes": len(search_bytes),
                                      "sha256": hashlib.sha256(search_bytes).hexdigest()},
            }))
            def download(_url, target, _token):
                target.write_bytes(source_bytes)
            with patch.object(pilot_epub_chapters, "download", side_effect=download), \
                    patch.object(pilot_epub_chapters, "office_to_epub", return_value=work / "office.epub"), \
                    patch.object(pilot_epub_chapters, "build_bundle", return_value=json.loads(
                        (bundle / "chapter-manifest.json").read_text())):
                entry, uploads = pilot_epub_chapters.build_one(item, work / "book", "token", "vomebook/reader-assets-v2")
            self.assertEqual(entry["extension"], "epub")
            self.assertTrue(entry["root"].startswith("chapters/ebook/epub/"))
            self.assertTrue(any(path.startswith(entry["root"] + "/") for path in uploads))

    def test_office_converter_uses_bounded_calibre_flow(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.docx"
            source.write_bytes(b"docx")
            output = work / "document.epub"
            with patch.object(pilot_epub_chapters.zipfile, "is_zipfile", return_value=True), \
                    patch.object(pilot_epub_chapters.subprocess, "run") as run:
                run.side_effect = lambda command, **kwargs: output.write_bytes(b"epub")
                self.assertEqual(pilot_epub_chapters.office_to_epub(source, "docx", work), output)
            command = run.call_args.args[0]
            self.assertIn("--flow-size", command)
            self.assertEqual(command[command.index("--flow-size") + 1], str(pilot_epub_chapters.OFFICE_FLOW_SIZE_KB))

    def test_html_document_with_doc_suffix_skips_libreoffice(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.doc"
            source.write_bytes(b"\xef\xbb\xbf<html><body><p>HTML document</p><script>unsafe()</script></body></html>")
            output = work / "document.epub"

            def convert(command, **_kwargs):
                Path(command[2]).write_bytes(b"epub")

            with patch.object(pilot_epub_chapters, "run_libreoffice",
                              side_effect=AssertionError("HTML input must not go through LibreOffice")), \
                    patch.object(pilot_epub_chapters.subprocess, "run", side_effect=convert) as run:
                self.assertEqual(pilot_epub_chapters.office_to_epub(source, "doc", work), output)

            command = run.call_args.args[0]
            self.assertEqual(Path(command[1]).name, "source.html")
            normalized = Path(command[1]).read_text(encoding="utf-8")
            self.assertIn("HTML document", normalized)
            self.assertNotIn("<script", normalized.lower())

    def test_legacy_doc_conversion_timeout_falls_back_to_antiword_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.doc"
            source.write_bytes(b"legacy OLE DOC")
            output = work / "document.epub"

            def antiword_fallback(_source, target):
                target.write_text("<html><body><pre>完整提取的正文</pre></body></html>", encoding="utf-8")

            def convert(command, **_kwargs):
                Path(command[2]).write_bytes(b"fallback epub")

            with patch.object(pilot_epub_chapters, "run_libreoffice",
                              side_effect=pilot_epub_chapters.subprocess.TimeoutExpired(["libreoffice"], 600)), \
                    patch.object(pilot_epub_chapters, "doc_to_html_fallback", side_effect=antiword_fallback) as antiword, \
                    patch.object(pilot_epub_chapters.subprocess, "run", side_effect=convert) as run:
                self.assertEqual(pilot_epub_chapters.office_to_epub(source, "doc", work), output)

            antiword.assert_called_once_with(source, work / "antiword-fallback.html")
            self.assertEqual(Path(run.call_args.args[0][1]).name, "antiword-fallback.html")
            self.assertEqual(output.read_bytes(), b"fallback epub")

    def test_large_binary_doc_gets_extended_conversion_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "large.doc"
            with source.open("wb") as handle:
                handle.truncate(pilot_epub_chapters.OFFICE_LARGE_DOC_BYTES + 1)
            normalized = work / "normalized.docx"
            normalized.write_bytes(b"docx")
            output = work / "document.epub"

            def convert(command, **_kwargs):
                Path(command[2]).write_bytes(b"epub")

            with patch.object(pilot_epub_chapters, "run_libreoffice") as libreoffice, \
                    patch.object(pilot_epub_chapters, "converted_output", return_value=normalized), \
                    patch.object(pilot_epub_chapters.subprocess, "run", side_effect=convert) as run:
                self.assertEqual(pilot_epub_chapters.office_to_epub(source, "doc", work), output)

            self.assertEqual(
                libreoffice.call_args.kwargs["timeout_seconds"],
                pilot_epub_chapters.OFFICE_LARGE_DOC_TIMEOUT_SECONDS)
            self.assertEqual(
                run.call_args.kwargs["timeout"],
                pilot_epub_chapters.OFFICE_LARGE_DOC_TIMEOUT_SECONDS)

    def test_large_binary_doc_timeout_does_not_downgrade_to_antiword_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "large.doc"
            with source.open("wb") as handle:
                handle.truncate(pilot_epub_chapters.OFFICE_LARGE_DOC_BYTES + 1)
            timeout = pilot_epub_chapters.subprocess.TimeoutExpired(["libreoffice"], 3600)
            with patch.object(pilot_epub_chapters, "run_libreoffice", side_effect=timeout), \
                    patch.object(pilot_epub_chapters, "doc_to_html_fallback") as antiword:
                with self.assertRaises(pilot_epub_chapters.subprocess.TimeoutExpired):
                    pilot_epub_chapters.office_to_epub(source, "doc", work)
            antiword.assert_not_called()

    def test_legacy_native_doc_html_is_reflowed_before_calibre(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.doc"
            source.write_bytes(b"legacy source")
            output = work / "document.epub"
            raw = ("<html><body><pre>\n\nЗаголовок\n\nСтрока 1\nСтрока 2\n\n[pic]"
                   "</pre></body></html>").encode("utf-8")

            def convert(command, **_kwargs):
                Path(command[2]).write_bytes(b"epub")

            with patch.object(pilot_epub_chapters.subprocess, "run", side_effect=convert) as run:
                self.assertEqual(
                    pilot_epub_chapters.office_to_epub(
                        source, "doc", work, legacy_html=raw, title="1942"), output)

            generated = Path(run.call_args.args[0][1]).read_text(encoding="utf-8")
            self.assertEqual(Path(run.call_args.args[0][1]).name, "legacy-native.html")
            self.assertIn("font-family:serif", generated)
            self.assertIn("\u0421\u0442\u0440\u043e\u043a\u0430 1 \u0421\u0442\u0440\u043e\u043a\u0430 2", generated)
            self.assertIn("\uff08\u56fe\u7247\uff09", generated)
            self.assertNotIn("<pre", generated.lower())

    def test_doc_build_reads_existing_native_html_before_retrying_libreoffice(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source_bytes = b"legacy source"
            source_digest = hashlib.sha256(source_bytes).hexdigest()
            item = {"repo": "VoiceOfML/Test", "path": "book.doc", "revision": "rev",
                    "source_bytes": len(source_bytes), "extension": "doc"}
            bundle = work / "book" / "bundle"
            (bundle / "chapters").mkdir(parents=True)
            chapter = bundle / "chapters" / "chapter-0001.xhtml"
            chapter.write_text("<html><body><p>body</p></body></html>")
            search_bytes = gzip.compress(b"search", mtime=0)
            (bundle / "epub-search-index.json.gz").write_bytes(search_bytes)
            manifest = {"version": 1, "kind": "epub-chapters", "chapters": [{
                "index": 1, "title": "Book", "path": "chapters/chapter-0001.xhtml",
                "bytes": chapter.stat().st_size, "sha256": hashlib.sha256(chapter.read_bytes()).hexdigest(),
            }], "search_index": {"path": "epub-search-index.json.gz", "bytes": len(search_bytes),
                                 "sha256": hashlib.sha256(search_bytes).hexdigest()}}
            (bundle / "chapter-manifest.json").write_text(json.dumps(manifest))

            def download(_url, target, _token):
                target.write_bytes(source_bytes)

            with patch.object(pilot_epub_chapters, "download", side_effect=download), \
                    patch.object(pilot_epub_chapters, "read_bytes", return_value=b"<pre>native</pre>") as read, \
                    patch.object(pilot_epub_chapters, "office_to_epub", return_value=work / "office.epub") as convert, \
                    patch.object(pilot_epub_chapters, "build_bundle", return_value=manifest):
                pilot_epub_chapters.build_one(
                    item, work / "book", "token", "vomebook/reader-assets-v2")

            read.assert_called_once_with(
                f"documents/office/doc/{source_digest}/document.html", "token", "vomebook/reader-assets-v2")
            self.assertEqual(convert.call_args.kwargs["legacy_html"], b"<pre>native</pre>")

    def test_office_chapter_bundle_obeys_browser_and_search_budgets(self):
        valid = {"chapters": [{"bytes": 7 * 1024 * 1024}],
                 "search_index": {"bytes": 64 * 1024 * 1024}}
        pilot_epub_chapters.validate_office_chapter_budget(valid)
        for value in (
            {"chapters": [{"bytes": 8 * 1024 * 1024}], "search_index": {"bytes": 1}},
            {"chapters": [{"bytes": 7 * 1024 * 1024}] * 72, "search_index": {"bytes": 1}},
            {"chapters": [{"bytes": 1}], "search_index": {"bytes": 65 * 1024 * 1024}},
        ):
            with self.assertRaises(ValueError):
                pilot_epub_chapters.validate_office_chapter_budget(value)

    def test_office_search_index_obeys_expanded_worker_limit(self):
        pilot_epub_chapters.validate_office_search_index_expanded_size(
            pilot_epub_chapters.OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES)
        with self.assertRaisesRegex(ValueError, "Worker text limit"):
            pilot_epub_chapters.validate_office_search_index_expanded_size(
                pilot_epub_chapters.OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES + 1)

    def test_encrypted_office_source_is_decrypted_with_its_source_password(self):
        captured = {}

        class FakeDocument:
            def is_encrypted(self):
                return True

            def load_key(self, password=None):
                captured["password"] = password

            def decrypt(self, handle):
                handle.write(b"plaintext")

        class FakeMsoffcrypto:
            @staticmethod
            def OfficeFile(handle):
                return FakeDocument()

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.docx"
            source.write_bytes(b"encrypted-package")
            with patch.dict("sys.modules", {"msoffcrypto": FakeMsoffcrypto}):
                decrypted = pilot_epub_chapters.decrypt_office_source(source, "docx", "123", work)
            self.assertEqual(decrypted, work / "decrypted.docx")
            self.assertEqual(decrypted.read_bytes(), b"plaintext")
            self.assertEqual(captured["password"], "123")

    def test_office_source_without_a_password_or_encryption_is_untouched(self):
        class PlainDocument:
            def is_encrypted(self):
                return False

        class PlainMsoffcrypto:
            @staticmethod
            def OfficeFile(handle):
                return PlainDocument()

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.docx"
            source.write_bytes(b"docx")
            self.assertEqual(pilot_epub_chapters.decrypt_office_source(source, "docx", "", work), source)
            self.assertEqual(pilot_epub_chapters.decrypt_office_source(source, "odt", "123", work), source)
            with patch.dict("sys.modules", {"msoffcrypto": PlainMsoffcrypto}):
                self.assertEqual(pilot_epub_chapters.decrypt_office_source(source, "docx", "123", work), source)


if __name__ == "__main__":
    unittest.main()
