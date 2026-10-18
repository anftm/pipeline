import unittest

from scripts import build_reader_assets_v2_sidecar as sidecar
from scripts.reader_lifecycle import processing_record


class ReaderSidecarHandoffTests(unittest.TestCase):
    def test_sidecar_handoff_scans_all_ebook_chapter_indexes_including_chm(self):
        indexes = sidecar.ebook_chapter_indexes("vomebook/reader-assets-v2")
        self.assertIn(("vomebook/reader-assets-v2", "chapters/ebook/chm/index.json"), indexes)
        self.assertEqual(len(indexes), 5)

    def test_only_new_processing_roots_are_published_as_released(self):
        bucket = "vomebook/reader-assets-v2"
        root = "chapters/ebook/epub/" + "a" * 64 + "/" + "b" * 16
        path = "reader-index/processing/office-chapters-" + "c" * 32 + ".json"
        entry = {"key": "VoiceOfML/Test\0book.docx", "manifest": root + "/chapter-manifest.json",
                 "processing_roots": [path]}
        record = processing_record([{"bucket": bucket, "root": root}], "office-chapter-upload", {})
        values = {path: record}
        written = []
        count = sidecar.release_processing_roots(
            [{"files": [entry]}], "d" * 32, values.__getitem__,
            lambda name, value: (written.append(name), values.__setitem__(name, value)))
        self.assertEqual(count, 1)
        self.assertEqual(written, [path])
        self.assertEqual(values[path]["status"], "released")
        self.assertEqual(values[path]["generation"], "d" * 32)

        written.clear()
        count = sidecar.release_processing_roots(
            [{"files": [entry]}], "e" * 32, values.__getitem__,
            lambda name, value: written.append(name))
        self.assertEqual(count, 0)
        self.assertEqual(written, [])

    def test_verified_text_roots_use_the_same_explicit_catalog_handoff(self):
        bucket = "vomebook/reader-assets-v2"
        root = "documents/text/txt/" + "a" * 64
        path = "reader-index/processing/text-" + "b" * 32 + ".json"
        entry = {"key": "VoiceOfML/Test\0book.txt", "object": root + "/document.txt",
                 "processing_roots": [path]}
        record = processing_record([{"bucket": bucket, "root": root}], "text-utf8-upload", {})
        written = []
        count = sidecar.release_processing_roots(
            [{"files": [entry]}], "c" * 32, lambda _: record,
            lambda name, value: written.append((name, value)))
        self.assertEqual(count, 1)
        self.assertEqual(written[0][1]["status"], "released")


if __name__ == "__main__":
    unittest.main()
