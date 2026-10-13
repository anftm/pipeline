import gzip
import hashlib
import json
import unittest
from unittest.mock import patch

from scripts import report_office_chapter_coverage as coverage
from scripts.report_office_chapter_coverage import (
    OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES, OFFICE_PROFILE, summarize,
    verify_bundle_payload, verify_processing_payload,
)


class OfficeChapterCoverageTests(unittest.TestCase):
    def test_gate_requires_current_revision_and_verified_profile_shape(self):
        doc = {"key": "repo\0one.doc", "repo": "repo", "path": "one.doc",
               "extension": "doc", "source_revision": "r1"}
        docx = {"key": "repo\0two.docx", "repo": "repo", "path": "two.docx",
                "extension": "docx", "source_revision": "r2"}
        current = {"key": doc["key"], "source_revision": "r1", "source_extension": "doc",
                   "extension": "epub", "profile": OFFICE_PROFILE,
                   "bucket": "vomebook/reader-assets-v2",
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

    def test_complete_search_bundle_checks_manifest_identity_and_compressed_digest(self):
        chapter_body = b"chapter body"
        chapter = {"index": 1, "title": "Chapter", "path": "chapters/chapter-0001.xhtml",
                   "bytes": len(chapter_body), "sha256": hashlib.sha256(chapter_body).hexdigest()}
        search_raw = gzip.compress(json.dumps({"version": 1, "kind": "epub-search-index",
                                             "chapters": [{"index": 1, "title": "Chapter",
                                                           "path": chapter["path"], "text": "needle"}]}
                                   ).encode(), mtime=0)
        manifest_raw = json.dumps({"version": 1, "kind": "epub-chapters", "chapters": [chapter],
                                   "search_index": {"path": "epub-search-index.json.gz",
                                                    "bytes": len(search_raw),
                                                    "sha256": hashlib.sha256(search_raw).hexdigest()}}).encode()
        result = verify_bundle_payload({"chapter_count": 1}, manifest_raw, search_raw, chapter_body)
        self.assertEqual(result["chapter_count"], 1)
        with self.assertRaisesRegex(ValueError, "checksum"):
            verify_bundle_payload({"chapter_count": 1}, manifest_raw, search_raw + b"x", chapter_body)
        with self.assertRaisesRegex(ValueError, "first chapter"):
            verify_bundle_payload({"chapter_count": 1}, manifest_raw, search_raw, b"wrong chapter")
        manifest = json.loads(manifest_raw)
        expanded_search = gzip.compress(json.dumps({"version": 1, "kind": "epub-search-index",
            "chapters": [{"index": 1, "title": "Chapter", "path": chapter["path"], "text": "123456"}]}).encode(), mtime=0)
        manifest["search_index"]["bytes"] = len(expanded_search)
        manifest["search_index"]["sha256"] = hashlib.sha256(expanded_search).hexdigest()
        with patch.object(coverage, "OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "expanded-text limit"):
                verify_bundle_payload({"chapter_count": 1}, json.dumps(manifest).encode(),
                                      expanded_search, chapter_body)
        self.assertEqual(OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES, 256 * 1024 * 1024)

    def test_processing_handoff_must_be_released_for_the_chapter_root(self):
        entry = {"root": "chapters/ebook/epub/" + "a" * 64 + "/" + "b" * 16}
        record = {"kind": "reader-processing-roots", "status": "released",
                  "generation": "c" * 32,
                  "resources": [{"bucket": "vomebook/reader-assets-v2", "root": entry["root"]}]}
        self.assertEqual(verify_processing_payload(record, entry, "vomebook/reader-assets-v2"), "c" * 32)
        for changed in ({**record, "status": "processing"},
                        {**record, "resources": []}):
            with self.assertRaises(ValueError):
                verify_processing_payload(changed, entry, "vomebook/reader-assets-v2")


if __name__ == "__main__":
    unittest.main()
