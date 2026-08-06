import io
import json
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import audit_bha_parsed


class AuditBhaParsedTests(unittest.TestCase):
    def make_snapshot(self, root: Path, files: dict[str, object]) -> Path:
        snapshot = root / "snapshot.tar.gz"
        with tarfile.open(snapshot, "w:gz") as archive:
            for path, value in files.items():
                raw = json.dumps(value, ensure_ascii=False).encode()
                member = tarfile.TarInfo(f"repo-commit/{path}")
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
        return snapshot

    def test_parse_archives_supports_ranges(self):
        self.assertEqual(audit_bha_parsed.parse_archives("1,3-5"), [1, 3, 4, 5])

    def test_mojibake_rule_does_not_flag_valid_french(self):
        self.assertIsNone(audit_bha_parsed.MOJIBAKE_RE.search("écrasez l’infâme"))
        self.assertIsNotNone(audit_bha_parsed.MOJIBAKE_RE.search("cafÃ©"))

    def test_audit_detects_complete_and_unclosed_ocr_markup(self):
        findings = audit_bha_parsed.Findings(5)
        audit_bha_parsed.audit_text({"text": "作者〖HH/换行〗DW：单位"}, "article.json", findings)
        self.assertEqual(findings.counts["ocr_markup"], 1)
        findings = audit_bha_parsed.Findings(5)
        audit_bha_parsed.audit_text({"text": "图片〖ZQ/总期"}, "article.json", findings)
        self.assertEqual(findings.counts["ocr_markup_unclosed"], 1)
        findings = audit_bha_parsed.Findings(5)
        audit_bha_parsed.audit_text({"text": "12968〖-ZQ/总期〗〖JZ；加重〗"}, "article.json", findings)
        self.assertEqual(findings.counts["ocr_markup"], 1)

    def test_request_json_retries_transient_network_errors(self):
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"ok": true}')
        with patch.object(audit_bha_parsed.urllib.request, "urlopen", side_effect=[
                urllib.error.URLError("temporary"), response,
        ]) as urlopen, patch.object(audit_bha_parsed.time, "sleep"):
            self.assertEqual(audit_bha_parsed.request_json("https://example.test"), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)

    def test_parsed_commit_uses_git_without_token(self):
        result = MagicMock(stdout=f"{'a' * 40}\trefs/heads/parsed\n")
        failure = audit_bha_parsed.subprocess.CalledProcessError(128, ["git"])
        with patch.object(audit_bha_parsed.subprocess, "run", side_effect=[failure, result]) as run, \
                patch.object(audit_bha_parsed.time, "sleep"):
            self.assertEqual(audit_bha_parsed.parsed_commit("owner", 3), "a" * 40)
        self.assertEqual(run.call_count, 2)
        self.assertIn("owner/banned-historical-archives3.git", run.call_args.args[0][2])

    def test_audit_counts_loader_documents_and_quality_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(Path(directory), {
                "collection/book/book.metadata": {"name": "Book"},
                "collection/book/aaa/same.json": {
                    "title": "One", "authors": ["(Author)", "img=504n0101aa>", "[Office]", "(Name"],
                    "parts": [{"text": "&lt;span&gt;body&lt;/span&gt;"}],
                },
                "collection/book/bbb/same.json": {
                    "title": "Two", "parts": [{"text": "body\u200b"}],
                },
                "collection/book/aaa/same.tags": [],
                "collection/orphan/aaa/lost.json": {"title": "Lost", "parts": []},
            })
            report = audit_bha_parsed.audit_snapshot(snapshot, 7, "a" * 40)
        self.assertEqual(report["article_files"], 3)
        self.assertEqual(report["indexed_documents"], 2)
        self.assertEqual(report["unique_doc_ids"], 1)
        self.assertEqual(report["overwritten_documents"], 1)
        self.assertEqual(report["findings"]["duplicate_doc_id"], 1)
        self.assertEqual(report["findings"]["orphan_article"], 1)
        self.assertEqual(report["findings"]["wrapped_author"], 1)
        self.assertEqual(report["findings"]["author_image_placeholder"], 1)
        self.assertEqual(report["findings"]["square_bracket_author"], 1)
        self.assertEqual(report["findings"]["unbalanced_author_parenthesis"], 1)
        self.assertEqual(report["findings"]["html_entity"], 1)
        self.assertEqual(report["findings"]["zero_width"], 1)
        self.assertEqual(report["findings"]["empty_content"], 1)

    def test_audit_flags_invalid_metadata_and_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.make_snapshot(Path(directory), {
                "collection/book/book.metadata": [],
                "collection/book/aaa/id.json": {
                    "title": "Title", "dates": [{"year": 1966, "month": 13}],
                    "parts": [{"text": "text"}],
                },
            })
            report = audit_bha_parsed.audit_snapshot(snapshot, 1, "b" * 40)
        self.assertEqual(report["indexed_documents"], 0)
        self.assertEqual(report["findings"]["metadata_not_object"], 1)
        self.assertEqual(report["findings"]["orphan_article"], 1)
        self.assertEqual(report["findings"]["invalid_date"], 1)


if __name__ == "__main__":
    unittest.main()
