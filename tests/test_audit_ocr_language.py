import unittest

from scripts import audit_ocr_language as audit


def entry(language, status="ready"):
    return {"status": status, "ocr_language": language}


class OcrLanguageAuditTests(unittest.TestCase):
    def test_healthy_chinese_book_is_not_flagged(self):
        text = "这是一本中文书。" * 40
        row = audit.judge("k", entry("ch"), [text, text])
        self.assertEqual(row["flags"], [])
        self.assertEqual(row["observed_script"], "Han")

    def test_russian_book_published_without_cyrillic_is_flagged(self):
        row = audit.judge("k", entry("ru"), ["Latin text only " * 40])
        self.assertIn("script-mismatch", row["flags"])
        self.assertEqual(row["observed_script"], "Latin")

    def test_chinese_book_with_cyrillic_text_is_flagged(self):
        row = audit.judge("k", entry("ch"), ["Русский текст " * 40])
        self.assertIn("script-mismatch", row["flags"])

    def test_japanese_book_without_kana_is_flagged(self):
        row = audit.judge("k", entry("japan"), ["漢字文章漢字文章" * 40])
        self.assertIn("script-mismatch", row["flags"])
        self.assertEqual(row["observed_script"], "Han")

    def test_english_book_with_chinese_text_is_flagged(self):
        row = audit.judge("k", entry("en"), ["中文正文" * 100])
        self.assertIn("script-mismatch", row["flags"])

    def test_stray_kana_on_a_chinese_page_does_not_win(self):
        text = "中文正文" * 100 + "の"
        row = audit.judge("k", entry("ch"), [text])
        self.assertEqual(row["observed_script"], "Han")
        self.assertEqual(row["flags"], [])

    def test_empty_or_sparse_text_is_flagged(self):
        row = audit.judge("k", entry("ch"), ["短", "", ""])
        self.assertIn("low-text-density", row["flags"])
        self.assertIn("mostly-empty-pages", row["flags"])

    def test_audit_skips_non_ready_books_and_collects_flags(self):
        entries = {"a": entry("ch"), "b": entry("ru"), "c": entry("ch", "failed")}
        texts = {"a": ["中文" * 200], "b": ["Latin " * 200], "c": ["中文" * 200]}
        report = audit.audit(entries, texts)
        self.assertEqual(report["checked"], 2)
        self.assertEqual(list(report["flags"]), ["b"])

    def test_draft_overrides_only_covers_confirmed_script_mismatches(self):
        entries = {"a": entry("ru"), "b": entry("ch")}
        texts = {"a": ["Latin " * 200], "b": ["中文" * 200]}
        report = audit.audit(entries, texts)
        draft = audit.draft_overrides(report["flags"], entries)
        self.assertEqual(draft["overrides"]["a"]["language"], "en")
        self.assertNotIn("b", draft["overrides"])


if __name__ == "__main__":
    unittest.main()