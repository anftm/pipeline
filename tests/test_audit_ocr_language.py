import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

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

    def test_framed_page_is_flagged_but_text_page_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boxed = root / "boxed.png"
            image = Image.new("L", (800, 1100), 255)
            draw = ImageDraw.Draw(image)
            for top, bottom in ((60, 500), (540, 1040)):
                draw.rectangle([60, top, 760, bottom], outline=0, width=6)
                for y in range(top + 40, bottom - 30, 30):
                    draw.rectangle([90, y, 730, y + 12], fill=0)
            image.save(boxed)
            text = root / "text.png"
            image = Image.new("L", (800, 1100), 255)
            draw = ImageDraw.Draw(image)
            for y in range(80, 1030, 26):
                for x in range(80, 700, 18):
                    draw.rectangle([x, y, x + 12, y + 14], fill=0)
            image.save(text)
            self.assertTrue(audit.frame_lines(boxed)["boxed"])
            self.assertFalse(audit.frame_lines(text)["boxed"])

    def test_audit_skips_non_ready_books_and_collects_flags(self):
        entries = {"a": entry("ch"), "b": entry("ru"), "c": entry("ch", "failed")}
        texts = {"a": ["中文" * 200], "b": ["Latin " * 200], "c": ["中文" * 200]}
        report = audit.audit(entries, texts)
        self.assertEqual(report["checked"], 2)
        self.assertEqual(list(report["flags"]), ["b"])

    def test_flag_updates_include_wrong_overrides_and_skip_already_flagged_books(self):
        entries = {"a": entry("ru"), "b": {**entry("ru"), "language_source": "override"},
                   "c": {**entry("ru"), "language_suspect": True}}
        report = {"flags": {key: {"flags": ["script-mismatch"], "observed_script": "Latin",
                                  "observed_share": 1.0} for key in entries}}
        updates = audit.select_flag_updates(entries, report)
        self.assertEqual(len(updates), 2)
        self.assertTrue(updates[0]["language_suspect"])
        self.assertEqual(updates[0]["language_check"]["observed_script"], "Latin")

    def test_draft_overrides_only_covers_confirmed_script_mismatches(self):
        entries = {"a": entry("ru"), "b": entry("ch")}
        texts = {"a": ["Latin " * 200], "b": ["中文" * 200]}
        report = audit.audit(entries, texts)
        draft = audit.draft_overrides(report["flags"], entries)
        self.assertEqual(draft["overrides"]["a"]["language"], "en")
        self.assertNotIn("b", draft["overrides"])


if __name__ == "__main__":
    unittest.main()
