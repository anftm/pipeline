import unittest

from scripts.report_office_chapter_coverage import OFFICE_PROFILE, summarize


class OfficeChapterCoverageTests(unittest.TestCase):
    def test_gate_requires_current_revision_and_verified_profile_shape(self):
        doc = {"key": "repo\0one.doc", "repo": "repo", "path": "one.doc",
               "extension": "doc", "source_revision": "r1"}
        docx = {"key": "repo\0two.docx", "repo": "repo", "path": "two.docx",
                "extension": "docx", "source_revision": "r2"}
        current = {"key": doc["key"], "source_revision": "r1", "source_extension": "doc",
                   "extension": "epub", "profile": OFFICE_PROFILE,
                   "root": "chapters/ebook/epub/" + "a" * 64 + "/" + "b" * 16,
                   "manifest": "chapters/ebook/epub/" + "a" * 64 + "/" + "b" * 16 + "/chapter-manifest.json",
                   "search_index": "chapters/ebook/epub/" + "a" * 64 + "/" + "b" * 16 + "/epub-search-index.json.gz",
                   "chapter_count": 1, "source_sha256": "c" * 64}
        report = summarize([doc, docx], {"files": [current], "failures": []})
        self.assertFalse(report["complete"])
        self.assertEqual(report["totals"], {"mapped": 1, "pending": 1, "failed": 0,
                         "stale-mapping": 0, "invalid-mapping": 0,
                         "missing-revision": 0, "total": 2})
        self.assertEqual(report["sources"][1]["status"], "pending")

    def test_failed_stale_and_malformed_mappings_are_not_counted_as_complete(self):
        source = {"key": "repo\0one.odt", "repo": "repo", "path": "one.odt",
                  "extension": "odt", "source_revision": "current"}
        report = summarize([source], {"files": [{"key": source["key"], "source_revision": "old"}],
                                     "failures": [{"key": source["key"], "source_revision": "current",
                                                   "error": "conversion error"}]})
        self.assertFalse(report["complete"])
        self.assertEqual(report["sources"][0]["status"], "failed")

        bad = {"key": source["key"], "source_revision": "current", "source_extension": "odt",
               "extension": "odt", "profile": OFFICE_PROFILE}
        report = summarize([source], {"files": [bad], "failures": []})
        self.assertEqual(report["sources"][0]["status"], "invalid-mapping")
        self.assertFalse(report["complete"])

    def test_active_source_without_repository_revision_blocks_retirement_gate(self):
        source = {"key": "repo\0one.rtf", "repo": "repo", "path": "one.rtf",
                  "extension": "rtf", "source_revision": ""}
        report = summarize([source], {"files": [], "failures": []})
        self.assertFalse(report["complete"])
        self.assertEqual(report["totals"]["missing-revision"], 1)


if __name__ == "__main__":
    unittest.main()
