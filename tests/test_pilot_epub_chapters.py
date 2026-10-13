import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import pilot_epub_chapters
from scripts.epub_chapters import _parse_package_xml


class ChmChapterPilotTests(unittest.TestCase):
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
            (bundle / "epub-search-index.json.gz").write_bytes(b"search")
            (bundle / "chapter-manifest.json").write_text(json.dumps({
                "version": 1, "kind": "epub-chapters", "chapters": [{
                    "index": 1, "title": "Office", "path": "chapters/chapter-0001.xhtml",
                    "bytes": chapter.stat().st_size, "sha256": hashlib.sha256(chapter.read_bytes()).hexdigest(),
                }], "search_index": {"path": "epub-search-index.json.gz", "bytes": 6,
                                      "sha256": hashlib.sha256(b"search").hexdigest()},
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


if __name__ == "__main__":
    unittest.main()
